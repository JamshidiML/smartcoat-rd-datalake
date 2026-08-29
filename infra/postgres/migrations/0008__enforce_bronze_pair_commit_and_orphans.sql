BEGIN;

-- BRONZE_PAIR_READY adds an exact original/manifest success boundary without
-- changing the accepted upload-state vocabulary or claiming cross-system
-- atomicity. Storage protection precedes this PostgreSQL transaction.
CREATE TABLE bronze_pairs (
    bronze_pair_id uuid PRIMARY KEY,
    ingestion_id uuid NOT NULL UNIQUE REFERENCES uploads (ingestion_id),
    original_bronze_object_id uuid NOT NULL UNIQUE REFERENCES bronze_objects (
        bronze_object_id
    ),
    manifest_bronze_object_id uuid NOT NULL UNIQUE REFERENCES bronze_objects (
        bronze_object_id
    ),
    pair_identity_sha256 text NOT NULL UNIQUE CHECK (
        pair_identity_sha256 ~ '^[0-9a-f]{64}$'
    ),
    retention_class text NOT NULL CHECK (
        retention_class IN ('permanent', 'long_term_10y', 'short_90d')
    ),
    retention_policy_version text NOT NULL REFERENCES retention_policy_versions (
        retention_policy_version
    ),
    committed_at_utc timestamptz NOT NULL,
    committed_by text NOT NULL CHECK (btrim(committed_by) <> ''),
    CHECK (original_bronze_object_id <> manifest_bronze_object_id)
);

CREATE TABLE bronze_protected_orphans (
    protected_orphan_id uuid PRIMARY KEY,
    ingestion_id uuid NOT NULL REFERENCES uploads (ingestion_id),
    bucket_name text NOT NULL,
    object_key text NOT NULL,
    object_kind text NOT NULL CHECK (object_kind IN ('ORIGINAL', 'MANIFEST')),
    object_version_id text NOT NULL CHECK (btrim(object_version_id) <> ''),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    retention_class text NOT NULL CHECK (
        retention_class IN ('permanent', 'long_term_10y', 'short_90d')
    ),
    retention_policy_version text NOT NULL REFERENCES retention_policy_versions (
        retention_policy_version
    ),
    observed_retention_mode text NOT NULL CHECK (
        observed_retention_mode = 'COMPLIANCE'
    ),
    observed_retain_until_utc timestamptz NOT NULL,
    observed_legal_hold_status text NOT NULL CHECK (
        observed_legal_hold_status IN ('ON', 'OFF')
    ),
    protection_verified_at_utc timestamptz NOT NULL,
    failure_stage text NOT NULL CHECK (btrim(failure_stage) <> ''),
    failure_code text NOT NULL CHECK (btrim(failure_code) <> ''),
    discovered_at_utc timestamptz NOT NULL,
    discovered_by text NOT NULL CHECK (btrim(discovered_by) <> ''),
    details_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (
        ingestion_id, bucket_name, object_key, object_version_id,
        retention_policy_version
    )
);

CREATE TABLE bronze_reconciliation_events (
    reconciliation_event_id uuid PRIMARY KEY,
    ingestion_id uuid NOT NULL REFERENCES uploads (ingestion_id),
    retry_identity_sha256 text NOT NULL CHECK (
        retry_identity_sha256 ~ '^[0-9a-f]{64}$'
    ),
    outcome text NOT NULL CHECK (
        outcome IN ('CONFIRMED_PROTECTED', 'COMPLETED_PAIR', 'CONFLICT')
    ),
    occurred_at_utc timestamptz NOT NULL,
    actor text NOT NULL CHECK (btrim(actor) <> ''),
    details_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (ingestion_id, retry_identity_sha256, outcome)
);

CREATE UNIQUE INDEX ocr_jobs_one_per_ingestion
ON ocr_jobs (ingestion_id);

CREATE FUNCTION validate_bronze_pair()
RETURNS trigger LANGUAGE plpgsql AS $bronze_pair_guard$
DECLARE
    original bronze_objects%ROWTYPE;
    manifest bronze_objects%ROWTYPE;
    original_assignment bronze_retention_assignments%ROWTYPE;
    manifest_assignment bronze_retention_assignments%ROWTYPE;
BEGIN
    SELECT * INTO original FROM bronze_objects
    WHERE bronze_object_id = NEW.original_bronze_object_id;
    SELECT * INTO manifest FROM bronze_objects
    WHERE bronze_object_id = NEW.manifest_bronze_object_id;

    IF original.bronze_object_id IS NULL
        OR manifest.bronze_object_id IS NULL
        OR original.ingestion_id <> NEW.ingestion_id
        OR manifest.ingestion_id <> NEW.ingestion_id
        OR original.object_kind <> 'ORIGINAL'
        OR manifest.object_kind <> 'MANIFEST'
        OR original.object_version_id IS NULL
        OR manifest.object_version_id IS NULL
        OR btrim(original.object_version_id) = ''
        OR btrim(manifest.object_version_id) = ''
    THEN
        RAISE EXCEPTION 'Bronze pair does not identify exact original and manifest versions';
    END IF;

    SELECT * INTO original_assignment FROM bronze_retention_assignments
    WHERE bronze_object_id = NEW.original_bronze_object_id;
    SELECT * INTO manifest_assignment FROM bronze_retention_assignments
    WHERE bronze_object_id = NEW.manifest_bronze_object_id;

    IF original_assignment.retention_assignment_id IS NULL
        OR manifest_assignment.retention_assignment_id IS NULL
        OR original_assignment.retention_class <> NEW.retention_class
        OR manifest_assignment.retention_class <> NEW.retention_class
        OR original_assignment.retention_policy_version <> NEW.retention_policy_version
        OR manifest_assignment.retention_policy_version <> NEW.retention_policy_version
        OR NOT EXISTS (
            SELECT 1 FROM bronze_retention_enforcement_evidence evidence
            WHERE evidence.retention_assignment_id =
                    original_assignment.retention_assignment_id
              AND evidence.enforcement_verification_result = 'SUCCESS'
              AND evidence.object_version_id = original.object_version_id
        )
        OR NOT EXISTS (
            SELECT 1 FROM bronze_retention_enforcement_evidence evidence
            WHERE evidence.retention_assignment_id =
                    manifest_assignment.retention_assignment_id
              AND evidence.enforcement_verification_result = 'SUCCESS'
              AND evidence.object_version_id = manifest.object_version_id
        )
    THEN
        RAISE EXCEPTION 'Bronze pair lacks matching successful protection evidence';
    END IF;

    RETURN NEW;
END;
$bronze_pair_guard$;

CREATE TRIGGER bronze_pairs_validate_exact_members
BEFORE INSERT ON bronze_pairs
FOR EACH ROW EXECUTE FUNCTION validate_bronze_pair();

CREATE FUNCTION require_bronze_pair_for_success()
RETURNS trigger LANGUAGE plpgsql AS $bronze_success_guard$
BEGIN
    IF NEW.state = 'BRONZE_COMMITTED'
        AND OLD.state <> 'BRONZE_COMMITTED'
        AND NOT EXISTS (
            SELECT 1 FROM bronze_pairs pair_record
            WHERE pair_record.ingestion_id = NEW.ingestion_id
        )
    THEN
        RAISE EXCEPTION 'BRONZE_COMMITTED requires a verified exact-version pair';
    END IF;
    RETURN NEW;
END;
$bronze_success_guard$;

CREATE TRIGGER uploads_require_bronze_pair_for_success
BEFORE UPDATE OF state ON uploads
FOR EACH ROW EXECUTE FUNCTION require_bronze_pair_for_success();

CREATE FUNCTION require_bronze_pair_for_ocr_job()
RETURNS trigger LANGUAGE plpgsql AS $bronze_ocr_guard$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM uploads upload_record
        JOIN bronze_pairs pair_record USING (ingestion_id)
        WHERE upload_record.ingestion_id = NEW.ingestion_id
          AND upload_record.state IN ('BRONZE_COMMITTED', 'OCR_QUEUED')
    ) THEN
        RAISE EXCEPTION 'OCR job requires a committed exact-version Bronze pair';
    END IF;
    RETURN NEW;
END;
$bronze_ocr_guard$;

CREATE TRIGGER ocr_jobs_require_bronze_pair
BEFORE INSERT ON ocr_jobs
FOR EACH ROW EXECUTE FUNCTION require_bronze_pair_for_ocr_job();

CREATE TRIGGER bronze_pairs_append_only
BEFORE UPDATE OR DELETE ON bronze_pairs
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER bronze_protected_orphans_append_only
BEFORE UPDATE OR DELETE ON bronze_protected_orphans
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER bronze_reconciliation_events_append_only
BEFORE UPDATE OR DELETE ON bronze_reconciliation_events
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

REVOKE ALL PRIVILEGES ON TABLE
    bronze_pairs,
    bronze_protected_orphans,
    bronze_reconciliation_events
FROM PUBLIC, smartcoat_app, smartcoat_ingestion, smartcoat_ocr,
    smartcoat_review, smartcoat_backup;

GRANT SELECT, INSERT ON TABLE
    bronze_pairs,
    bronze_protected_orphans,
    bronze_reconciliation_events
TO smartcoat_ingestion;

-- OCR resolves its queued source through the committed pair and reads only the
-- exact original version. It receives no Bronze write or orphan authority.
GRANT SELECT ON TABLE
    bronze_objects,
    bronze_pairs
TO smartcoat_ocr;

GRANT SELECT ON TABLE
    bronze_pairs,
    bronze_protected_orphans,
    bronze_reconciliation_events
TO smartcoat_backup;

REVOKE EXECUTE ON FUNCTION validate_bronze_pair() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION require_bronze_pair_for_success() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION require_bronze_pair_for_ocr_job() FROM PUBLIC;

COMMIT;
