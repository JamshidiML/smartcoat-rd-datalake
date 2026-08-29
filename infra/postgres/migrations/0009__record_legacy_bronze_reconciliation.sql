BEGIN;

CREATE TABLE legacy_reconciliation_runs (
    reconciliation_run_id uuid PRIMARY KEY,
    inventory_sha256 text NOT NULL CHECK (inventory_sha256 ~ '^[0-9a-f]{64}$'),
    outcome_sha256 text NOT NULL CHECK (outcome_sha256 ~ '^[0-9a-f]{64}$'),
    remediation_policy_version text NOT NULL,
    started_at_utc timestamptz NOT NULL,
    completed_at_utc timestamptz NOT NULL,
    database_row_count integer NOT NULL CHECK (database_row_count >= 0),
    storage_version_count integer NOT NULL CHECK (storage_version_count >= 0),
    delete_marker_count integer NOT NULL CHECK (delete_marker_count >= 0),
    reconciled_entity_count integer NOT NULL CHECK (reconciled_entity_count >= 0),
    quarantined_entity_count integer NOT NULL CHECK (quarantined_entity_count >= 0),
    summary_json jsonb NOT NULL,
    executed_by text NOT NULL CHECK (btrim(executed_by) <> ''),
    UNIQUE (inventory_sha256, remediation_policy_version)
);

CREATE TABLE legacy_reconciliation_items (
    reconciliation_item_id uuid PRIMARY KEY,
    reconciliation_run_id uuid NOT NULL REFERENCES legacy_reconciliation_runs (
        reconciliation_run_id
    ),
    entity_type text NOT NULL CHECK (
        entity_type IN ('DATABASE_ROW', 'STORAGE_VERSION', 'DELETE_MARKER')
    ),
    entity_identity_sha256 text NOT NULL CHECK (
        entity_identity_sha256 ~ '^[0-9a-f]{64}$'
    ),
    bronze_object_id uuid REFERENCES bronze_objects (bronze_object_id),
    ingestion_id uuid REFERENCES uploads (ingestion_id),
    bucket_name text,
    object_key text,
    object_kind text CHECK (object_kind IN ('ORIGINAL', 'MANIFEST')),
    object_version_id text,
    content_sha256 text CHECK (
        content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    mapping_identity_sha256 text CHECK (
        mapping_identity_sha256 IS NULL
        OR mapping_identity_sha256 ~ '^[0-9a-f]{64}$'
    ),
    matching_basis text,
    prior_retention_mode text,
    prior_retain_until_utc timestamptz,
    prior_legal_hold_status text CHECK (
        prior_legal_hold_status IS NULL
        OR prior_legal_hold_status IN ('ON', 'OFF', 'UNAVAILABLE')
    ),
    requested_retention_mode text,
    requested_retain_until_utc timestamptz,
    requested_legal_hold_status text CHECK (
        requested_legal_hold_status IS NULL
        OR requested_legal_hold_status = 'ON'
    ),
    observed_retention_mode text,
    observed_retain_until_utc timestamptz,
    observed_legal_hold_status text CHECK (
        observed_legal_hold_status IS NULL
        OR observed_legal_hold_status IN ('ON', 'OFF', 'UNAVAILABLE')
    ),
    outcome text NOT NULL CHECK (outcome IN ('RECONCILED', 'QUARANTINED')),
    classification text NOT NULL CHECK (btrim(classification) <> ''),
    attempt_identity_sha256 text NOT NULL CHECK (
        attempt_identity_sha256 ~ '^[0-9a-f]{64}$'
    ),
    recorded_at_utc timestamptz NOT NULL,
    details_json jsonb NOT NULL,
    UNIQUE (reconciliation_run_id, entity_identity_sha256)
);

CREATE TABLE legacy_reconciliation_successes (
    mapping_identity_sha256 text PRIMARY KEY CHECK (
        mapping_identity_sha256 ~ '^[0-9a-f]{64}$'
    ),
    reconciliation_run_id uuid NOT NULL REFERENCES legacy_reconciliation_runs (
        reconciliation_run_id
    ),
    bronze_object_id uuid NOT NULL UNIQUE REFERENCES bronze_objects (bronze_object_id),
    ingestion_id uuid NOT NULL REFERENCES uploads (ingestion_id),
    bucket_name text NOT NULL,
    object_key text NOT NULL,
    object_kind text NOT NULL CHECK (object_kind IN ('ORIGINAL', 'MANIFEST')),
    object_version_id text NOT NULL CHECK (btrim(object_version_id) <> ''),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    matching_basis text NOT NULL,
    retention_class text NOT NULL CHECK (retention_class = 'permanent'),
    remediation_policy_version text NOT NULL,
    requested_retention_mode text NOT NULL CHECK (requested_retention_mode = 'COMPLIANCE'),
    requested_retain_until_utc timestamptz NOT NULL,
    requested_legal_hold_status text NOT NULL CHECK (requested_legal_hold_status = 'ON'),
    observed_retention_mode text NOT NULL CHECK (observed_retention_mode = 'COMPLIANCE'),
    observed_retain_until_utc timestamptz NOT NULL,
    observed_legal_hold_status text NOT NULL CHECK (observed_legal_hold_status = 'ON'),
    reconciled_at_utc timestamptz NOT NULL,
    details_json jsonb NOT NULL,
    UNIQUE (bucket_name, object_key, object_version_id)
);

CREATE TRIGGER legacy_reconciliation_runs_append_only
BEFORE UPDATE OR DELETE ON legacy_reconciliation_runs
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER legacy_reconciliation_items_append_only
BEFORE UPDATE OR DELETE ON legacy_reconciliation_items
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER legacy_reconciliation_successes_append_only
BEFORE UPDATE OR DELETE ON legacy_reconciliation_successes
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

REVOKE ALL PRIVILEGES ON TABLE
    legacy_reconciliation_runs,
    legacy_reconciliation_items,
    legacy_reconciliation_successes
FROM PUBLIC, smartcoat_app, smartcoat_ingestion, smartcoat_ocr,
    smartcoat_review, smartcoat_backup;

GRANT SELECT ON TABLE
    legacy_reconciliation_runs,
    legacy_reconciliation_items,
    legacy_reconciliation_successes
TO smartcoat_backup;

COMMIT;
