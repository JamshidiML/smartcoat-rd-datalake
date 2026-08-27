"""Pinned structural recognition contract for the unchanged Phase 1 bootstrap.

This manifest is hand-authored from ``init.sql`` and bound to that file's exact
SHA-256.  It is deliberately catalog-only: no application row is queried or
included in the fingerprint.  Live PostgreSQL validation of the catalog query
shapes remains the responsibility of M0-R01.4.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


CONTRACT_VERSION = "smartcoat-bootstrap-v1"
EXPECTED_INIT_SQL_SHA256 = (
    "9733855250e800c9a1e44f90d72711b8af49d8c940e89c6751020c1c5292e272"
)
EXPECTED_BASELINE_VERSION = 1
EXPECTED_BASELINE_NAME = "validate_bootstrap_prerequisites"
EXPECTED_BASELINE_SHA256 = (
    "7f34c9aba3819a49a5bb6c83f75bceaf436009d36c1c62eb46d0ddfa425529e5"
)


CATALOG_QUERIES: Mapping[str, str] = {
    "schemas": """
        SELECT n.nspname
        FROM pg_namespace AS n
        WHERE n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname
    """,
    "tables": """
        SELECT n.nspname, c.relname, c.relkind, c.relpersistence
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, c.relname
    """,
    "columns": """
        SELECT n.nspname, c.relname, a.attnum, a.attname,
               pg_catalog.format_type(a.atttypid, a.atttypmod),
               a.attnotnull,
               COALESCE(pg_get_expr(d.adbin, d.adrelid, false), '')
        FROM pg_attribute AS a
        JOIN pg_class AS c ON c.oid = a.attrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef AS d
          ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attnum > 0
          AND NOT a.attisdropped
          AND c.relkind IN ('r', 'p')
          AND n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, c.relname, a.attnum
    """,
    "key_constraints": """
        SELECT n.nspname, c.relname, con.conname, con.contype,
               string_agg(a.attname, ',' ORDER BY key_column.ordinality),
               COALESCE(ref_n.nspname, ''), COALESCE(ref_c.relname, ''),
               COALESCE(string_agg(ref_a.attname, ',' ORDER BY key_column.ordinality)
                   FILTER (WHERE ref_a.attname IS NOT NULL), ''),
               CASE WHEN con.contype = 'f' THEN con.confupdtype::text ELSE '' END,
               CASE WHEN con.contype = 'f' THEN con.confdeltype::text ELSE '' END,
               CASE WHEN con.contype = 'f' THEN con.confmatchtype::text ELSE '' END,
               con.condeferrable, con.condeferred
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, ordinality)
          ON true
        JOIN pg_attribute AS a
          ON a.attrelid = c.oid AND a.attnum = key_column.attnum
        LEFT JOIN pg_class AS ref_c ON ref_c.oid = con.confrelid
        LEFT JOIN pg_namespace AS ref_n ON ref_n.oid = ref_c.relnamespace
        LEFT JOIN pg_attribute AS ref_a
          ON ref_a.attrelid = ref_c.oid
         AND ref_a.attnum = con.confkey[key_column.ordinality]
        WHERE con.contype IN ('p', 'u', 'f')
          AND n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        GROUP BY n.nspname, c.relname, con.conname, con.contype,
                 ref_n.nspname, ref_c.relname, con.confupdtype,
                 con.confdeltype, con.confmatchtype, con.condeferrable,
                 con.condeferred
        ORDER BY n.nspname, c.relname, con.conname
    """,
    "check_constraints": """
        SELECT n.nspname, c.relname, con.conname,
               pg_get_constraintdef(con.oid, false)
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE con.contype = 'c'
          AND n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, c.relname, con.conname
    """,
    "indexes": """
        SELECT n.nspname, table_c.relname, index_c.relname,
               am.amname, i.indisunique,
               string_agg(pg_get_indexdef(i.indexrelid, key_number, true), ','
                          ORDER BY key_number),
               COALESCE(pg_get_expr(i.indpred, i.indrelid, false), '')
        FROM pg_index AS i
        JOIN pg_class AS table_c ON table_c.oid = i.indrelid
        JOIN pg_class AS index_c ON index_c.oid = i.indexrelid
        JOIN pg_namespace AS n ON n.oid = table_c.relnamespace
        JOIN pg_am AS am ON am.oid = index_c.relam
        JOIN generate_series(1, i.indnkeyatts) AS key_number ON true
        WHERE n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
          AND NOT EXISTS (
              SELECT 1 FROM pg_constraint AS con WHERE con.conindid = i.indexrelid
          )
        GROUP BY n.nspname, table_c.relname, index_c.relname, am.amname,
                 i.indisunique, i.indpred, i.indrelid
        ORDER BY n.nspname, table_c.relname, index_c.relname
    """,
    "enums": """
        SELECT n.nspname, t.typname, e.enumsortorder, e.enumlabel
        FROM pg_type AS t
        JOIN pg_namespace AS n ON n.oid = t.typnamespace
        JOIN pg_enum AS e ON e.enumtypid = t.oid
        WHERE n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, t.typname, e.enumsortorder
    """,
    "triggers": """
        SELECT n.nspname, c.relname, t.tgname, t.tgenabled,
               (t.tgtype & 1) <> 0, (t.tgtype & 2) <> 0,
               (t.tgtype & 4) <> 0, (t.tgtype & 8) <> 0,
               (t.tgtype & 16) <> 0, (t.tgtype & 32) <> 0,
               fn_n.nspname, p.proname
        FROM pg_trigger AS t
        JOIN pg_class AS c ON c.oid = t.tgrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_proc AS p ON p.oid = t.tgfoid
        JOIN pg_namespace AS fn_n ON fn_n.oid = p.pronamespace
        WHERE NOT t.tgisinternal
          AND n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, c.relname, t.tgname
    """,
    "trigger_functions": """
        SELECT n.nspname, p.proname, pg_get_function_result(p.oid),
               l.lanname, p.provolatile, p.prosecdef, p.prosrc
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        JOIN pg_language AS l ON l.oid = p.prolang
        WHERE pg_get_function_result(p.oid) = 'trigger'
          AND n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
        ORDER BY n.nspname, p.proname
    """,
    "roles": """
        SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb,
               rolcanlogin, rolreplication, rolbypassrls
        FROM pg_roles
        WHERE rolname = 'smartcoat_app'
        ORDER BY rolname
    """,
    "role_memberships": """
        SELECT granted.rolname, member.rolname, membership.admin_option
        FROM pg_auth_members AS membership
        JOIN pg_roles AS granted ON granted.oid = membership.roleid
        JOIN pg_roles AS member ON member.oid = membership.member
        WHERE granted.rolname = 'smartcoat_app' OR member.rolname = 'smartcoat_app'
        ORDER BY granted.rolname, member.rolname
    """,
    "schema_privileges": """
        SELECT n.nspname,
               CASE acl.grantee WHEN 0 THEN 'PUBLIC' ELSE role.rolname END,
               acl.privilege_type, acl.is_grantable
        FROM pg_namespace AS n
        CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) AS acl
        LEFT JOIN pg_roles AS role ON role.oid = acl.grantee
        WHERE n.nspname = 'public'
          AND (acl.grantee = 0 OR role.rolname = 'smartcoat_app')
        ORDER BY n.nspname, 2, acl.privilege_type
    """,
    "table_privileges": """
        SELECT n.nspname, c.relname,
               CASE acl.grantee WHEN 0 THEN 'PUBLIC' ELSE role.rolname END,
               acl.privilege_type, acl.is_grantable
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) AS acl
        LEFT JOIN pg_roles AS role ON role.oid = acl.grantee
        WHERE c.relkind IN ('r', 'p')
          AND n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
          AND (acl.grantee = 0 OR role.rolname = 'smartcoat_app')
        ORDER BY n.nspname, c.relname, 3, acl.privilege_type
    """,
    "column_privileges": """
        SELECT n.nspname, c.relname, a.attname,
               CASE acl.grantee WHEN 0 THEN 'PUBLIC' ELSE role.rolname END,
               acl.privilege_type, acl.is_grantable
        FROM pg_attribute AS a
        JOIN pg_class AS c ON c.oid = a.attrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(a.attacl) AS acl
        LEFT JOIN pg_roles AS role ON role.oid = acl.grantee
        WHERE a.attnum > 0
          AND NOT a.attisdropped
          AND n.nspname <> 'smartcoat_migrations'
          AND n.nspname <> 'information_schema'
          AND n.nspname !~ '^pg_'
          AND (acl.grantee = 0 OR role.rolname = 'smartcoat_app')
        ORDER BY n.nspname, c.relname, a.attname, 4, acl.privilege_type
    """,
}


def _columns(table: str, definitions: Sequence[tuple[str, str, bool, str]]) -> list[tuple[Any, ...]]:
    return [
        ("public", table, position, name, data_type, not_null, default)
        for position, (name, data_type, not_null, default) in enumerate(definitions, 1)
    ]


_TABLES = (
    "audit_events",
    "bronze_objects",
    "ocr_jobs",
    "ocr_runs",
    "review_decisions",
    "silver_drafts",
    "silver_verified_records",
    "uploads",
    "users",
)

_COLUMNS = (
    _columns("users", (
        ("user_id", "text", True, ""), ("display_name", "text", True, ""),
        ("email", "text", True, ""), ("role", "text", True, ""),
        ("active", "boolean", True, "true"),
        ("created_at_utc", "timestamp with time zone", True, "now()"),
    ))
    + _columns("uploads", (
        ("ingestion_id", "uuid", True, ""), ("department", "text", True, ""),
        ("uploader_user_id", "text", True, ""),
        ("uploader_display_name", "text", True, ""),
        ("uploaded_at_utc", "timestamp with time zone", True, ""),
        ("original_filename", "text", True, ""),
        ("stored_object_key", "text", True, ""),
        ("manifest_object_key", "text", True, ""),
        ("detected_mime_type", "text", True, ""),
        ("declared_file_type", "text", True, ""),
        ("document_category", "text", True, ""),
        ("context_note", "text", True, ""), ("capture_date", "date", False, ""),
        ("byte_size", "bigint", True, ""), ("source_sha256", "text", True, ""),
        ("duplicate_of_ingestion_id", "uuid", False, ""),
        ("source_channel", "text", True, ""), ("state", "text", True, ""),
    ))
    + _columns("bronze_objects", (
        ("bronze_object_id", "uuid", True, ""), ("ingestion_id", "uuid", True, ""),
        ("bucket_name", "text", True, ""), ("object_key", "text", True, ""),
        ("object_kind", "text", True, ""), ("sha256", "text", True, ""),
        ("object_version_id", "text", False, ""), ("retention_mode", "text", True, ""),
        ("retain_until_utc", "timestamp with time zone", True, ""),
        ("created_at_utc", "timestamp with time zone", True, ""),
    ))
    + _columns("ocr_jobs", (
        ("ocr_job_id", "uuid", True, ""), ("ingestion_id", "uuid", True, ""),
        ("status", "text", True, ""),
        ("queued_at_utc", "timestamp with time zone", True, ""),
        ("started_at_utc", "timestamp with time zone", False, ""),
        ("completed_at_utc", "timestamp with time zone", False, ""),
        ("attempt_count", "integer", True, "0"), ("error_reason", "text", False, ""),
    ))
    + _columns("ocr_runs", (
        ("ocr_run_id", "uuid", True, ""), ("ocr_job_id", "uuid", True, ""),
        ("ingestion_id", "uuid", True, ""), ("engine", "text", True, ""),
        ("engine_version", "text", True, ""), ("configuration_json", "jsonb", True, ""),
        ("source_sha256", "text", True, ""), ("raw_output_sha256", "text", False, ""),
        ("raw_artifact_key", "text", False, ""), ("status", "text", True, ""),
        ("started_at_utc", "timestamp with time zone", True, ""),
        ("completed_at_utc", "timestamp with time zone", False, ""),
    ))
    + _columns("silver_drafts", (
        ("silver_draft_id", "uuid", True, ""), ("ingestion_id", "uuid", True, ""),
        ("source_sha256", "text", True, ""), ("ocr_run_id", "uuid", True, ""),
        ("status", "text", True, ""), ("extracted_text", "text", True, ""),
        ("text_blocks_json", "jsonb", True, ""), ("source_file_type", "text", True, ""),
        ("document_category", "text", True, ""),
        ("extraction_engine", "text", True, ""),
        ("extraction_engine_version", "text", True, ""),
        ("created_at_utc", "timestamp with time zone", True, ""),
    ))
    + _columns("review_decisions", (
        ("review_decision_id", "uuid", True, ""), ("silver_draft_id", "uuid", True, ""),
        ("ingestion_id", "uuid", True, ""), ("reviewer_user_id", "text", True, ""),
        ("reviewed_at_utc", "timestamp with time zone", True, ""),
        ("decision", "text", True, ""), ("explicit_confirmation", "boolean", True, ""),
        ("correction_summary", "text", True, ""),
        ("self_review_detected", "boolean", True, ""),
        ("solo_exception_applied", "boolean", True, ""),
        ("administrator_exception_reason", "text", False, ""),
    ))
    + _columns("silver_verified_records", (
        ("silver_record_id", "uuid", True, ""), ("silver_revision", "integer", True, ""),
        ("ingestion_id", "uuid", True, ""), ("source_sha256", "text", True, ""),
        ("status", "text", True, ""), ("verified_text", "text", True, ""),
        ("reviewer_user_id", "text", True, ""),
        ("reviewed_at_utc", "timestamp with time zone", True, ""),
        ("review_decision", "text", True, ""), ("correction_summary", "text", True, ""),
        ("source_object_key", "text", True, ""), ("ocr_artifact_key", "text", True, ""),
        ("review_decision_id", "uuid", True, ""),
    ))
    + _columns("audit_events", (
        ("event_id", "uuid", True, ""),
        ("occurred_at_utc", "timestamp with time zone", True, ""),
        ("actor_user_id", "text", False, ""), ("system_actor", "text", False, ""),
        ("entity_type", "text", True, ""), ("entity_id", "text", True, ""),
        ("event_type", "text", True, ""), ("previous_state", "text", False, ""),
        ("new_state", "text", False, ""), ("request_id", "uuid", True, ""),
        ("details_json", "jsonb", True, ""),
    ))
)


def _key(table: str, name: str, kind: str, columns: str,
         referenced_table: str = "", referenced_columns: str = "") -> tuple[Any, ...]:
    is_foreign = kind == "f"
    return (
        "public", table, name, kind, columns,
        "public" if is_foreign else "", referenced_table, referenced_columns,
        "a" if is_foreign else "", "a" if is_foreign else "",
        "s" if is_foreign else "", False, False,
    )


_KEY_CONSTRAINTS = (
    _key("users", "users_pkey", "p", "user_id"),
    _key("users", "users_email_key", "u", "email"),
    _key("uploads", "uploads_pkey", "p", "ingestion_id"),
    _key("uploads", "uploads_uploader_user_id_fkey", "f", "uploader_user_id", "users", "user_id"),
    _key("uploads", "uploads_stored_object_key_key", "u", "stored_object_key"),
    _key("uploads", "uploads_manifest_object_key_key", "u", "manifest_object_key"),
    _key("uploads", "uploads_duplicate_of_ingestion_id_fkey", "f", "duplicate_of_ingestion_id", "uploads", "ingestion_id"),
    _key("bronze_objects", "bronze_objects_pkey", "p", "bronze_object_id"),
    _key("bronze_objects", "bronze_objects_ingestion_id_fkey", "f", "ingestion_id", "uploads", "ingestion_id"),
    _key("bronze_objects", "bronze_objects_bucket_name_object_key_key", "u", "bucket_name,object_key"),
    _key("bronze_objects", "bronze_objects_ingestion_id_object_kind_key", "u", "ingestion_id,object_kind"),
    _key("ocr_jobs", "ocr_jobs_pkey", "p", "ocr_job_id"),
    _key("ocr_jobs", "ocr_jobs_ingestion_id_fkey", "f", "ingestion_id", "uploads", "ingestion_id"),
    _key("ocr_runs", "ocr_runs_pkey", "p", "ocr_run_id"),
    _key("ocr_runs", "ocr_runs_ocr_job_id_fkey", "f", "ocr_job_id", "ocr_jobs", "ocr_job_id"),
    _key("ocr_runs", "ocr_runs_ingestion_id_fkey", "f", "ingestion_id", "uploads", "ingestion_id"),
    _key("silver_drafts", "silver_drafts_pkey", "p", "silver_draft_id"),
    _key("silver_drafts", "silver_drafts_ingestion_id_fkey", "f", "ingestion_id", "uploads", "ingestion_id"),
    _key("silver_drafts", "silver_drafts_ocr_run_id_fkey", "f", "ocr_run_id", "ocr_runs", "ocr_run_id"),
    _key("review_decisions", "review_decisions_pkey", "p", "review_decision_id"),
    _key("review_decisions", "review_decisions_silver_draft_id_fkey", "f", "silver_draft_id", "silver_drafts", "silver_draft_id"),
    _key("review_decisions", "review_decisions_ingestion_id_fkey", "f", "ingestion_id", "uploads", "ingestion_id"),
    _key("review_decisions", "review_decisions_reviewer_user_id_fkey", "f", "reviewer_user_id", "users", "user_id"),
    _key("silver_verified_records", "silver_verified_records_pkey", "p", "silver_record_id"),
    _key("silver_verified_records", "silver_verified_records_ingestion_id_fkey", "f", "ingestion_id", "uploads", "ingestion_id"),
    _key("silver_verified_records", "silver_verified_records_reviewer_user_id_fkey", "f", "reviewer_user_id", "users", "user_id"),
    _key("silver_verified_records", "silver_verified_records_review_decision_id_fkey", "f", "review_decision_id", "review_decisions", "review_decision_id"),
    _key("silver_verified_records", "silver_verified_records_ingestion_id_silver_revision_key", "u", "ingestion_id,silver_revision"),
    _key("audit_events", "audit_events_pkey", "p", "event_id"),
    _key("audit_events", "audit_events_actor_user_id_fkey", "f", "actor_user_id", "users", "user_id"),
)


_CHECK_EXPRESSIONS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "users": (
        ("users_user_id_check", "CHECK ((user_id ~ '^usr_[A-Za-z0-9_-]+$'::text))"),
        ("users_role_check", "CHECK ((role = ANY (ARRAY['UPLOADER'::text, 'REVIEWER'::text, 'ADMIN_REVIEWER'::text])))"),
    ),
    "uploads": (
        ("uploads_department_check", "CHECK ((department = 'RND'::text))"),
        ("uploads_declared_file_type_check", "CHECK ((declared_file_type = ANY (ARRAY['PHOTO'::text, 'PDF'::text, 'EXCEL'::text])))"),
        ("uploads_document_category_check", "CHECK ((document_category = ANY (ARRAY['LAB_NOTE'::text, 'TEST_RESULT'::text, 'FORMULATION_SCREEN'::text, 'MATERIAL_DOCUMENT'::text, 'OTHER'::text])))"),
        ("uploads_context_note_check", "CHECK (((char_length(context_note) >= 10) AND (char_length(context_note) <= 500)))"),
        ("uploads_byte_size_check", "CHECK (((byte_size > 0) AND (byte_size <= 52428800)))"),
        ("uploads_source_sha256_check", "CHECK ((source_sha256 ~ '^[0-9a-f]{64}$'::text))"),
        ("uploads_source_channel_check", "CHECK ((source_channel = 'WEB_UPLOAD'::text))"),
        ("uploads_state_check", "CHECK ((state = ANY (ARRAY['RECEIVED'::text, 'BRONZE_COMMITTED'::text, 'OCR_QUEUED'::text, 'OCR_COMPLETED'::text, 'SILVER_DRAFT_READY'::text, 'UNDER_HUMAN_REVIEW'::text, 'VERIFIED'::text, 'REJECTED'::text, 'OCR_FAILED'::text, 'REVIEW_REJECTED'::text])))"),
    ),
    "bronze_objects": (
        ("bronze_objects_object_kind_check", "CHECK ((object_kind = ANY (ARRAY['ORIGINAL'::text, 'MANIFEST'::text])))"),
        ("bronze_objects_sha256_check", "CHECK ((sha256 ~ '^[0-9a-f]{64}$'::text))"),
        ("bronze_objects_retention_mode_check", "CHECK ((retention_mode = 'COMPLIANCE'::text))"),
    ),
    "ocr_jobs": (
        ("ocr_jobs_status_check", "CHECK ((status = ANY (ARRAY['QUEUED'::text, 'RUNNING'::text, 'COMPLETED'::text, 'FAILED'::text])))"),
    ),
    "ocr_runs": (
        ("ocr_runs_engine_check", "CHECK ((engine = ANY (ARRAY['paddleocr'::text, 'openpyxl'::text])))"),
        ("ocr_runs_source_sha256_check", "CHECK ((source_sha256 ~ '^[0-9a-f]{64}$'::text))"),
        ("ocr_runs_raw_output_sha256_check", "CHECK ((raw_output_sha256 ~ '^[0-9a-f]{64}$'::text))"),
        ("ocr_runs_status_check", "CHECK ((status = ANY (ARRAY['RUNNING'::text, 'COMPLETED'::text, 'FAILED'::text])))"),
    ),
    "silver_drafts": (
        ("silver_drafts_source_sha256_check", "CHECK ((source_sha256 ~ '^[0-9a-f]{64}$'::text))"),
        ("silver_drafts_status_check", "CHECK ((status = ANY (ARRAY['DRAFT_UNVERIFIED'::text, 'REVIEWED'::text])))"),
        ("silver_drafts_source_file_type_check", "CHECK ((source_file_type = ANY (ARRAY['PHOTO'::text, 'PDF'::text, 'EXCEL'::text])))"),
    ),
    "review_decisions": (
        ("review_decisions_decision_check", "CHECK ((decision = ANY (ARRAY['APPROVED_NO_CHANGES'::text, 'APPROVED_WITH_CORRECTIONS'::text, 'REJECTED_UNREADABLE'::text, 'REJECTED_WRONG_DOCUMENT_TYPE'::text, 'REQUIRES_REUPLOAD'::text])))"),
        ("review_decisions_explicit_confirmation_check", "CHECK (explicit_confirmation)"),
    ),
    "silver_verified_records": (
        ("silver_verified_records_silver_revision_check", "CHECK ((silver_revision > 0))"),
        ("silver_verified_records_source_sha256_check", "CHECK ((source_sha256 ~ '^[0-9a-f]{64}$'::text))"),
        ("silver_verified_records_status_check", "CHECK ((status = 'VERIFIED'::text))"),
        ("silver_verified_records_review_decision_check", "CHECK ((review_decision = ANY (ARRAY['APPROVED_NO_CHANGES'::text, 'APPROVED_WITH_CORRECTIONS'::text])))"),
    ),
    "audit_events": (
        ("audit_events_check", "CHECK (((actor_user_id IS NOT NULL) <> (system_actor IS NOT NULL)))"),
    ),
}


def _check_rows() -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for table, definitions in _CHECK_EXPRESSIONS.items():
        for name, definition in definitions:
            rows.append(("public", table, name, definition))
    return rows


_TRIGGER_TABLES = {
    "audit_events": "audit_events_append_only",
    "bronze_objects": "bronze_objects_append_only",
    "review_decisions": "review_decisions_append_only",
    "silver_verified_records": "verified_records_append_only",
}

EXPECTED_CATALOG: Mapping[str, Sequence[Sequence[Any]]] = {
    "schemas": [("public",)],
    "tables": [("public", table, "r", "p") for table in _TABLES],
    "columns": _COLUMNS,
    "key_constraints": _KEY_CONSTRAINTS,
    "check_constraints": _check_rows(),
    "indexes": [
        ("public", "audit_events", "audit_entity_idx", "btree", False,
         "entity_type,entity_id,occurred_at_utc", ""),
        ("public", "uploads", "uploads_sha256_idx", "btree", False,
         "source_sha256,uploaded_at_utc", ""),
    ],
    "enums": [],
    "triggers": [
        ("public", table, trigger, "O", True, True, False, True, True, False,
         "public", "reject_immutable_mutation")
        for table, trigger in _TRIGGER_TABLES.items()
    ],
    "trigger_functions": [
        ("public", "reject_immutable_mutation", "trigger", "plpgsql", "v", False,
         "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END;")
    ],
    "roles": [
        ("smartcoat_app", False, True, False, False, True, False, False)
    ],
    "role_memberships": [],
    "schema_privileges": [
        ("public", "PUBLIC", "USAGE", False),
        ("public", "smartcoat_app", "USAGE", False),
    ],
    "table_privileges": [
        ("public", table, "smartcoat_app", privilege, False)
        for table in _TABLES
        for privilege in ("INSERT", "SELECT")
    ],
    "column_privileges": [
        ("public", table, column, "smartcoat_app", "UPDATE", False)
        for table, columns in {
            "ocr_jobs": ("attempt_count", "completed_at_utc", "error_reason", "started_at_utc", "status"),
            "ocr_runs": ("completed_at_utc", "raw_artifact_key", "raw_output_sha256", "status"),
            "silver_drafts": ("status",),
            "uploads": ("state",),
            "users": ("active", "display_name", "email"),
        }.items()
        for column in columns
    ],
}


def _normalize_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, (bool, int, float)):
        return value
    return " ".join(str(value).split())


def normalize_catalog(
    catalog: Mapping[str, Sequence[Sequence[Any]]],
) -> dict[str, list[list[Any]]]:
    """Normalize and sort every required catalog category, failing closed."""
    missing = sorted(set(CATALOG_QUERIES) - set(catalog))
    unexpected = sorted(set(catalog) - set(CATALOG_QUERIES))
    if missing or unexpected:
        raise ValueError(
            f"Catalog categories differ; missing={missing!r} unexpected={unexpected!r}"
        )
    normalized: dict[str, list[list[Any]]] = {}
    for category in sorted(CATALOG_QUERIES):
        try:
            rows = [list(_normalize_value(row)) for row in catalog[category]]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Catalog category {category!r} is unreadable") from exc
        normalized[category] = sorted(
            rows,
            key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
        )
    return normalized


def canonical_catalog_json(catalog: Mapping[str, Sequence[Sequence[Any]]]) -> str:
    return json.dumps(
        normalize_catalog(catalog),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def catalog_fingerprint(catalog: Mapping[str, Sequence[Sequence[Any]]]) -> str:
    return hashlib.sha256(canonical_catalog_json(catalog).encode("utf-8")).hexdigest()


EXPECTED_NORMALIZED_CATALOG = normalize_catalog(EXPECTED_CATALOG)
EXPECTED_STRUCTURAL_FINGERPRINT = catalog_fingerprint(EXPECTED_CATALOG)
COMPARED_CATEGORIES_JSON = json.dumps(
    [
        {"category": category, "expected_rows": len(EXPECTED_NORMALIZED_CATALOG[category])}
        for category in sorted(EXPECTED_NORMALIZED_CATALOG)
    ],
    sort_keys=True,
    separators=(",", ":"),
)
