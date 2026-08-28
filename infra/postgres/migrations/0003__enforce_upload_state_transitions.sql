-- M0-R03: one database-owned legal transition graph for upload lifecycle state.
--
-- The trigger evaluates OLD.state from the row that PostgreSQL has locked.  A
-- caller-provided expected state can therefore provide compare-and-swap
-- behavior, but it cannot authorize an edge that is absent from this graph.

CREATE SCHEMA smartcoat_state;

REVOKE ALL ON SCHEMA smartcoat_state FROM PUBLIC;

CREATE TABLE smartcoat_state.legal_upload_transitions (
    previous_state text NOT NULL,
    next_state text NOT NULL,
    transition_name text NOT NULL UNIQUE,
    PRIMARY KEY (previous_state, next_state),
    CHECK (previous_state <> next_state)
);

INSERT INTO smartcoat_state.legal_upload_transitions (
    previous_state,
    next_state,
    transition_name
) VALUES
    ('RECEIVED', 'BRONZE_COMMITTED', 'commit_verified_bronze_pair'),
    ('RECEIVED', 'REJECTED', 'reject_received_upload'),
    ('BRONZE_COMMITTED', 'OCR_QUEUED', 'queue_ocr'),
    ('OCR_QUEUED', 'OCR_COMPLETED', 'complete_ocr'),
    ('OCR_QUEUED', 'OCR_FAILED', 'fail_ocr'),
    ('OCR_COMPLETED', 'SILVER_DRAFT_READY', 'publish_unverified_draft'),
    ('SILVER_DRAFT_READY', 'UNDER_HUMAN_REVIEW', 'begin_human_review'),
    ('UNDER_HUMAN_REVIEW', 'VERIFIED', 'verify_reviewed_draft'),
    ('UNDER_HUMAN_REVIEW', 'REVIEW_REJECTED', 'reject_reviewed_draft'),
    ('VERIFIED', 'UNDER_HUMAN_REVIEW', 'begin_verified_revision_review');

CREATE FUNCTION smartcoat_state.reject_transition_contract_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $guard$
BEGIN
    RAISE EXCEPTION 'legal_upload_transitions is immutable'
        USING ERRCODE = '55000';
END;
$guard$;

CREATE TRIGGER legal_upload_transitions_immutable
BEFORE UPDATE OR DELETE ON smartcoat_state.legal_upload_transitions
FOR EACH ROW
EXECUTE FUNCTION smartcoat_state.reject_transition_contract_mutation();

CREATE FUNCTION smartcoat_state.enforce_upload_state_transition()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, smartcoat_state
AS $transition$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'RECEIVED' THEN
            RAISE EXCEPTION 'upload initial state must be RECEIVED, got %', NEW.state
                USING ERRCODE = '23514',
                      CONSTRAINT = 'uploads_legal_initial_state';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.state IS NOT DISTINCT FROM OLD.state THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM smartcoat_state.legal_upload_transitions AS legal
        WHERE legal.previous_state = OLD.state
          AND legal.next_state = NEW.state
    ) THEN
        RAISE EXCEPTION 'illegal upload state transition: % -> %', OLD.state, NEW.state
            USING ERRCODE = '23514',
                  CONSTRAINT = 'uploads_legal_state_transition';
    END IF;

    RETURN NEW;
END;
$transition$;

CREATE TRIGGER uploads_initial_state_guard
BEFORE INSERT ON public.uploads
FOR EACH ROW
EXECUTE FUNCTION smartcoat_state.enforce_upload_state_transition();

CREATE TRIGGER uploads_state_transition_guard
BEFORE UPDATE OF state ON public.uploads
FOR EACH ROW
EXECUTE FUNCTION smartcoat_state.enforce_upload_state_transition();

REVOKE ALL ON TABLE smartcoat_state.legal_upload_transitions FROM PUBLIC;
REVOKE ALL ON FUNCTION smartcoat_state.reject_transition_contract_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION smartcoat_state.enforce_upload_state_transition() FROM PUBLIC;
