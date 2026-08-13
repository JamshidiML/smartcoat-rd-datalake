from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from identifiers import uuid7
from validation import UploadValidationError, validate_upload


ORIGINALS_BUCKET = "sc-rd-bronze-originals"
MANIFESTS_BUCKET = "sc-rd-bronze-manifests"
ARTIFACTS_BUCKET = "sc-rd-ocr-artifacts"
SOLO_EXCEPTION_REASON = "PHASE_1_SOLO_FOUNDER_REVIEW"
APPROVAL_DECISIONS = {"APPROVED_NO_CHANGES", "APPROVED_WITH_CORRECTIONS"}
REJECTION_DECISIONS = {
    "REJECTED_UNREADABLE",
    "REJECTED_WRONG_DOCUMENT_TYPE",
    "REQUIRES_REUPLOAD",
}


class StateConflict(RuntimeError):
    pass


class ReviewValidationError(ValueError):
    pass


class Repository(Protocol):
    def record_rejection(self, ingestion_id: str, actor_user_id: str, reason: str, code: str) -> None: ...

    def first_ingestion_by_sha256(self, digest: str) -> str | None: ...

    def create_received(self, upload: dict[str, Any]) -> None: ...

    def commit_bronze(self, upload: dict[str, Any], objects: list[dict[str, Any]]) -> None: ...

    def transition(
        self,
        ingestion_id: str,
        previous_state: str,
        new_state: str,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> None: ...

    def queue_ocr(self, ingestion_id: str) -> str: ...

    def get_upload(self, ingestion_id: str) -> dict[str, Any]: ...

    def start_ocr_run(self, ingestion_id: str, run: dict[str, Any]) -> None: ...

    def complete_ocr_run(self, ingestion_id: str, run: dict[str, Any], draft: dict[str, Any]) -> None: ...

    def get_draft(self, draft_id: str) -> dict[str, Any]: ...

    def create_review_decision(self, decision: dict[str, Any], verified: dict[str, Any] | None) -> None: ...

    def max_silver_revision(self, ingestion_id: str) -> int: ...

    def create_revision_draft(self, ingestion_id: str, actor_user_id: str, text: str) -> dict[str, Any]: ...


class ObjectStorage(Protocol):
    def put_once(self, bucket: str, key: str, data: bytes, content_type: str, locked: bool) -> dict[str, Any]: ...

    def get(self, bucket: str, key: str) -> bytes: ...


@dataclass(frozen=True)
class Actor:
    user_id: str
    display_name: str


def utc_now() -> datetime:
    return datetime.now(UTC)


class IngestionService:
    def __init__(self, repository: Repository, storage: ObjectStorage, max_upload_bytes: int) -> None:
        self.repository = repository
        self.storage = storage
        self.max_upload_bytes = max_upload_bytes

    def ingest(
        self,
        actor: Actor,
        filename: str,
        data: bytes,
        document_category: str,
        context_note: str,
        capture_date: str | None,
    ) -> dict[str, Any]:
        ingestion_id = uuid7()
        try:
            validated = validate_upload(
                filename,
                data,
                document_category,
                context_note,
                capture_date,
                self.max_upload_bytes,
            )
        except UploadValidationError as exc:
            self.repository.record_rejection(ingestion_id, actor.user_id, exc.reason, exc.code)
            raise

        now = utc_now()
        digest = hashlib.sha256(data).hexdigest()
        duplicate_of = self.repository.first_ingestion_by_sha256(digest)
        prefix = f"rd/{now:%Y/%m}/{ingestion_id}"
        original_key = f"{prefix}/original/{validated.sanitized_filename}"
        manifest_key = f"{prefix}/manifest/v1.json"
        upload = {
            "ingestion_id": ingestion_id,
            "department": "RND",
            "uploader_user_id": actor.user_id,
            "uploader_display_name": actor.display_name,
            "uploaded_at_utc": now,
            "original_filename": filename,
            "stored_object_key": original_key,
            "manifest_object_key": manifest_key,
            "detected_mime_type": validated.mime_type,
            "declared_file_type": validated.file_type,
            "document_category": document_category,
            "context_note": context_note.strip(),
            "capture_date": capture_date,
            "byte_size": len(data),
            "sha256": digest,
            "duplicate_of_ingestion_id": duplicate_of,
            "source_channel": "WEB_UPLOAD",
            "state": "RECEIVED",
        }
        self.repository.create_received(upload)

        original_result = self.storage.put_once(
            ORIGINALS_BUCKET, original_key, data, validated.mime_type, locked=True
        )
        stored_bytes = self.storage.get(ORIGINALS_BUCKET, original_key)
        if hashlib.sha256(stored_bytes).hexdigest() != digest:
            raise StateConflict("Stored Bronze bytes failed SHA-256 verification")

        manifest = {
            "manifest_version": "1.0",
            "ingestion_id": ingestion_id,
            "bronze_status": "COMMITTED",
            "department": "RND",
            "uploader_user_id": actor.user_id,
            "uploader_display_name": actor.display_name,
            "uploaded_at_utc": now.isoformat().replace("+00:00", "Z"),
            "original_filename": filename,
            "stored_object_key": original_key,
            "detected_mime_type": validated.mime_type,
            "declared_file_type": validated.file_type,
            "byte_size": len(data),
            "sha256": digest,
            "duplicate_of_ingestion_id": duplicate_of,
            "source_channel": "WEB_UPLOAD",
            "metadata_schema_version": "1.0",
        }
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        manifest_result = self.storage.put_once(
            MANIFESTS_BUCKET, manifest_key, manifest_bytes, "application/json", locked=True
        )
        if self.storage.get(MANIFESTS_BUCKET, manifest_key) != manifest_bytes:
            raise StateConflict("Stored Bronze manifest failed byte verification")

        self.repository.commit_bronze(
            upload,
            [
                {
                    "bucket": ORIGINALS_BUCKET,
                    "key": original_key,
                    "sha256": digest,
                    "version_id": original_result.get("version_id"),
                    "kind": "ORIGINAL",
                },
                {
                    "bucket": MANIFESTS_BUCKET,
                    "key": manifest_key,
                    "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "version_id": manifest_result.get("version_id"),
                    "kind": "MANIFEST",
                },
            ],
        )
        self.repository.transition(ingestion_id, "RECEIVED", "BRONZE_COMMITTED", "system")
        job_id = self.repository.queue_ocr(ingestion_id)
        self.repository.transition(ingestion_id, "BRONZE_COMMITTED", "OCR_QUEUED", "system")
        return {"ingestion_id": ingestion_id, "ocr_job_id": job_id, "manifest": manifest}


class OCRDomainService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def start(self, ingestion_id: str, engine: str, engine_version: str, configuration: dict[str, Any]) -> str:
        upload = self.repository.get_upload(ingestion_id)
        if upload["state"] not in {"BRONZE_COMMITTED", "OCR_QUEUED"}:
            raise StateConflict("OCR cannot start before Bronze is committed")
        run_id = uuid7()
        self.repository.start_ocr_run(
            ingestion_id,
            {
                "ocr_run_id": run_id,
                "engine": engine,
                "engine_version": engine_version,
                "configuration": configuration,
                "source_sha256": upload["sha256"],
                "started_at_utc": utc_now(),
            },
        )
        return run_id

    def complete(
        self,
        ingestion_id: str,
        run_id: str,
        extracted_text: str,
        text_blocks: list[dict[str, Any]],
        raw_output: bytes,
        artifact_key: str,
    ) -> dict[str, Any]:
        upload = self.repository.get_upload(ingestion_id)
        run = {
            "ocr_run_id": run_id,
            "raw_output_sha256": hashlib.sha256(raw_output).hexdigest(),
            "raw_artifact_key": artifact_key,
            "completed_at_utc": utc_now(),
        }
        draft = {
            "silver_draft_id": uuid7(),
            "ingestion_id": ingestion_id,
            "source_sha256": upload["sha256"],
            "ocr_run_id": run_id,
            "status": "DRAFT_UNVERIFIED",
            "extracted_text": extracted_text,
            "text_blocks": text_blocks,
            "source_file_type": upload["declared_file_type"],
            "document_category": upload["document_category"],
            "created_at_utc": utc_now(),
        }
        self.repository.complete_ocr_run(ingestion_id, run, draft)
        self.repository.transition(ingestion_id, "OCR_QUEUED", "OCR_COMPLETED", "ocr-worker")
        self.repository.transition(ingestion_id, "OCR_COMPLETED", "SILVER_DRAFT_READY", "ocr-worker")
        return draft


class ReviewService:
    def __init__(self, repository: Repository, allow_phase_1_solo_self_review: bool) -> None:
        self.repository = repository
        self.allow_phase_1_solo_self_review = allow_phase_1_solo_self_review

    def review(
        self,
        draft_id: str,
        reviewer: Actor,
        verified_text: str,
        decision: str,
        correction_summary: str,
        explicit_confirmation: bool,
        administrator_exception_reason: str | None = None,
    ) -> dict[str, Any] | None:
        draft = self.repository.get_draft(draft_id)
        upload = self.repository.get_upload(draft["ingestion_id"])
        if draft["status"] != "DRAFT_UNVERIFIED":
            raise StateConflict("Only an unverified draft can be reviewed")
        if decision not in APPROVAL_DECISIONS | REJECTION_DECISIONS:
            raise ReviewValidationError("Unsupported review decision")
        if not explicit_confirmation:
            raise ReviewValidationError("Explicit source-comparison confirmation is required")
        if decision in APPROVAL_DECISIONS and not verified_text.strip():
            raise ReviewValidationError("Verified text is required for approval")

        self_review_detected = reviewer.user_id == upload["uploader_user_id"]
        solo_exception_applied = False
        exception_reason = administrator_exception_reason
        if self_review_detected:
            if self.allow_phase_1_solo_self_review:
                solo_exception_applied = True
                exception_reason = SOLO_EXCEPTION_REASON
            elif not administrator_exception_reason:
                raise ReviewValidationError("Self-review requires an administrator exception reason")

        previous = upload["state"]
        if previous == "SILVER_DRAFT_READY":
            self.repository.transition(upload["ingestion_id"], previous, "UNDER_HUMAN_REVIEW", reviewer.user_id)
        review_id = uuid7()
        reviewed_at = utc_now()
        review = {
            "review_decision_id": review_id,
            "silver_draft_id": draft_id,
            "ingestion_id": upload["ingestion_id"],
            "reviewer_user_id": reviewer.user_id,
            "reviewed_at_utc": reviewed_at,
            "decision": decision,
            "explicit_confirmation": explicit_confirmation,
            "correction_summary": correction_summary,
            "self_review_detected": self_review_detected,
            "solo_exception_applied": solo_exception_applied,
            "administrator_exception_reason": exception_reason,
        }
        verified = None
        if decision in APPROVAL_DECISIONS:
            verified = {
                "silver_record_id": uuid7(),
                "silver_revision": self.repository.max_silver_revision(upload["ingestion_id"]) + 1,
                "ingestion_id": upload["ingestion_id"],
                "source_sha256": upload["sha256"],
                "status": "VERIFIED",
                "verified_text": verified_text,
                "reviewer_user_id": reviewer.user_id,
                "reviewed_at_utc": reviewed_at,
                "review_decision": decision,
                "correction_summary": correction_summary,
                "source_object_key": upload["stored_object_key"],
                "ocr_artifact_key": draft["raw_artifact_key"],
            }
            final_state = "VERIFIED"
        else:
            final_state = "REVIEW_REJECTED"
        self.repository.create_review_decision(review, verified)
        self.repository.transition(
            upload["ingestion_id"],
            "UNDER_HUMAN_REVIEW",
            final_state,
            reviewer.user_id,
            {
                "self_review_detected": self_review_detected,
                "phase_1_solo_exception_applied": solo_exception_applied,
                "exception_reason": exception_reason,
            },
        )
        return verified

    def edit_verified(self, ingestion_id: str, actor: Actor, text: str) -> dict[str, Any]:
        upload = self.repository.get_upload(ingestion_id)
        if upload["state"] != "VERIFIED":
            raise StateConflict("Only a verified record can be revised")
        draft = self.repository.create_revision_draft(ingestion_id, actor.user_id, text)
        self.repository.transition(
            ingestion_id,
            "VERIFIED",
            "UNDER_HUMAN_REVIEW",
            actor.user_id,
            {"pending_silver_revision": self.repository.max_silver_revision(ingestion_id) + 1},
        )
        return draft
