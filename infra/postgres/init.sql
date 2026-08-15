BEGIN;

\getenv app_password POSTGRES_APP_PASSWORD
SELECT format('CREATE ROLE smartcoat_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'smartcoat_app')
\gexec

CREATE TABLE users (
    user_id text PRIMARY KEY CHECK (user_id ~ '^usr_[A-Za-z0-9_-]+$'),
    display_name text NOT NULL,
    email text NOT NULL UNIQUE,
    role text NOT NULL CHECK (role IN ('UPLOADER', 'REVIEWER', 'ADMIN_REVIEWER')),
    active boolean NOT NULL DEFAULT true,
    created_at_utc timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE uploads (
    ingestion_id uuid PRIMARY KEY,
    department text NOT NULL CHECK (department = 'RND'),
    uploader_user_id text NOT NULL REFERENCES users(user_id),
    uploader_display_name text NOT NULL,
    uploaded_at_utc timestamptz NOT NULL,
    original_filename text NOT NULL,
    stored_object_key text NOT NULL UNIQUE,
    manifest_object_key text NOT NULL UNIQUE,
    detected_mime_type text NOT NULL,
    declared_file_type text NOT NULL CHECK (declared_file_type IN ('PHOTO', 'PDF', 'EXCEL')),
    document_category text NOT NULL CHECK (
        document_category IN ('LAB_NOTE', 'TEST_RESULT', 'FORMULATION_SCREEN', 'MATERIAL_DOCUMENT', 'OTHER')
    ),
    context_note text NOT NULL CHECK (char_length(context_note) BETWEEN 10 AND 500),
    capture_date date,
    byte_size bigint NOT NULL CHECK (byte_size > 0 AND byte_size <= 52428800),
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    duplicate_of_ingestion_id uuid REFERENCES uploads(ingestion_id),
    source_channel text NOT NULL CHECK (source_channel = 'WEB_UPLOAD'),
    state text NOT NULL CHECK (state IN (
        'RECEIVED', 'BRONZE_COMMITTED', 'OCR_QUEUED', 'OCR_COMPLETED',
        'SILVER_DRAFT_READY', 'UNDER_HUMAN_REVIEW', 'VERIFIED',
        'REJECTED', 'OCR_FAILED', 'REVIEW_REJECTED'
    ))
);
CREATE INDEX uploads_sha256_idx ON uploads (source_sha256, uploaded_at_utc);

CREATE TABLE bronze_objects (
    bronze_object_id uuid PRIMARY KEY,
    ingestion_id uuid NOT NULL REFERENCES uploads(ingestion_id),
    bucket_name text NOT NULL,
    object_key text NOT NULL,
    object_kind text NOT NULL CHECK (object_kind IN ('ORIGINAL', 'MANIFEST')),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    object_version_id text,
    retention_mode text NOT NULL CHECK (retention_mode = 'COMPLIANCE'),
    retain_until_utc timestamptz NOT NULL,
    created_at_utc timestamptz NOT NULL,
    UNIQUE (bucket_name, object_key),
    UNIQUE (ingestion_id, object_kind)
);

CREATE TABLE ocr_jobs (
    ocr_job_id uuid PRIMARY KEY,
    ingestion_id uuid NOT NULL REFERENCES uploads(ingestion_id),
    status text NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')),
    queued_at_utc timestamptz NOT NULL,
    started_at_utc timestamptz,
    completed_at_utc timestamptz,
    attempt_count integer NOT NULL DEFAULT 0,
    error_reason text
);

CREATE TABLE ocr_runs (
    ocr_run_id uuid PRIMARY KEY,
    ocr_job_id uuid NOT NULL REFERENCES ocr_jobs(ocr_job_id),
    ingestion_id uuid NOT NULL REFERENCES uploads(ingestion_id),
    engine text NOT NULL CHECK (engine IN ('paddleocr', 'openpyxl')),
    engine_version text NOT NULL,
    configuration_json jsonb NOT NULL,
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    raw_output_sha256 text CHECK (raw_output_sha256 ~ '^[0-9a-f]{64}$'),
    raw_artifact_key text,
    status text NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    started_at_utc timestamptz NOT NULL,
    completed_at_utc timestamptz
);

CREATE TABLE silver_drafts (
    silver_draft_id uuid PRIMARY KEY,
    ingestion_id uuid NOT NULL REFERENCES uploads(ingestion_id),
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    ocr_run_id uuid NOT NULL REFERENCES ocr_runs(ocr_run_id),
    status text NOT NULL CHECK (status IN ('DRAFT_UNVERIFIED', 'REVIEWED')),
    extracted_text text NOT NULL,
    text_blocks_json jsonb NOT NULL,
    source_file_type text NOT NULL CHECK (source_file_type IN ('PHOTO', 'PDF', 'EXCEL')),
    document_category text NOT NULL,
    extraction_engine text NOT NULL,
    extraction_engine_version text NOT NULL,
    created_at_utc timestamptz NOT NULL
);

CREATE TABLE review_decisions (
    review_decision_id uuid PRIMARY KEY,
    silver_draft_id uuid NOT NULL REFERENCES silver_drafts(silver_draft_id),
    ingestion_id uuid NOT NULL REFERENCES uploads(ingestion_id),
    reviewer_user_id text NOT NULL REFERENCES users(user_id),
    reviewed_at_utc timestamptz NOT NULL,
    decision text NOT NULL CHECK (decision IN (
        'APPROVED_NO_CHANGES', 'APPROVED_WITH_CORRECTIONS', 'REJECTED_UNREADABLE',
        'REJECTED_WRONG_DOCUMENT_TYPE', 'REQUIRES_REUPLOAD'
    )),
    explicit_confirmation boolean NOT NULL CHECK (explicit_confirmation),
    correction_summary text NOT NULL,
    self_review_detected boolean NOT NULL,
    solo_exception_applied boolean NOT NULL,
    administrator_exception_reason text
);

CREATE TABLE silver_verified_records (
    silver_record_id uuid PRIMARY KEY,
    silver_revision integer NOT NULL CHECK (silver_revision > 0),
    ingestion_id uuid NOT NULL REFERENCES uploads(ingestion_id),
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status = 'VERIFIED'),
    verified_text text NOT NULL,
    reviewer_user_id text NOT NULL REFERENCES users(user_id),
    reviewed_at_utc timestamptz NOT NULL,
    review_decision text NOT NULL CHECK (review_decision IN ('APPROVED_NO_CHANGES', 'APPROVED_WITH_CORRECTIONS')),
    correction_summary text NOT NULL,
    source_object_key text NOT NULL,
    ocr_artifact_key text NOT NULL,
    review_decision_id uuid NOT NULL REFERENCES review_decisions(review_decision_id),
    UNIQUE (ingestion_id, silver_revision)
);

CREATE TABLE audit_events (
    event_id uuid PRIMARY KEY,
    occurred_at_utc timestamptz NOT NULL,
    actor_user_id text REFERENCES users(user_id),
    system_actor text,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    event_type text NOT NULL,
    previous_state text,
    new_state text,
    request_id uuid NOT NULL,
    details_json jsonb NOT NULL,
    CHECK ((actor_user_id IS NOT NULL) <> (system_actor IS NOT NULL))
);
CREATE INDEX audit_entity_idx ON audit_events (entity_type, entity_id, occurred_at_utc);

CREATE FUNCTION reject_immutable_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER bronze_objects_append_only
BEFORE UPDATE OR DELETE ON bronze_objects
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER verified_records_append_only
BEFORE UPDATE OR DELETE ON silver_verified_records
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER review_decisions_append_only
BEFORE UPDATE OR DELETE ON review_decisions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

COMMIT;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO smartcoat_app;
GRANT SELECT, INSERT ON users, uploads, ocr_jobs, ocr_runs, silver_drafts TO smartcoat_app;
GRANT UPDATE (display_name, email, active) ON users TO smartcoat_app;
GRANT UPDATE (state) ON uploads TO smartcoat_app;
GRANT UPDATE (status, started_at_utc, completed_at_utc, attempt_count, error_reason) ON ocr_jobs TO smartcoat_app;
GRANT UPDATE (status, raw_output_sha256, raw_artifact_key, completed_at_utc) ON ocr_runs TO smartcoat_app;
GRANT UPDATE (status) ON silver_drafts TO smartcoat_app;
GRANT SELECT, INSERT ON bronze_objects, silver_verified_records, review_decisions, audit_events TO smartcoat_app;
