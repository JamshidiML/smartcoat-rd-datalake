from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra/postgres/migrations"
MIGRATION = MIGRATIONS / "0008__enforce_bronze_pair_commit_and_orphans.sql"
LIVE_ACCEPTANCE = MIGRATIONS.parent / "tests/live_bronze_pair_acceptance.py"


class BronzePairMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_collision_free_monotonic_migration_identity(self) -> None:
        names = sorted(path.name for path in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]__*.sql"))
        self.assertEqual(list(range(1, 9)), [int(name[:4]) for name in names])
        self.assertEqual("0008__enforce_bronze_pair_commit_and_orphans.sql", names[-1])

    def test_pair_orphan_and_reconciliation_evidence_are_append_only(self) -> None:
        for table in (
            "bronze_pairs", "bronze_protected_orphans",
            "bronze_reconciliation_events",
        ):
            self.assertIn(f"CREATE TABLE {table}", self.sql)
            self.assertRegex(
                self.sql,
                rf"CREATE TRIGGER {table}_append_only\s+BEFORE UPDATE OR DELETE ON {table}",
            )

    def test_success_requires_both_exact_versions_and_protection_evidence(self) -> None:
        for marker in (
            "original_bronze_object_id", "manifest_bronze_object_id",
            "pair_identity_sha256", "object_version_id IS NULL",
            "enforcement_verification_result = 'SUCCESS'",
            "BRONZE_COMMITTED requires a verified exact-version pair",
        ):
            self.assertIn(marker, self.sql)
        self.assertIn("uploads_require_bronze_pair_for_success", self.sql)

    def test_ocr_is_once_per_ingestion_and_guarded_by_pair(self) -> None:
        self.assertIn("CREATE UNIQUE INDEX ocr_jobs_one_per_ingestion", self.sql)
        self.assertIn("CREATE TRIGGER ocr_jobs_require_bronze_pair", self.sql)
        self.assertIn("OCR job requires a committed exact-version Bronze pair", self.sql)

    def test_existing_state_vocabulary_is_not_changed(self) -> None:
        self.assertNotRegex(self.sql, r"ALTER\s+TABLE\s+uploads.*state")
        self.assertNotIn("ADD VALUE", self.sql)
        self.assertNotIn("PROTECTED_ORPHAN'", self.sql)

    def test_runtime_authority_is_narrow_and_backup_is_read_only(self) -> None:
        self.assertRegex(
            self.sql,
            r"GRANT SELECT, INSERT ON TABLE\s+bronze_pairs,\s+bronze_protected_orphans,\s+bronze_reconciliation_events\s+TO smartcoat_ingestion",
        )
        self.assertRegex(
            self.sql,
            r"GRANT SELECT ON TABLE\s+bronze_pairs,\s+bronze_protected_orphans,\s+bronze_reconciliation_events\s+TO smartcoat_backup",
        )
        grants = "\n".join(
            line for line in self.sql.splitlines()
            if line.strip().startswith("GRANT") or line.strip().startswith("TO smartcoat")
        )
        self.assertNotIn("TO smartcoat_ocr", grants)
        self.assertNotIn("TO smartcoat_review", grants)
        self.assertNotIn("TO smartcoat_app", grants)

    def test_no_storage_atomicity_or_destructive_compensation(self) -> None:
        lowered = self.sql.lower()
        for forbidden in ("delete from bronze", "legal hold off", "set_object_retention"):
            self.assertNotIn(forbidden, lowered)

    def test_live_acceptance_is_explicitly_opt_in(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(LIVE_ACCEPTANCE)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("BLOCKED_ISOLATION", completed.stdout)
        self.assertIn(
            "--confirm-disposable-synthetic-bronze-pair-run",
            LIVE_ACCEPTANCE.read_text(),
        )


if __name__ == "__main__":
    unittest.main()
