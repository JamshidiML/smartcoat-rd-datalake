# Bronze immutability policy

## Purpose

Bronze is source evidence. Originals and their provenance manifests are never corrected, overwritten, merged, or deleted through the application. A duplicate upload remains a separate provenance event linked by checksum. Corrections belong in a new Silver review revision.

## Controls

- `sc-rd-bronze-originals` and `sc-rd-bronze-manifests` have versioning, Object Lock, default compliance-mode retention for 365 days, and anonymous access disabled.
- Each upload has one key under `rd/{yyyy}/{mm}/{ingestion_id}/original/` and exactly one manifest at `.../manifest/v1.json`.
- The application performs a stat-before-put and refuses an existing key. Service policies contain no `DeleteObject` action.
- The manifest is canonical JSON stored independently from PostgreSQL. The API reads both objects back before recording `BRONZE_COMMITTED`.
- PostgreSQL triggers reject updates and deletes of `bronze_objects`; its rows retain bucket, key, version identifier, digest, and retention deadline.
- Root MinIO credentials are restricted to bootstrap and emergency administration. They are never available to the API, OCR worker, human user, or Git.

Compliance retention is deliberately difficult to undo. Before changing the retention period, legal/governance ownership must record the decision; existing retained versions remain protected until their own deadlines.

## Integrity verification

`scripts/verify-bronze-integrity.sh` reads each locked manifest using the read-only backup identity, recomputes the original object's SHA-256, and fails on any mismatch. The real-batch report records its result. A database row alone is not accepted as evidence of a Bronze commit.

## Repository data rule

Only synthetic fixtures may be committed. Company R&D photos, PDFs, spreadsheets, formulations, customer data, personal data, and screenshots remain in local MinIO. `.gitignore` is a guardrail; the tracked-file CI scan is the enforcement check.
