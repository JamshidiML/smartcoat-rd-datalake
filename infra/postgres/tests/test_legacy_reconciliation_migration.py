from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra/postgres/migrations"
MIGRATION = MIGRATIONS / "0009__record_legacy_bronze_reconciliation.sql"


class LegacyReconciliationMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_chain_is_exactly_0001_through_0009(self) -> None:
        names = sorted(path.name for path in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]__*.sql"))
        self.assertEqual(list(range(1, 10)), [int(name[:4]) for name in names])
        self.assertEqual("0009__record_legacy_bronze_reconciliation.sql", names[-1])

    def test_dedicated_evidence_is_append_only(self) -> None:
        for table in (
            "legacy_reconciliation_runs",
            "legacy_reconciliation_items",
            "legacy_reconciliation_successes",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertRegex(
                self.sql,
                rf"CREATE TRIGGER {table}_append_only\s+BEFORE UPDATE OR DELETE ON {table}",
            )

    def test_every_inventory_entity_has_one_run_scoped_outcome(self) -> None:
        self.assertIn("UNIQUE (reconciliation_run_id, entity_identity_sha256)", self.sql)
        self.assertIn("outcome IN ('RECONCILED', 'QUARANTINED')", self.sql)
        for evidence in (
            "inventory_sha256", "outcome_sha256", "matching_basis",
            "prior_retain_until_utc", "requested_retain_until_utc",
            "observed_retain_until_utc", "observed_legal_hold_status",
            "classification", "attempt_identity_sha256",
        ):
            self.assertIn(evidence, self.sql)

    def test_success_is_unique_for_database_row_and_exact_storage_version(self) -> None:
        self.assertIn("bronze_object_id uuid NOT NULL UNIQUE", self.sql)
        self.assertIn("UNIQUE (bucket_name, object_key, object_version_id)", self.sql)
        self.assertIn("requested_retention_mode = 'COMPLIANCE'", self.sql)
        self.assertIn("observed_retention_mode = 'COMPLIANCE'", self.sql)
        self.assertIn("observed_legal_hold_status = 'ON'", self.sql)

    def test_runtime_roles_have_no_remediation_write_authority(self) -> None:
        normalized = re.sub(r"\s+", " ", self.sql)
        self.assertIn(
            "REVOKE ALL PRIVILEGES ON TABLE legacy_reconciliation_runs, "
            "legacy_reconciliation_items, legacy_reconciliation_successes FROM PUBLIC, "
            "smartcoat_app, smartcoat_ingestion, smartcoat_ocr, smartcoat_review, "
            "smartcoat_backup",
            normalized,
        )
        self.assertIn(
            "GRANT SELECT ON TABLE legacy_reconciliation_runs, "
            "legacy_reconciliation_items, legacy_reconciliation_successes TO smartcoat_backup",
            normalized,
        )
        self.assertNotRegex(
            normalized,
            r"GRANT (?:INSERT|UPDATE|DELETE|ALL).* TO smartcoat_(?:ingestion|ocr|review|backup|app)",
        )

    def test_upload_state_graph_and_existing_bronze_rows_are_not_modified(self) -> None:
        self.assertNotRegex(self.sql, r"(?i)ALTER\s+TABLE\s+(?:uploads|bronze_objects)")
        self.assertNotRegex(self.sql, r"(?i)(?:UPDATE|DELETE)\s+(?:uploads|bronze_objects)")
        self.assertNotIn("ADD VALUE", self.sql)


if __name__ == "__main__":
    unittest.main()
