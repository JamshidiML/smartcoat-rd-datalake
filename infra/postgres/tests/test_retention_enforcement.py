from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra/postgres/migrations/0007__record_retention_enforcement_evidence.sql"
DATABASE = ROOT / "apps/api/src/database.py"


class RetentionEnforcementMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text()
        cls.database = DATABASE.read_text()

    def test_collision_free_migration_identity_and_full_intended_chain_slot(self) -> None:
        sys.path.insert(0, str(ROOT / "infra/postgres"))
        import migrate

        discovered = migrate.discover_migrations(ROOT / "infra/postgres/migrations")
        self.assertEqual([1, 2, 3, 4, 5, 6, 7, 8, 9], [item.version for item in discovered])
        candidate = {item.version: item for item in discovered}[7]
        self.assertEqual(7, candidate.version)
        self.assertEqual("record_retention_enforcement_evidence", candidate.name)

    def test_exact_version_policy_and_observed_readback_are_recorded(self) -> None:
        for required in (
            "bucket_name text NOT NULL",
            "object_key text NOT NULL",
            "object_kind text NOT NULL",
            "object_version_id text NOT NULL",
            "data_category text NOT NULL",
            "retention_class text NOT NULL",
            "retention_policy_version text NOT NULL",
            "accepted_storage_at_utc timestamptz NOT NULL",
            "requested_retention_mode text NOT NULL",
            "requested_retain_until_utc timestamptz NOT NULL",
            "requested_legal_hold_status text NOT NULL",
            "observed_object_version_id text",
            "observed_retention_mode text",
            "observed_retain_until_utc timestamptz",
            "observed_legal_hold_status text",
            "enforcement_verified_at_utc timestamptz NOT NULL",
            "enforcement_verification_result text NOT NULL",
            "enforced_by text NOT NULL",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.sql)

    def test_success_requires_exact_identity_compliance_floor_and_hold(self) -> None:
        for guard in (
            "observed_object_version_id = object_version_id",
            "observed_retention_mode = 'COMPLIANCE'",
            "observed_retain_until_utc >= requested_retain_until_utc",
            "requested_legal_hold_status = 'ON'",
            "observed_legal_hold_status = 'ON'",
        ):
            self.assertIn(guard, self.sql)
        self.assertIn("assignment.expected_retain_until_utc > NEW.requested_retain_until_utc", self.sql)
        self.assertIn("assignment.accepted_storage_at_utc <> NEW.accepted_storage_at_utc", self.sql)

    def test_evidence_is_append_only_and_one_success_per_assignment(self) -> None:
        self.assertRegex(
            self.sql,
            r"CREATE TRIGGER bronze_retention_enforcement_evidence_append_only\s+"
            r"BEFORE UPDATE OR DELETE ON bronze_retention_enforcement_evidence\s+"
            r"FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation\(\);",
        )
        self.assertIn("CREATE UNIQUE INDEX bronze_retention_enforcement_one_success", self.sql)
        self.assertRegex(
            self.sql,
            r"WHERE enforcement_verification_result = 'SUCCESS';",
        )

    def test_runtime_grants_match_split_role_least_privilege(self) -> None:
        normalized = " ".join(self.sql.split())
        self.assertIn(
            "GRANT SELECT, INSERT ON TABLE bronze_retention_assignments, "
            "bronze_retention_enforcement_evidence TO smartcoat_ingestion;",
            normalized,
        )
        self.assertIn(
            "GRANT SELECT ON TABLE canonical_retention_classes, "
            "retention_policy_versions, retention_category_rules, "
            "bronze_retention_assignments, "
            "bronze_retention_enforcement_evidence TO smartcoat_backup;",
            normalized,
        )
        self.assertIn("ALTER ROLE smartcoat_app NOLOGIN PASSWORD NULL", self.sql)
        grants = re.findall(r"GRANT\s+([^;]+);", self.sql, re.IGNORECASE)
        self.assertFalse(any("smartcoat_app" in value for value in grants))
        self.assertFalse(any("smartcoat_ocr" in value for value in grants))
        self.assertFalse(any("smartcoat_review" in value for value in grants))
        self.assertNotRegex(self.sql, r"(?i)GRANT\s+[^;]*(UPDATE|DELETE|TRUNCATE|CREATE|ALL)")

    def test_identity_guards_do_not_require_update_class_row_locks(self) -> None:
        self.assertIn(
            "CREATE OR REPLACE FUNCTION validate_bronze_retention_assignment()",
            self.sql,
        )
        self.assertNotIn("FOR KEY SHARE", self.sql)
        self.assertNotIn("SECURITY DEFINER", self.sql)

    def test_migration_does_not_mutate_existing_evidence_or_orchestrate_bronze(self) -> None:
        self.assertNotRegex(self.sql, r"(?im)^\s*(UPDATE|DELETE)\s+")
        for forbidden in (
            "BRONZE_COMMITTED",
            "OCR_QUEUED",
            "put_object",
            "set_object_retention",
            "enable_object_legal_hold",
            "lifecycle",
        ):
            self.assertNotIn(forbidden, self.sql.lower())

    def test_repository_persists_assignment_and_success_in_one_transaction(self) -> None:
        start = self.database.index("    def record_retention_enforcement(")
        end = self.database.index("\n    def transition(", start)
        method = self.database[start:end]
        self.assertIn("with self.connection() as connection:", method)
        self.assertIn("INSERT INTO bronze_retention_assignments", method)
        self.assertIn("INSERT INTO bronze_retention_enforcement_evidence", method)
        self.assertIn("Conflicting exact-version retention assignment", method)
        self.assertIn("Conflicting retention enforcement evidence", method)
        self.assertNotIn("DELETE", method.upper())
        self.assertNotIn("UPDATE", method.upper())


if __name__ == "__main__":
    unittest.main()
