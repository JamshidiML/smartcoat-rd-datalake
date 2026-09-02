BEGIN;

-- The OCR worker formerly held table-wide Bronze read authority.  Its complete
-- active path needs only pair joins, exact-version lookup, and get_upload().
REVOKE SELECT ON TABLE bronze_objects FROM smartcoat_ocr, smartcoat_review;

GRANT SELECT (
    bronze_object_id,
    ingestion_id,
    object_kind,
    object_version_id
) ON bronze_objects TO smartcoat_ocr;

-- Review resolves the immutable source version before approving or rejecting a
-- draft.  It receives no object identifier, storage location, digest, retention,
-- or mutation authority beyond the three columns used by get_upload().
GRANT SELECT (
    ingestion_id,
    object_kind,
    object_version_id
) ON bronze_objects TO smartcoat_review;

COMMIT;
