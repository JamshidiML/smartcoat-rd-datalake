BEGIN;

-- OCR failure preserves the committed Bronze pair and requires an explicit,
-- audited operator action before the existing job can be retried. A direct
-- human-review edge is intentionally absent: silver_drafts requires a real
-- ocr_runs row and the platform must not fabricate successful OCR provenance.
INSERT INTO smartcoat_state.legal_upload_transitions (
    previous_state,
    next_state,
    transition_name
) VALUES ('OCR_FAILED', 'OCR_QUEUED', 'operator_retry_failed_ocr');

COMMIT;
