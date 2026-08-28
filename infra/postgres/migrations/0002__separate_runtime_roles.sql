-- M0-R02: replace the shared PostgreSQL runtime login with workflow identities.
--
-- Password material is deliberately absent.  A separate backend-only one-shot
-- provisioner sets credentials after this transactional role/grant migration.

DO $roles$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'smartcoat_ingestion',
        'smartcoat_ocr',
        'smartcoat_review',
        'smartcoat_backup'
    ]
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
            RAISE EXCEPTION 'M0-R02 runtime role collision: %', role_name;
        END IF;
        EXECUTE format(
            'CREATE ROLE %I LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
            role_name
        );
    END LOOP;
END
$roles$;

-- The bootstrap-only shared identity remains as historical catalog evidence,
-- but cannot log in and retains no application-object or database authority
-- after this migration.  Revocation also removes its table authority from
-- already-open legacy sessions immediately.
ALTER ROLE smartcoat_app NOLOGIN PASSWORD NULL;

DO $database_privileges$
BEGIN
    EXECUTE format(
        'REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC',
        current_database()
    );
    EXECUTE format(
        'REVOKE ALL PRIVILEGES ON DATABASE %I FROM smartcoat_app',
        current_database()
    );
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO smartcoat_ingestion, smartcoat_ocr, smartcoat_review, smartcoat_backup',
        current_database()
    );
END
$database_privileges$;

REVOKE ALL ON SCHEMA public FROM smartcoat_app;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM smartcoat_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM smartcoat_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM smartcoat_app;

REVOKE ALL ON SCHEMA public
FROM smartcoat_ingestion, smartcoat_ocr, smartcoat_review, smartcoat_backup;
REVOKE ALL ON ALL TABLES IN SCHEMA public
FROM smartcoat_ingestion, smartcoat_ocr, smartcoat_review, smartcoat_backup;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public
FROM smartcoat_ingestion, smartcoat_ocr, smartcoat_review, smartcoat_backup;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public
FROM smartcoat_ingestion, smartcoat_ocr, smartcoat_review, smartcoat_backup;

GRANT USAGE ON SCHEMA public
TO smartcoat_ingestion, smartcoat_ocr, smartcoat_review, smartcoat_backup;

GRANT SELECT ON
    users, uploads, bronze_objects, ocr_jobs, ocr_runs, silver_drafts,
    review_decisions, silver_verified_records, audit_events
TO smartcoat_ingestion;
GRANT INSERT ON users, uploads, bronze_objects, ocr_jobs, audit_events
TO smartcoat_ingestion;
GRANT UPDATE (display_name, email, active) ON users TO smartcoat_ingestion;
GRANT UPDATE (state) ON uploads TO smartcoat_ingestion;

GRANT SELECT ON uploads, ocr_jobs, ocr_runs TO smartcoat_ocr;
GRANT INSERT ON ocr_runs, silver_drafts, audit_events TO smartcoat_ocr;
GRANT UPDATE (state) ON uploads TO smartcoat_ocr;
GRANT UPDATE (
    status, started_at_utc, completed_at_utc, attempt_count, error_reason
) ON ocr_jobs TO smartcoat_ocr;
GRANT UPDATE (
    status, raw_output_sha256, raw_artifact_key, completed_at_utc
) ON ocr_runs TO smartcoat_ocr;

GRANT SELECT ON
    uploads, ocr_runs, silver_drafts, review_decisions, silver_verified_records
TO smartcoat_review;
GRANT INSERT ON
    silver_drafts, review_decisions, silver_verified_records, audit_events
TO smartcoat_review;
GRANT UPDATE (state) ON uploads TO smartcoat_review;
GRANT UPDATE (status) ON silver_drafts TO smartcoat_review;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO smartcoat_backup;

REVOKE ALL ON SCHEMA smartcoat_migrations
FROM smartcoat_app, smartcoat_ingestion, smartcoat_ocr, smartcoat_review, smartcoat_backup;
REVOKE ALL ON ALL TABLES IN SCHEMA smartcoat_migrations
FROM smartcoat_app, smartcoat_ingestion, smartcoat_ocr, smartcoat_review, smartcoat_backup;
GRANT USAGE ON SCHEMA smartcoat_migrations TO smartcoat_backup;
GRANT SELECT ON
    smartcoat_migrations.applied_migrations,
    smartcoat_migrations.adoption_decisions
TO smartcoat_backup;

-- Trigger execution does not require callers to invoke the function directly.
-- Removing PUBLIC execute authority closes an unnecessary public capability.
REVOKE EXECUTE ON FUNCTION public.reject_immutable_mutation() FROM PUBLIC;
