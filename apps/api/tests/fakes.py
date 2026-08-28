from __future__ import annotations

import hashlib
from copy import deepcopy
from threading import RLock
from typing import Any

from domain import StateConflict
from identifiers import uuid7


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[tuple[str, str, bool]] = []

    def put_once(
        self, bucket: str, key: str, data: bytes, content_type: str, locked: bool
    ) -> dict[str, Any]:
        del content_type
        identifier = (bucket, key)
        if identifier in self.objects:
            raise StateConflict("immutable key exists")
        self.objects[identifier] = bytes(data)
        self.puts.append((bucket, key, locked))
        return {"version_id": uuid7()}

    def get(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]

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

    def commit_bronze(self, upload: dict[str, Any], objects: list[dict[str, Any]]) -> None:
        self.objects.extend({**deepcopy(item), "ingestion_id": upload["ingestion_id"]} for item in objects)
        self._audit(upload["ingestion_id"], "BRONZE_OBJECTS_VERIFIED", "RECEIVED", "RECEIVED", "system")

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

    def queue_ocr(self, ingestion_id: str) -> str:
        job_id = uuid7()
        self.jobs[job_id] = {"ocr_job_id": job_id, "ingestion_id": ingestion_id, "status": "QUEUED"}
        return job_id

    def get_upload(self, ingestion_id: str) -> dict[str, Any]:
        return self.uploads[ingestion_id]

    def start_ocr_run(self, ingestion_id: str, run: dict[str, Any]) -> None:
        job = next(value for value in self.jobs.values() if value["ingestion_id"] == ingestion_id)
        if job["status"] != "QUEUED":
            raise StateConflict("job is not queued")
        job["status"] = "RUNNING"
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
