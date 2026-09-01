from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from identifiers import uuid7
from operational_logging import current_correlation_id, log_event
from retention_enforcement import ExactVersionTarget, RetentionEnforcementEvidence
from retention_policy import (
    RETENTION_POLICY_VERSION,
    RetentionPolicyError,
    resolve_category_rule,
)
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

    def commit_bronze_pair(
        self,
        upload: dict[str, Any],
        members: list[dict[str, Any]],
        pair: dict[str, Any],
    ) -> None: ...

    def record_protected_orphans(
        self,
        upload: dict[str, Any],
        members: list[dict[str, Any]],
        failure_stage: str,
        failure_code: str,
    ) -> None: ...

    def bronze_reconciliation_context(self, ingestion_id: str) -> dict[str, Any]: ...

    def record_reconciliation(
        self,
        ingestion_id: str,
        retry_identity_sha256: str,
        outcome: str,
        details: dict[str, Any],
    ) -> None: ...

    def transition(
        self,
        ingestion_id: str,
        previous_state: str,
        new_state: str,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> None: ...

    def ensure_ocr_queued(
        self, ingestion_id: str, correlation_id: str | None = None
    ) -> str: ...

    def retry_failed_ocr(
        self, ingestion_id: str, actor_id: str, max_attempts: int
    ) -> dict[str, Any]: ...

    def get_upload(self, ingestion_id: str) -> dict[str, Any]: ...

    def start_ocr_run(self, ingestion_id: str, run: dict[str, Any]) -> None: ...

    def complete_ocr_run(self, ingestion_id: str, run: dict[str, Any], draft: dict[str, Any]) -> None: ...

    def get_draft(self, draft_id: str) -> dict[str, Any]: ...

    def complete_review(
        self,
        decision: dict[str, Any],
        verified: dict[str, Any] | None,
    ) -> dict[str, Any] | None: ...

    def max_silver_revision(self, ingestion_id: str) -> int: ...

    def create_revision_draft(self, ingestion_id: str, actor_user_id: str, text: str) -> dict[str, Any]: ...


class ObjectStorage(Protocol):
    def put_once(self, bucket: str, key: str, data: bytes, content_type: str, locked: bool) -> dict[str, Any]: ...

    def get_exact(self, bucket: str, key: str, version_id: str) -> bytes: ...

    def list_exact_versions(self, bucket: str, key: str) -> list[str]: ...


class RetentionEnforcer(Protocol):
    def enforce(
        self,
        *,
        target: ExactVersionTarget,
        retention_assignment_id: str,
        data_category: str,
        retention_class: str,
        retention_policy_version: str,
        enforced_by: str,
    ) -> RetentionEnforcementEvidence: ...


@dataclass(frozen=True)
class Actor:
    user_id: str
    display_name: str


def utc_now() -> datetime:
    return datetime.now(UTC)


class IngestionService:
    def __init__(
        self,
        repository: Repository,
        storage: ObjectStorage,
        max_upload_bytes: int,
        retention_enforcer: RetentionEnforcer,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.max_upload_bytes = max_upload_bytes
        self.retention_enforcer = retention_enforcer

    @staticmethod
    def _member(
        *,
        bucket: str,
        key: str,
        kind: str,
        digest: str,
        version_id: Any,
        evidence: RetentionEnforcementEvidence,
    ) -> dict[str, Any]:
        if not isinstance(version_id, str) or not version_id.strip():
            raise StateConflict(f"{kind} upload did not return an exact version ID")
        if evidence.object_version_id != version_id:
            raise StateConflict(f"{kind} protection targeted a different object version")
        return {
            "bucket": bucket,
            "key": key,
            "kind": kind,
            "sha256": digest,
            "version_id": version_id,
            "retention_assignment_id": evidence.retention_assignment_id,
            "retention": evidence.as_record(),
        }

    def _protect_member(
        self,
        *,
        bucket: str,
        key: str,
        kind: str,
        digest: str,
        version_id: Any,
        document_category: str,
    ) -> dict[str, Any]:
        if not isinstance(version_id, str) or not version_id.strip():
            raise StateConflict(f"{kind} upload did not return an exact version ID")
        rule = resolve_category_rule(document_category)
        assignment_id = uuid7()
        try:
            evidence = self.retention_enforcer.enforce(
                target=ExactVersionTarget(bucket, key, version_id, kind),
                retention_assignment_id=assignment_id,
                data_category=rule.data_category,
                retention_class=rule.retention_class,
                retention_policy_version=RETENTION_POLICY_VERSION,
                enforced_by="ingestion-service",
            )
        except Exception as exc:
            log_event(
                "ERROR",
                "retention.enforcement.failed",
                bucket=bucket,
                object_key=key,
                object_version_id=version_id,
                retention_class=rule.retention_class,
                error_type=type(exc).__name__,
            )
            raise
        log_event(
            "INFO",
            "retention.enforcement.completed",
            bucket=bucket,
            object_key=key,
            object_version_id=version_id,
            retention_class=evidence.retention_class,
        )
        stored = self.storage.get_exact(bucket, key, version_id)
        if hashlib.sha256(stored).hexdigest() != digest:
            raise StateConflict(f"Stored Bronze {kind.lower()} failed exact-version SHA-256 verification")
        return self._member(
            bucket=bucket,
            key=key,
            kind=kind,
            digest=digest,
            version_id=version_id,
            evidence=evidence,
        )

    @staticmethod
    def _pair_identity(members: list[dict[str, Any]]) -> str:
        canonical = [
            {
                "bucket": item["bucket"],
                "key": item["key"],
                "version_id": item["version_id"],
                "retention_policy_version": item["retention"]["retention_policy_version"],
            }
            for item in sorted(members, key=lambda value: value["kind"])
        ]
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _record_orphans(
        self,
        upload: dict[str, Any],
        protected: list[dict[str, Any]],
        stage: str,
        exc: Exception,
    ) -> None:
        if not protected:
            return
        code = getattr(exc, "code", type(exc).__name__)
        self.repository.record_protected_orphans(
            upload, protected, stage, str(code)[:200]
        )

    def _discover_protected_members(
        self,
        upload: dict[str, Any],
        existing: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Recover exact candidates by deterministic key, never latest-key state."""

        discovered = list(existing)
        known_kinds = {item["kind"] for item in discovered}
        targets = (
            ("ORIGINAL", ORIGINALS_BUCKET, upload["stored_object_key"]),
            ("MANIFEST", MANIFESTS_BUCKET, upload["manifest_object_key"]),
        )
        for kind, bucket, key in targets:
            if kind in known_kinds:
                continue
            versions = self.storage.list_exact_versions(bucket, key)
            if not versions:
                continue
            if len(versions) != 1:
                raise StateConflict(
                    f"Protected Bronze {kind.lower()} discovery is ambiguous"
                )
            version_id = versions[0]
            content = self.storage.get_exact(bucket, key, version_id)
            digest = hashlib.sha256(content).hexdigest()
            if kind == "ORIGINAL" and digest != upload["sha256"]:
                raise StateConflict("Discovered original does not match upload SHA-256")
            if kind == "MANIFEST":
                try:
                    manifest = json.loads(content)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    log_event(
                        "WARNING",
                        "bronze.manifest.discovery_rejected",
                        ingestion_id=str(upload["ingestion_id"]),
                        bucket=bucket,
                        object_key=key,
                        object_version_id=version_id,
                        reason="NON_CANONICAL_JSON",
                        error_type=type(exc).__name__,
                    )
                    raise StateConflict("Discovered manifest is not canonical JSON") from exc
                original_version_id = next(
                    (
                        member["version_id"]
                        for member in discovered
                        if member["kind"] == "ORIGINAL"
                    ),
                    None,
                )
                if (
                    manifest.get("ingestion_id") != str(upload["ingestion_id"])
                    or manifest.get("stored_object_key") != upload["stored_object_key"]
                    or manifest.get("sha256") != upload["sha256"]
                    or manifest.get("original_object_version_id") != original_version_id
                ):
                    raise StateConflict("Discovered manifest does not match the ingestion")
            discovered.append(
                self._protect_member(
                    bucket=bucket,
                    key=key,
                    kind=kind,
                    digest=digest,
                    version_id=version_id,
                    document_category=upload["document_category"],
                )
            )
        return discovered

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
            log_event(
                "WARNING",
                "ingestion.validation.rejected",
                ingestion_id=ingestion_id,
                actor_id=actor.user_id,
                reason=exc.reason,
                validation_code=exc.code,
                error_type=type(exc).__name__,
            )
            self.repository.record_rejection(ingestion_id, actor.user_id, exc.reason, exc.code)
            log_event(
                "INFO",
                "state.transition",
                ingestion_id=ingestion_id,
                actor_id=actor.user_id,
                state_from="RECEIVED",
                state_to="REJECTED",
            )
            raise

        try:
            resolve_category_rule(document_category)
        except RetentionPolicyError as exc:
            reason = "Document category has no approved retention assignment"
            log_event(
                "WARNING",
                "ingestion.retention.rejected",
                ingestion_id=ingestion_id,
                actor_id=actor.user_id,
                document_category=document_category,
                reason="RETENTION_CLASSIFICATION_PENDING",
                error_type=type(exc).__name__,
            )
            self.repository.record_rejection(
                ingestion_id,
                actor.user_id,
                reason,
                "RETENTION_CLASSIFICATION_PENDING",
            )
            log_event(
                "INFO",
                "state.transition",
                ingestion_id=ingestion_id,
                actor_id=actor.user_id,
                state_from="RECEIVED",
                state_to="REJECTED",
            )
            raise UploadValidationError(
                reason,
                "RETENTION_CLASSIFICATION_PENDING",
            ) from exc

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
        log_event(
            "INFO",
            "state.transition",
            ingestion_id=ingestion_id,
            actor_id=actor.user_id,
            state_from=None,
            state_to="RECEIVED",
        )

        protected: list[dict[str, Any]] = []
        stage = "original_upload"
        try:
            original_result = self.storage.put_once(
                ORIGINALS_BUCKET, original_key, data, validated.mime_type, locked=False
            )
            log_event(
                "INFO",
                "bronze.original.committed",
                ingestion_id=ingestion_id,
                bucket=ORIGINALS_BUCKET,
                object_key=original_key,
                object_version_id=original_result.get("version_id"),
                byte_count=len(data),
                sha256=digest,
            )
            stage = "original_protection"
            original_member = self._protect_member(
                bucket=ORIGINALS_BUCKET,
                key=original_key,
                kind="ORIGINAL",
                digest=digest,
                version_id=original_result.get("version_id"),
                document_category=document_category,
            )
            protected.append(original_member)

            manifest = {
                "manifest_version": "1.0",
                "ingestion_id": ingestion_id,
                "bronze_status": "PAIR_CANDIDATE",
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
                "original_object_version_id": original_member["version_id"],
                "retention_class": original_member["retention"]["retention_class"],
                "retention_policy_version": original_member["retention"]["retention_policy_version"],
            }
            manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
            stage = "manifest_upload"
            manifest_result = self.storage.put_once(
                MANIFESTS_BUCKET, manifest_key, manifest_bytes, "application/json", locked=False
            )
            log_event(
                "INFO",
                "bronze.manifest.committed",
                ingestion_id=ingestion_id,
                bucket=MANIFESTS_BUCKET,
                object_key=manifest_key,
                object_version_id=manifest_result.get("version_id"),
                byte_count=len(manifest_bytes),
                sha256=manifest_digest,
            )
            stage = "manifest_protection"
            manifest_member = self._protect_member(
                bucket=MANIFESTS_BUCKET,
                key=manifest_key,
                kind="MANIFEST",
                digest=manifest_digest,
                version_id=manifest_result.get("version_id"),
                document_category=document_category,
            )
            protected.append(manifest_member)
            stage = "pair_consistency"
            policies = {
                (item["retention"]["retention_class"], item["retention"]["retention_policy_version"])
                for item in protected
            }
            if len(policies) != 1:
                raise StateConflict("Original and manifest retention policy mismatch")
            pair_identity = self._pair_identity(protected)
            stage = "postgres_success_transaction"
            self.repository.commit_bronze_pair(
                upload,
                protected,
                {
                    "bronze_pair_id": uuid7(),
                    "pair_identity_sha256": pair_identity,
                    "retention_class": original_member["retention"]["retention_class"],
                    "retention_policy_version": original_member["retention"]["retention_policy_version"],
                },
            )
            log_event(
                "INFO",
                "bronze.pair.committed",
                ingestion_id=ingestion_id,
                pair_identity_sha256=pair_identity,
                original_object_version_id=original_member["version_id"],
                manifest_object_version_id=manifest_member["version_id"],
                retention_class=original_member["retention"]["retention_class"],
            )
            log_event(
                "INFO",
                "state.transition",
                ingestion_id=ingestion_id,
                actor_id="system",
                state_from="RECEIVED",
                state_to="BRONZE_COMMITTED",
            )
        except Exception as exc:
            log_event(
                "ERROR",
                "bronze.pair.failed",
                ingestion_id=ingestion_id,
                failure_stage=stage,
                protected_member_count=len(protected),
                error_type=type(exc).__name__,
            )
            self._record_orphans(upload, protected, stage, exc)
            raise
        job_id = self.repository.ensure_ocr_queued(
            ingestion_id, current_correlation_id()
        )
        log_event(
            "INFO",
            "ocr.job.queued",
            ingestion_id=ingestion_id,
            ocr_job_id=job_id,
        )
        log_event(
            "INFO",
            "state.transition",
            ingestion_id=ingestion_id,
            actor_id="system",
            state_from="BRONZE_COMMITTED",
            state_to="OCR_QUEUED",
        )
        return {"ingestion_id": ingestion_id, "ocr_job_id": job_id, "manifest": manifest}

    def reconcile(self, ingestion_id: str) -> dict[str, Any]:
        context = self.repository.bronze_reconciliation_context(ingestion_id)
        upload = context["upload"]
        if context.get("pair"):
            job_id = self.repository.ensure_ocr_queued(
                ingestion_id, current_correlation_id()
            )
            log_event(
                "INFO",
                "ocr.job.reused",
                ingestion_id=ingestion_id,
                ocr_job_id=job_id,
            )
            return {"ingestion_id": ingestion_id, "ocr_job_id": job_id, "status": "ALREADY_COMMITTED"}
        members = list(context.get("orphans", []))
        if len(members) != 2:
            discovered = self._discover_protected_members(upload, members)
            newly_discovered = [
                item for item in discovered
                if item["kind"] not in {member["kind"] for member in members}
            ]
            if newly_discovered:
                self.repository.record_protected_orphans(
                    upload,
                    newly_discovered,
                    "reconciliation_exact_version_discovery",
                    "RECOVERED_AFTER_EVIDENCE_WRITE_FAILURE",
                )
                context = self.repository.bronze_reconciliation_context(ingestion_id)
                members = list(context.get("orphans", []))
        if len(members) != 2 or {item["kind"] for item in members} != {"ORIGINAL", "MANIFEST"}:
            raise StateConflict("Protected Bronze pair is incomplete")
        refreshed: list[dict[str, Any]] = []
        for item in members:
            refreshed.append(
                self._protect_member(
                    bucket=item["bucket"], key=item["key"], kind=item["kind"],
                    digest=item["sha256"], version_id=item["version_id"],
                    document_category=upload["document_category"],
                )
            )
        identity = self._pair_identity(refreshed)
        if identity != context["retry_identity_sha256"]:
            self.repository.record_reconciliation(
                ingestion_id, identity, "CONFLICT", {"expected": context["retry_identity_sha256"]}
            )
            raise StateConflict("Stale or conflicting Bronze reconciliation retry")
        self.repository.commit_bronze_pair(
            upload,
            refreshed,
            {
                "bronze_pair_id": uuid7(),
                "pair_identity_sha256": identity,
                "retention_class": refreshed[0]["retention"]["retention_class"],
                "retention_policy_version": refreshed[0]["retention"]["retention_policy_version"],
            },
        )
        self.repository.record_reconciliation(
            ingestion_id, identity, "COMPLETED_PAIR", {"exact_version_readback": True}
        )
        log_event(
            "INFO",
            "bronze.reconciliation.completed",
            ingestion_id=ingestion_id,
            pair_identity_sha256=identity,
        )
        job_id = self.repository.ensure_ocr_queued(
            ingestion_id, current_correlation_id()
        )
        log_event(
            "INFO",
            "ocr.job.queued",
            ingestion_id=ingestion_id,
            ocr_job_id=job_id,
        )
        return {"ingestion_id": ingestion_id, "ocr_job_id": job_id, "status": "RECONCILED"}


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
        log_event(
            "INFO",
            "ocr.run.started",
            ingestion_id=ingestion_id,
            ocr_run_id=run_id,
            engine=engine,
            engine_version=engine_version,
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
        log_event(
            "INFO",
            "ocr.run.completed",
            ingestion_id=ingestion_id,
            ocr_run_id=run_id,
            raw_output_sha256=run["raw_output_sha256"],
            object_key=artifact_key,
        )
        return draft


class OCRRecoveryService:
    """Explicit, bounded recovery for the existing failed OCR job."""

    def __init__(self, repository: Repository, max_attempts: int = 3) -> None:
        if max_attempts <= 0:
            raise ValueError("OCR retry maximum must be positive")
        self.repository = repository
        self.max_attempts = max_attempts

    def retry(self, ingestion_id: str, actor: Actor) -> dict[str, Any]:
        result = self.repository.retry_failed_ocr(
            ingestion_id, actor.user_id, self.max_attempts
        )
        log_event(
            "INFO",
            "ocr.job.queued",
            ingestion_id=ingestion_id,
            ocr_job_id=result["ocr_job_id"],
            actor_id=actor.user_id,
            attempt_count=result["attempt_count"],
            max_attempts=self.max_attempts,
            recovery=True,
        )
        log_event(
            "INFO",
            "state.transition",
            ingestion_id=ingestion_id,
            actor_id=actor.user_id,
            state_from="OCR_FAILED",
            state_to="OCR_QUEUED",
        )
        return result


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
        ingestion_id = str(upload["ingestion_id"])
        was_replay = draft["status"] == "REVIEWED"
        if draft["status"] not in {"DRAFT_UNVERIFIED", "REVIEWED"}:
            log_event(
                "WARNING",
                "review.validation.rejected",
                ingestion_id=ingestion_id,
                actor_id=reviewer.user_id,
                reason="DRAFT_NOT_REVIEWABLE",
            )
            raise StateConflict("Only an unverified draft can be reviewed")
        if decision not in APPROVAL_DECISIONS | REJECTION_DECISIONS:
            log_event(
                "WARNING",
                "review.validation.rejected",
                ingestion_id=ingestion_id,
                actor_id=reviewer.user_id,
                reason="UNSUPPORTED_DECISION",
            )
            raise ReviewValidationError("Unsupported review decision")
        if not explicit_confirmation:
            log_event(
                "WARNING",
                "review.validation.rejected",
                ingestion_id=ingestion_id,
                actor_id=reviewer.user_id,
                reason="SOURCE_CONFIRMATION_REQUIRED",
            )
            raise ReviewValidationError("Explicit source-comparison confirmation is required")
        if decision in APPROVAL_DECISIONS and not verified_text.strip():
            log_event(
                "WARNING",
                "review.validation.rejected",
                ingestion_id=ingestion_id,
                actor_id=reviewer.user_id,
                reason="VERIFIED_TEXT_REQUIRED",
            )
            raise ReviewValidationError("Verified text is required for approval")

        self_review_detected = reviewer.user_id == upload["uploader_user_id"]
        solo_exception_applied = False
        exception_reason = administrator_exception_reason
        if self_review_detected:
            if self.allow_phase_1_solo_self_review:
                solo_exception_applied = True
                exception_reason = SOLO_EXCEPTION_REASON
            elif not administrator_exception_reason:
                log_event(
                    "WARNING",
                    "review.validation.rejected",
                    ingestion_id=ingestion_id,
                    actor_id=reviewer.user_id,
                    reason="SELF_REVIEW_EXCEPTION_REQUIRED",
                )
                raise ReviewValidationError("Self-review requires an administrator exception reason")

        review_id = uuid7()
        reviewed_at = utc_now()
        request_payload = {
            "contract": "smartcoat-review-operation-v1",
            "silver_draft_id": draft_id,
            "ingestion_id": ingestion_id,
            "reviewer_user_id": reviewer.user_id,
            "verified_text": verified_text,
            "decision": decision,
            "correction_summary": correction_summary,
            "explicit_confirmation": explicit_confirmation,
            "self_review_detected": self_review_detected,
            "solo_exception_applied": solo_exception_applied,
            "administrator_exception_reason": exception_reason,
        }
        review_request_sha256 = hashlib.sha256(
            json.dumps(
                request_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        review = {
            "review_decision_id": review_id,
            "silver_draft_id": draft_id,
            "ingestion_id": ingestion_id,
            "reviewer_user_id": reviewer.user_id,
            "reviewed_at_utc": reviewed_at,
            "decision": decision,
            "explicit_confirmation": explicit_confirmation,
            "correction_summary": correction_summary,
            "self_review_detected": self_review_detected,
            "solo_exception_applied": solo_exception_applied,
            "administrator_exception_reason": exception_reason,
            "review_request_sha256": review_request_sha256,
        }
        verified = None
        if decision in APPROVAL_DECISIONS:
            verified = {
                "silver_record_id": uuid7(),
                "ingestion_id": ingestion_id,
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
        result = self.repository.complete_review(review, verified)
        final_state = "VERIFIED" if verified else "REVIEW_REJECTED"
        if was_replay:
            log_event(
                "INFO",
                "review.decision.replayed",
                ingestion_id=ingestion_id,
                draft_id=draft_id,
                actor_id=reviewer.user_id,
                decision=decision,
                outcome=final_state,
            )
            return result
        log_event(
            "INFO",
            "review.decision.recorded",
            ingestion_id=ingestion_id,
            draft_id=draft_id,
            actor_id=reviewer.user_id,
            decision=decision,
            outcome=final_state,
        )
        if upload["state"] == "SILVER_DRAFT_READY":
            log_event(
                "INFO",
                "state.transition",
                ingestion_id=ingestion_id,
                actor_id=reviewer.user_id,
                state_from="SILVER_DRAFT_READY",
                state_to="UNDER_HUMAN_REVIEW",
            )
        log_event(
            "INFO",
            "state.transition",
            ingestion_id=ingestion_id,
            actor_id=reviewer.user_id,
            state_from=(
                "UNDER_HUMAN_REVIEW"
                if upload["state"] == "SILVER_DRAFT_READY"
                else upload["state"]
            ),
            state_to=final_state,
        )
        log_event(
            "INFO",
            (
                "review.verification.completed"
                if verified
                else "review.rejection.completed"
            ),
            ingestion_id=ingestion_id,
            draft_id=draft_id,
            actor_id=reviewer.user_id,
            decision=decision,
        )
        return result

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
        log_event(
            "INFO",
            "review.revision.created",
            ingestion_id=ingestion_id,
            draft_id=draft["silver_draft_id"],
            actor_id=actor.user_id,
        )
        return draft
