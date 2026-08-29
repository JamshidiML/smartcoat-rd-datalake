from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "infra/legacy/legacy_reconciliation.py"
POLICY_PATH = ROOT / "infra/minio/policies/legacy-reconciliation.json"
LIVE_PATH = ROOT / "infra/legacy/tests/live_legacy_reconciliation_acceptance.py"
RUNBOOK_PATH = ROOT / "docs/runbooks/LEGACY_BRONZE_RECONCILIATION.md"
LAUNCHER_PATH = ROOT / "infra/legacy/run_legacy_reconciliation.sh"

spec = importlib.util.spec_from_file_location("legacy_reconciliation", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("legacy reconciliation module could not be loaded")
legacy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = legacy
spec.loader.exec_module(legacy)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def row(
    number: int,
    *,
    bucket: str = "sc-rd-bronze-originals",
    key: str | None = None,
    sha256: str | None = None,
    version_id: str | None = None,
    kind: str = "ORIGINAL",
) -> legacy.DatabaseBronzeRow:
    return legacy.DatabaseBronzeRow(
        bronze_object_id=f"00000000-0000-7000-8000-{number:012d}",
        ingestion_id=f"10000000-0000-7000-8000-{number:012d}",
        bucket_name=bucket,
        object_key=key or f"rd/legacy/{number}/object.bin",
        object_kind=kind,
        sha256=sha256 or digest(f"content-{number}"),
        object_version_id=version_id,
    )


def version(
    number: int,
    *,
    bucket: str = "sc-rd-bronze-originals",
    key: str | None = None,
    sha256: str | None = None,
    version_id: str | None = None,
    delete_marker: bool = False,
) -> legacy.StorageVersion:
    return legacy.StorageVersion(
        bucket_name=bucket,
        object_key=key or f"rd/legacy/{number}/object.bin",
        object_version_id=version_id or f"version-{number}",
        sha256=None if delete_marker else (sha256 or digest(f"content-{number}")),
        last_modified_utc=datetime(2026, 8, 29, tzinfo=UTC),
        is_delete_marker=delete_marker,
    )


class FakeStorage:
    def __init__(self, versions: list[legacy.StorageVersion]) -> None:
        from retention_enforcement import StorageRetention, StorageVersionMetadata

        self.StorageRetention = StorageRetention
        self.StorageVersionMetadata = StorageVersionMetadata
        self.versions = {
            (item.bucket_name, item.object_key, item.object_version_id): item
            for item in versions
            if not item.is_delete_marker
        }
        self.retentions: dict[tuple[str, str, str], object] = {}
        self.set_calls = 0

    @staticmethod
    def identity(target: object) -> tuple[str, str, str]:
        return (target.bucket_name, target.object_key, target.object_version_id)

    def stat_exact(self, target: object) -> object:
        item = self.versions[self.identity(target)]
        return self.StorageVersionMetadata(
            object_version_id=item.object_version_id,
            last_modified_utc=item.last_modified_utc,
        )

    def get_retention_exact(self, target: object) -> object | None:
        return self.retentions.get(self.identity(target))

    def set_retention_exact(self, target: object, retain_until_utc: datetime) -> None:
        self.set_calls += 1
        self.retentions[self.identity(target)] = self.StorageRetention(
            mode="COMPLIANCE", retain_until_utc=retain_until_utc
        )


class FakeMediator:
    def __init__(self) -> None:
        self.holds: dict[tuple[str, str, str], str] = {}
        self.apply_calls = 0

    @staticmethod
    def identity(target: object) -> tuple[str, str, str]:
        return (target.bucket_name, target.object_key, target.object_version_id)

    def read_status(self, target: object) -> str:
        return self.holds.get(self.identity(target), "OFF")

    def apply_on(self, target: object) -> str:
        self.apply_calls += 1
        self.holds[self.identity(target)] = "ON"
        return "ON"


class FakeRepository:
    def __init__(self) -> None:
        self.run: dict[str, object] | None = None
        self.successes: dict[str, dict[str, object]] = {}
        self.mapping_outcomes: dict[str, tuple[str, ...]] = {}
        self.persist_calls = 0

    def existing_run(self, inventory_sha256: str) -> dict[str, object] | None:
        if self.run and self.run["inventory_sha256"] == inventory_sha256:
            return self.run
        return None

    def existing_successes(self) -> dict[str, dict[str, object]]:
        return dict(self.successes)

    def existing_mapping_outcomes(self, _run_id: str) -> dict[str, tuple[str, ...]]:
        return dict(self.mapping_outcomes)

    def persist(self, **values: object) -> None:
        self.persist_calls += 1
        plan = values["plan"]
        run_id = values["run_id"]
        summary = values["summary"]
        self.run = {
            "inventory_sha256": plan.inventory_sha256,
            "reconciliation_run_id": run_id,
            "summary_json": summary,
        }
        self.successes.update({
            item["mapping_identity_sha256"]: item
            for item in values["successes"]
        })
        outcomes: dict[str, list[str]] = {}
        for item in values["items"]:
            if item.mapping_identity_sha256:
                outcomes.setdefault(item.mapping_identity_sha256, []).append(
                    item.classification
                )
        self.mapping_outcomes = {
            identity: tuple(sorted(classifications))
            for identity, classifications in outcomes.items()
        }


class InventoryPlanningTests(unittest.TestCase):
    def test_required_matching_and_quarantine_cases_account_every_entity_once(self) -> None:
        exact = row(1, version_id="version-1")
        null_single = row(2)
        null_unique = row(3, sha256=digest("wanted"))
        ambiguous = row(4, sha256=digest("same"))
        mismatch = row(5, sha256=digest("database"), version_id="version-5")
        database_orphan = row(6, version_id="missing-version")
        contradictory = row(7, kind="MANIFEST")
        versions = [
            version(1),
            version(2),
            version(3, sha256=digest("wanted"), version_id="version-3a"),
            version(3, sha256=digest("other"), version_id="version-3b"),
            version(4, sha256=digest("same"), version_id="version-4a"),
            version(4, sha256=digest("same"), version_id="version-4b"),
            version(5, sha256=digest("storage")),
            version(8),
            version(1, version_id="delete-marker-1", delete_marker=True),
        ]
        plan = legacy.plan_inventory(
            [exact, null_single, null_unique, ambiguous, mismatch, database_orphan, contradictory],
            versions,
        )
        self.assertEqual(3, len(plan.mappings))
        self.assertEqual(
            {
                "EXISTING_EXACT_VERSION_ID",
                "NULL_VERSION_SINGLE_HASH_MATCH",
                "NULL_VERSION_UNIQUE_HASH_MATCH_AMONG_VERSIONS",
            },
            {mapping.matching_basis for mapping in plan.mappings},
        )
        self.assertEqual(len(plan.rows) + len(plan.versions), len(plan.items))
        self.assertEqual(
            len(plan.items), len({item.entity_identity_sha256 for item in plan.items})
        )
        classifications = {item.classification for item in plan.items}
        for expected in (
            "AMBIGUOUS_VERSION_MATCH",
            "HASH_MISMATCH",
            "ORPHAN_STORAGE_VERSION",
            "ORPHAN_DATABASE_ROW",
            "CONTRADICTORY_METADATA",
            "DELETE_MARKER_RETAINED",
        ):
            self.assertIn(expected, classifications)
        self.assertEqual(1, plan.counts()["delete_markers"])

    def test_inventory_and_counts_are_deterministic(self) -> None:
        rows = [row(2), row(1, version_id="version-1")]
        versions = [version(2), version(1)]
        first = legacy.plan_inventory(rows, versions)
        second = legacy.plan_inventory(reversed(rows), reversed(versions))
        self.assertEqual(first.inventory_sha256, second.inventory_sha256)
        self.assertEqual(first.counts(), second.counts())

    def test_calendar_year_floor_handles_leap_day_in_utc(self) -> None:
        self.assertEqual(
            datetime(2034, 2, 28, 12, tzinfo=UTC),
            legacy.add_calendar_years(datetime(2024, 2, 29, 12, tzinfo=UTC), 10),
        )


class ApplyTests(unittest.TestCase):
    def test_apply_is_monotonic_and_repeat_revalidates_without_duplicate_evidence(self) -> None:
        database_row = row(1, version_id="version-1")
        storage_version = version(1)
        plan = legacy.plan_inventory([database_row], [storage_version])
        repository = FakeRepository()
        storage = FakeStorage([storage_version])
        mediator = FakeMediator()
        clock = datetime(2026, 8, 29, 12, tzinfo=UTC)

        first = legacy.apply_plan(plan, repository, storage, mediator, clock)
        second = legacy.apply_plan(plan, repository, storage, mediator, clock)

        self.assertFalse(first["reused_run"])
        self.assertTrue(second["reused_run"])
        self.assertEqual(first["reconciliation_run_id"], second["reconciliation_run_id"])
        self.assertEqual(1, repository.persist_calls)
        self.assertEqual(1, storage.set_calls)
        self.assertEqual(2, mediator.apply_calls)
        self.assertEqual("ON", next(iter(mediator.holds.values())))
        requested = next(iter(repository.successes.values()))["requested_retain_until_utc"]
        self.assertEqual(datetime(2036, 8, 29, 12, tzinfo=UTC), requested)

    def test_existing_stronger_compliance_floor_is_never_shortened(self) -> None:
        database_row = row(1, version_id="version-1")
        storage_version = version(1)
        plan = legacy.plan_inventory([database_row], [storage_version])
        repository = FakeRepository()
        storage = FakeStorage([storage_version])
        mediator = FakeMediator()
        from retention_enforcement import StorageRetention

        stronger = datetime(2045, 1, 1, tzinfo=UTC)
        identity = (storage_version.bucket_name, storage_version.object_key, "version-1")
        storage.retentions[identity] = StorageRetention("COMPLIANCE", stronger)
        legacy.apply_plan(
            plan, repository, storage, mediator, datetime(2026, 8, 29, tzinfo=UTC)
        )
        self.assertEqual(0, storage.set_calls)
        self.assertEqual(
            stronger,
            next(iter(repository.successes.values()))["requested_retain_until_utc"],
        )

    def test_provider_precision_uses_a_whole_second_reconciliation_anchor(self) -> None:
        database_row = row(1, version_id="version-1")
        storage_version = version(1)
        plan = legacy.plan_inventory([database_row], [storage_version])
        repository = FakeRepository()
        legacy.apply_plan(
            plan,
            repository,
            FakeStorage([storage_version]),
            FakeMediator(),
            datetime(2026, 8, 29, 12, 30, 45, 987654, tzinfo=UTC),
        )
        requested = next(iter(repository.successes.values()))["requested_retain_until_utc"]
        self.assertEqual(datetime(2036, 8, 29, 12, 30, 45, tzinfo=UTC), requested)

    def test_missing_success_for_existing_run_fails_closed(self) -> None:
        database_row = row(1, version_id="version-1")
        storage_version = version(1)
        plan = legacy.plan_inventory([database_row], [storage_version])
        repository = FakeRepository()
        repository.run = {
            "inventory_sha256": plan.inventory_sha256,
            "reconciliation_run_id": "00000000-0000-7000-8000-000000000999",
            "summary_json": {},
        }
        with self.assertRaisesRegex(legacy.LegacyReconciliationError, "missing"):
            legacy.apply_plan(
                plan, repository, FakeStorage([storage_version]), FakeMediator(),
                datetime(2026, 8, 29, tzinfo=UTC),
            )

    def test_enforcement_failure_is_durably_quarantined(self) -> None:
        database_row = row(1, version_id="version-1")
        storage_version = version(1)
        plan = legacy.plan_inventory([database_row], [storage_version])
        repository = FakeRepository()
        storage = FakeStorage([storage_version])
        storage.stat_exact = lambda _target: (_ for _ in ()).throw(
            RuntimeError("synthetic readback failure")
        )
        result = legacy.apply_plan(
            plan, repository, storage, FakeMediator(), datetime(2026, 8, 29, tzinfo=UTC)
        )
        self.assertEqual(2, result["summary"]["enforcement_failures"])
        self.assertEqual(2, result["summary"]["quarantined_entities"])
        self.assertFalse(repository.successes)
        repeated = legacy.apply_plan(
            plan, repository, FakeStorage([storage_version]), FakeMediator(),
            datetime(2026, 8, 29, tzinfo=UTC),
        )
        self.assertTrue(repeated["reused_run"])
        self.assertEqual(1, repository.persist_calls)


class BoundaryTests(unittest.TestCase):
    def test_apply_requires_exact_confirmation_before_adapter_construction(self) -> None:
        with patch.object(legacy, "build_adapters", side_effect=AssertionError("touched")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = legacy.main(["--apply"])
        self.assertEqual(2, exit_code)
        self.assertIn("BLOCKED_EXPLICIT_CONFIRMATION_REQUIRED", output.getvalue())

    def test_dry_run_does_not_use_evidence_or_enforcement_mutation_boundaries(self) -> None:
        plan = legacy.plan_inventory([row(1, version_id="version-1")], [version(1)])
        adapters = SimpleNamespace(
            database_rows=lambda: plan.rows,
            storage_versions=lambda: plan.versions,
        )
        repository = SimpleNamespace(
            existing_run=lambda *_: (_ for _ in ()).throw(AssertionError("mutated")),
            persist=lambda **_: (_ for _ in ()).throw(AssertionError("mutated")),
        )
        with patch.object(legacy, "build_adapters", return_value=(adapters, repository, object())):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = legacy.main(["--dry-run"])
        self.assertEqual(0, exit_code)
        evidence = json.loads(output.getvalue())
        self.assertEqual("DRY_RUN_COMPLETE_NO_MUTATION", evidence["classification"])
        self.assertFalse(evidence["mutated"])

    def test_policy_is_inventory_and_retention_only_with_explicit_destructive_denies(self) -> None:
        policy = json.loads(POLICY_PATH.read_text())
        allowed = {
            action
            for statement in policy["Statement"]
            if statement["Effect"] == "Allow"
            for action in statement["Action"]
        }
        denied = {
            action
            for statement in policy["Statement"]
            if statement["Effect"] == "Deny"
            for action in statement["Action"]
        }
        self.assertIn("s3:ListBucket", allowed)
        self.assertIn("s3:GetObjectVersion", allowed)
        self.assertIn("s3:PutObjectRetention", allowed)
        self.assertTrue({
            "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutObject",
            "s3:GetObjectLegalHold", "s3:PutObjectLegalHold",
        }.issubset(denied))
        self.assertFalse({"s3:DeleteObject", "s3:PutObjectLegalHold"} & allowed)

    def test_no_authorization_is_nonzero_without_docker_or_database(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--apply"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("BLOCKED_EXPLICIT_CONFIRMATION_REQUIRED", completed.stdout)

    def test_live_acceptance_is_explicitly_opt_in(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(LIVE_PATH)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("BLOCKED_ISOLATION", completed.stdout)
        self.assertIn(
            "--confirm-disposable-synthetic-legacy-reconciliation-run",
            LIVE_PATH.read_text(encoding="utf-8"),
        )

    def test_runbook_documents_dry_run_authority_and_non_destructive_contract(self) -> None:
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
        for required in (
            "LEGACY_RECONCILIATION_DATABASE_URL",
            "MINIO_LEGACY_RECONCILIATION_ACCESS_KEY",
            "LEGAL_HOLD_APPLIER_CALL_TOKEN",
            "--dry-run",
            "DRY_RUN_COMPLETE_NO_MUTATION",
            "--confirm-legacy-365-day-reconciliation",
            "PASS_LEGACY_RECONCILED_OR_QUARANTINED",
            "delete markers",
            "quarantined",
        ):
            self.assertIn(required, runbook)
        self.assertIn("never updates `bronze_objects`", runbook)

    def test_one_shot_launcher_is_internal_immutable_and_secret_name_only(self) -> None:
        launcher = LAUNCHER_PATH.read_text(encoding="utf-8")
        for required in (
            "--pull=never",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "LEGACY_RECONCILIATION_OPERATOR_IMAGE_ID",
            "LEGACY_RECONCILIATION_DOCKER_NETWORK",
            "--network",
            "--confirm-legacy-365-day-reconciliation",
        ):
            self.assertIn(required, launcher)
        self.assertNotIn(".env", launcher)
        self.assertNotIn("MINIO_ROOT", launcher)
        self.assertNotIn("-p ", launcher)


if __name__ == "__main__":
    unittest.main()
