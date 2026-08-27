from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("live_network_segmentation_acceptance.py")
SPEC = importlib.util.spec_from_file_location("live_network_segmentation_acceptance", MODULE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import contract
    raise RuntimeError("could not load live network acceptance module")
network = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = network
SPEC.loader.exec_module(network)


def compose_fixture() -> dict:
    def service(networks: set[str], ports: tuple[int, ...] = ()) -> dict:
        rendered = {"networks": {name: None for name in networks}}
        if ports:
            rendered["ports"] = [
                {
                    "host_ip": "127.0.0.1",
                    "published": str(port),
                    "target": port,
                }
                for port in ports
            ]
        return rendered

    return {
        "services": {
            "postgres": service({"backend"}, (5432,)),
            "minio": service({"backend"}, (9000, 9001)),
            "minio-bootstrap": service({"backend"}),
            "postgres-migrate": service({"backend"}),
            "api": service({"backend", "edge"}, (8000,)),
            "web": service({"edge"}, (8080,)),
            "ocr-worker": service({"backend"}),
        },
        "networks": {
            "backend": {"internal": True},
            "edge": {},
        },
    }


class NetworkContractTests(unittest.TestCase):
    def test_accepted_topology_passes_with_explicit_evidence(self) -> None:
        evidence = network.validate_compose_contract(compose_fixture())

        self.assertTrue(evidence["backend_internal"])
        self.assertEqual(["api", "web"], evidence["edge_services"])
        self.assertEqual(["backend"], evidence["service_networks"]["postgres"])
        self.assertEqual(["backend"], evidence["service_networks"]["minio"])

    def test_backend_data_service_on_edge_fails_closed(self) -> None:
        compose = compose_fixture()
        compose["services"]["postgres"]["networks"]["edge"] = None

        with self.assertRaises(network.ProductContractFailure):
            network.validate_compose_contract(compose)

    def test_unexpected_edge_peer_fails_closed(self) -> None:
        compose = compose_fixture()
        compose["services"]["ocr-worker"]["networks"]["edge"] = None

        with self.assertRaises(network.ProductContractFailure):
            network.validate_compose_contract(compose)

    def test_non_internal_backend_fails_closed(self) -> None:
        compose = compose_fixture()
        compose["networks"]["backend"]["internal"] = False

        with self.assertRaises(network.ProductContractFailure):
            network.validate_compose_contract(compose)

    def test_non_loopback_publication_fails_closed(self) -> None:
        compose = compose_fixture()
        compose["services"]["minio"]["ports"][0]["host_ip"] = "0.0.0.0"

        with self.assertRaises(network.ProductContractFailure):
            network.validate_compose_contract(compose)

    def test_missing_or_extra_publication_fails_closed(self) -> None:
        for mutation in ("missing", "extra"):
            with self.subTest(mutation=mutation):
                compose = compose_fixture()
                if mutation == "missing":
                    compose["services"]["api"].pop("ports")
                else:
                    compose["services"]["ocr-worker"]["ports"] = [
                        {"host_ip": "127.0.0.1", "published": "9999", "target": 9999}
                    ]
                with self.assertRaises(network.ProductContractFailure):
                    network.validate_compose_contract(compose)

    def test_missing_authorization_never_calls_live_boundary(self) -> None:
        output = io.StringIO()
        with mock.patch.object(
            network, "_run_live", side_effect=AssertionError("must not run")
        ), contextlib.redirect_stdout(output):
            exit_code = network.main([])

        self.assertEqual(2, exit_code)
        self.assertIn(network.BLOCKED_ISOLATION, output.getvalue())

    def test_disposable_construct_requires_cleanup_registration(self) -> None:
        topology = network.DisposableTopology(token="offline")
        topology.image_id = "sha256:" + ("0" * 64)

        with self.assertRaises(network.IsolationFailure):
            topology.construct()

    def test_probe_commands_are_pull_disabled_and_immutable(self) -> None:
        captured = []

        def fake_run(command, **_kwargs):
            captured.append(tuple(command))
            return network.CommandResult(tuple(command), 0, "[]\n", "")

        topology = network.DisposableTopology(fake_run, token="offline")
        topology.image_id = "sha256:" + ("a" * 64)
        result = topology._probe("backend", "print('[]')")

        self.assertEqual(0, result.returncode)
        self.assertIn("--pull=never", captured[0])
        self.assertIn(topology.image_id, captured[0])

    def test_edge_probe_requires_direct_tcp_denial_and_api_reachability(self) -> None:
        self.assertIn('sys.argv.index("--tcp-targets")', network.EDGE_PROBE_CODE)
        self.assertIn(
            "socket.create_connection((host, int(raw_port))", network.EDGE_PROBE_CODE
        )
        self.assertIn('"api_reachable": True', network.EDGE_PROBE_CODE)
        self.assertIn(
            '"backend_tcp_denied": sorted(denied_tcp)', network.EDGE_PROBE_CODE
        )

    def test_cleanup_mismatch_overrides_prior_product_failure(self) -> None:
        events = []

        class FakeHarness:
            def finalize(self):
                events.append("finalize")

            def assert_clean(self):
                events.append("assert_clean")
                raise network.IsolationFailure("owned resource survived")

        before = {"containers": [], "networks": [], "volumes": [], "images": []}

        with self.assertRaises(network.IsolationFailure) as raised:
            network._finalize_and_reconcile(
                FakeHarness(),
                before,
                network.ProductContractFailure("probe failed"),
                inventory=lambda: events.append("inventory") or before,
            )

        self.assertEqual("owned resource survived", str(raised.exception))
        self.assertEqual(["finalize", "assert_clean", "inventory"], events)

    def test_inventory_mismatch_overrides_prior_environment_failure(self) -> None:
        events = []

        class FakeHarness:
            def finalize(self):
                events.append("finalize")

            def assert_clean(self):
                events.append("assert_clean")

        before = {"containers": [], "networks": [], "volumes": [], "images": []}
        after = {**before, "containers": ["unexpected-container"]}

        with self.assertRaises(network.IsolationFailure) as raised:
            network._finalize_and_reconcile(
                FakeHarness(),
                before,
                network.EnvironmentFailure("probe command failed"),
                inventory=lambda: events.append("inventory") or after,
            )

        self.assertEqual("pre-existing Docker inventory changed", str(raised.exception))
        self.assertEqual(["finalize", "assert_clean", "inventory"], events)

    def test_finalize_attempts_all_owned_resource_cleanup_after_inventory_failure(self) -> None:
        events = []

        def fake_run(command, **_kwargs):
            command = tuple(command)
            if command[1:3] == ("ps", "-aq"):
                events.append("containers")
                return network.CommandResult(command, 1, "", "synthetic failure")
            if command[1:3] == ("network", "ls"):
                events.append("networks")
                return network.CommandResult(command, 0, "network-id\n", "")
            if command[1:3] == ("network", "rm"):
                events.append("remove_network")
                return network.CommandResult(command, 0, "network-id\n", "")
            raise AssertionError(command)

        topology = network.DisposableTopology(fake_run, token="offline")
        topology.finalize()

        self.assertEqual(["containers", "networks", "remove_network"], events)
        self.assertEqual("owned container inventory failed", topology._cleanup_error)

    def test_evidence_fingerprint_is_order_independent_for_mapping_keys(self) -> None:
        first = {"networks": ["b", "a"], "containers": []}
        second = {"containers": [], "networks": ["b", "a"]}

        self.assertEqual(network._sha256_json(first), network._sha256_json(second))


if __name__ == "__main__":
    unittest.main()
