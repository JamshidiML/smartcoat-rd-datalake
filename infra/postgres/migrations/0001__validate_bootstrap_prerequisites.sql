-- M0-R01.1 migration-foundation baseline validation marker.
--
-- infra/postgres/init.sql remains the authoritative first-bootstrap definition.
-- This marker is read-only with respect to application business schema: it only
-- verifies that the bootstrap tables exist. Existing-volume adoption and the
-- decision to record this baseline on an existing database belong to M0-R01.2.
-- M0-R01.1 does not authorize recording this baseline on an unmanaged database.

DO $migration$
DECLARE
    missing_tables text[];
BEGIN
    SELECT array_agg(required_table ORDER BY required_table)
    INTO missing_tables
    FROM unnest(ARRAY[
        'users',
        'uploads',
        'bronze_objects',
        'ocr_jobs',
        'ocr_runs',
        'silver_drafts',
        'review_decisions',
        'silver_verified_records',
        'audit_events'
    ]) AS required_table
    WHERE to_regclass('public.' || quote_ident(required_table)) IS NULL;

    IF missing_tables IS NOT NULL THEN
        RAISE EXCEPTION
            'Bootstrap prerequisite tables are missing: %',
            array_to_string(missing_tables, ', ');
    END IF;
END
$migration$;
