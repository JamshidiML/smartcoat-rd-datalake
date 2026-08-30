from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any


SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))

from domain import Actor, IngestionService, StateConflict  # noqa: E402
from fakes import (  # noqa: E402
    MemoryRepository,
    MemoryRetentionEnforcer,
    MemoryStorage,
)


JPEG = b"\xff\xd8\xff\xe0synthetic-bronze-pair\xff\xd9"
ACTOR = Actor("usr_pair", "Synthetic Pair User")


class FaultStorage(MemoryStorage):
    def __init__(self, fault: str | None = None) -> None:
        super().__init__()
        self.fault = fault
        self.put_count = 0

    def put_once(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.put_count += 1
        if self.fault == "original_upload" and self.put_count == 1:
            raise RuntimeError("synthetic original upload failure")
        if self.fault == "manifest_upload" and self.put_count == 2:
            raise RuntimeError("synthetic manifest upload failure")
        result = super().put_once(*args, **kwargs)
        if self.fault == "original_version_missing" and self.put_count == 1:
            result["version_id"] = None
        if self.fault == "manifest_version_missing" and self.put_count == 2:
            result["version_id"] = None
        return result

    def get_exact(self, bucket: str, key: str, version_id: str) -> bytes:
        value = super().get_exact(bucket, key, version_id)
        if self.fault == "content_mismatch" and self.put_count == 2:
            return value + b"corrupt"
        return value


class PolicyMismatchEnforcer(MemoryRetentionEnforcer):
    def enforce(self, **values: Any):
        evidence = super().enforce(**values)
        if values["target"].object_kind == "MANIFEST":
            return type(evidence)(
                **{
                    **evidence.as_record(),
                    "retention_class": "long_term_10y",
                    "requested_legal_hold_status": "UNCHANGED",
                    "observed_legal_hold_status": "OFF",
                }
            )
        return evidence


class PreTransactionFailureRepository(MemoryRepository):
    def commit_bronze_pair(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic failure before PostgreSQL transaction")


class QueueCrashRepository(MemoryRepository):
    def __init__(self) -> None:
        super().__init__()
        self.queue_crashes = 1

    def ensure_ocr_queued(
        self, ingestion_id: str, correlation_id: str | None = None
    ) -> str:
        if self.queue_crashes:
            self.queue_crashes -= 1
            raise RuntimeError("synthetic crash before OCR queue")
        return super().ensure_ocr_queued(ingestion_id, correlation_id)


class ConflictingContextRepository(MemoryRepository):
    def bronze_reconciliation_context(self, ingestion_id: str) -> dict[str, Any]:
        result = super().bronze_reconciliation_context(ingestion_id)
        result["retry_identity_sha256"] = "f" * 64
        return result


class LostInitialOrphanEvidenceRepository(PreTransactionFailureRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_orphan_write = True

    def record_protected_orphans(self, *args: Any, **kwargs: Any) -> None:
        if self.fail_orphan_write:
            self.fail_orphan_write = False
            raise RuntimeError("synthetic orphan evidence database outage")
        super().record_protected_orphans(*args, **kwargs)


class BronzePairFailureTests(unittest.TestCase):
    def service(
        self,
        repository: MemoryRepository | None = None,
        storage: MemoryStorage | None = None,
        enforcer: MemoryRetentionEnforcer | None = None,
    ) -> tuple[IngestionService, MemoryRepository, MemoryStorage]:
        repository = repository or MemoryRepository()
        storage = storage or FaultStorage()
        service = IngestionService(
            repository, storage, 1024 * 1024,
            enforcer or MemoryRetentionEnforcer(),
        )
        return service, repository, storage

    def ingest(self, service: IngestionService) -> dict[str, Any]:
        return service.ingest(
            ACTOR, "synthetic.jpg", JPEG, "LAB_NOTE",
            "Synthetic Bronze Pair fixture only.", None,
        )

    def assert_no_success(self, repository: MemoryRepository) -> None:
        self.assertFalse(repository.pairs)
        self.assertFalse(repository.jobs)
        self.assertNotIn(
            "BRONZE_COMMITTED",
            {upload["state"] for upload in repository.uploads.values()},
        )

    def test_upload_and_version_failures_never_commit_or_queue(self) -> None:
        for fault in (
            "original_upload", "original_version_missing",
            "manifest_upload", "manifest_version_missing",
        ):
            with self.subTest(fault=fault):
                service, repository, _ = self.service(storage=FaultStorage(fault))
                with self.assertRaises(Exception):
                    self.ingest(service)
                self.assert_no_success(repository)
                if fault in {"manifest_upload", "manifest_version_missing"}:
                    self.assertEqual(1, len(repository.orphans))
                    self.assertEqual("ORIGINAL", repository.orphans[0]["kind"])

    def test_unapproved_retention_category_fails_before_storage(self) -> None:
        service, repository, storage = self.service()
        with self.assertRaisesRegex(ValueError, "approved retention assignment"):
            service.ingest(
                ACTOR,
                "synthetic.jpg",
                JPEG,
                "OTHER",
                "Synthetic classification-pending fixture.",
                None,
            )
        self.assert_no_success(repository)
        self.assertFalse(storage.puts)
        self.assertFalse(repository.uploads)
        self.assertEqual("UPLOAD_REJECTED", repository.audit[-1]["event_type"])

    def test_protection_and_content_failures_preserve_protected_members(self) -> None:
        for fault in ("original_protection", "manifest_protection", "content_mismatch"):
            with self.subTest(fault=fault):
                enforcer = MemoryRetentionEnforcer()
                storage = FaultStorage("content_mismatch" if fault == "content_mismatch" else None)
                if fault == "original_protection":
                    enforcer.fail_kind = "ORIGINAL"
                if fault == "manifest_protection":
                    enforcer.fail_kind = "MANIFEST"
                service, repository, _ = self.service(storage=storage, enforcer=enforcer)
                with self.assertRaises(Exception):
                    self.ingest(service)
                self.assert_no_success(repository)
                expected = 0 if fault == "original_protection" else 1
                self.assertEqual(expected, len(repository.orphans))

    def test_pair_policy_mismatch_is_a_two_member_protected_orphan(self) -> None:
        service, repository, _ = self.service(enforcer=PolicyMismatchEnforcer())
        with self.assertRaisesRegex(StateConflict, "policy mismatch"):
            self.ingest(service)
        self.assert_no_success(repository)
        self.assertEqual({"ORIGINAL", "MANIFEST"}, {row["kind"] for row in repository.orphans})

    def test_pretransaction_and_transaction_failures_record_discoverable_orphans(self) -> None:
        repositories = [PreTransactionFailureRepository()]
        for checkpoint in ("after_objects", "after_retention_evidence", "after_pair", "before_commit"):
            repository = MemoryRepository()
            repository.bronze_fault_checkpoint = checkpoint
            repositories.append(repository)
        for repository in repositories:
            with self.subTest(repository=type(repository).__name__, fault=repository.bronze_fault_checkpoint):
                service, _, _ = self.service(repository=repository)
                with self.assertRaises(RuntimeError):
                    self.ingest(service)
                self.assert_no_success(repository)
                self.assertEqual(2, len(repository.orphans))
                self.assertTrue(all(row["retention"]["observed_retention_mode"] == "COMPLIANCE" for row in repository.orphans))

    def test_crash_after_commit_before_queue_is_idempotently_recovered(self) -> None:
        repository = QueueCrashRepository()
        service, _, _ = self.service(repository=repository)
        with self.assertRaisesRegex(RuntimeError, "before OCR queue"):
            self.ingest(service)
        ingestion_id = next(iter(repository.uploads))
        self.assertEqual("BRONZE_COMMITTED", repository.uploads[ingestion_id]["state"])
        self.assertEqual(1, len(repository.pairs))
        self.assertFalse(repository.jobs)
        first = service.reconcile(ingestion_id)
        second = service.reconcile(ingestion_id)
        self.assertEqual(first["ocr_job_id"], second["ocr_job_id"])
        self.assertEqual(1, len(repository.jobs))

    def test_reconciliation_completes_once_without_duplicate_facts(self) -> None:
        repository = MemoryRepository()
        repository.bronze_fault_checkpoint = "before_commit"
        service, _, _ = self.service(repository=repository)
        with self.assertRaises(RuntimeError):
            self.ingest(service)
        ingestion_id = next(iter(repository.uploads))
        repository.bronze_fault_checkpoint = None
        first = service.reconcile(ingestion_id)
        second = service.reconcile(ingestion_id)
        self.assertEqual("RECONCILED", first["status"])
        self.assertEqual("ALREADY_COMMITTED", second["status"])
        self.assertEqual(1, len(repository.pairs))
        self.assertEqual(1, len(repository.jobs))
        self.assertEqual(1, len(repository.reconciliations))

    def test_stale_conflicting_reconciliation_never_commits_or_queues(self) -> None:
        repository = ConflictingContextRepository()
        repository.bronze_fault_checkpoint = "before_commit"
        service, _, _ = self.service(repository=repository)
        with self.assertRaises(RuntimeError):
            self.ingest(service)
        repository.bronze_fault_checkpoint = None
        ingestion_id = next(iter(repository.uploads))
        with self.assertRaisesRegex(StateConflict, "Stale or conflicting"):
            service.reconcile(ingestion_id)
        self.assert_no_success(repository)
        self.assertEqual(1, len(repository.reconciliations))

    def test_reconciliation_rediscovers_exact_versions_after_evidence_outage(self) -> None:
        repository = LostInitialOrphanEvidenceRepository()
        service, _, _ = self.service(repository=repository)
        with self.assertRaisesRegex(RuntimeError, "orphan evidence database outage"):
            self.ingest(service)
        self.assertFalse(repository.orphans)
        repository.commit_bronze_pair = MemoryRepository.commit_bronze_pair.__get__(
            repository,
            LostInitialOrphanEvidenceRepository,
        )
        ingestion_id = next(iter(repository.uploads))
        result = service.reconcile(ingestion_id)
        self.assertEqual("RECONCILED", result["status"])
        self.assertEqual({"ORIGINAL", "MANIFEST"}, {row["kind"] for row in repository.orphans})
        self.assertEqual(1, len(repository.pairs))
        self.assertEqual(1, len(repository.jobs))


class ExactVersionDiscoveryAdapterTests(unittest.TestCase):
    def test_discovery_enumerates_exact_key_versions_and_ignores_delete_markers(self) -> None:
        source = (SOURCE / "storage.py").read_text()
        method = source[source.index("def list_exact_versions"):]
        self.assertIn("self.client.list_objects(", method)
        self.assertIn("prefix=key", method)
        self.assertIn("recursive=True", method)
        self.assertIn("include_version=True", method)
        self.assertIn("item.object_name == key", method)
        self.assertIn("not item.is_delete_marker", method)
        self.assertNotIn("stat_object", method)


if __name__ == "__main__":
    unittest.main()
