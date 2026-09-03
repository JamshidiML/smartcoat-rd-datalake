from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from domain import StateConflict
from identifiers import uuid7
from retention_enforcement import RetentionEnforcementEvidence


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.versions: dict[tuple[str, str], str] = {}
        self.puts: list[tuple[str, str, bool]] = []

    def put_once(
        self, bucket: str, key: str, data: bytes, content_type: str, locked: bool
    ) -> dict[str, Any]:
        del content_type
        identifier = (bucket, key)
        if identifier in self.objects:
            raise StateConflict("immutable key exists")
        version_id = uuid7()
        self.objects[identifier] = bytes(data)
        self.versions[identifier] = version_id
        self.puts.append((bucket, key, locked))
        return {"version_id": version_id}

    def get(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]

    def get_exact(self, bucket: str, key: str, version_id: str) -> bytes:
        identifier = (bucket, key)
        if self.versions[identifier] != version_id:
            raise KeyError(version_id)
        return self.objects[identifier]

    def list_exact_versions(self, bucket: str, key: str) -> list[str]:
        identifier = (bucket, key)
        return [self.versions[identifier]] if identifier in self.versions else []

    def delete(self, bucket: str, key: str) -> None:
        del bucket, key
        raise PermissionError("service identity has no delete permission")


class MemoryRepository:
    def __init__(self) -> None:
        self.uploads: dict[str, dict[str, Any]] = {}
        self.objects: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.reviews: list[dict[str, Any]] = []
        self.verified: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self.pairs: dict[str, dict[str, Any]] = {}
        self.orphans: list[dict[str, Any]] = []
        self.reconciliations: list[dict[str, Any]] = []
        self.bronze_fault_checkpoint: str | None = None
        self.review_fault_checkpoint: str | None = None
        self._review_lock = RLock()

    def _audit(
        self,
        entity_id: str,
        event_type: str,
        previous_state: str | None,
        new_state: str | None,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.audit.append(
            {
                "event_id": uuid7(),
                "entity_id": entity_id,
                "event_type": event_type,
                "previous_state": previous_state,
                "new_state": new_state,
                "actor": actor,
                "details": details or {},
            }
        )

    def record_rejection(self, ingestion_id: str, actor_user_id: str, reason: str, code: str) -> None:
        self._audit(
            ingestion_id,
            "UPLOAD_REJECTED",
            "RECEIVED",
            "REJECTED",
            actor_user_id,
            {"reason": reason, "code": code},
        )

    def first_ingestion_by_sha256(self, digest: str) -> str | None:
        matches = [value for value in self.uploads.values() if value["sha256"] == digest]
        return matches[0]["ingestion_id"] if matches else None

    def create_received(self, upload: dict[str, Any]) -> None:
        self.uploads[upload["ingestion_id"]] = deepcopy(upload)
        self._audit(upload["ingestion_id"], "UPLOAD_RECEIVED", None, "RECEIVED", upload["uploader_user_id"])

    def _bronze_checkpoint(self, checkpoint: str) -> None:
        if self.bronze_fault_checkpoint == checkpoint:
            raise RuntimeError(f"synthetic Bronze fault at {checkpoint}")

    def commit_bronze_pair(
        self,
        upload: dict[str, Any],
        members: list[dict[str, Any]],
        pair: dict[str, Any],
    ) -> None:
        ingestion_id = upload["ingestion_id"]
        existing = self.pairs.get(ingestion_id)
        if existing:
            if existing["pair_identity_sha256"] != pair["pair_identity_sha256"]:
                raise StateConflict("Conflicting successful Bronze pair")
            return
        snapshot = deepcopy((self.objects, self.pairs, self.uploads, self.audit))
        try:
            if {member["kind"] for member in members} != {"ORIGINAL", "MANIFEST"}:
                raise StateConflict("Successful Bronze commit requires an original and manifest")
            self.objects.extend(
                {**deepcopy(item), "ingestion_id": ingestion_id} for item in members
            )
            self._bronze_checkpoint("after_objects")
            self._bronze_checkpoint("after_retention_evidence")
            self.pairs[ingestion_id] = {**deepcopy(pair), "ingestion_id": ingestion_id}
            self._bronze_checkpoint("after_pair")
            self._bronze_checkpoint("before_state_transition")
            if self.uploads[ingestion_id]["state"] != "RECEIVED":
                raise StateConflict("Bronze pair cannot commit from current state")
            self.uploads[ingestion_id]["state"] = "BRONZE_COMMITTED"
            self._audit(
                ingestion_id, "BRONZE_PAIR_COMMITTED", "RECEIVED",
                "BRONZE_COMMITTED", "system",
                {"pair_identity_sha256": pair["pair_identity_sha256"]},
            )
            self._bronze_checkpoint("before_commit")
        except Exception:
            self.objects, self.pairs, self.uploads, self.audit = snapshot
            raise

    def record_protected_orphans(
        self,
        upload: dict[str, Any],
        members: list[dict[str, Any]],
        failure_stage: str,
        failure_code: str,
    ) -> None:
        for item in members:
            identity = (
                upload["ingestion_id"], item["bucket"], item["key"],
                item["version_id"], item["retention"]["retention_policy_version"],
            )
            if any(row["identity"] == identity for row in self.orphans):
                continue
            self.orphans.append(
                {
                    **deepcopy(item), "identity": identity,
                    "ingestion_id": upload["ingestion_id"],
                    "failure_stage": failure_stage, "failure_code": failure_code,
                }
            )
        self._audit(
            upload["ingestion_id"], "PROTECTED_ORPHAN_RECORDED",
            "RECEIVED", "RECEIVED", "system",
            {"member_count": len(members), "failure_stage": failure_stage},
        )

    def bronze_reconciliation_context(self, ingestion_id: str) -> dict[str, Any]:
        members = [
            {key: deepcopy(row[key]) for key in (
                "bucket", "key", "kind", "version_id", "sha256"
            )} | {
                "retention_policy_version": row["retention"]["retention_policy_version"]
            }
            for row in self.orphans if row["ingestion_id"] == ingestion_id
        ]
        canonical = [
            {key: row[key] for key in (
                "bucket", "key", "version_id", "retention_policy_version"
            )}
            for row in sorted(members, key=lambda value: value["kind"])
        ]
        retry = hashlib.sha256(
            __import__("json").dumps(
                canonical, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest() if canonical else None
        return {
            "upload": deepcopy(self.uploads[ingestion_id]),
            "pair": deepcopy(self.pairs.get(ingestion_id)),
            "orphans": members,
            "retry_identity_sha256": retry,
        }

    def record_reconciliation(
        self,
        ingestion_id: str,
        retry_identity_sha256: str,
        outcome: str,
        details: dict[str, Any],
    ) -> None:
        identity = (ingestion_id, retry_identity_sha256, outcome)
        if not any(row["identity"] == identity for row in self.reconciliations):
            self.reconciliations.append(
                {"identity": identity, "details": deepcopy(details)}
            )

    def transition(
        self,
        ingestion_id: str,
        previous_state: str,
        new_state: str,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        upload = self.uploads[ingestion_id]
        if upload["state"] != previous_state:
            raise StateConflict(f"expected {previous_state}, got {upload['state']}")
        upload["state"] = new_state
        self._audit(ingestion_id, "UPLOAD_STATE_CHANGED", previous_state, new_state, actor, details)

    def ensure_ocr_queued(
        self, ingestion_id: str, correlation_id: str | None = None
    ) -> str:
        existing = next(
            (job for job in self.jobs.values() if job["ingestion_id"] == ingestion_id),
            None,
        )
        if existing:
            return existing["ocr_job_id"]
        if ingestion_id not in self.pairs or self.uploads[ingestion_id]["state"] != "BRONZE_COMMITTED":
            raise StateConflict("OCR cannot be queued before a successful Bronze pair commit")
        job_id = correlation_id or uuid7()
        self.jobs[job_id] = {
            "ocr_job_id": job_id,
            "ingestion_id": ingestion_id,
            "status": "QUEUED",
            "attempt_count": 0,
            "error_reason": None,
        }
        self.uploads[ingestion_id]["state"] = "OCR_QUEUED"
        self._audit(
            ingestion_id, "UPLOAD_STATE_CHANGED", "BRONZE_COMMITTED",
            "OCR_QUEUED", "system", {"ocr_job_id": job_id},
        )
        return job_id

    def retry_failed_ocr(
        self, ingestion_id: str, actor_id: str, max_attempts: int
    ) -> dict[str, Any]:
        job = next(
            value for value in self.jobs.values()
            if value["ingestion_id"] == ingestion_id
        )
        if self.uploads[ingestion_id]["state"] != "OCR_FAILED" or job["status"] != "FAILED":
            raise StateConflict("Only a failed OCR job can be retried")
        if job["attempt_count"] >= max_attempts:
            raise StateConflict(
                f"OCR retry limit reached ({max_attempts} attempts); operator review is required"
            )
        original = next(
            item for item in self.objects
            if item["ingestion_id"] == ingestion_id and item["kind"] == "ORIGINAL"
        )
        previous_error = job["error_reason"]
        job.update(status="QUEUED", error_reason=None)
        self.uploads[ingestion_id]["state"] = "OCR_QUEUED"
        details = {
            "ingestion_id": ingestion_id,
            "attempt_count": job["attempt_count"],
            "max_attempts": max_attempts,
            "previous_error_reason": previous_error,
            "original_object_version_id": original["version_id"],
        }
        self._audit(
            job["ocr_job_id"], "OCR_RETRY_INITIATED", "FAILED", "QUEUED",
            actor_id, details,
        )
        self._audit(
            ingestion_id, "UPLOAD_STATE_CHANGED", "OCR_FAILED", "OCR_QUEUED",
            actor_id, details,
        )
        return {
            "ingestion_id": ingestion_id,
            "ocr_job_id": job["ocr_job_id"],
            "status": "QUEUED",
            "attempt_count": job["attempt_count"],
            "max_attempts": max_attempts,
            "original_object_version_id": original["version_id"],
        }

    def get_upload(self, ingestion_id: str) -> dict[str, Any]:
        return self.uploads[ingestion_id]

    def start_ocr_run(self, ingestion_id: str, run: dict[str, Any]) -> None:
        job = next(value for value in self.jobs.values() if value["ingestion_id"] == ingestion_id)
        if job["status"] != "QUEUED":
            raise StateConflict("job is not queued")
        job["status"] = "RUNNING"
        job["attempt_count"] += 1
        self.runs[run["ocr_run_id"]] = {**deepcopy(run), "status": "RUNNING", "ocr_job_id": job["ocr_job_id"]}

    def complete_ocr_run(self, ingestion_id: str, run: dict[str, Any], draft: dict[str, Any]) -> None:
        stored = self.runs[run["ocr_run_id"]]
        stored.update(deepcopy(run))
        stored["status"] = "COMPLETED"
        self.jobs[stored["ocr_job_id"]]["status"] = "COMPLETED"
        self.drafts[draft["silver_draft_id"]] = {
            **deepcopy(draft),
            "raw_artifact_key": run["raw_artifact_key"],
        }

    def mark_ocr_failed(self, ingestion_id: str, reason: str) -> None:
        job = next(
            value for value in self.jobs.values()
            if value["ingestion_id"] == ingestion_id
        )
        previous_status = job["status"]
        if previous_status == "QUEUED":
            job["attempt_count"] += 1
        original = next(
            item for item in self.objects
            if item["ingestion_id"] == ingestion_id and item["kind"] == "ORIGINAL"
        )
        running = next(
            (
                run for run in self.runs.values()
                if run["ocr_job_id"] == job["ocr_job_id"] and run["status"] == "RUNNING"
            ),
            None,
        )
        if running:
            running["status"] = "FAILED"
        job.update(status="FAILED", error_reason=reason)
        self.uploads[ingestion_id]["state"] = "OCR_FAILED"
        details = {
            "ingestion_id": ingestion_id,
            "attempt_count": job["attempt_count"],
            "error_reason": reason,
            "ocr_run_id": running["ocr_run_id"] if running else None,
            "original_object_version_id": original["version_id"],
        }
        self._audit(
            job["ocr_job_id"], "OCR_JOB_FAILED", previous_status, "FAILED",
            "ocr-worker", details,
        )
        self._audit(
            ingestion_id, "UPLOAD_STATE_CHANGED", "OCR_QUEUED", "OCR_FAILED",
            "ocr-worker", details,
        )

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        return self.drafts[draft_id]

    def _review_checkpoint(self, checkpoint: str) -> None:
        if self.review_fault_checkpoint == checkpoint:
            raise RuntimeError(f"synthetic review fault at {checkpoint}")

    def complete_review(
        self,
        decision: dict[str, Any],
        verified: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        with self._review_lock:
            existing = next(
                (
                    row
                    for row in self.reviews
                    if row["silver_draft_id"] == decision["silver_draft_id"]
                ),
                None,
            )
            if existing:
                if existing["review_request_sha256"] != decision["review_request_sha256"]:
                    raise StateConflict(
                        "The Silver draft already has a different effective review decision"
                    )
                result = next(
                    (
                        row
                        for row in self.verified
                        if row["review_decision_id"] == existing["review_decision_id"]
                    ),
                    None,
                )
                expected_state = "VERIFIED" if result else "REVIEW_REJECTED"
                review_audits = [
                    row
                    for row in self.audit
                    if row["entity_id"] == decision["silver_draft_id"]
                    and row["event_type"] == "HUMAN_REVIEW_RECORDED"
                    and row["details"].get("review_request_sha256")
                    == decision["review_request_sha256"]
                ]
                final_audits = [
                    row
                    for row in self.audit
                    if row["entity_id"] == decision["ingestion_id"]
                    and row["event_type"] == "UPLOAD_STATE_CHANGED"
                    and row["new_state"] == expected_state
                    and row["details"].get("review_request_sha256")
                    == decision["review_request_sha256"]
                ]
                if (
                    self.drafts[decision["silver_draft_id"]]["status"] != "REVIEWED"
                    or self.uploads[decision["ingestion_id"]]["state"] != expected_state
                    or len(review_audits) != 1
                    or len(final_audits) != 1
                ):
                    raise StateConflict("Stored review outcome is incomplete or internally inconsistent")
                if result is None:
                    return None
                replay = deepcopy(result)
                replay.pop("review_decision_id")
                return replay

            snapshot = deepcopy(
                {
                    "uploads": self.uploads,
                    "drafts": self.drafts,
                    "reviews": self.reviews,
                    "verified": self.verified,
                    "audit": self.audit,
                }
            )
            try:
                draft = self.drafts[decision["silver_draft_id"]]
                upload = self.uploads[decision["ingestion_id"]]
                if draft["status"] != "DRAFT_UNVERIFIED":
                    raise StateConflict("Only an unverified draft can be reviewed")
                if upload["state"] not in {"SILVER_DRAFT_READY", "UNDER_HUMAN_REVIEW"}:
                    raise StateConflict("Upload is not at a reviewable state")

                approved = decision["decision"] in {
                    "APPROVED_NO_CHANGES",
                    "APPROVED_WITH_CORRECTIONS",
                }
                if approved != (verified is not None):
                    raise StateConflict("Review decision and verified outcome do not agree")

                self.reviews.append(deepcopy(decision))
                self._review_checkpoint("after_decision")
                draft["status"] = "REVIEWED"
                self._review_checkpoint("after_draft_disposition")

                verified_result = None
                if verified:
                    verified_result = {
                        **deepcopy(verified),
                        "silver_revision": self.max_silver_revision(
                            decision["ingestion_id"]
                        )
                        + 1,
                    }
                    self.verified.append(
                        {
                            **deepcopy(verified_result),
                            "review_decision_id": decision["review_decision_id"],
                        }
                    )
                self._review_checkpoint("after_verified_revision")

                final_state = "VERIFIED" if verified else "REVIEW_REJECTED"
                self._audit(
                    decision["silver_draft_id"],
                    "HUMAN_REVIEW_RECORDED",
                    "DRAFT_UNVERIFIED",
                    final_state,
                    decision["reviewer_user_id"],
                    {
                        "decision": decision["decision"],
                        "review_request_sha256": decision["review_request_sha256"],
                        "self_review_detected": decision["self_review_detected"],
                        "solo_exception_applied": decision["solo_exception_applied"],
                    },
                )
                self._review_checkpoint("after_review_audit")

                transition_details = {
                    "review_request_sha256": decision["review_request_sha256"],
                    "self_review_detected": decision["self_review_detected"],
                    "phase_1_solo_exception_applied": decision["solo_exception_applied"],
                    "exception_reason": decision["administrator_exception_reason"],
                }
                if upload["state"] == "SILVER_DRAFT_READY":
                    self.transition(
                        decision["ingestion_id"],
                        "SILVER_DRAFT_READY",
                        "UNDER_HUMAN_REVIEW",
                        decision["reviewer_user_id"],
                        transition_details,
                    )
                self._review_checkpoint("after_enter_review_state")
                self.transition(
                    decision["ingestion_id"],
                    "UNDER_HUMAN_REVIEW",
                    final_state,
                    decision["reviewer_user_id"],
                    transition_details,
                )
                self._review_checkpoint("after_final_state")
                return deepcopy(verified_result)
            except Exception:
                self.uploads = snapshot["uploads"]
                self.drafts = snapshot["drafts"]
                self.reviews = snapshot["reviews"]
                self.verified = snapshot["verified"]
                self.audit = snapshot["audit"]
                raise

    def max_silver_revision(self, ingestion_id: str) -> int:
        return max(
            (row["silver_revision"] for row in self.verified if row["ingestion_id"] == ingestion_id),
            default=0,
        )

    def create_revision_draft(self, ingestion_id: str, actor_user_id: str, text: str) -> dict[str, Any]:
        previous = next(row for row in reversed(list(self.drafts.values())) if row["ingestion_id"] == ingestion_id)
        draft = {
            **deepcopy(previous),
            "silver_draft_id": uuid7(),
            "status": "DRAFT_UNVERIFIED",
            "extracted_text": text,
        }
        self.drafts[draft["silver_draft_id"]] = draft
        self._audit(draft["silver_draft_id"], "VERIFIED_TEXT_EDITED", "VERIFIED", "DRAFT_UNVERIFIED", actor_user_id)
        return draft


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MemoryRetentionEnforcer:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.fail_kind: str | None = None

    def enforce(self, **values: Any) -> RetentionEnforcementEvidence:
        target = values["target"]
        self.calls.append(target)
        if self.fail_kind == target.object_kind:
            raise RuntimeError(f"synthetic {target.object_kind} protection failure")
        accepted = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
        return RetentionEnforcementEvidence(
            retention_assignment_id=values["retention_assignment_id"],
            bucket_name=target.bucket_name,
            object_key=target.object_key,
            object_kind=target.object_kind,
            object_version_id=target.object_version_id,
            data_category=values["data_category"],
            retention_class=values["retention_class"],
            retention_policy_version=values["retention_policy_version"],
            accepted_storage_at_utc=accepted,
            requested_retention_mode="COMPLIANCE",
            requested_retain_until_utc=datetime(2036, 8, 29, 12, 0, tzinfo=UTC),
            requested_legal_hold_status="ON",
            observed_object_version_id=target.object_version_id,
            observed_retention_mode="COMPLIANCE",
            observed_retain_until_utc=datetime(2036, 8, 29, 12, 0, tzinfo=UTC),
            observed_legal_hold_status="ON",
            enforcement_verified_at_utc=accepted,
            enforcement_verification_result="SUCCESS",
            failure_code=None,
            enforced_by=values["enforced_by"],
            details_json={"exact_version_readback": True},
        )
