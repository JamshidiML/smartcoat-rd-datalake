#!/usr/bin/env python3
"""Opt-in fresh/upgraded PostgreSQL acceptance for CAND-META.

The script authenticates and reuses the accepted M0-R01.4.1 isolation
infrastructure.  It creates two independent disposable projects, never reads
the repository ``.env``, never publishes a port, and uses only already-present
immutable local images.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import stat
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
LIFECYCLE_PATH = ROOT / "infra/postgres/tests/live_migration_lifecycle_acceptance.py"
CANDIDATE_MIGRATION = (
    ROOT / "infra/postgres/migrations/0005__expand_retention_metadata.sql"
)
POLICY_MODULE_PATH = ROOT / "apps/api/src/retention_policy.py"
EXPECTED_LIFECYCLE_SHA256 = (
    "4d7fbe8d33d36b6ff50161f4374cf16477667903253b790cbc37cb3e54707cfd"
)
AUTHORIZATION_FLAG = "--confirm-disposable-synthetic-retention-metadata-run"
PASS = "PASS_METADATA_EXPAND"
BLOCKED_ISOLATION = "BLOCKED_ISOLATION"
BLOCKED_IMPLEMENTATION_BOUNDARY = "BLOCKED_IMPLEMENTATION_BOUNDARY"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_lifecycle_module():
    if not LIFECYCLE_PATH.is_file() or sha256(LIFECYCLE_PATH) != EXPECTED_LIFECYCLE_SHA256:
        raise RuntimeError(BLOCKED_IMPLEMENTATION_BOUNDARY)
    spec = importlib.util.spec_from_file_location(
        "accepted_live_migration_lifecycle",
        LIFECYCLE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(BLOCKED_IMPLEMENTATION_BOUNDARY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lifecycle = load_lifecycle_module()


def load_policy_module():
    spec = importlib.util.spec_from_file_location(
        "retention_policy_live_contract",
        POLICY_MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(BLOCKED_IMPLEMENTATION_BOUNDARY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


policy = load_policy_module()


LEGACY_FIXTURE_SQL = """
INSERT INTO users (
    user_id, display_name, email, role, active, created_at_utc
) VALUES (
    'usr_cand_meta_synthetic',
    'CAND META Synthetic',
    'cand-meta@example.invalid',
    'UPLOADER',
    true,
    TIMESTAMPTZ '2026-08-20T00:00:00Z'
);

INSERT INTO uploads (
    ingestion_id, department, uploader_user_id, uploader_display_name,
    uploaded_at_utc, original_filename, stored_object_key, manifest_object_key,
    detected_mime_type, declared_file_type, document_category, context_note,
    capture_date, byte_size, source_sha256, duplicate_of_ingestion_id,
    source_channel, state
) VALUES (
    '00000000-0000-7000-8000-000000000501',
    'RND',
    'usr_cand_meta_synthetic',
    'CAND META Synthetic',
    TIMESTAMPTZ '2026-08-20T00:00:00Z',
    'synthetic.jpg',
    'synthetic/cand-meta/original.jpg',
    'synthetic/cand-meta/manifest.json',
    'image/jpeg',
    'PHOTO',
    'LAB_NOTE',
    'Synthetic upgraded-volume fixture only.',
    DATE '2026-08-20',
    42,
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    NULL,
    'WEB_UPLOAD',
    'RECEIVED'
);

INSERT INTO bronze_objects (
    bronze_object_id, ingestion_id, bucket_name, object_key, object_kind,
    sha256, object_version_id, retention_mode, retain_until_utc, created_at_utc
) VALUES
(
    '00000000-0000-7000-8000-000000000511',
    '00000000-0000-7000-8000-000000000501',
    'sc-rd-bronze-originals',
    'synthetic/cand-meta/original.jpg',
    'ORIGINAL',
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    NULL,
    'COMPLIANCE',
    TIMESTAMPTZ '2027-08-20T00:00:00Z',
    TIMESTAMPTZ '2026-08-20T00:00:00Z'
),
(
    '00000000-0000-7000-8000-000000000512',
    '00000000-0000-7000-8000-000000000501',
    'sc-rd-bronze-manifests',
    'synthetic/cand-meta/manifest.json',
    'MANIFEST',
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    'synthetic-version-manifest-1',
    'COMPLIANCE',
    TIMESTAMPTZ '2027-08-20T00:00:00Z',
    TIMESTAMPTZ '2026-08-20T00:00:00Z'
);
"""


BRONZE_SNAPSHOT_SQL = """
SELECT bronze_object_id::text, ingestion_id::text, bucket_name, object_key,
       object_kind, sha256, object_version_id, retention_mode,
       retain_until_utc::text, created_at_utc::text
FROM bronze_objects
ORDER BY bronze_object_id
"""


class MetadataScenario(lifecycle.LiveMigrationLifecycleAcceptance):
    def __init__(self, scenario: str) -> None:
        super().__init__()
        self.scenario = scenario
        self.candidate_fixture = (
            self.migration_fixture_directory / CANDIDATE_MIGRATION.name
        )
        self.evidence["scenario"] = scenario

    def install_candidate_fixture(self) -> str:
        self._require(
            not self.candidate_fixture.exists(),
            "Candidate fixture unexpectedly existed before controlled installation",
        )
        source = CANDIDATE_MIGRATION.read_bytes()
        self.candidate_fixture.write_bytes(source)
        self.candidate_fixture.chmod(0o400)
        self._require(
            self.candidate_fixture.stat().st_uid == self.temporary_directory.stat().st_uid
            and stat.S_IMODE(self.candidate_fixture.stat().st_mode) == 0o400
            and self.candidate_fixture.read_bytes() == source,
            "Candidate fixture ownership, mode, or bytes are invalid",
            lifecycle.IsolationBlocked,
        )
        return hashlib.sha256(source).hexdigest()

    def _negative_insert(self, label: str, statement: str, marker: str) -> None:
        result = self._psql(label, statement)
        self._require(result.returncode != 0, f"{label} unexpectedly succeeded")
        self._require(marker in result.stderr, f"{label} failed for an unexpected reason")

    def execute(self) -> None:
        self._install_cleanup_handlers()
        self.state_change_attempted = True
        self._verify_migration_execution_image_boundary()
        started = self._compose(
            "start_isolated_postgres",
            "up",
            "--detach",
            "--no-deps",
            "--pull",
            "never",
            "postgres",
            timeout=180,
        )
        self._require(started.returncode == 0, "Disposable PostgreSQL did not start")
        container_id = self._wait_for_postgres()
        self._verify_running_postgres_isolation(container_id)

        adoption = self._run_migration("explicit_adoption", "adopt", self.database_name)
        self._require(
            adoption.returncode == 0 and "status=ADOPTED" in adoption.stdout,
            "Fresh bootstrap adoption failed",
        )

        if self.scenario == "upgraded_volume":
            self._psql_success("install_legacy_fixture", LEGACY_FIXTURE_SQL)
        legacy_before = self._psql_rows("bronze_before_migration", BRONZE_SNAPSHOT_SQL)

        candidate_sha = self.install_candidate_fixture()
        applied = self._run_migration("apply_metadata_expand")
        self._require(
            applied.returncode == 0
            and "discovered=2" in applied.stdout
            and "applied_now=1" in applied.stdout,
            "METADATA_EXPAND did not apply exactly once",
        )
        repeated = self._run_migration("reapply_metadata_expand")
        self._require(
            repeated.returncode == 0
            and "already_applied=2" in repeated.stdout
            and "applied_now=0" in repeated.stdout,
            "METADATA_EXPAND reapplication was not idempotent",
        )

        legacy_after = self._psql_rows("bronze_after_migration", BRONZE_SNAPSHOT_SQL)
        self._require(
            legacy_after == legacy_before,
            "METADATA_EXPAND mutated a legacy Bronze row",
        )
        if self.scenario == "upgraded_volume":
            self._require(
                len(legacy_after) == 2
                and sum(row["object_version_id"] is None for row in legacy_after) == 1,
                "Upgraded-volume null-version compatibility was not preserved",
            )

        classes = self._psql_rows(
            "canonical_classes",
            "SELECT retention_class, calendar_years, fixed_duration_hours, "
            "legal_hold_required FROM canonical_retention_classes ORDER BY retention_class",
        )
        self._require(
            {row["retention_class"] for row in classes}
            == {"permanent", "long_term_10y", "short_90d"}
            and len(classes) == 3,
            "Canonical retention-class rows are not exact",
        )
        database_rules = self._psql_rows(
            "approved_category_rules",
            "SELECT retention_policy_version, data_category, retention_class, "
            "records_purpose, legal_basis_classification "
            "FROM retention_category_rules ORDER BY data_category",
        )
        python_rules = sorted(
            (
                {
                    "retention_policy_version": policy.RETENTION_POLICY_VERSION,
                    "data_category": category,
                    "retention_class": rule.retention_class,
                    "records_purpose": rule.records_purpose,
                    "legal_basis_classification": rule.legal_basis_classification,
                }
                for category, rule in policy.approved_rules().items()
            ),
            key=lambda row: row["data_category"],
        )
        self._require(
            database_rules == python_rules,
            "Approved PostgreSQL category rules differ from Python policy rules",
        )
        deadlines = self._psql_rows(
            "deadline_semantics",
            """
            SELECT
              retention_deadline_utc(
                'long_term_10y', TIMESTAMPTZ '2024-02-29T23:59:58.987Z'
              )::text AS leap_deadline,
              EXTRACT(
                EPOCH FROM (
                  retention_deadline_utc(
                    'short_90d', TIMESTAMPTZ '2026-03-29T00:30:45.987Z'
                  ) - TIMESTAMPTZ '2026-03-29T00:30:45Z'
                )
              )::bigint AS short_seconds
            """,
        )
        self._require(
            len(deadlines) == 1
            and deadlines[0]["leap_deadline"].startswith("2034-02-28 23:59:58")
            and deadlines[0]["short_seconds"] == 7776000,
            "Live deadline semantics differ from the accepted policy",
        )

        if self.scenario == "fresh_volume":
            self._psql_success("install_post_migration_bronze_fixture", LEGACY_FIXTURE_SQL)

        self._negative_insert(
            "approved_policy_rule_insert_rejected",
            """
            INSERT INTO retention_category_rules (
              retention_policy_version, data_category, retention_class,
              records_purpose, legal_basis_classification
            ) VALUES (
              'smartcoat_retention_2026_08_v1', 'LATE_POLICY_MUTATION',
              'permanent', 'Synthetic forbidden late rule',
              'approved_non_personal_evidence'
            )
            """,
            "Retention category rules are sealed for approved policy version",
        )
        self._negative_insert(
            "empty_policy_approval_rejected",
            """
            INSERT INTO retention_policy_versions (
              retention_policy_version, policy_document_path,
              policy_document_sha256, approved_at_utc, approved_by
            ) VALUES (
              'synthetic_empty_policy_v1', 'synthetic/never-approved',
              'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
              TIMESTAMPTZ '2026-08-20T00:00:00Z', 'synthetic_test'
            )
            """,
            "Retention policy cannot be approved without category rules",
        )
        self._negative_insert(
            "unknown_category_rejected",
            """
            INSERT INTO bronze_retention_assignments (
              retention_assignment_id, bronze_object_id, ingestion_id, bucket_name,
              object_key, object_kind, object_version_id, data_category,
              retention_class, retention_policy_version, retention_assigned_at_utc,
              retention_assigned_by, accepted_storage_at_utc,
              expected_retain_until_utc, legal_hold_required
            ) VALUES (
              '00000000-0000-7000-8000-000000000521',
              '00000000-0000-7000-8000-000000000512',
              '00000000-0000-7000-8000-000000000501',
              'sc-rd-bronze-manifests', 'synthetic/cand-meta/manifest.json',
              'MANIFEST', 'synthetic-version-manifest-1', 'UNKNOWN', 'permanent',
              'smartcoat_retention_2026_08_v1', TIMESTAMPTZ '2026-08-20T00:01:00Z',
              'synthetic_rule', TIMESTAMPTZ '2026-08-20T00:00:00Z',
              TIMESTAMPTZ '2036-08-20T00:00:00Z', true
            )
            """,
            "foreign key constraint",
        )
        self._negative_insert(
            "mismatched_version_rejected",
            """
            INSERT INTO bronze_retention_assignments (
              retention_assignment_id, bronze_object_id, ingestion_id, bucket_name,
              object_key, object_kind, object_version_id, data_category,
              retention_class, retention_policy_version, retention_assigned_at_utc,
              retention_assigned_by, accepted_storage_at_utc,
              expected_retain_until_utc, legal_hold_required
            ) VALUES (
              '00000000-0000-7000-8000-000000000522',
              '00000000-0000-7000-8000-000000000512',
              '00000000-0000-7000-8000-000000000501',
              'sc-rd-bronze-manifests', 'synthetic/cand-meta/manifest.json',
              'MANIFEST', 'wrong-version', 'LAB_NOTE', 'permanent',
              'smartcoat_retention_2026_08_v1', TIMESTAMPTZ '2026-08-20T00:01:00Z',
              'synthetic_rule', TIMESTAMPTZ '2026-08-20T00:00:00Z',
              TIMESTAMPTZ '2036-08-20T00:00:00Z', true
            )
            """,
            "does not match exact Bronze version identity",
        )
        self._negative_insert(
            "arbitrary_deadline_rejected",
            """
            INSERT INTO bronze_retention_assignments (
              retention_assignment_id, bronze_object_id, ingestion_id, bucket_name,
              object_key, object_kind, object_version_id, data_category,
              retention_class, retention_policy_version, retention_assigned_at_utc,
              retention_assigned_by, accepted_storage_at_utc,
              expected_retain_until_utc, legal_hold_required
            ) VALUES (
              '00000000-0000-7000-8000-000000000523',
              '00000000-0000-7000-8000-000000000512',
              '00000000-0000-7000-8000-000000000501',
              'sc-rd-bronze-manifests', 'synthetic/cand-meta/manifest.json',
              'MANIFEST', 'synthetic-version-manifest-1', 'LAB_NOTE', 'permanent',
              'smartcoat_retention_2026_08_v1', TIMESTAMPTZ '2026-08-20T00:01:00Z',
              'synthetic_rule', TIMESTAMPTZ '2026-08-20T00:00:00Z',
              TIMESTAMPTZ '2027-08-20T00:00:00Z', true
            )
            """,
            "check constraint",
        )
        self._negative_insert(
            "fractional_storage_anchor_rejected",
            """
            INSERT INTO bronze_retention_assignments (
              retention_assignment_id, bronze_object_id, ingestion_id, bucket_name,
              object_key, object_kind, object_version_id, data_category,
              retention_class, retention_policy_version, retention_assigned_at_utc,
              retention_assigned_by, accepted_storage_at_utc,
              expected_retain_until_utc, legal_hold_required
            ) VALUES (
              '00000000-0000-7000-8000-000000000525',
              '00000000-0000-7000-8000-000000000512',
              '00000000-0000-7000-8000-000000000501',
              'sc-rd-bronze-manifests', 'synthetic/cand-meta/manifest.json',
              'MANIFEST', 'synthetic-version-manifest-1', 'LAB_NOTE', 'permanent',
              'smartcoat_retention_2026_08_v1', TIMESTAMPTZ '2026-08-20T00:01:00Z',
              'synthetic_rule', TIMESTAMPTZ '2026-08-20T00:00:00.123Z',
              TIMESTAMPTZ '2036-08-20T00:00:00Z', true
            )
            """,
            "bronze_retention_assignments_whole_second_anchor",
        )

        self._psql_success(
            "insert_valid_exact_version_assignment",
            """
            INSERT INTO bronze_retention_assignments (
              retention_assignment_id, bronze_object_id, ingestion_id, bucket_name,
              object_key, object_kind, object_version_id, data_category,
              retention_class, retention_policy_version, retention_assigned_at_utc,
              retention_assigned_by, accepted_storage_at_utc,
              expected_retain_until_utc, legal_hold_required
            ) VALUES (
              '00000000-0000-7000-8000-000000000524',
              '00000000-0000-7000-8000-000000000512',
              '00000000-0000-7000-8000-000000000501',
              'sc-rd-bronze-manifests', 'synthetic/cand-meta/manifest.json',
              'MANIFEST', 'synthetic-version-manifest-1', 'LAB_NOTE', 'permanent',
              'smartcoat_retention_2026_08_v1', TIMESTAMPTZ '2026-08-20T00:01:00Z',
              'synthetic_rule', TIMESTAMPTZ '2026-08-20T00:00:00Z',
              TIMESTAMPTZ '2036-08-20T00:00:00Z', true
            )
            """,
        )
        self._negative_insert(
            "assignment_update_rejected",
            "UPDATE bronze_retention_assignments SET retention_assigned_by = 'forbidden'",
            "append-only",
        )
        self._negative_insert(
            "policy_update_rejected",
            "UPDATE retention_policy_versions SET approved_by = 'forbidden'",
            "append-only",
        )

        ledger = self._psql_rows(
            "candidate_ledger",
            "SELECT version, name, sha256 FROM smartcoat_migrations.applied_migrations "
            "ORDER BY version",
        )
        self._require(
            len(ledger) == 2
            and ledger[1] == {
                "version": 5,
                "name": "expand_retention_metadata",
                "sha256": candidate_sha,
            },
            "Candidate migration ledger identity is wrong",
        )
        self._require(
            self._psql_success(
                "migration_lock_rows",
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND granted",
            )
            == "0",
            "Migration advisory lock remained held",
        )
        self._verify_owned_resources()
        self.evidence.update(
            {
                "candidate_migration": {
                    "version": 5,
                    "name": "expand_retention_metadata",
                    "sha256": candidate_sha,
                    "applied_exactly_once": True,
                    "idempotent_reapply": True,
                },
                "legacy_rows_before": len(legacy_before),
                "legacy_rows_after": len(legacy_after),
                "legacy_rows_equal": legacy_before == legacy_after,
                "canonical_classes": sorted(row["retention_class"] for row in classes),
                "negative_checks": [
                    "approved_policy_rule_insert",
                    "empty_policy_approval",
                    "unknown_category",
                    "mismatched_version",
                    "arbitrary_deadline",
                    "fractional_storage_anchor",
                    "assignment_update",
                    "policy_update",
                ],
                "python_database_rules_equal": database_rules == python_rules,
                "deadline_semantics": deadlines[0],
                "valid_exact_version_assignments": 1,
                "migration_advisory_lock_rows": 0,
            }
        )


def run_scenario(scenario: str) -> tuple[str, dict[str, Any], str]:
    harness = MetadataScenario(scenario)
    result = PASS
    failure = ""
    try:
        harness.preflight()
        harness.execute()
    except lifecycle.AcceptanceError as exc:
        result = exc.result
        failure = str(exc)
    except Exception as exc:
        result = lifecycle.RESULT_PRODUCT_FAILURE
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        result, failure = lifecycle.finalize_harness(harness, result, failure)
    evidence = harness.sanitized_evidence()
    if failure:
        evidence["failure"] = failure
    return result, evidence, failure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(AUTHORIZATION_FLAG, action="store_true")
    args = parser.parse_args(argv)
    if not getattr(args, AUTHORIZATION_FLAG[2:].replace("-", "_")):
        print(json.dumps({"classification": BLOCKED_ISOLATION, "authorized": False}))
        print(BLOCKED_ISOLATION)
        return 2

    scenarios: list[dict[str, Any]] = []
    overall = PASS
    for name in ("fresh_volume", "upgraded_volume"):
        result, evidence, _failure = run_scenario(name)
        scenarios.append({"result": result, "evidence": evidence})
        if result != PASS:
            overall = result
            break
    print(
        json.dumps(
            {
                "classification": overall,
                "accepted_lifecycle_sha256": EXPECTED_LIFECYCLE_SHA256,
                "candidate_source_sha256": sha256(CANDIDATE_MIGRATION),
                "scenarios": scenarios,
            },
            indent=2,
            sort_keys=True,
        )
    )
    print(overall)
    return 0 if overall == PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
