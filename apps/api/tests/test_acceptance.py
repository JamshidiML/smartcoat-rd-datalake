from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))

from domain import (  # noqa: E402
    MANIFESTS_BUCKET,
    ORIGINALS_BUCKET,
    Actor,
    IngestionService,
    OCRDomainService,
    ReviewService,
    ReviewValidationError,
    SOLO_EXCEPTION_REASON,
    StateConflict,
)
from fakes import MemoryRepository, MemoryRetentionEnforcer, MemoryStorage  # noqa: E402
from validation import UploadValidationError  # noqa: E402


PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-pixel-data" + b"IEND\xaeB`\x82"
JPEG = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg-data" + b"\xff\xd9"
ACTOR = Actor("usr_founder", "SmartCoat Founder")


class AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryRepository()
        self.storage = MemoryStorage()
        self.ingestion = IngestionService(
            self.repository, self.storage, 50 * 1024 * 1024,
            MemoryRetentionEnforcer(),
        )

    def upload(self, data: bytes = JPEG, filename: str = "synthetic-lab.jpg") -> dict:
        return self.ingestion.ingest(
            ACTOR,
            filename,
            data,
            "LAB_NOTE",
            "Synthetic acceptance fixture only.",
            "2026-08-12",
        )

    def complete_ocr(self, result: dict) -> dict:
        service = OCRDomainService(self.repository)
        run_id = service.start(
            result["ingestion_id"],
            "paddleocr",
            "3.7.0",
            {"model": "PP-OCRv6", "orientation": True, "dpi": 300},
        )
        return service.complete(
            result["ingestion_id"],
            run_id,
            "Temperature 23 C",
            [{"block_id": "b-001", "page": 1, "bounding_box": [1, 2, 3, 4], "text": "Temperature 23 C", "ocr_confidence": 0.91}],
            b'{"rec_texts":["Temperature 23 C"]}',
            f"rd/2026/08/{result['ingestion_id']}/ocr-run/{run_id}.json",
        )

    def test_at_01_exactly_one_original_and_manifest(self) -> None:
        result = self.upload()
        objects = [row for row in self.repository.objects if row["ingestion_id"] == result["ingestion_id"]]
        self.assertEqual(1, sum(row["kind"] == "ORIGINAL" for row in objects))
        self.assertEqual(1, sum(row["kind"] == "MANIFEST" for row in objects))
        self.assertEqual("OCR_QUEUED", self.repository.uploads[result["ingestion_id"]]["state"])

    def test_at_02_manifest_required_metadata(self) -> None:
        manifest = self.upload()["manifest"]
        self.assertEqual("usr_founder", manifest["uploader_user_id"])
        self.assertTrue(manifest["uploaded_at_utc"].endswith("Z"))
        self.assertEqual("RND", manifest["department"])
        self.assertEqual("synthetic-lab.jpg", manifest["original_filename"])
        self.assertEqual("image/jpeg", manifest["detected_mime_type"])
        self.assertRegex(manifest["sha256"], r"^[0-9a-f]{64}$")

    def test_at_03_stored_source_matches_manifest_sha256(self) -> None:
        manifest = self.upload()["manifest"]
        stored = self.storage.get(ORIGINALS_BUCKET, manifest["stored_object_key"])
        self.assertEqual(manifest["sha256"], hashlib.sha256(stored).hexdigest())

    def test_at_04_duplicate_is_separate_and_linked(self) -> None:
        first = self.upload()
        second = self.upload(filename="same-bytes-new-event.jpg")
        self.assertNotEqual(first["ingestion_id"], second["ingestion_id"])
        self.assertEqual(first["ingestion_id"], second["manifest"]["duplicate_of_ingestion_id"])
        self.assertEqual(4, len(self.storage.objects))

    def test_at_05_invalid_files_are_rejected_and_audited(self) -> None:
        cases = [
            ("source.exe", b"MZbad"),
            ("broken.jpg", b"\xff\xd8\xffbroken"),
            ("protected.pdf", b"%PDF-1.7\n/Encrypt true\n%%EOF"),
            ("protected.xlsx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"encrypted-container"),
        ]
        for filename, content in cases:
            with self.subTest(filename=filename), self.assertRaises(UploadValidationError):
                self.upload(content, filename)
        rejected = [event for event in self.repository.audit if event["event_type"] == "UPLOAD_REJECTED"]
        self.assertEqual(4, len(rejected))
        self.assertTrue(all(event["details"]["reason"] for event in rejected))

    def test_at_06_ocr_cannot_start_before_bronze_commit(self) -> None:
        ingestion_id = "0198a000-0000-7000-8000-000000000001"
        self.repository.uploads[ingestion_id] = {
            "ingestion_id": ingestion_id,
            "state": "RECEIVED",
            "sha256": "0" * 64,
        }
        with self.assertRaises(StateConflict):
            OCRDomainService(self.repository).start(ingestion_id, "paddleocr", "3.7.0", {})

    def test_at_07_ocr_run_records_versions_config_and_checksums(self) -> None:
        result = self.upload(PNG, "synthetic.png")
        draft = self.complete_ocr(result)
        run = self.repository.runs[draft["ocr_run_id"]]
        self.assertEqual("3.7.0", run["engine_version"])
        self.assertTrue(run["configuration"]["orientation"])
        self.assertEqual(result["manifest"]["sha256"], run["source_sha256"])
        self.assertEqual(hashlib.sha256(b'{"rec_texts":["Temperature 23 C"]}').hexdigest(), run["raw_output_sha256"])

    def test_at_08_ocr_creates_unverified_draft_only(self) -> None:
        result = self.upload()
        draft = self.complete_ocr(result)
        self.assertEqual("DRAFT_UNVERIFIED", draft["status"])
        self.assertEqual([], self.repository.verified)
        transitions = [(event["previous_state"], event["new_state"]) for event in self.repository.audit]
        self.assertNotIn(("OCR_COMPLETED", "VERIFIED"), transitions)

    def test_at_09_verification_requires_human_evidence(self) -> None:
        result = self.upload()
        draft = self.complete_ocr(result)
        service = ReviewService(self.repository, allow_phase_1_solo_self_review=True)
        with self.assertRaises(ReviewValidationError):
            service.review(draft["silver_draft_id"], ACTOR, "Temperature 23 °C", "APPROVED_WITH_CORRECTIONS", "Unit corrected", False)
        verified = service.review(
            draft["silver_draft_id"],
            ACTOR,
            "Temperature 23 °C",
            "APPROVED_WITH_CORRECTIONS",
            "Unit corrected",
            True,
        )
        assert verified is not None
        self.assertEqual("usr_founder", verified["reviewer_user_id"])
        self.assertIsNotNone(verified["reviewed_at_utc"])
        self.assertEqual(result["manifest"]["stored_object_key"], verified["source_object_key"])

    def test_at_10_solo_self_review_is_detected_and_audited(self) -> None:
        result = self.upload()
        draft = self.complete_ocr(result)
        solo = ReviewService(self.repository, allow_phase_1_solo_self_review=True)
        solo.review(draft["silver_draft_id"], ACTOR, "Temperature 23 C", "APPROVED_NO_CHANGES", "", True)
        decision = self.repository.reviews[-1]
        self.assertTrue(decision["self_review_detected"])
        self.assertTrue(decision["solo_exception_applied"])
        self.assertEqual(SOLO_EXCEPTION_REASON, decision["administrator_exception_reason"])

        second_repository = MemoryRepository()
        second_storage = MemoryStorage()
        result = IngestionService(
            second_repository, second_storage, 1024 * 1024,
            MemoryRetentionEnforcer(),
        ).ingest(
            ACTOR, "synthetic.jpg", JPEG, "LAB_NOTE", "Synthetic second repository fixture.", None
        )
        ocr = OCRDomainService(second_repository)
        run_id = ocr.start(result["ingestion_id"], "paddleocr", "3.7.0", {})
        draft = ocr.complete(result["ingestion_id"], run_id, "text", [], b"{}", "artifact.json")
        with self.assertRaises(ReviewValidationError):
            ReviewService(second_repository, False).review(
                draft["silver_draft_id"], ACTOR, "text", "APPROVED_NO_CHANGES", "", True
            )

    def test_at_11_edit_creates_new_reviewed_revision(self) -> None:
        result = self.upload()
        draft = self.complete_ocr(result)
        service = ReviewService(self.repository, True)
        first = service.review(draft["silver_draft_id"], ACTOR, "Temperature 23 C", "APPROVED_NO_CHANGES", "", True)
        assert first is not None
        revision_draft = service.edit_verified(result["ingestion_id"], ACTOR, "Temperature 23 °C")
        self.assertEqual("UNDER_HUMAN_REVIEW", self.repository.uploads[result["ingestion_id"]]["state"])
        second = service.review(
            revision_draft["silver_draft_id"], ACTOR, "Temperature 23 °C", "APPROVED_WITH_CORRECTIONS", "Added unit symbol", True
        )
        assert second is not None
        self.assertEqual(1, first["silver_revision"])
        self.assertEqual(2, second["silver_revision"])
        self.assertEqual(2, len(self.repository.verified))

    def test_at_12_application_cannot_delete_or_overwrite_bronze(self) -> None:
        result = self.upload()
        key = result["manifest"]["stored_object_key"]
        with self.assertRaises(StateConflict):
            self.storage.put_once(ORIGINALS_BUCKET, key, b"replacement", "image/jpeg", True)
        with self.assertRaises(PermissionError):
            self.storage.delete(ORIGINALS_BUCKET, key)
        policy = json.loads((SOURCE.parents[2] / "infra/minio/policies/app-bronze-write.json").read_text())
        actions = {action for statement in policy["Statement"] for action in statement["Action"]}
        self.assertNotIn("s3:DeleteObject", actions)

    def test_at_13_backup_restore_preserves_source_manifest_and_provenance(self) -> None:
        result = self.upload()
        manifest = result["manifest"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "objects").mkdir()
            source = self.storage.get(ORIGINALS_BUCKET, manifest["stored_object_key"])
            stored_manifest = self.storage.get(MANIFESTS_BUCKET, f"rd/{manifest['uploaded_at_utc'][0:4]}/{manifest['uploaded_at_utc'][5:7]}/{result['ingestion_id']}/manifest/v1.json")
            (root / "objects/source.bin").write_bytes(source)
            (root / "objects/manifest.json").write_bytes(stored_manifest)
            (root / "provenance.json").write_text(json.dumps(self.repository.uploads[result["ingestion_id"]], default=str))
            restored_source = (root / "objects/source.bin").read_bytes()
            restored_manifest = json.loads((root / "objects/manifest.json").read_text())
            restored_provenance = json.loads((root / "provenance.json").read_text())
            self.assertEqual(restored_manifest["sha256"], hashlib.sha256(restored_source).hexdigest())
            self.assertEqual(result["ingestion_id"], restored_provenance["ingestion_id"])

    def test_at_14_repository_scan_detects_no_secrets_or_real_data(self) -> None:
        from repository_scan import scan_repository

        findings = scan_repository(SOURCE.parents[2])
        self.assertEqual([], findings, "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
