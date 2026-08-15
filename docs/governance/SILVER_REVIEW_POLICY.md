# Silver human-review policy

## Authority

Machine extraction is not factual evidence. PaddleOCR confidence values are triage signals only, and neither a high score nor a successful job can create `VERIFIED`. A reviewer must see the immutable source, edit or confirm the transcription, select a decision, and check the source-comparison confirmation.

Approved output is a new `silver_verified_records` row. Later edits create a new unverified draft and return the upload to human review; approval then creates `silver_revision + 1`. Verified rows and decisions are append-only.

## Phase-1 solo self-review exception

In a multi-user phase, self-review is rejected unless an administrator records an explicit exception reason. Phase 1 has one uploader and reviewer: the founder reviewing their own upload is the expected default, not an exceptional administrator action.

The detection logic remains enabled. Each self-review stores:

- `self_review_detected = true`;
- `solo_exception_applied = true`;
- exception reason `PHASE_1_SOLO_FOUNDER_REVIEW` in the decision/audit details.

Setting `ALLOW_PHASE_1_SOLO_SELF_REVIEW=false` restores the multi-user control immediately; self-review then fails without an administrator reason. The flag must be false before a second active reviewer is introduced.

## Authorization on record — 2026-08-13

Darmstädter management has granted full legal authorization, including GDPR compliance, for processing real R&D data—including personal and confidential data—through this local pilot infrastructure. This dated record documents authorization already granted; it is not a further sign-off gate before testing.

Authorization does not permit real data in Git or transfer to an external OCR/vision provider. Authorized real files exist only in local MinIO and local encrypted backups.

## Standard capture policy

Pilot photos are captured on company-issued iPhone 17 devices. The operator should:

1. clean the lens and use the native camera at full resolution;
2. frame the complete source with a small margin and keep it as square as practical;
3. avoid digital zoom; move closer while preserving focus;
4. capture the screen/document once normally and, where practical, once from a slightly different angle;
5. inspect readability before upload and retain the original capture bytes.

Device quality does not lower the real-batch difficulty. At least one source must exhibit screen glare/reflection because it arises from photographing a lit display, not from sensor quality.
