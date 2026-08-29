from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MINIO = ROOT / "infra" / "minio"
APPLIER = ROOT / "apps" / "legal-hold-applier" / "src"


def compose_service(compose: str, name: str) -> str:
    start = compose.index(f"  {name}:")
    following = re.search(r"(?m)^  [a-z][a-z0-9-]*:$", compose[start + 1 :])
    end = start + 1 + following.start() if following else len(compose)
    return compose[start:end]


def load_contract():
    spec = importlib.util.spec_from_file_location("legal_hold_contract", APPLIER / "contract.py")
    if spec is None or spec.loader is None:
        raise AssertionError("mediator contract could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RequestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = load_contract()

    def valid(self) -> dict[str, str]:
        return {
            "bucket": "sc-rd-bronze-originals",
            "object_key": "rd/synthetic/original.bin",
            "version_id": "8f9084d8-8a5e-4a62-9090-77aa11bb22cc",
        }

    def test_exact_on_only_request_is_accepted(self) -> None:
        target = self.contract.validate_request(self.valid())
        self.assertEqual(target.bucket, "sc-rd-bronze-originals")
        self.assertIs(self.contract.LEGAL_HOLD_ON, True)

    def test_status_off_and_generic_proxy_shapes_are_rejected(self) -> None:
        for extra in (
            {"status": "OFF"},
            {"action": "delete"},
            {"operation": "put_object"},
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(self.contract.RequestRejected):
                    self.contract.validate_request({**self.valid(), **extra})

    def test_missing_malformed_and_out_of_scope_targets_fail_closed(self) -> None:
        variants = [
            {key: value for key, value in self.valid().items() if key != "version_id"},
            {**self.valid(), "version_id": ""},
            {**self.valid(), "version_id": "bad/version"},
            {**self.valid(), "bucket": "unapproved"},
            {**self.valid(), "object_key": "../escape"},
            {**self.valid(), "object_key": "rd//escape"},
        ]
        for value in variants:
            with self.subTest(value=value):
                with self.assertRaises(self.contract.RequestRejected):
                    self.contract.validate_request(value)


class AuthorityPolicyTests(unittest.TestCase):
    def policy(self, name: str) -> dict:
        return json.loads((MINIO / "policies" / name).read_text())

    def actions(self, name: str, effect: str) -> set[str]:
        result: set[str] = set()
        for statement in self.policy(name)["Statement"]:
            if statement["Effect"] != effect:
                continue
            value = statement["Action"]
            result.update([value] if isinstance(value, str) else value)
        return result

    def test_mediator_has_legal_hold_only_and_no_retention_or_delete_authority(self) -> None:
        allowed = self.actions("legal-hold-applier.json", "Allow")
        self.assertEqual(
            allowed,
            {"s3:GetBucketLocation", "s3:GetObjectLegalHold", "s3:PutObjectLegalHold"},
        )
        denied = self.actions("legal-hold-applier.json", "Deny")
        self.assertIn("s3:PutObjectRetention", denied)
        self.assertIn("s3:DeleteObjectVersion", denied)
        self.assertIn("s3:BypassGovernanceRetention", denied)

    def test_all_ordinary_policies_explicitly_deny_legal_hold_changes(self) -> None:
        for name in ("app-bronze-write.json", "ocr-worker.json", "reviewer-read.json"):
            with self.subTest(name=name):
                denied = self.actions(name, "Deny")
                self.assertIn("s3:PutObjectLegalHold", denied)
                self.assertIn("s3:GetObjectLegalHold", denied)

    def test_break_glass_cannot_delete_or_change_retention(self) -> None:
        allowed = self.actions("legal-hold-break-glass.json", "Allow")
        self.assertIn("s3:PutObjectLegalHold", allowed)
        self.assertNotIn("s3:PutObjectRetention", allowed)
        self.assertNotIn("s3:DeleteObjectVersion", allowed)
        self.assertNotIn("s3:BypassGovernanceRetention", allowed)
        denied = self.actions("legal-hold-break-glass.json", "Deny")
        self.assertIn("s3:PutObjectRetention", denied)
        self.assertIn("s3:DeleteObjectVersion", denied)


class RuntimeBoundaryTests(unittest.TestCase):
    def test_mediator_implementation_has_no_off_method_or_generic_action(self) -> None:
        source = (APPLIER / "main.py").read_text()
        self.assertIn("enable_object_legal_hold", source)
        self.assertIn("is_object_legal_hold_enabled", source)
        self.assertNotIn("disable_object_legal_hold", source)
        self.assertNotIn("set_object_legal_hold", source)
        self.assertNotIn('value["status"]', source)

    def test_compose_is_backend_only_without_a_host_port(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()
        start = compose.index("  legal-hold-applier:")
        end = compose.index("\n  postgres-migrate:", start)
        service = compose[start:end]
        self.assertIn("networks: [backend]", service)
        self.assertNotIn("edge", service)
        self.assertNotIn("ports:", service)
        self.assertIn('expose: ["8090"]', service)

    def test_caller_token_is_constant_time_and_confined_to_api_and_mediator(self) -> None:
        source = (APPLIER / "main.py").read_text()
        self.assertIn("hmac.compare_digest(candidate, CALL_TOKEN)", source)
        self.assertLess(
            source.index("hmac.compare_digest(candidate, CALL_TOKEN)"),
            source.index("self.rfile.read(length)"),
        )
        compose = (ROOT / "compose.yaml").read_text()
        services = {
            name: compose_service(compose, name)
            for name in (
                "minio-bootstrap", "legal-hold-applier", "postgres-migrate",
                "postgres-role-provision", "api", "web", "ocr-worker",
            )
        }
        for name in ("legal-hold-applier", "api"):
            self.assertIn("LEGAL_HOLD_APPLIER_CALL_TOKEN", services[name])
        for name in (
            "minio-bootstrap", "postgres-migrate", "postgres-role-provision",
            "web", "ocr-worker",
        ):
            self.assertNotIn("LEGAL_HOLD_APPLIER_CALL_TOKEN", services[name])

    def test_mediator_secret_is_not_injected_into_ordinary_services(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()
        self.assertEqual(compose.count("MINIO_HOLD_APPLIER_SECRET_KEY"), 6)
        for service_name in ("api", "ocr-worker", "web"):
            service = compose_service(compose, service_name)
            self.assertNotIn("MINIO_HOLD_APPLIER_ACCESS_KEY", service)
            self.assertNotIn("MINIO_HOLD_APPLIER_SECRET_KEY", service)

    def test_pinned_runtime_and_sdk_are_unchanged(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("minio/minio:RELEASE.2025-07-23T15-54-02Z", compose)
        self.assertIn("minio/mc:RELEASE.2025-07-21T05-28-08Z", compose)
        self.assertEqual((APPLIER / "requirements.txt").read_text(), "minio==7.2.16\n")

    def test_break_glass_requires_full_decision_and_has_no_retention_command(self) -> None:
        source = (MINIO / "legal_hold_break_glass.py").read_text()
        for marker in (
            "--decision-id", "--actor", "--reason", "--timestamp-utc",
            "--bucket", "--key", "--version-id",
            "CONFIRM_BREAK_GLASS_LEGAL_HOLD_CLEAR",
        ):
            self.assertIn(marker, source)
        self.assertIn("disable_object_legal_hold", source)
        self.assertIn("get_object_retention", source)
        self.assertNotIn("set_object_retention", source)
        self.assertNotIn("delete_object", source)
        self.assertIn("REQUESTED", source)
        self.assertIn("COMPLETED", source)

    def test_live_harness_is_explicitly_opt_in(self) -> None:
        source = (MINIO / "tests" / "live_legal_hold_mediation_acceptance.py").read_text()
        self.assertIn("--confirm-disposable-synthetic-legal-hold-mediation-run", source)
        self.assertIn("PASS_LEGAL_HOLD_CALLER_AUTH_REMEDIATION", source)
