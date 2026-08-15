from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from domain import StateConflict
from identifiers import uuid7


class PostgresRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, default=str, separators=(",", ":"))

    def _audit(
        self,
        connection: psycopg.Connection[Any],
        actor: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        previous_state: str | None,
        new_state: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                event_id, occurred_at_utc, actor_user_id, system_actor, entity_type,
                entity_id, event_type, previous_state, new_state, request_id, details_json
            ) VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                uuid7(),
                None if actor in {"system", "ocr-worker"} else actor,
                actor if actor in {"system", "ocr-worker"} else None,
                entity_type,
                entity_id,
                event_type,
                previous_state,
                new_state,
                uuid7(),
                self._json(details or {}),
            ),
        )

    def ensure_local_user(self, user_id: str, display_name: str, email: str) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO users (user_id, display_name, email, role, active, created_at_utc)
                VALUES (%s, %s, %s, 'ADMIN_REVIEWER', true, now())
                ON CONFLICT (user_id) DO UPDATE
                SET display_name = EXCLUDED.display_name, email = EXCLUDED.email, active = true
                """,
                (user_id, display_name, email),
            )

    def record_rejection(self, ingestion_id: str, actor_user_id: str, reason: str, code: str) -> None:
        with self.connection() as connection:
            self._audit(
                connection,
                actor_user_id,
                "UPLOAD",
                ingestion_id,
                "UPLOAD_REJECTED",
                "RECEIVED",
                "REJECTED",
                {"reason": reason, "code": code},
            )

    def first_ingestion_by_sha256(self, digest: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT ingestion_id FROM uploads
                WHERE source_sha256 = %s AND state <> 'REJECTED'
                ORDER BY uploaded_at_utc ASC LIMIT 1
                """,
                (digest,),
            ).fetchone()
            return str(row["ingestion_id"]) if row else None

    def create_received(self, upload: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO uploads (
                    ingestion_id, department, uploader_user_id, uploader_display_name,
                    uploaded_at_utc, original_filename, stored_object_key, manifest_object_key,
                    detected_mime_type, declared_file_type, document_category, context_note,
                    capture_date, byte_size, source_sha256, duplicate_of_ingestion_id,
                    source_channel, state
                ) VALUES (
                    %(ingestion_id)s, %(department)s, %(uploader_user_id)s, %(uploader_display_name)s,
                    %(uploaded_at_utc)s, %(original_filename)s, %(stored_object_key)s,
                    %(manifest_object_key)s, %(detected_mime_type)s, %(declared_file_type)s,
                    %(document_category)s, %(context_note)s, %(capture_date)s, %(byte_size)s,
                    %(sha256)s, %(duplicate_of_ingestion_id)s, %(source_channel)s, %(state)s
                )
                """,
                upload,
            )
            self._audit(
                connection,
                upload["uploader_user_id"],
                "UPLOAD",
                upload["ingestion_id"],
                "UPLOAD_RECEIVED",
                None,
                "RECEIVED",
            )

    def commit_bronze(self, upload: dict[str, Any], objects: list[dict[str, Any]]) -> None:
        with self.connection() as connection:
            for item in objects:
                connection.execute(
                    """
                    INSERT INTO bronze_objects (
                        bronze_object_id, ingestion_id, bucket_name, object_key, object_kind,
                        sha256, object_version_id, retention_mode, retain_until_utc, created_at_utc
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'COMPLIANCE', now() + interval '365 days', now())
                    """,
                    (
                        uuid7(),
                        upload["ingestion_id"],
                        item["bucket"],
                        item["key"],
                        item["kind"],
                        item["sha256"],
                        item["version_id"],
                    ),
                )
            self._audit(
                connection,
                "system",
                "UPLOAD",
                upload["ingestion_id"],
                "BRONZE_OBJECTS_VERIFIED",
                "RECEIVED",
                "RECEIVED",
                {"object_count": len(objects)},
            )

    def transition(
        self,
        ingestion_id: str,
        previous_state: str,
        new_state: str,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE uploads SET state = %s WHERE ingestion_id = %s AND state = %s",
                (new_state, ingestion_id, previous_state),
            )
            if cursor.rowcount != 1:
                raise StateConflict(
                    f"Invalid or concurrent transition for {ingestion_id}: {previous_state} -> {new_state}"
                )
            self._audit(
                connection,
                actor,
                "UPLOAD",
                ingestion_id,
                "UPLOAD_STATE_CHANGED",
                previous_state,
                new_state,
                details,
            )

    def queue_ocr(self, ingestion_id: str) -> str:
        job_id = uuid7()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO ocr_jobs (ocr_job_id, ingestion_id, status, queued_at_utc, attempt_count)
                VALUES (%s, %s, 'QUEUED', now(), 0)
                """,
                (job_id, ingestion_id),
            )
            self._audit(
                connection, "system", "OCR_JOB", job_id, "OCR_QUEUED", None, "QUEUED", {"ingestion_id": ingestion_id}
            )
        return job_id

    def get_upload(self, ingestion_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM uploads WHERE ingestion_id = %s", (ingestion_id,)
            ).fetchone()
            if not row:
                raise KeyError(ingestion_id)
            result = dict(row)
            result["sha256"] = result["source_sha256"]
            return result

    def start_ocr_run(self, ingestion_id: str, run: dict[str, Any]) -> None:
        with self.connection() as connection:
            job = connection.execute(
                """
                UPDATE ocr_jobs SET status = 'RUNNING', started_at_utc = now(), attempt_count = attempt_count + 1
                WHERE ocr_job_id = (
                    SELECT ocr_job_id FROM ocr_jobs WHERE ingestion_id = %s AND status = 'QUEUED'
                    ORDER BY queued_at_utc LIMIT 1 FOR UPDATE SKIP LOCKED
                ) RETURNING ocr_job_id
                """,
                (ingestion_id,),
            ).fetchone()
            if not job:
                raise StateConflict("No queued OCR job is available")
            connection.execute(
                """
                INSERT INTO ocr_runs (
                    ocr_run_id, ocr_job_id, ingestion_id, engine, engine_version,
                    configuration_json, source_sha256, status, started_at_utc
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, 'RUNNING', %s)
                """,
                (
                    run["ocr_run_id"],
                    job["ocr_job_id"],
                    ingestion_id,
                    run["engine"],
                    run["engine_version"],
                    self._json(run["configuration"]),
                    run["source_sha256"],
                    run["started_at_utc"],
                ),
            )

    def complete_ocr_run(self, ingestion_id: str, run: dict[str, Any], draft: dict[str, Any]) -> None:
        with self.connection() as connection:
            row = connection.execute(
                """
                UPDATE ocr_runs SET status = 'COMPLETED', raw_output_sha256 = %s,
                    raw_artifact_key = %s, completed_at_utc = %s
                WHERE ocr_run_id = %s AND ingestion_id = %s AND status = 'RUNNING'
                RETURNING ocr_job_id, engine, engine_version
                """,
                (
                    run["raw_output_sha256"],
                    run["raw_artifact_key"],
                    run["completed_at_utc"],
                    run["ocr_run_id"],
                    ingestion_id,
                ),
            ).fetchone()
            if not row:
                raise StateConflict("OCR run is not active")
            connection.execute(
                "UPDATE ocr_jobs SET status = 'COMPLETED', completed_at_utc = now() WHERE ocr_job_id = %s",
                (row["ocr_job_id"],),
            )
            connection.execute(
                """
                INSERT INTO silver_drafts (
                    silver_draft_id, ingestion_id, source_sha256, ocr_run_id, status,
                    extracted_text, text_blocks_json, source_file_type, document_category,
                    extraction_engine, extraction_engine_version, created_at_utc
                ) VALUES (%s, %s, %s, %s, 'DRAFT_UNVERIFIED', %s, %s::jsonb, %s, %s, %s, %s, %s)
                """,
                (
                    draft["silver_draft_id"],
                    ingestion_id,
                    draft["source_sha256"],
                    draft["ocr_run_id"],
                    draft["extracted_text"],
                    self._json(draft["text_blocks"]),
                    draft["source_file_type"],
                    draft["document_category"],
                    row["engine"],
                    row["engine_version"],
                    draft["created_at_utc"],
                ),
            )

    def get_draft(self, draft_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT d.*, r.raw_artifact_key FROM silver_drafts d
                JOIN ocr_runs r ON r.ocr_run_id = d.ocr_run_id
                WHERE d.silver_draft_id = %s
                """,
                (draft_id,),
            ).fetchone()
            if not row:
                raise KeyError(draft_id)
            return dict(row)

    def create_review_decision(self, decision: dict[str, Any], verified: dict[str, Any] | None) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO review_decisions (
                    review_decision_id, silver_draft_id, ingestion_id, reviewer_user_id,
                    reviewed_at_utc, decision, explicit_confirmation, correction_summary,
                    self_review_detected, solo_exception_applied, administrator_exception_reason
                ) VALUES (%(review_decision_id)s, %(silver_draft_id)s, %(ingestion_id)s,
                    %(reviewer_user_id)s, %(reviewed_at_utc)s, %(decision)s, %(explicit_confirmation)s,
                    %(correction_summary)s, %(self_review_detected)s, %(solo_exception_applied)s,
                    %(administrator_exception_reason)s)
                """,
                decision,
            )
            connection.execute(
                "UPDATE silver_drafts SET status = 'REVIEWED' WHERE silver_draft_id = %s",
                (decision["silver_draft_id"],),
            )
            if verified:
                connection.execute(
                    """
                    INSERT INTO silver_verified_records (
                        silver_record_id, silver_revision, ingestion_id, source_sha256, status,
                        verified_text, reviewer_user_id, reviewed_at_utc, review_decision,
                        correction_summary, source_object_key, ocr_artifact_key, review_decision_id
                    ) VALUES (%(silver_record_id)s, %(silver_revision)s, %(ingestion_id)s,
                        %(source_sha256)s, %(status)s, %(verified_text)s, %(reviewer_user_id)s,
                        %(reviewed_at_utc)s, %(review_decision)s, %(correction_summary)s,
                        %(source_object_key)s, %(ocr_artifact_key)s, %(review_decision_id)s)
                    """,
                    {**verified, "review_decision_id": decision["review_decision_id"]},
                )
            self._audit(
                connection,
                decision["reviewer_user_id"],
                "SILVER_DRAFT",
                decision["silver_draft_id"],
                "HUMAN_REVIEW_RECORDED",
                "DRAFT_UNVERIFIED",
                "VERIFIED" if verified else "REVIEW_REJECTED",
                {
                    "decision": decision["decision"],
                    "self_review_detected": decision["self_review_detected"],
                    "solo_exception_applied": decision["solo_exception_applied"],
                },
            )

    def max_silver_revision(self, ingestion_id: str) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(silver_revision), 0) AS revision FROM silver_verified_records WHERE ingestion_id = %s",
                (ingestion_id,),
            ).fetchone()
            return int(row["revision"])

    def create_revision_draft(self, ingestion_id: str, actor_user_id: str, text: str) -> dict[str, Any]:
        draft_id = uuid7()
        with self.connection() as connection:
            source = connection.execute(
                """
                SELECT u.source_sha256, u.declared_file_type, u.document_category,
                    d.ocr_run_id, r.engine, r.engine_version
                FROM uploads u
                JOIN silver_drafts d ON d.ingestion_id = u.ingestion_id
                JOIN ocr_runs r ON r.ocr_run_id = d.ocr_run_id
                WHERE u.ingestion_id = %s ORDER BY d.created_at_utc DESC LIMIT 1
                """,
                (ingestion_id,),
            ).fetchone()
            if not source:
                raise KeyError(ingestion_id)
            connection.execute(
                """
                INSERT INTO silver_drafts (
                    silver_draft_id, ingestion_id, source_sha256, ocr_run_id, status,
                    extracted_text, text_blocks_json, source_file_type, document_category,
                    extraction_engine, extraction_engine_version, created_at_utc
                ) VALUES (%s, %s, %s, %s, 'DRAFT_UNVERIFIED', %s, '[]'::jsonb,
                    %s, %s, %s, %s, now())
                """,
                (
                    draft_id,
                    ingestion_id,
                    source["source_sha256"],
                    source["ocr_run_id"],
                    text,
                    source["declared_file_type"],
                    source["document_category"],
                    source["engine"],
                    source["engine_version"],
                ),
            )
            self._audit(
                connection,
                actor_user_id,
                "SILVER_DRAFT",
                draft_id,
                "VERIFIED_TEXT_EDITED",
                "VERIFIED",
                "DRAFT_UNVERIFIED",
            )
        return {"silver_draft_id": draft_id, "ingestion_id": ingestion_id, "status": "DRAFT_UNVERIFIED"}

    def list_uploads(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT ingestion_id, original_filename, document_category, state, uploaded_at_utc,
                    source_sha256, duplicate_of_ingestion_id
                FROM uploads ORDER BY uploaded_at_utc DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def list_drafts(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT d.silver_draft_id, d.ingestion_id, d.status, d.extracted_text,
                    d.created_at_utc, u.original_filename
                FROM silver_drafts d JOIN uploads u USING (ingestion_id)
                WHERE d.status = 'DRAFT_UNVERIFIED' ORDER BY d.created_at_utc
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_review_context(self, draft_id: str) -> dict[str, Any]:
        draft = self.get_draft(draft_id)
        upload = self.get_upload(draft["ingestion_id"])
        return {"draft": draft, "upload": upload}

    def audit_events(self, ingestion_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE entity_id = %s ORDER BY occurred_at_utc", (ingestion_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_next_job(self) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT j.ocr_job_id, j.ingestion_id, u.stored_object_key, u.detected_mime_type,
                    u.declared_file_type, u.document_category, u.source_sha256
                FROM ocr_jobs j JOIN uploads u USING (ingestion_id)
                WHERE j.status = 'QUEUED' AND u.state = 'OCR_QUEUED'
                ORDER BY j.queued_at_utc LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None

    def mark_ocr_failed(self, ingestion_id: str, reason: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE ocr_jobs SET status = 'FAILED', error_reason = %s, completed_at_utc = now() WHERE ingestion_id = %s AND status IN ('QUEUED', 'RUNNING')",
                (reason[:1000], ingestion_id),
            )
        upload = self.get_upload(ingestion_id)
        self.transition(ingestion_id, upload["state"], "OCR_FAILED", "ocr-worker", {"reason": reason[:500]})
