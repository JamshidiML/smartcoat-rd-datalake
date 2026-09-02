# OCR failure recovery

## Meaning of `OCR_FAILED`

`OCR_FAILED` means that an OCR attempt against the exact committed Bronze
original did not produce a reviewable Silver draft. It is a recoverable
processing state, not a data-loss state.

The Bronze original and its manifest remain immutable, retention-protected,
and Legal Hold ON according to their accepted retention class. The committed
Bronze pair is not replaced or recommitted during recovery. No evidence is
lost when OCR fails.

Deleting the failed item, shortening its retention deadline, releasing its
Legal Hold, or otherwise weakening its storage protection is never an OCR
recovery action.

## Bounded operator retry

An authenticated operator may explicitly request a retry:

```text
POST /api/uploads/{ingestion_id}/retry-ocr
Authorization: Bearer <operator-session-token>
```

The request changes only `OCR_FAILED` to `OCR_QUEUED` and reuses the existing
`ocr_jobs` row. It does not create a second job, OCR run, Silver draft, Bronze
object, or Bronze pair. Retry initiation and state transition are appended to
the audit history with the operator identity, attempt count, prior failure,
and exact Bronze original version.

`OCR_MAX_ATTEMPTS` controls the maximum number of OCR attempts. It is read once
when the API starts, accepts only a positive integer, and defaults safely to
`3` when absent, malformed, zero, or negative. When the configured limit is
reached, another retry is rejected and the item remains discoverable in
`OCR_FAILED` with its last failed job and provenance intact.

## When retries are exhausted

For the R&D pilot, an operator must not force the item into a Silver or review
state and must not fabricate a successful OCR run. The operator may either:

1. leave the protected item in `OCR_FAILED`; or
2. create a new source file outside the platform, then upload it as a separate
   ingestion with an explicit relationship to the failed ingestion.

For option 2, put this marker at the start of the new upload's context note:

```text
OCR_FAILED_REUPLOAD_OF=<original-ingestion-id>
```

Follow it with a short explanation of whether the new file is a recapture,
re-export, or external human transcription. The context note becomes part of
the new immutable manifest, making the operator-declared relationship
discoverable during later review.

A re-upload always creates a second Bronze original and manifest with an
independent retention obligation and Legal Hold. It does not replace or reduce
the protection of the failed ingestion.

If the uploaded bytes are identical, existing SHA-256 duplicate detection also
sets `duplicate_of_ingestion_id` to the first matching non-rejected ingestion.
If the bytes differ because the source was recaptured, re-exported, or
transcribed, checksum detection cannot establish that relationship. The
context-note marker is therefore mandatory for those files. The current pilot
has no separate first-class relationship field for changed-byte re-uploads;
operators must not imply that checksum linkage covers them.

## Deferred human-transcription path

The pilot deliberately does not provide `OCR_FAILED → SILVER_DRAFT_READY`.
`silver_drafts` requires a real `ocr_runs` row, and the platform never invents
OCR provenance. P2 will model a non-OCR Silver-draft origin so human
transcription can become a first-class, provenance-preserving workflow.
