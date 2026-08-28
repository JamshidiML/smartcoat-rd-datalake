BEGIN;

-- RETENTION_ENFORCEMENT_READY records only exact-version observations.  It
-- does not change Bronze commit orchestration or repair legacy rows.
CREATE TABLE bronze_retention_enforcement_evidence (
    enforcement_evidence_id uuid PRIMARY KEY,
    retention_assignment_id uuid NOT NULL REFERENCES bronze_retention_assignments (
        retention_assignment_id
    ),
    bucket_name text NOT NULL,
    object_key text NOT NULL,
    object_kind text NOT NULL CHECK (object_kind IN ('ORIGINAL', 'MANIFEST')),
    object_version_id text NOT NULL CHECK (btrim(object_version_id) <> ''),
    data_category text NOT NULL,
    retention_class text NOT NULL CHECK (
        retention_class IN ('permanent', 'long_term_10y', 'short_90d')
    ),
    retention_policy_version text NOT NULL,
    accepted_storage_at_utc timestamptz NOT NULL,
    requested_retention_mode text NOT NULL CHECK (
        requested_retention_mode = 'COMPLIANCE'
    ),
    requested_retain_until_utc timestamptz NOT NULL,
    requested_legal_hold_status text NOT NULL CHECK (
        requested_legal_hold_status IN ('ON', 'UNCHANGED')
    ),
    observed_object_version_id text,
    observed_retention_mode text,
    observed_retain_until_utc timestamptz,
    observed_legal_hold_status text CHECK (
        observed_legal_hold_status IN ('ON', 'OFF')
    ),
    enforcement_verified_at_utc timestamptz NOT NULL,
    enforcement_verification_result text NOT NULL CHECK (
        enforcement_verification_result IN (
            'SUCCESS', 'MISMATCH', 'UNAVAILABLE', 'QUARANTINE'
        )
    ),
    failure_code text,
    enforced_by text NOT NULL CHECK (btrim(enforced_by) <> ''),
    details_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT retention_evidence_whole_second_times CHECK (
        accepted_storage_at_utc = date_trunc('second', accepted_storage_at_utc)
        AND requested_retain_until_utc = date_trunc('second', requested_retain_until_utc)
        AND (
            observed_retain_until_utc IS NULL
            OR observed_retain_until_utc = date_trunc('second', observed_retain_until_utc)
        )
    ),
    CONSTRAINT retention_evidence_result_shape CHECK (
        (
            enforcement_verification_result = 'SUCCESS'
            AND failure_code IS NULL
            AND observed_object_version_id = object_version_id
            AND observed_retention_mode = 'COMPLIANCE'
            AND observed_retain_until_utc >= requested_retain_until_utc
            AND observed_legal_hold_status IS NOT NULL
            AND (
                retention_class <> 'permanent'
                OR (
                    requested_legal_hold_status = 'ON'
                    AND observed_legal_hold_status = 'ON'
                )
            )
        )
        OR (
            enforcement_verification_result <> 'SUCCESS'
            AND failure_code IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX bronze_retention_enforcement_one_success
ON bronze_retention_enforcement_evidence (
    retention_assignment_id,
    retention_policy_version
)
WHERE enforcement_verification_result = 'SUCCESS';

CREATE FUNCTION validate_bronze_retention_enforcement_evidence()
RETURNS trigger LANGUAGE plpgsql AS $retention_enforcement_identity_guard$
DECLARE
    assignment bronze_retention_assignments%ROWTYPE;
BEGIN
    SELECT *
    INTO assignment
    FROM bronze_retention_assignments
    WHERE retention_assignment_id = NEW.retention_assignment_id
    FOR KEY SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Retention assignment does not exist for enforcement evidence';
    END IF;

    IF assignment.bucket_name <> NEW.bucket_name
        OR assignment.object_key <> NEW.object_key
        OR assignment.object_kind <> NEW.object_kind
        OR assignment.object_version_id <> NEW.object_version_id
        OR assignment.data_category <> NEW.data_category
        OR assignment.retention_class <> NEW.retention_class
        OR assignment.retention_policy_version <> NEW.retention_policy_version
        OR assignment.accepted_storage_at_utc <> NEW.accepted_storage_at_utc
        OR assignment.expected_retain_until_utc > NEW.requested_retain_until_utc
    THEN
        RAISE EXCEPTION 'Enforcement evidence does not match exact retention assignment';
    END IF;

    RETURN NEW;
END;
$retention_enforcement_identity_guard$;

CREATE TRIGGER bronze_retention_enforcement_identity
BEFORE INSERT ON bronze_retention_enforcement_evidence
FOR EACH ROW EXECUTE FUNCTION validate_bronze_retention_enforcement_evidence();

CREATE TRIGGER bronze_retention_enforcement_evidence_append_only
BEFORE UPDATE OR DELETE ON bronze_retention_enforcement_evidence
FOR EACH ROW EXECUTE FUNCTION reject_immutable_mutation();

GRANT SELECT, INSERT ON bronze_retention_enforcement_evidence TO smartcoat_app;

COMMIT;
