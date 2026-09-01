from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra/postgres/migrations"
MIGRATION = MIGRATIONS / "0008__enforce_bronze_pair_commit_and_orphans.sql"
LIVE_ACCEPTANCE = MIGRATIONS.parent / "tests/live_bronze_pair_acceptance.py"
RUN_EXTERNAL_TESTS = os.environ.get("SMARTCOAT_EXTERNAL_TESTS") == "1"


class BronzePairMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_collision_free_monotonic_migration_identity(self) -> None:
        names = sorted(path.name for path in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]__*.sql"))
        self.assertEqual(list(range(1, 10)), [int(name[:4]) for name in names])
        self.assertIn("0008__enforce_bronze_pair_commit_and_orphans.sql", names)
        self.assertEqual("0009__add_operator_ocr_retry_transition.sql", names[-1])

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
        self.assertRegex(
            self.sql,
            r"GRANT SELECT ON TABLE\s+bronze_objects,\s+bronze_pairs\s+TO smartcoat_ocr",
        )
        self.assertNotRegex(
            self.sql,
            r"(?s)GRANT\s+(?:INSERT|UPDATE|DELETE|ALL).*?TO smartcoat_ocr",
        )
        self.assertNotIn("TO smartcoat_review", self.sql)
        self.assertNotIn("TO smartcoat_app", self.sql)

    def test_no_storage_atomicity_or_destructive_compensation(self) -> None:
        lowered = self.sql.lower()
        for forbidden in ("delete from bronze", "legal hold off", "set_object_retention"):
            self.assertNotIn(forbidden, lowered)

    @unittest.skipUnless(
        RUN_EXTERNAL_TESTS,
        "requires an external Python process; enabled by manual live acceptance",
    )
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

    def test_production_image_gate_has_no_candidate_source_bind_mount(self) -> None:
        source = LIVE_ACCEPTANCE.read_text(encoding="utf-8")
        self.assertIn("--api-image-id", source)
        self.assertIn("--ocr-image-id", source)
        self.assertNotIn("dst=/candidate", source)
        self.assertIn("PASS_BRONZE_PAIR_PRODUCTION_IMAGE", source)
        self.assertIn("PASS_BRONZE_PAIR_LOST_EVIDENCE_RECOVERY", source)
        self.assertIn("PASS_OCR_EXACT_BRONZE_VERSION_SOURCE", source)


if __name__ == "__main__":
    unittest.main()
