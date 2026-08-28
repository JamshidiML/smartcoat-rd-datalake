from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / "src"
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SOURCE))

import retention_enforcement as enforcement  # noqa: E402
import retention_policy as policy  # noqa: E402


ANCHOR = datetime(2024, 2, 29, 23, 59, 58, tzinfo=UTC)


class FakeStorage:
    def __init__(self, *, current: enforcement.StorageRetention | None = None) -> None:
        self.current = current
        self.calls: list[tuple[str, str]] = []
        self.metadata_version = "version-one"
        self.metadata_time = ANCHOR
        self.raise_on_set = False

    def stat_exact(self, target: enforcement.ExactVersionTarget):
        self.calls.append(("stat", target.object_version_id))
        return enforcement.StorageVersionMetadata(
            self.metadata_version,
            self.metadata_time,
        )

    def get_retention_exact(self, target: enforcement.ExactVersionTarget):
        self.calls.append(("get", target.object_version_id))
        return self.current

    def set_retention_exact(self, target, retain_until_utc):
        self.calls.append(("set", target.object_version_id))
        if self.raise_on_set:
            raise enforcement.RetentionEnforcementError("RETENTION_API_DENIED")
        self.current = enforcement.StorageRetention("COMPLIANCE", retain_until_utc)


class FakeMediator:
    def __init__(self, status: str = "OFF") -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []
        self.fail = False

    def apply_on(self, target):
        self.calls.append(("apply", target.object_version_id))
        if self.fail:
            raise enforcement.RetentionEnforcementError("LEGAL_HOLD_MEDIATOR_UNAVAILABLE")
        self.status = "ON"
        return self.status

    def read_status(self, target):
        self.calls.append(("status", target.object_version_id))
        return self.status


def target(kind: str = "ORIGINAL") -> enforcement.ExactVersionTarget:
    bucket = (
        "sc-rd-bronze-originals"
        if kind == "ORIGINAL"
        else "sc-rd-bronze-manifests"
    )
    return enforcement.ExactVersionTarget(
        bucket,
        f"rd/synthetic/{kind.lower()}.bin",
        "version-one",
        kind,
    )


def enforce(
    storage: FakeStorage,
    mediator: FakeMediator,
    *,
    kind: str = "ORIGINAL",
    category: str = "LAB_NOTE",
    retention_class: str = "permanent",
):
    return enforcement.ExactVersionRetentionEnforcer(
        storage,
        mediator,
        clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
    ).enforce(
        target=target(kind),
        retention_assignment_id="00000000-0000-7000-8000-000000000701",
        data_category=category,
        retention_class=retention_class,
        retention_policy_version=policy.RETENTION_POLICY_VERSION,
        enforced_by="synthetic_retention_enforcer",
    )


class RetentionEnforcerTests(unittest.TestCase):
    def test_permanent_uses_storage_anchor_exact_version_and_mediator_on(self) -> None:
        storage = FakeStorage()
        mediator = FakeMediator()
        evidence = enforce(storage, mediator)
        self.assertEqual(datetime(2034, 2, 28, 23, 59, 58, tzinfo=UTC), evidence.requested_retain_until_utc)
        self.assertEqual("ON", evidence.observed_legal_hold_status)
        self.assertEqual("SUCCESS", evidence.enforcement_verification_result)
        self.assertEqual(["version-one"] * 5, [value for _, value in storage.calls])
        self.assertEqual([("apply", "version-one"), ("status", "version-one")], mediator.calls)

    def test_short_90d_is_exact_2160_hours_and_does_not_apply_hold(self) -> None:
        storage = FakeStorage()
        mediator = FakeMediator()
        evidence = enforce(
            storage,
            mediator,
            category="PLATFORM_OPERATIONAL_LOG",
            retention_class="short_90d",
        )
        self.assertEqual(timedelta(hours=2160), evidence.requested_retain_until_utc - ANCHOR)
        self.assertEqual("UNCHANGED", evidence.requested_legal_hold_status)
        self.assertEqual([("status", "version-one")], mediator.calls)

    def test_existing_stronger_compliance_floor_is_never_shortened(self) -> None:
        stronger = enforcement.StorageRetention(
            "COMPLIANCE", datetime(2040, 1, 1, tzinfo=UTC)
        )
        storage = FakeStorage(current=stronger)
        evidence = enforce(storage, FakeMediator())
        self.assertEqual(stronger.retain_until_utc, evidence.requested_retain_until_utc)
        self.assertEqual(stronger.retain_until_utc, storage.current.retain_until_utc)
        self.assertNotIn(("set", "version-one"), storage.calls)

    def test_original_and_manifest_are_individually_enforceable(self) -> None:
        for kind in ("ORIGINAL", "MANIFEST"):
            with self.subTest(kind=kind):
                evidence = enforce(FakeStorage(), FakeMediator(), kind=kind)
                self.assertEqual(kind, evidence.object_kind)
                self.assertEqual(target(kind).bucket_name, evidence.bucket_name)

    def test_unknown_class_policy_and_classification_fail_closed(self) -> None:
        service = enforcement.ExactVersionRetentionEnforcer(FakeStorage(), FakeMediator())
        base = {
            "target": target(),
            "retention_assignment_id": "assignment",
            "data_category": "LAB_NOTE",
            "retention_class": "permanent",
            "retention_policy_version": policy.RETENTION_POLICY_VERSION,
            "enforced_by": "actor",
        }
        variants = (
            ({**base, "retention_class": "365_days"}, "UNKNOWN_RETENTION_CLASS"),
            ({**base, "retention_policy_version": "unapproved"}, "UNKNOWN_RETENTION_POLICY"),
            ({**base, "data_category": "OTHER"}, None),
            ({**base, "retention_class": "short_90d"}, "RETENTION_CLASS_POLICY_MISMATCH"),
        )
        for arguments, code in variants:
            with self.subTest(code=code), self.assertRaises(Exception) as caught:
                service.enforce(**arguments)
            if code:
                self.assertEqual(code, caught.exception.code)

    def test_invalid_exact_version_targets_fail_before_storage(self) -> None:
        storage = FakeStorage()
        service = enforcement.ExactVersionRetentionEnforcer(storage, FakeMediator())
        variants = (
            enforcement.ExactVersionTarget("wrong", "rd/x", "v", "ORIGINAL"),
            enforcement.ExactVersionTarget("sc-rd-bronze-originals", "latest", "v", "ORIGINAL"),
            enforcement.ExactVersionTarget("sc-rd-bronze-originals", "rd/x", "", "ORIGINAL"),
            enforcement.ExactVersionTarget("sc-rd-bronze-originals", "rd/x", "v", "MANIFEST"),
        )
        for value in variants:
            with self.subTest(value=value), self.assertRaises(enforcement.RetentionEnforcementError):
                service.enforce(
                    target=value,
                    retention_assignment_id="assignment",
                    data_category="LAB_NOTE",
                    retention_class="permanent",
                    retention_policy_version=policy.RETENTION_POLICY_VERSION,
                    enforced_by="actor",
                )
        self.assertEqual([], storage.calls)

    def test_returned_version_timestamp_mode_deadline_and_hold_mismatches_fail(self) -> None:
        cases: list[tuple[str, FakeStorage, FakeMediator]] = []
        wrong_version = FakeStorage()
        wrong_version.metadata_version = "different-version"
        cases.append(("EXACT_VERSION_READBACK", wrong_version, FakeMediator()))
        wrong_mode = FakeStorage(current=enforcement.StorageRetention("GOVERNANCE", datetime(2040, 1, 1, tzinfo=UTC)))
        cases.append(("RETENTION_MODE", wrong_mode, FakeMediator()))
        for label, storage, mediator in cases:
            with self.subTest(label=label), self.assertRaises(enforcement.RetentionEnforcementError):
                enforce(storage, mediator)

    def test_retention_denial_and_mediator_failure_never_return_success(self) -> None:
        storage = FakeStorage()
        storage.raise_on_set = True
        with self.assertRaises(enforcement.RetentionEnforcementError) as denied:
            enforce(storage, FakeMediator())
        self.assertEqual("RETENTION_API_DENIED", denied.exception.code)

        mediator = FakeMediator()
        mediator.fail = True
        with self.assertRaises(enforcement.RetentionEnforcementError) as unavailable:
            enforce(FakeStorage(), mediator)
        self.assertEqual("LEGAL_HOLD_MEDIATOR_UNAVAILABLE", unavailable.exception.code)


class SDKAndAuthorityBoundaryTests(unittest.TestCase):
    def test_sdk_adapter_passes_exact_version_to_every_call(self) -> None:
        calls: list[tuple[str, str | None]] = []

        class Client:
            def stat_object(self, bucket, key, *, version_id=None):
                calls.append(("stat", version_id))
                return SimpleNamespace(version_id=version_id, last_modified=ANCHOR)

            def get_object_retention(self, bucket, key, *, version_id=None):
                calls.append(("get_retention", version_id))
                return SimpleNamespace(mode="COMPLIANCE", retain_until_date=datetime(2034, 2, 28, 23, 59, 58, tzinfo=UTC))

            def set_object_retention(self, bucket, key, value, *, version_id=None):
                calls.append(("set_retention", version_id))

        adapter = enforcement.MinioExactVersionRetentionStorage(
            Client(),
            retention_factory=lambda retain_until: retain_until,
            s3_error_type=RuntimeError,
        )
        adapter.stat_exact(target())
        adapter.get_retention_exact(target())
        adapter.set_retention_exact(target(), datetime(2034, 2, 28, 23, 59, 58, tzinfo=UTC))
        self.assertEqual(["version-one"] * 3, [version for _, version in calls])

    def test_app_policy_gains_readback_not_legal_hold_or_delete_authority(self) -> None:
        value = json.loads((ROOT / "infra/minio/policies/app-bronze-write.json").read_text())
        allowed = {
            action
            for statement in value["Statement"]
            if statement["Effect"] == "Allow"
            for action in statement["Action"]
        }
        self.assertIn("s3:GetObjectRetention", allowed)
        self.assertIn("s3:GetObjectVersion", allowed)
        self.assertNotIn("s3:PutObjectLegalHold", allowed)
        self.assertNotIn("s3:DeleteObject", allowed)
        self.assertNotIn("s3:BypassGovernanceRetention", allowed)

    def test_mediator_status_is_read_only_and_apply_remains_on_only(self) -> None:
        source = (ROOT / "apps/legal-hold-applier/src/main.py").read_text()
        self.assertIn('{"/apply", "/status"}', source)
        self.assertIn("enable_object_legal_hold", source)
        self.assertIn("is_object_legal_hold_enabled", source)
        self.assertNotIn("disable_object_legal_hold", source)
        self.assertNotIn("set_object_legal_hold", source)

    def test_compose_injects_url_but_never_mediator_credentials_into_api(self) -> None:
        compose = (ROOT / "compose.yaml").read_text()
        start = compose.index("  api:")
        end = compose.index("\n  web:", start)
        api = compose[start:end]
        self.assertIn("LEGAL_HOLD_APPLIER_URL: http://legal-hold-applier:8090", api)
        self.assertNotIn("MINIO_HOLD_APPLIER_ACCESS_KEY", api)
        self.assertNotIn("MINIO_HOLD_APPLIER_SECRET_KEY", api)


if __name__ == "__main__":
    unittest.main()
