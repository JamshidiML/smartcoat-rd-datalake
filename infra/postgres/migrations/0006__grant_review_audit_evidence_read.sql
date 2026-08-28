-- M0-R02/R04 compatibility proposal: allow the review boundary to
-- authenticate an exact completed retry from append-only audit evidence.
-- This is deliberately column-level read authority, not table-level access.

GRANT SELECT (entity_type, entity_id, event_type, details_json, new_state)
ON audit_events TO smartcoat_review;
