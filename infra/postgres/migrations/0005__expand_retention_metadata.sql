-- CAND-META / METADATA_EXPAND
--
-- This migration adds append-only policy and exact-version assignment
-- primitives.  It intentionally does not mutate legacy Bronze rows, enforce
-- MinIO retention, orchestrate the original/manifest pair, or impose the
-- final successful-record constraint.  Those operations belong to later
-- dependency-gated candidate packages.

CREATE TABLE canonical_retention_classes (
    retention_class text PRIMARY KEY CHECK (
        retention_class IN ('permanent', 'long_term_10y', 'short_90d')
    ),
    calendar_years integer,
    fixed_duration_hours integer,
    legal_hold_required boolean NOT NULL,
    CHECK (
        (retention_class = 'permanent'
            AND calendar_years = 10
            AND fixed_duration_hours IS NULL
            AND legal_hold_required)
        OR
        (retention_class = 'long_term_10y'
            AND calendar_years = 10
            AND fixed_duration_hours IS NULL
            AND NOT legal_hold_required)
        OR
        (retention_class = 'short_90d'
            AND calendar_years IS NULL
            AND fixed_duration_hours = 2160
            AND NOT legal_hold_required)
    )
);

INSERT INTO canonical_retention_classes (
    retention_class,
    calendar_years,
    fixed_duration_hours,
    legal_hold_required
) VALUES
    ('permanent', 10, NULL, true),
    ('long_term_10y', 10, NULL, false),
    ('short_90d', NULL, 2160, false);

CREATE TABLE retention_policy_versions (
    retention_policy_version text PRIMARY KEY CHECK (
        retention_policy_version ~ '^[a-z][a-z0-9_-]{2,127}$'
    ),
    policy_document_path text NOT NULL,
    policy_document_sha256 text NOT NULL CHECK (
        policy_document_sha256 ~ '^[0-9a-f]{64}$'
    ),
    approved_at_utc timestamptz NOT NULL,
    approved_by text NOT NULL CHECK (btrim(approved_by) <> '')
);

CREATE TABLE retention_category_rules (
    retention_policy_version text NOT NULL,
    data_category text NOT NULL CHECK (
        data_category ~ '^[A-Z][A-Z0-9_]{2,127}$'
    ),
    retention_class text NOT NULL REFERENCES canonical_retention_classes (
        retention_class
    ),
    records_purpose text NOT NULL CHECK (btrim(records_purpose) <> ''),
    legal_basis_classification text NOT NULL CHECK (
        btrim(legal_basis_classification) <> ''
    ),
    PRIMARY KEY (retention_policy_version, data_category),
    UNIQUE (retention_policy_version, data_category, retention_class),
    FOREIGN KEY (retention_policy_version) REFERENCES retention_policy_versions (
        retention_policy_version
    ) DEFERRABLE INITIALLY DEFERRED
);

CREATE FUNCTION reject_rule_for_approved_retention_policy()
RETURNS trigger LANGUAGE plpgsql AS $approved_policy_rule_guard$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'smartcoat.retention_policy.' || NEW.retention_policy_version,
            0
        )
    );

    IF EXISTS (
        SELECT 1
        FROM retention_policy_versions
        WHERE retention_policy_version = NEW.retention_policy_version
    ) THEN
        RAISE EXCEPTION 'Retention category rules are sealed for approved policy version';
    END IF;

    RETURN NEW;
END;
$approved_policy_rule_guard$;

CREATE FUNCTION require_rules_before_retention_policy_approval()
RETURNS trigger LANGUAGE plpgsql AS $policy_approval_guard$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'smartcoat.retention_policy.' || NEW.retention_policy_version,
            0
        )
    );

    IF NOT EXISTS (
        SELECT 1
        FROM retention_category_rules
        WHERE retention_policy_version = NEW.retention_policy_version
    ) THEN
        RAISE EXCEPTION 'Retention policy cannot be approved without category rules';
    END IF;

    RETURN NEW;
END;
$policy_approval_guard$;

CREATE TRIGGER retention_category_rules_seal_approved_version
BEFORE INSERT ON retention_category_rules
FOR EACH ROW EXECUTE FUNCTION reject_rule_for_approved_retention_policy();

CREATE TRIGGER retention_policy_versions_require_rules
BEFORE INSERT ON retention_policy_versions
FOR EACH ROW EXECUTE FUNCTION require_rules_before_retention_policy_approval();

CREATE FUNCTION retention_deadline_utc(
    declared_retention_class text,
    accepted_storage_at_utc timestamptz
)
RETURNS timestamptz
LANGUAGE sql
IMMUTABLE
STRICT
AS $deadline$
    SELECT CASE declared_retention_class
        WHEN 'permanent' THEN
            (
                make_date(
                    EXTRACT(YEAR FROM accepted_storage_at_utc AT TIME ZONE 'UTC')::integer + 10,
                    EXTRACT(MONTH FROM accepted_storage_at_utc AT TIME ZONE 'UTC')::integer,
                    LEAST(
                        EXTRACT(DAY FROM accepted_storage_at_utc AT TIME ZONE 'UTC')::integer,
                        EXTRACT(
                            DAY FROM (
                                date_trunc(
                                    'month',
                                    make_date(
                                        EXTRACT(YEAR FROM accepted_storage_at_utc AT TIME ZONE 'UTC')::integer + 10,
                                        EXTRACT(MONTH FROM accepted_storage_at_utc AT TIME ZONE 'UTC')::integer,
                                        1
                                    )::timestamp
                                ) + interval '1 month - 1 day'
                            )
                        )::integer
                    )
                )::timestamp
                + date_trunc(
                    'second',
                    accepted_storage_at_utc AT TIME ZONE 'UTC'
                )::time
            ) AT TIME ZONE 'UTC'
        WHEN 'long_term_10y' THEN
            (
                make_date(
                    EXTRACT(YEAR FROM accepted_storage_at_utc AT TIME ZONE 'UTC')::integer + 10,
                    EXTRACT(MONTH FROM accepted_storage_at_utc AT TIME ZONE 'UTC')::integer,
                    LEAST(
                        EXTRACT(DAY FROM accepted_storage_at_utc AT TIME ZONE 'UTC')::integer,
                        EXTRACT(
                            DAY FROM (
                                date_trunc(
                                    'month',
                                    make_date(
                                        EXTRACT(YEAR FROM accepted_storage_at_utc AT TIME ZONE 'UTC')::integer + 10,
                                        EXTRACT(MONTH FROM accepted_storage_at_utc AT TIME ZONE 'UTC')::integer,
                                        1
                                    )::timestamp
                                ) + interval '1 month - 1 day'
                            )
                        )::integer
                    )
                )::timestamp
                + date_trunc(
                    'second',
                    accepted_storage_at_utc AT TIME ZONE 'UTC'
                )::time
            ) AT TIME ZONE 'UTC'
        WHEN 'short_90d' THEN
            date_trunc('second', accepted_storage_at_utc) + interval '2160 hours'
        ELSE NULL
    END
$deadline$;

INSERT INTO retention_category_rules (
    retention_policy_version,
    data_category,
    retention_class,
    records_purpose,
    legal_basis_classification
) VALUES
    (
        'smartcoat_retention_2026_08_v1',
        'LAB_NOTE',
        'permanent',
        'R&D evidentiary record',
        'approved_non_personal_evidence'
    ),
    (
        'smartcoat_retention_2026_08_v1',
        'TEST_RESULT',
        'permanent',
        'R&D evidentiary record',
        'approved_non_personal_evidence'
    ),
    (
        'smartcoat_retention_2026_08_v1',
        'FORMULATION_SCREEN',
        'permanent',
        'R&D evidentiary record',
        'approved_non_personal_evidence'
    ),
    (
        'smartcoat_retention_2026_08_v1',
        'MATERIAL_DOCUMENT',
        'permanent',
        'R&D evidentiary record',
        'approved_non_personal_evidence'
    ),
    (
        'smartcoat_retention_2026_08_v1',
        'TRIAL_VIDEO',
        'permanent',
        'R&D trial-video evidence',
        'approved_non_personal_evidence'
    ),
    (
        'smartcoat_retention_2026_08_v1',
        'PLATFORM_OPERATIONAL_LOG',
        'short_90d',
        'Platform operational health',
        'approved_operational_record'
    ),
    (
        'smartcoat_retention_2026_08_v1',
        'PLATFORM_DEBUG_LOG',
        'short_90d',
        'Platform troubleshooting',
        'approved_operational_record'
    );

-- A policy version becomes approved only after its complete rule set exists.
-- The deferred foreign key permits this transaction-local population order;
-- the paired advisory-lock triggers serialize rule insertion with approval.
INSERT INTO retention_policy_versions (
    retention_policy_version,
    policy_document_path,
    policy_document_sha256,
    approved_at_utc,
    approved_by
) VALUES (
    'smartcoat_retention_2026_08_v1',
    'docs/architecture/decisions/ADR-0002-retention-semantics-and-enforcement-contract.md',
    '307ce9d9484b3819d16c5178a3dc61fb56e257376779e679e4923b1e7f5beb37',
    TIMESTAMPTZ '2026-08-20T00:00:00Z',
    'ratified_architecture_decision'
);

CREATE TABLE bronze_retention_assignments (
    retention_assignment_id uuid PRIMARY KEY,
    bronze_object_id uuid NOT NULL UNIQUE REFERENCES bronze_objects (
        bronze_object_id
    ),
    ingestion_id uuid NOT NULL REFERENCES uploads (ingestion_id),
    bucket_name text NOT NULL,
    object_key text NOT NULL,
    object_kind text NOT NULL CHECK (object_kind IN ('ORIGINAL', 'MANIFEST')),
    object_version_id text NOT NULL CHECK (btrim(object_version_id) <> ''),
    data_category text NOT NULL,
    retention_class text NOT NULL,
    retention_policy_version text NOT NULL,
    retention_assigned_at_utc timestamptz NOT NULL,
    retention_assigned_by text NOT NULL CHECK (btrim(retention_assigned_by) <> ''),
    accepted_storage_at_utc timestamptz NOT NULL,
    expected_retain_until_utc timestamptz NOT NULL,
    legal_hold_required boolean NOT NULL,
    FOREIGN KEY (
        retention_policy_version,
        data_category,
        retention_class
    ) REFERENCES retention_category_rules (
        retention_policy_version,
        data_category,
        retention_class
    ),
    UNIQUE (ingestion_id, object_kind),
    UNIQUE (bucket_name, object_key, object_version_id),
    CHECK (
        (retention_class = 'permanent' AND legal_hold_required)
        OR
        (retention_class IN ('long_term_10y', 'short_90d') AND NOT legal_hold_required)
    ),
    CHECK (
        expected_retain_until_utc = retention_deadline_utc(
            retention_class,
            accepted_storage_at_utc
        )
    ),
    CONSTRAINT bronze_retention_assignments_whole_second_anchor CHECK (
        accepted_storage_at_utc = date_trunc(
            'second',
            accepted_storage_at_utc
        )
    )
);

CREATE FUNCTION validate_bronze_retention_assignment()
RETURNS trigger LANGUAGE plpgsql AS $assignment_guard$
DECLARE
    bronze_record bronze_objects%ROWTYPE;
BEGIN
    SELECT *
    INTO bronze_record
    FROM bronze_objects
    WHERE bronze_object_id = NEW.bronze_object_id
    FOR KEY SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Bronze object does not exist for retention assignment';
    END IF;

    IF bronze_record.ingestion_id <> NEW.ingestion_id
        OR bronze_record.bucket_name <> NEW.bucket_name
        OR bronze_record.object_key <> NEW.object_key
        OR bronze_record.object_kind <> NEW.object_kind
        OR bronze_record.object_version_id IS NULL
        OR btrim(bronze_record.object_version_id) = ''
        OR bronze_record.object_version_id <> NEW.object_version_id
    THEN
        RAISE EXCEPTION 'Retention assignment does not match exact Bronze version identity';
    END IF;

    RETURN NEW;
END;
$assignment_guard$;

CREATE TRIGGER bronze_retention_assignment_identity
BEFORE INSERT ON bronze_retention_assignments
FOR EACH ROW EXECUTE FUNCTION validate_bronze_retention_assignment();

CREATE TRIGGER canonical_retention_classes_append_only
BEFORE UPDATE OR DELETE ON canonical_retention_classes
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER retention_policy_versions_append_only
BEFORE UPDATE OR DELETE ON retention_policy_versions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER retention_category_rules_append_only
BEFORE UPDATE OR DELETE ON retention_category_rules
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

CREATE TRIGGER bronze_retention_assignments_append_only
BEFORE UPDATE OR DELETE ON bronze_retention_assignments
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();
