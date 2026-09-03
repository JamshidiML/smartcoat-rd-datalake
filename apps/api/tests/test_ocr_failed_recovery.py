from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "apps/api/src"
sys.path.insert(0, str(SOURCE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain import (  # noqa: E402
    Actor,
    DEFAULT_OCR_MAX_ATTEMPTS,
    IngestionService,
    OCRDomainService,
    OCRRecoveryService,
    StateConflict,
    configured_ocr_max_attempts,
)
from fakes import MemoryRepository, MemoryRetentionEnforcer, MemoryStorage  # noqa: E402


ACTOR = Actor("usr_ocr_recovery", "Synthetic OCR Recovery Operator")
JPEG = b"\xff\xd8\xff\xe0synthetic-ocr-recovery\xff\xd9"


class OCRFailedRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryRepository()
        self.storage = MemoryStorage()
        result = IngestionService(
            self.repository,
            self.storage,
            1024 * 1024,
            MemoryRetentionEnforcer(),
        ).ingest(
            ACTOR,
            "synthetic-recovery.jpg",
            JPEG,
            "LAB_NOTE",
            "Synthetic OCR recovery fixture only.",
            None,
        )
        self.ingestion_id = result["ingestion_id"]
        self.job_id = result["ocr_job_id"]
        self.ocr = OCRDomainService(self.repository)
        self.recovery = OCRRecoveryService(self.repository, max_attempts=3)

    def fail_attempt(self, reason: str = "synthetic OCR failure") -> None:
        self.ocr.start(self.ingestion_id, "paddleocr", "synthetic", {})
        self.repository.mark_ocr_failed(self.ingestion_id, reason)

    def test_retry_is_operator_initiated_and_reuses_the_existing_job(self) -> None:
        self.fail_attempt()
        self.assertEqual("OCR_FAILED", self.repository.uploads[self.ingestion_id]["state"])
        self.assertEqual("FAILED", self.repository.jobs[self.job_id]["status"])

        result = self.recovery.retry(self.ingestion_id, ACTOR)

        self.assertEqual(self.job_id, result["ocr_job_id"])
        self.assertEqual(1, len(self.repository.jobs))
        self.assertEqual("QUEUED", self.repository.jobs[self.job_id]["status"])
        self.assertEqual("OCR_QUEUED", self.repository.uploads[self.ingestion_id]["state"])
        self.assertEqual(
            1,
            sum(event["event_type"] == "OCR_RETRY_INITIATED" for event in self.repository.audit),
        )

    def test_retry_limit_leaves_item_discoverable_in_ocr_failed(self) -> None:
        for attempt in range(1, 4):
            self.fail_attempt(f"synthetic failure {attempt}")
            if attempt < 3:
                self.recovery.retry(self.ingestion_id, ACTOR)

        with self.assertRaisesRegex(StateConflict, "OCR retry limit reached"):
            self.recovery.retry(self.ingestion_id, ACTOR)
        self.assertEqual(3, self.repository.jobs[self.job_id]["attempt_count"])
        self.assertEqual("FAILED", self.repository.jobs[self.job_id]["status"])
        self.assertEqual("OCR_FAILED", self.repository.uploads[self.ingestion_id]["state"])

    def test_pre_run_failures_count_each_attempt_and_reach_retry_limit(self) -> None:
        for attempt in range(1, 4):
            self.repository.mark_ocr_failed(
                self.ingestion_id,
                f"synthetic pre-start failure {attempt}",
            )
            self.assertEqual(
                attempt,
                self.repository.jobs[self.job_id]["attempt_count"],
            )
            if attempt < 3:
                self.recovery.retry(self.ingestion_id, ACTOR)

        with self.assertRaisesRegex(StateConflict, "OCR retry limit reached"):
            self.recovery.retry(self.ingestion_id, ACTOR)
        self.assertFalse(self.repository.runs)
        self.assertEqual("FAILED", self.repository.jobs[self.job_id]["status"])
        self.assertEqual("OCR_FAILED", self.repository.uploads[self.ingestion_id]["state"])

    def test_database_failure_path_counts_only_jobs_that_have_not_started(self) -> None:
        source = (SOURCE / "database.py").read_text(encoding="utf-8")
        failure_body = source.split("def mark_ocr_failed", 1)[1]
        failure_update = failure_body.split("UPDATE ocr_runs", 1)[0]
        self.assertIn(
            "attempt_count = attempt_count + CASE WHEN status = 'QUEUED' THEN 1 ELSE 0 END",
            failure_update,
        )
        self.assertIn("RETURNING attempt_count", failure_update)

    def test_recovery_does_not_change_bronze_or_storage_protection(self) -> None:
        self.fail_attempt()
        before = deepcopy(
            (
                self.repository.objects,
                self.repository.pairs,
                self.storage.objects,
                self.storage.versions,
            )
        )
        self.recovery.retry(self.ingestion_id, ACTOR)
        self.assertEqual(
            before,
            (
                self.repository.objects,
                self.repository.pairs,
                self.storage.objects,
                self.storage.versions,
            ),
        )

    def test_retry_does_not_fabricate_run_or_silver_draft(self) -> None:
        self.fail_attempt()
        runs_before = deepcopy(self.repository.runs)
        self.recovery.retry(self.ingestion_id, ACTOR)
        self.assertEqual(runs_before, self.repository.runs)
        self.assertFalse(self.repository.drafts)
        self.assertTrue(all(run["status"] == "FAILED" for run in self.repository.runs.values()))

    def test_failure_audit_records_attempt_reason_and_exact_bronze_version(self) -> None:
        self.fail_attempt("synthetic provenance marker")
        event = next(
            event for event in self.repository.audit
            if event["event_type"] == "OCR_JOB_FAILED"
        )
        self.assertEqual(1, event["details"]["attempt_count"])
        self.assertEqual("synthetic provenance marker", event["details"]["error_reason"])
        self.assertTrue(event["details"]["original_object_version_id"])

    def test_database_retry_uses_update_and_never_inserts_another_job(self) -> None:
        source = (SOURCE / "database.py").read_text(encoding="utf-8")
        retry_body = source.split("def retry_failed_ocr", 1)[1].split("def get_upload", 1)[0]
        self.assertIn("UPDATE ocr_jobs", retry_body)
        self.assertNotIn("INSERT INTO ocr_jobs", retry_body)
        self.assertIn("FOR UPDATE OF upload_record, job", retry_body)

    def test_silver_draft_requires_a_real_ocr_run_basis(self) -> None:
        init_sql = (ROOT / "infra/postgres/init.sql").read_text(encoding="utf-8")
        self.assertIn(
            "ocr_run_id uuid NOT NULL REFERENCES ocr_runs(ocr_run_id)",
            init_sql,
        )
        migration = (
            ROOT / "infra/postgres/migrations/0009__add_operator_ocr_retry_transition.sql"
        ).read_text(encoding="utf-8")
        self.assertNotIn("SILVER_DRAFT_READY", migration)

    def test_operator_endpoint_and_existing_ocr_role_boundary_are_explicit(self) -> None:
        main = (SOURCE / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.post("/api/uploads/{ingestion_id}/retry-ocr")', main)
        self.assertIn("actor: Annotated[Actor, Depends(current_actor)]", main)
        rbac = (ROOT / "infra/postgres/rbac_contract.py").read_text(encoding="utf-8")
        for column in (
            '"status"',
            '"started_at_utc"',
            '"completed_at_utc"',
            '"attempt_count"',
            '"error_reason"',
        ):
            self.assertIn(column, rbac)
        self.assertIn('("smartcoat_ocr", "uploads", "state")', rbac)

    def test_retry_limit_configuration_defaults_safely(self) -> None:
        self.assertEqual(DEFAULT_OCR_MAX_ATTEMPTS, configured_ocr_max_attempts({}))
        for malformed in ("not-an-integer", "3.5", "", "0", "-4"):
            with self.subTest(malformed=malformed):
                self.assertEqual(
                    DEFAULT_OCR_MAX_ATTEMPTS,
                    configured_ocr_max_attempts({"OCR_MAX_ATTEMPTS": malformed}),
                )
        self.assertEqual(5, configured_ocr_max_attempts({"OCR_MAX_ATTEMPTS": "5"}))

    def test_retry_limit_is_read_once_during_api_startup(self) -> None:
        main = (SOURCE / "main.py").read_text(encoding="utf-8")
        self.assertEqual(1, main.count("configured_ocr_max_attempts()"))
        self.assertIn(
            "OCRRecoveryService(ocr_repository, OCR_MAX_ATTEMPTS)",
            main,
        )


if __name__ == "__main__":
    unittest.main()
