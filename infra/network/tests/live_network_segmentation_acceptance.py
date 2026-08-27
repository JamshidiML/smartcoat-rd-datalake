#!/usr/bin/env python3
"""Explicitly opt-in, disposable live acceptance for the M0-R06 boundary.

The harness uses one already-present local Python image by immutable image ID.
It never builds or pulls an image, never reads ``.env``, and never starts the
repository services.  The disposable topology mirrors the trust-boundary
shape from ``compose.yaml`` so Docker DNS and reachability can be tested
without touching persistent data services or volumes.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[3]
AUTHORIZATION_FLAG = "--confirm-disposable-synthetic-network-run"
PROBE_IMAGE_TAG = "python:3.12.11-slim-bookworm"
OWNER_LABEL = "com.smartcoat.acceptance.m0-r06"

PASS = "PASS_M0_R06"
FAIL_PRODUCT_CONTRACT = "FAIL_PRODUCT_CONTRACT"
FAIL_VERIFICATION_HARNESS = "FAIL_VERIFICATION_HARNESS"
BLOCKED_ISOLATION = "BLOCKED_ISOLATION"
BLOCKED_ENVIRONMENT = "BLOCKED_ENVIRONMENT"

EXPECTED_NETWORKS = {
    "postgres": {"backend"},
    "minio": {"backend"},
    "minio-bootstrap": {"backend"},
    "postgres-migrate": {"backend"},
    "api": {"backend", "edge"},
    "web": {"edge"},
    "ocr-worker": {"backend"},
}
EXPECTED_EDGE_SERVICES = {"api", "web"}
EXPECTED_LOOPBACK_TARGETS = {
    "postgres": {5432},
    "minio": {9000, 9001},
    "api": {8000},
    "web": {8080},
}


class AcceptanceFailure(RuntimeError):
    classification = FAIL_VERIFICATION_HARNESS


class ProductContractFailure(AcceptanceFailure):
    classification = FAIL_PRODUCT_CONTRACT


class IsolationFailure(AcceptanceFailure):
    classification = BLOCKED_ISOLATION


class EnvironmentFailure(AcceptanceFailure):
    classification = BLOCKED_ENVIRONMENT


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _run(
    command: Sequence[str],
    *,
    cwd: Path = ROOT,
    timeout: int = 60,
) -> CommandResult:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env={**os.environ, "DOCKER_CLI_HINTS": "false"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EnvironmentFailure(f"command unavailable or timed out: {command[0]}") from exc
    return CommandResult(tuple(command), result.returncode, result.stdout, result.stderr)


def _require_success(result: CommandResult, label: str) -> CommandResult:
    if result.returncode != 0:
        raise EnvironmentFailure(f"{label} failed with exit {result.returncode}")
    return result


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _network_names(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks", {})
    if isinstance(networks, list):
        return set(networks)
    return set(networks)


def validate_compose_contract(compose: dict[str, Any]) -> dict[str, Any]:
    """Validate the static M0-R06 contract against rendered Compose JSON."""

    services = compose.get("services")
    networks = compose.get("networks")
    if not isinstance(services, dict) or not isinstance(networks, dict):
        raise ProductContractFailure("rendered Compose lacks services or networks")

    missing = sorted(set(EXPECTED_NETWORKS) - set(services))
    if missing:
        raise ProductContractFailure("required services are absent from Compose")

    observed = {
        name: _network_names(services[name])
        for name in EXPECTED_NETWORKS
    }
    if observed != EXPECTED_NETWORKS:
        raise ProductContractFailure("service network membership violates M0-R06")

    edge_services = {
        name for name, service in services.items() if "edge" in _network_names(service)
    }
    if edge_services != EXPECTED_EDGE_SERVICES:
        raise ProductContractFailure("edge includes a non-entrypoint or omits an entrypoint")

    backend = networks.get("backend", {})
    edge = networks.get("edge", {})
    if backend.get("internal") is not True:
        raise ProductContractFailure("backend network is not internal")
    if edge.get("internal") is True:
        raise ProductContractFailure("edge network must remain the forwarding bridge")

    observed_targets: dict[str, set[int]] = {}
    for service_name, service in services.items():
        ports = service.get("ports", [])
        targets: set[int] = set()
        for port in ports:
            if not isinstance(port, dict):
                raise ProductContractFailure("rendered port is not normalized mapping data")
            if port.get("host_ip") != "127.0.0.1":
                raise ProductContractFailure("a published port is not loopback-bound")
            target = port.get("target")
            if not isinstance(target, int):
                raise ProductContractFailure("a published port lacks an integer target")
            targets.add(target)
        if targets:
            observed_targets[service_name] = targets
    if observed_targets != EXPECTED_LOOPBACK_TARGETS:
        raise ProductContractFailure("published service targets violate the frozen boundary")

    for service_name in ("minio-bootstrap", "postgres-migrate", "ocr-worker"):
        if services[service_name].get("ports"):
            raise ProductContractFailure("a backend-only worker publishes a host port")

    return {
        "backend_internal": True,
        "edge_services": sorted(edge_services),
        "loopback_targets": {
            name: sorted(targets) for name, targets in sorted(observed_targets.items())
        },
        "service_networks": {
            name: sorted(names) for name, names in sorted(observed.items())
        },
    }


def render_and_validate_compose() -> dict[str, Any]:
    result = _require_success(
        _run(
            (
                "docker",
                "compose",
                "--env-file",
                str(ROOT / ".env.example"),
                "config",
                "--format",
                "json",
            )
        ),
        "synthetic Compose rendering",
    )
    try:
        compose = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EnvironmentFailure("Compose did not return JSON") from exc
    return validate_compose_contract(compose)


def docker_inventory() -> dict[str, list[str]]:
    commands = {
        "containers": ("docker", "ps", "-aq", "--no-trunc"),
        "networks": ("docker", "network", "ls", "-q", "--no-trunc"),
        "volumes": ("docker", "volume", "ls", "-q"),
        "images": ("docker", "image", "ls", "-q", "--no-trunc"),
    }
    inventory: dict[str, list[str]] = {}
    for kind, command in commands.items():
        result = _require_success(_run(command), f"Docker {kind} inventory")
        inventory[kind] = sorted(set(result.stdout.split()))
    return inventory


SERVER_CODE = """
import socket
import sys

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("0.0.0.0", int(sys.argv[1])))
listener.listen()
while True:
    connection, _ = listener.accept()
    connection.sendall(b"synthetic-ok")
    connection.close()
""".strip()


POSITIVE_PROBE_CODE = """
import json
import socket
import sys

observed = []
for endpoint in sys.argv[1:]:
    host, raw_port = endpoint.rsplit(":", 1)
    port = int(raw_port)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise SystemExit(20)
    connection = socket.create_connection((host, port), timeout=3)
    payload = connection.recv(64)
    connection.close()
    if payload != b"synthetic-ok":
        raise SystemExit(21)
    observed.append(host)
print(json.dumps(sorted(observed)))
""".strip()


EDGE_PROBE_CODE = """
import json
import socket
import sys

api_host = sys.argv[1]
api_port = int(sys.argv[2])
for forbidden in sys.argv[3:]:
    try:
        socket.getaddrinfo(forbidden, 1, type=socket.SOCK_STREAM)
    except socket.gaierror:
        continue
    raise SystemExit(30)
connection = socket.create_connection((api_host, api_port), timeout=3)
payload = connection.recv(64)
connection.close()
if payload != b"synthetic-ok":
    raise SystemExit(31)
print(json.dumps({"api_reachable": True, "backend_dns_denied": sorted(sys.argv[3:])}))
""".strip()


EGRESS_PROBE_CODE = """
import json
import socket

try:
    socket.create_connection(("1.1.1.1", 53), timeout=2)
except OSError:
    print(json.dumps({"external_tcp_denied": True}))
else:
    raise SystemExit(40)
""".strip()


class DisposableTopology:
    def __init__(
        self,
        run: Callable[..., CommandResult] = _run,
        *,
        token: str | None = None,
    ) -> None:
        suffix = token or uuid.uuid4().hex[:12]
        self.owner = f"m0-r06-{suffix}"
        self.backend = f"sc-m0-r06-{suffix}-backend"
        self.edge = f"sc-m0-r06-{suffix}-edge"
        self.postgres = f"sc-m0-r06-{suffix}-postgres"
        self.minio = f"sc-m0-r06-{suffix}-minio"
        self.api = f"sc-m0-r06-{suffix}-api"
        self.postgres_alias = f"pg-{suffix}"
        self.minio_alias = f"object-{suffix}"
        self.api_alias = f"entry-{suffix}"
        self.run = run
        self.image_id = ""
        self._cleanup_installed = False
        self._finalized = False
        self._constructed = False
        self._cleanup_error: str | None = None

    def _docker(self, *arguments: str, timeout: int = 60) -> CommandResult:
        return self.run(("docker", *arguments), timeout=timeout)

    def install_cleanup(self) -> None:
        if self._cleanup_installed:
            return
        atexit.register(self.finalize)
        self._cleanup_installed = True

    def resolve_image(self) -> str:
        result = _require_success(
            self._docker("image", "inspect", "--format", "{{.Id}}", PROBE_IMAGE_TAG),
            "local probe-image inspection",
        )
        image_id = result.stdout.strip()
        if not image_id.startswith("sha256:") or len(image_id) != 71:
            raise EnvironmentFailure("local probe image lacks an immutable image ID")
        self.image_id = image_id
        return image_id

    def _create_networks(self) -> None:
        label = f"{OWNER_LABEL}={self.owner}"
        _require_success(
            self._docker("network", "create", "--internal", "--label", label, self.backend),
            "internal backend creation",
        )
        self._constructed = True
        _require_success(
            self._docker("network", "create", "--label", label, self.edge),
            "edge creation",
        )

    def _start_server(
        self,
        name: str,
        network: str,
        alias: str,
        port: int,
    ) -> None:
        label = f"{OWNER_LABEL}={self.owner}"
        _require_success(
            self._docker(
                "run",
                "-d",
                "--pull=never",
                "--name",
                name,
                "--label",
                label,
                "--network",
                network,
                "--network-alias",
                alias,
                self.image_id,
                "python",
                "-u",
                "-c",
                SERVER_CODE,
                str(port),
            ),
            f"synthetic {name} start",
        )

    def construct(self) -> None:
        if not self._cleanup_installed:
            raise IsolationFailure("cleanup was not installed before construction")
        if not self.image_id:
            raise EnvironmentFailure("immutable probe image was not resolved")
        self._create_networks()
        self._start_server(
            self.postgres, self.backend, self.postgres_alias, 5432
        )
        self._start_server(self.minio, self.backend, self.minio_alias, 9000)
        self._start_server(self.api, self.backend, self.api_alias, 8000)
        _require_success(
            self._docker(
                "network", "connect", "--alias", self.api_alias, self.edge, self.api
            ),
            "API edge attachment",
        )

    def _probe(self, network: str, code: str, *arguments: str) -> CommandResult:
        label = f"{OWNER_LABEL}={self.owner}"
        return self._docker(
            "run",
            "--rm",
            "--pull=never",
            "--label",
            label,
            "--network",
            network,
            self.image_id,
            "python",
            "-c",
            code,
            *arguments,
            timeout=30,
        )

    def _container_networks(self, container: str) -> set[str]:
        result = _require_success(
            self._docker(
                "inspect",
                "--format",
                "{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}",
                container,
            ),
            "container network inspection",
        )
        return set(result.stdout.split())

    def _network_internal(self, network: str) -> bool:
        result = _require_success(
            self._docker("network", "inspect", "--format", "{{.Internal}}", network),
            "network isolation inspection",
        )
        value = result.stdout.strip().lower()
        if value not in {"true", "false"}:
            raise EnvironmentFailure("Docker returned an invalid network isolation value")
        return value == "true"

    def verify(self) -> dict[str, Any]:
        expected_memberships = {
            self.postgres: {self.backend},
            self.minio: {self.backend},
            self.api: {self.backend, self.edge},
        }
        observed_memberships = {
            name: self._container_networks(name) for name in expected_memberships
        }
        if observed_memberships != expected_memberships:
            raise ProductContractFailure("live container memberships violate M0-R06")
        if not self._network_internal(self.backend):
            raise ProductContractFailure("live backend network is not internal")
        if self._network_internal(self.edge):
            raise ProductContractFailure("live edge network unexpectedly is internal")

        deadline = time.monotonic() + 15
        positive: CommandResult | None = None
        while time.monotonic() < deadline:
            positive = self._probe(
                self.backend,
                POSITIVE_PROBE_CODE,
                f"{self.postgres_alias}:5432",
                f"{self.minio_alias}:9000",
                f"{self.api_alias}:8000",
            )
            if positive.returncode == 0:
                break
            time.sleep(0.25)
        if positive is None or positive.returncode != 0:
            raise ProductContractFailure("allowed backend paths did not become reachable")

        edge = self._probe(
            self.edge,
            EDGE_PROBE_CODE,
            self.api_alias,
            "8000",
            self.postgres_alias,
            self.minio_alias,
        )
        if edge.returncode != 0:
            raise ProductContractFailure("edge DNS/reachability boundary failed")

        egress = self._docker(
            "exec", self.postgres, "python", "-c", EGRESS_PROBE_CODE, timeout=15
        )
        if egress.returncode != 0:
            raise ProductContractFailure("backend-only data-service egress was not denied")

        try:
            positive_evidence = json.loads(positive.stdout)
            edge_evidence = json.loads(edge.stdout)
            egress_evidence = json.loads(egress.stdout)
        except json.JSONDecodeError as exc:
            raise EnvironmentFailure("a synthetic probe returned malformed evidence") from exc

        return {
            "allowed_backend_paths": positive_evidence,
            "backend_external_egress": egress_evidence,
            "edge_boundary": edge_evidence,
            "live_memberships": {
                name.rsplit("-", 1)[-1]: sorted(networks)
                for name, networks in sorted(observed_memberships.items())
            },
            "network_flags": {"backend_internal": True, "edge_internal": False},
        }

    def _owned_container_ids(self) -> list[str]:
        result = _require_success(
            self._docker(
                "ps", "-aq", "--filter", f"label={OWNER_LABEL}={self.owner}"
            ),
            "owned-container inventory",
        )
        return result.stdout.split()

    def _owned_network_ids(self) -> list[str]:
        result = _require_success(
            self._docker(
                "network", "ls", "-q", "--filter", f"label={OWNER_LABEL}={self.owner}"
            ),
            "owned-network inventory",
        )
        return result.stdout.split()

    def finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        errors: list[str] = []
        if self._constructed:
            try:
                containers = self._owned_container_ids()
                if containers:
                    result = self._docker("rm", "-f", *containers)
                    if result.returncode != 0:
                        errors.append("owned container removal failed")
            except AcceptanceFailure:
                errors.append("owned container inventory failed")
            try:
                networks = self._owned_network_ids()
                if networks:
                    result = self._docker("network", "rm", *networks)
                    if result.returncode != 0:
                        errors.append("owned network removal failed")
            except AcceptanceFailure:
                errors.append("owned network inventory failed")
        self._cleanup_error = "; ".join(errors) or None

    def assert_clean(self) -> None:
        if self._cleanup_error:
            raise IsolationFailure(self._cleanup_error)
        if self._owned_container_ids() or self._owned_network_ids():
            raise IsolationFailure("generated Docker resources remain after finalization")


def _parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(AUTHORIZATION_FLAG, action="store_true")
    return parser.parse_args(list(argv))


def _run_live() -> dict[str, Any]:
    static_evidence = render_and_validate_compose()
    before = docker_inventory()
    harness = DisposableTopology()
    harness.install_cleanup()
    try:
        image_id = harness.resolve_image()
        harness.construct()
        live_evidence = harness.verify()
    finally:
        harness.finalize()
    harness.assert_clean()
    after = docker_inventory()
    if after != before:
        raise IsolationFailure("pre-existing Docker inventory changed")
    return {
        "classification": PASS,
        "compose_contract": static_evidence,
        "docker_inventory": {
            "before_counts": {kind: len(items) for kind, items in before.items()},
            "after_counts": {kind: len(items) for kind, items in after.items()},
            "before_fingerprint": _sha256_json(before),
            "after_fingerprint": _sha256_json(after),
            "unchanged": True,
        },
        "image": {"tag": PROBE_IMAGE_TAG, "immutable_id": image_id, "pull": False},
        "live_contract": live_evidence,
        "owned_resources_remaining": 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(sys.argv[1:] if argv is None else argv)
    if not getattr(arguments, AUTHORIZATION_FLAG[2:].replace("-", "_")):
        print(json.dumps({"classification": BLOCKED_ISOLATION, "authorized": False}))
        print(BLOCKED_ISOLATION)
        return 2

    interrupted = False

    def handle_signal(_signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        raise KeyboardInterrupt

    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    for signum in previous_handlers:
        signal.signal(signum, handle_signal)
    try:
        evidence = _run_live()
        print(json.dumps(evidence, indent=2, sort_keys=True))
        print(PASS)
        return 0
    except AcceptanceFailure as exc:
        print(
            json.dumps(
                {"classification": exc.classification, "reason": str(exc)},
                sort_keys=True,
            )
        )
        print(exc.classification)
        return 1
    except KeyboardInterrupt:
        classification = BLOCKED_ISOLATION if interrupted else FAIL_VERIFICATION_HARNESS
        print(json.dumps({"classification": classification, "reason": "interrupted"}))
        print(classification)
        return 130
    except Exception as exc:  # pragma: no cover - defensive fail-closed boundary
        print(
            json.dumps(
                {"classification": FAIL_VERIFICATION_HARNESS, "reason": type(exc).__name__},
                sort_keys=True,
            )
        )
        print(FAIL_VERIFICATION_HARNESS)
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
