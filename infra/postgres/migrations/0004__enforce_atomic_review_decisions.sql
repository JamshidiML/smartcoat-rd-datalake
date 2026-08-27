-- M0-R04: database guards for one effective review outcome per Silver draft.
--
-- Historical review evidence remains append-only. Existing rows predate the
-- deterministic request fingerprint and therefore retain NULL in that column;
-- every review written by the M0-R04 application boundary supplies a SHA-256.

ALTER TABLE public.review_decisions
    ADD COLUMN review_request_sha256 text;

ALTER TABLE public.review_decisions
    ADD CONSTRAINT review_decisions_request_sha256_format
    CHECK (
        review_request_sha256 IS NULL
        OR review_request_sha256 ~ '^[0-9a-f]{64}$'
    );

-- NOT VALID preserves readable pre-M0-R04 history while PostgreSQL still
-- enforces the predicate for every row inserted or updated after this
-- migration.  New review operations therefore cannot omit their retry key.
ALTER TABLE public.review_decisions
    ADD CONSTRAINT review_decisions_request_sha256_required_for_new_rows
    CHECK (review_request_sha256 IS NOT NULL) NOT VALID;

CREATE UNIQUE INDEX review_decisions_one_per_draft_uidx
    ON public.review_decisions (silver_draft_id);

CREATE UNIQUE INDEX silver_verified_records_one_per_decision_uidx
    ON public.silver_verified_records (review_decision_id);

COMMENT ON COLUMN public.review_decisions.review_request_sha256 IS
    'M0-R04 deterministic fingerprint for exact retry detection; NULL only for pre-M0-R04 history';
