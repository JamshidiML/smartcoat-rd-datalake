# ADR-0002: Retention semantics and enforcement contract

## Status

`ACCEPTED`

The retention semantics and enforcement contract was accepted on 2026-08-20. Acceptance covers the architectural contract and ratified decisions. It does not assert that schemas, migrations, MinIO behavior, credential separation, reconciliation, tests, remediation, or real-data readiness are implemented. Real company data remains prohibited until M0-R05 passes.

## Date

2026-08-20

## Context and verified current state

Bronze originals and provenance manifests are intended to be immutable, versioned, content-hash-verifiable evidence. Master Roadmap v2 requires retention to be assigned per record from the data category rather than inferred from file format. The accepted sequencing in ADR-0001 places this design work before remediation and prohibits real company data until M0-R05 passes.

The evidence below distinguishes intent, static implementation, executed evidence, and unimplemented target behavior.

### Documented intent

- `README.md` describes MinIO Bronze storage with versioning, Object Lock, and 365-day COMPLIANCE retention.
- `docs/governance/BRONZE_IMMUTABILITY_POLICY.md` states that `sc-rd-bronze-originals` and `sc-rd-bronze-manifests` use versioning, Object Lock, and default 365-day COMPLIANCE retention; that application policies omit `s3:DeleteObject`; and that a database row alone is not evidence of a Bronze commit.
- `docs/architecture/PHASE_1_ARCHITECTURE.md` describes locked originals and manifests and requires Bronze verification before OCR, but it does not define per-record retention classes or legal holds.
- `docs/governance/PHASE_1_ACCEPTANCE_REPORT_TEMPLATE.md` includes AT-12 for application-level delete/overwrite denial, but does not contain version-specific retention or legal-hold integration evidence.

These documents express policy intent. They are not runtime proof.

### Static configuration and code

- `compose.yaml` pins the object store to `minio/minio:RELEASE.2025-07-23T15-54-02Z`, the bootstrap client to `minio/mc:RELEASE.2025-07-21T05-28-08Z`, and provides distinct application, OCR, and backup MinIO identities through named environment variables.
- `apps/api/src/requirements.txt` and `apps/ocr-worker/src/requirements.txt` pin the Python MinIO client to `minio==7.2.16`.
- `infra/minio/bootstrap.sh` creates `sc-rd-bronze-originals` and `sc-rd-bronze-manifests` with `mc mb --with-lock`, explicitly enables versioning, and configures bucket-default `COMPLIANCE 365d` retention on both buckets.
- `apps/api/src/domain.py::IngestionService.ingest` calls `put_once(..., locked=True)` for both the original and the manifest.
- `apps/api/src/storage.py::MinioObjectStorage.put_once` independently supplies per-write `Retention(COMPLIANCE, datetime.now(UTC) + timedelta(days=365))` when `locked=True`, then returns the SDK result's `version_id`.
- `apps/api/src/storage.py` reads object bytes without selecting an object version and contains no version-specific retention or legal-hold read-back method.
- `infra/minio/policies/app-bronze-write.json` grants `s3:PutObjectRetention` but not `s3:DeleteObject` or `s3:PutObjectLegalHold` to the ingestion identity.
- `infra/minio/policies/reviewer-read.json`, attached to the backup identity by `infra/minio/bootstrap.sh`, grants `s3:GetObjectRetention` and version reads but no legal-hold permission.
- `infra/postgres/init.sql` defines `bronze_objects.object_version_id` as nullable text, restricts `retention_mode` to `COMPLIANCE`, and requires `retain_until_utc` without recording storage read-back or legal-hold state.
- `apps/api/src/database.py::PostgresRepository.commit_bronze` writes `retention_mode = 'COMPLIANCE'` and computes `retain_until_utc` independently as `now() + interval '365 days'`; it does not persist a storage-reported retain-until value.
- No current schema column or ingestion field represents a per-record retention class, retention-policy version, data-category retention assignment, storage-reported legal-hold state, or enforcement-verification result.
- The repository-wide pre-draft search found no use of `legal_hold`, `legalhold`, `PutObjectLegalHold`, lifecycle configuration, or legal-hold read-back. No legal-hold implementation currently exists.

This is static evidence only. In particular, the bucket default, explicit per-write retention, and database-calculated deadline are three declarations that are not reconciled through a version-specific storage read-back.

### Executed evidence

- `apps/api/tests/test_acceptance.py::test_at_12_application_cannot_delete_or_overwrite_bronze` uses `MemoryStorage`, checks its synthetic `PermissionError`, and inspects the policy JSON for absence of `s3:DeleteObject`.
- `apps/api/tests/fakes.py` generates a synthetic version identifier and denies deletion in memory.
- No current test exercises retention metadata, legal hold, explicit protected-version deletion, delete-marker behavior, or retention expiry against the pinned MinIO server and client images.
- M0-T02 closed with the product baseline `BLOCKED`. No MinIO/PostgreSQL integration proof for these retention contracts was produced.

No current test result proves the target semantics in this ADR.

### Unimplemented target behavior

The repository does not currently implement:

- the canonical `permanent`, `long_term_10y`, and `short_90d` classes;
- data-category-based retention assignment;
- indefinite legal hold for permanent evidence;
- version-specific enforcement read-back and reconciliation;
- fail-closed quarantine for missing or contradictory storage enforcement;
- a governed break-glass hold-clearing authority;
- migration of existing 365-day object versions to the target contract;
- lifecycle deletion based on eligibility.

## Standing policy direction

Retention is assigned per record according to data category. Source format, MIME type, extension, modality, and department alone do not determine retention.

The canonical classes are exactly:

- `permanent`
- `long_term_10y`
- `short_90d`

Per-record retention is already required by the accepted roadmap direction. This ADR decides the semantic and evidence contract for those classes; it does not reopen whether per-record retention is required.

## `permanent` semantics

For each applicable Bronze object version:

- Legal-hold status is set to ON for that exact version.
- The legal hold supplies indefinite protection with no automatic expiry.
- The same object version also receives COMPLIANCE retention with a retain-until safety floor of ten UTC calendar years from its authoritative accepted storage timestamp.
- The ten-year calculation uses the calendar rule defined for `long_term_10y`.
- Permanent object versions are never automatically deleted by lifecycle policy.
- Expiry of the fixed COMPLIANCE safety floor does not remove or weaken the legal hold.
- Clearing the legal hold while the COMPLIANCE floor remains active does not make the version deletable.
- `permanent` is not satisfied by only an arbitrary far-future retain-until timestamp or by repeatedly renewing fixed retention.
- `permanent` does not mean that release is technically impossible. Legal-hold removal requires a separately governed, audited break-glass decision.
- Normal API, OCR-worker, reviewer, backup, and ingestion runtime identities cannot remove a legal hold.

The provider contract distinguishes legal holds from fixed retention: a version may have either or both; a legal hold has no expiry and remains until explicitly removed; and removing a hold does not remove an active retention period. The current MinIO AIStor documentation describes the same independence and WORM behavior, while the Amazon S3 Object Lock documentation defines the underlying S3 semantics. These pages are semantic inputs, not proof for the repository's pinned MinIO image.

The S3 permission model uses the same `s3:PutObjectLegalHold` action to set legal-hold status ON or OFF. An ordinary runtime identity holding unrestricted direct `s3:PutObjectLegalHold` permission therefore cannot be treated as hold-setting-only. Permanent-hold application must pass through an isolated or mediated control whose credentials and OFF operation are unavailable to ordinary API, OCR-worker, reviewer, backup, and ingestion services. The exact mediation implementation remains deferred, but this authority separation is mandatory. If the pinned MinIO release cannot enforce the separation through policy alone, later least-privilege design must introduce a controlled mediation boundary and compensating controls. That limitation must be visible; it must not be concealed by granting legal-hold modification to an ordinary runtime identity.

## `long_term_10y` semantics

The default contract is COMPLIANCE retention without an indefinite legal hold:

- Retain-until equals the authoritative accepted storage timestamp plus ten UTC calendar years.
- Calendar-year addition preserves the original UTC month, day, and time.
- If the target year lacks the original calendar date, use the last valid day of that month while preserving the UTC time. For example, a February 29 timestamp maps to February 28 in a non-leap target year.
- Active protection cannot be shortened, downgraded, or replaced by a weaker mode.
- A case-specific legal hold may be added to the same object version without changing its declared `long_term_10y` class.
- Adding or later removing that separate hold does not alter the COMPLIANCE retain-until value.

## `short_90d` semantics

The contract is COMPLIANCE retention for exactly `90 × 24` hours from the authoritative accepted storage timestamp:

- Retain-until equals the authoritative accepted storage timestamp plus 2,160 hours.
- Active protection cannot be shortened or downgraded.
- Expiry creates deletion eligibility only.
- Expiry does not authorize deletion and does not itself perform physical deletion.
- No lifecycle deletion rule is authorized by this ADR. Lifecycle behavior requires a separate approved implementation decision.
- A case-specific legal hold, if applied, continues to block deletion independently of retention expiry.

## Data-category assignment

The minimum assignment policy is:

- Formulation and R&D data, including trial video: `permanent`.
- Production process, machine, batch, and raw-material evidence: `permanent`.
- QC and laboratory results and their evidence: `permanent`.
- Customer, sales, contact-person, and email sources containing personal data have no default retention class and must not default to `permanent`.
- Before such a source is onboarded, an approved, versioned records policy must define its documented purpose and legal-basis classification and map the governed data components to one of the three canonical retention classes.
- `long_term_10y` applies to personal-data-bearing components only when that approved records policy requires the ten-year class.
- `permanent` applies only to a specifically approved non-personal evidentiary component or an explicitly governed exception; it is not a conservative fallback for personal data.
- A case-specific legal hold is independent of the normal retention class and does not silently reclassify the source.
- If no approved policy with a documented purpose and legal-basis classification exists, onboarding is rejected or placed in classification-pending quarantine. The system must not silently choose `permanent`.
- Platform operational health and debug logs: `short_90d`.
- Unknown or unclassified categories: fail closed into a restricted classification-pending condition with explicitly recorded conservative protection. No invisible default may be applied.

Until the classification-pending state and transition are designed through the later centrally enforced state-machine remediation, “classification pending” is a required non-success/quarantine behavior rather than a new state silently added by this ADR.

The GDPR Article 5(1)(e) storage-limitation principle is a governing policy input for personal-data records design. This ADR is an architecture contract, not legal advice, and does not itself determine a lawful purpose, legal basis, or records schedule.

Retention is not derived solely from:

- MIME type;
- filename extension;
- ingestion modality;
- department;
- storage bucket;
- source application.

Department may constrain valid data categories and access policy, but the approved data category and retention-policy version determine the retention class.

## Record-level metadata contract

Every protected Bronze object version must be represented by an append-only, version-specific enforcement record. Existing names are retained where they already carry the required meaning; proposed contract fields do not claim current schema support.

### Existing fields requiring stronger guarantees

- `bronze_objects.bucket_name`: identifies the storage bucket.
- `bronze_objects.object_key`: identifies the object key.
- `bronze_objects.object_version_id`: identifies the immutable version; currently nullable, but target enforcement requires a non-null value.
- `bronze_objects.sha256`: links the version to content-hash evidence.
- `bronze_objects.retention_mode`: currently constrained to `COMPLIANCE`; the target record must represent the storage-observed mode.
- `bronze_objects.retain_until_utc`: currently database-generated; the target record must hold the storage-reported version-specific retain-until timestamp.
- `bronze_objects.created_at_utc`: currently database time; it is not automatically the authoritative accepted storage timestamp.

### Proposed target contract fields

The later versioned schema design must provide, by these names or an explicitly mapped equivalent:

- `retention_class`: one of the three canonical classes.
- `retention_policy_version`: immutable identifier of the assignment policy used.
- `data_category`: approved category that drove assignment.
- `retention_assigned_at_utc`: assignment timestamp.
- `retention_assigned_by`: accountable actor or deterministic rule identifier.
- `accepted_storage_at_utc`: authoritative storage timestamp for class-duration calculation.
- `storage_retain_until_utc`: version-specific retain-until value read back from MinIO.
- `storage_legal_hold_status`: version-specific legal-hold status read back from MinIO.
- `enforcement_verified_at_utc`: read-back verification timestamp.
- `enforcement_verification_result`: append-only success, mismatch, unavailable, or quarantine outcome.
- `retention_exception_decision_ref`: approved exception or legal-hold decision reference when applicable.

Exact schema shape is deferred. The information content and version-specific linkage are mandatory. Recording later enforcement evidence must not mutate an immutable provenance manifest or overwrite prior audit evidence.

## Retention-duration anchor

`accepted_storage_at_utc` is the storage service's server-observed, version-specific Last-Modified instant for the exact bucket, object key, and object version ID returned by the successful write. The target implementation must obtain that timestamp from version-specific storage metadata read-back after the write; an upload-request timestamp, application-host clock, PostgreSQL clock, or locally inferred timestamp is not authoritative storage evidence.

The anchor must be normalized to UTC and serialized with a `Z` suffix at whole-second precision. If the storage service supplies finer precision, normalization truncates rather than rounds the fractional second. The expected class-specific retain-until value is calculated from this normalized anchor, and the MinIO version-specific retain-until read-back is normalized to the same whole-second precision before comparison.

This is a target evidence requirement, not a claim that the pinned MinIO release or `minio==7.2.16` currently supplies a verified result with these semantics. No particular unverified MinIO API response is assumed. If the pinned server or SDK cannot provide a sufficiently reliable server-observed Last-Modified instant for the exact returned version, implementation is blocked until M0-R design establishes an alternative authoritative server-time contract and M0-R05 verifies it against the pinned runtime.

## Original/manifest pair invariant

A successful ingestion commit consists of both the original object version and its provenance-manifest object version.

- The original and manifest must have the same declared retention class and retention-policy version unless a later approved policy explicitly requires stronger protection for the manifest.
- Both members must have non-null object version IDs.
- Version-specific retention mode, retain-until timestamp, and legal-hold status must be read back and verified for both members.
- `BRONZE_COMMITTED` is permitted only after both members satisfy their declared policies and pair-consistency requirements.
- If only one member is successfully stored or protected, the ingestion remains non-successful and enters quarantine/reconciliation handling.
- The successfully protected member remains protected and discoverable. It must not be deleted, weakened, or have its hold cleared as compensation for failure of the other member.

## Source of truth and fail-closed behavior

MinIO's version-specific read-back is authoritative for storage enforcement. PostgreSQL records the declared policy and the enforcement evidence observed from MinIO.

A local calculation, requested retention value, successful upload response, bucket default, or database label is not proof that the accepted object version has the required storage protection.

MinIO and PostgreSQL do not share an atomic transaction. Storage writes, storage protection, and version-specific read-back occur before successful Bronze commitment. Atomicity applies to the PostgreSQL success boundary, not across the two systems.

The target commit contract is:

1. Store the original and provenance manifest and obtain a non-null object version ID for each member.
2. Apply or confirm class-specific retention and legal hold for each exact version.
3. Read back the version-specific storage timestamp, retention mode, retain-until timestamp, and legal-hold status for both versions.
4. Compare both read-backs with the declared retention class, retention-policy version, pair invariant, and defined `accepted_storage_at_utc` anchor.
5. Within one PostgreSQL transaction, atomically persist the policy declaration, enforcement-evidence rows for both versions, the ingestion state transition, and the append-only audit event.
6. Enter the existing successful `BRONZE_COMMITTED` state only when that PostgreSQL transaction commits after all required evidence matches.
7. Queue OCR only after the PostgreSQL success transaction has committed.

If object storage and enforcement succeed but PostgreSQL persistence fails, every successfully protected object version remains protected and becomes a discoverable unresolved/protected orphan. A protected orphan must be reconciled idempotently; it must not be deleted or weakened as rollback behavior.

Missing version ID, unavailable read-back, absent retention metadata, absent required legal hold, contradictory mode or deadline, or any other mismatch must produce a non-success/quarantined outcome and an append-only audit event. It must not queue OCR or be represented as a successful Bronze commit.

Retry identity must include, at minimum, bucket name, object key, object version ID, and retention-policy version for each protected member. Retrying enforcement, commitment, and reconciliation must be idempotent: a retry may confirm or extend protection, but must never shorten protection, create multiple successful Bronze commitments, duplicate factual commit evidence, or create conflicting retention assignments.

Later detection of a storage/database mismatch must fail closed, append enforcement and audit evidence, and prevent OCR or other downstream processing until reconciliation succeeds. This ADR does not add or rename a state. Exact schema mechanics, quarantine states, legal transitions, and PostgreSQL transaction enforcement remain deferred to M0-R01, M0-R03, and M0-R04 as appropriate.

## Authority and exception model

- Ordinary application identities cannot shorten retention, bypass active COMPLIANCE protection, or clear legal holds.
- The API, OCR worker, reviewer, backup, and ingestion identities do not receive break-glass hold-clearing authority.
- Because `s3:PutObjectLegalHold` can set status ON or OFF, ordinary runtime identities cannot directly hold unrestricted use of that action and be represented as hold-setting-only.
- Permanent-hold application uses an isolated or mediated control whose credentials and OFF operation are unavailable to the API, OCR worker, reviewer, backup, and ingestion services.
- Legal-hold clearing uses a dedicated break-glass authority unavailable to normal runtime services.
- Every clearing action requires an accountable decision record containing reason, actor, timestamp, affected bucket/key/version identifiers, and an append-only audit event.
- Policy exceptions are explicit, versioned, approved, and linked to the affected object versions. They are never inferred from file type or a mutable database label.
- Changing a PostgreSQL label cannot weaken protection already enforced by MinIO.
- Enforcement is monotonic: later retain-until extensions and added holds are allowed; silent shortening, downgrade, or hold clearing is prohibited.

Target governance should require dual approval for release of permanent evidence. No dual-approval system or personal approver is claimed to exist today. If the pinned MinIO authority model cannot distinguish hold ON from hold OFF using IAM alone, the later design must use a brokered or otherwise isolated break-glass control with approval evidence and monitoring.

## Existing 365-day-object compatibility

Every existing Bronze object version must undergo a non-destructive reconciliation before the target policy is considered enforced:

1. Inventory all versions in `sc-rd-bronze-originals` and `sc-rd-bronze-manifests`; do not inspect only each key's latest version.
2. Reconcile storage versions to PostgreSQL using `bronze_objects.bucket_name`, `object_key`, `object_version_id` where present, and `sha256`.
3. Treat a missing `object_version_id` as unresolved. Use version inventory and content hash to attempt an unambiguous reconciliation; quarantine ambiguity rather than guessing.
4. Classify current R&D evidence as `permanent` unless an explicit approved exception states otherwise.
5. Apply legal-hold status ON to every reconciled permanent object version.
6. Extend the COMPLIANCE safety floor without shortening existing protection. For a permanent legacy version, the resulting retain-until value must be no earlier than both its existing storage-reported retain-until value and ten UTC calendar years after the remediation timestamp.
7. Read back and record the resulting retention mode, retain-until timestamp, and legal-hold status for each exact version.
8. Quarantine and report missing version IDs, hash mismatches, orphaned storage versions, orphaned database rows, ambiguous matches, and failed enforcement.
9. Make the migration retryable and idempotent. Repeated execution must preserve ON holds, retain the same assignment, and use a retain-until value that never moves earlier.
10. Keep real company data prohibited until the repaired ingestion and migration behavior passes M0-R05.

The migration must not overwrite Bronze content, collapse versions, remove delete markers, shorten existing retention, or silently convert an unresolved record into success. M0-T04 does not implement this migration.

## Delete-marker and lifecycle semantics

- Deleting an object key without a version ID and deleting a protected object version are different operations.
- In a versioned Object Lock bucket, an unversioned delete may create a delete marker while retained evidence versions remain protected.
- A delete marker hides a current object view; it must never be reported as physical deletion of retained evidence.
- Protected versions remain discoverable by version inventory and restorable even when a delete marker is current.
- Retention expiry means a version may become eligible for deletion. It does not execute deletion and does not constitute authorization.
- A legal hold continues to block deletion after fixed retention expires until the hold is explicitly removed.
- No lifecycle rule is created or authorized by this ADR.
- Any future lifecycle or deletion mechanism must target only eligible versions, preserve required recovery behavior, and append audit evidence for authorization and outcome.

## Required implementation and integration evidence

Later remediation and M0-R05 must produce concrete evidence for the pinned MinIO server, client image, and Python SDK versions:

- the retention class is stored per record and linked to each Bronze version;
- a non-null version ID is captured from MinIO for both the original and provenance-manifest members of every ingestion;
- both pair members carry the same retention class and retention-policy version unless an approved policy requires stronger manifest protection;
- version-specific storage timestamps, retention modes, retain-until timestamps, and legal-hold statuses are read back for both members;
- `accepted_storage_at_utc` and retain-until comparisons use the defined exact-version, storage-service-observed, whole-second UTC contract or a later authoritative server-time contract verified by M0-R05;
- `BRONZE_COMMITTED` is denied until both members pass enforcement and pair-consistency verification;
- a one-member success produces quarantine and idempotent reconciliation without deleting or weakening the protected member;
- `permanent` receives legal hold ON plus a ten-calendar-year COMPLIANCE floor and no auto-delete rule;
- `long_term_10y` passes normal-date and leap-day boundary tests;
- `short_90d` proves exactly 2,160 hours across daylight-saving and calendar boundaries because timestamps are UTC;
- overwrite and explicit version deletion are denied during active protection;
- ordinary API, OCR-worker, reviewer, backup, and ingestion identities cannot clear legal holds;
- the shared ON/OFF authority of `s3:PutObjectLegalHold` is mediated so ordinary runtime identities cannot directly exercise unrestricted legal-hold modification;
- controlled hold removal requires the break-glass path and produces decision and audit evidence;
- clearing a legal hold does not bypass an active COMPLIANCE floor;
- storage/database mismatches, absent legal holds, missing version IDs, and read-back failures are rejected or quarantined;
- MinIO success followed by PostgreSQL failure produces discoverable protected-orphan evidence and idempotent reconciliation rather than storage rollback;
- the PostgreSQL policy declaration, pair enforcement evidence, state transition, and audit event commit atomically without claiming a MinIO/PostgreSQL distributed transaction;
- retry identity includes bucket name, object key, object version ID, and retention-policy version and cannot create multiple successful Bronze commitments or conflicting assignments;
- OCR is not queued before the PostgreSQL success transaction commits, and later mismatches stop downstream processing;
- the legacy 365-day inventory reconciles every database row and storage version or reports it unresolved;
- retries are idempotent and never shorten retention or duplicate successful commit evidence;
- unversioned delete-marker behavior and explicit protected-version deletion behavior are tested separately;
- lifecycle absence or separately approved lifecycle behavior is verified;
- all retention and legal-hold behavior is verified against `minio/minio:RELEASE.2025-07-23T15-54-02Z`, `minio/mc:RELEASE.2025-07-21T05-28-08Z`, and `minio==7.2.16` rather than inferred from newer documentation.

These tests do not currently pass because they do not currently exist as pinned-MinIO integration evidence.

## Dependencies and sequencing

- M0-T04 is design-only. ADR-0002 does not authorize schema, MinIO, credential, lifecycle, state-machine, or migration changes.
- Any database change depends on completion of M0-R01's versioned migration foundation.
- Least-privilege enforcement aligns with M0-R02 and the later MinIO authority design, including the hold-setting/hold-clearing boundary.
- Any new quarantine transition or central enforcement rule aligns with M0-R03.
- Atomic and idempotent commit/reconciliation behavior aligns with M0-R04.
- Exact schema, state-machine, and PostgreSQL transaction mechanics for the pair and protected-orphan contracts remain deferred to M0-R01, M0-R03, and M0-R04 as appropriate.
- M0-R06 remains a direct prerequisite for M0-R05.
- Runtime PostgreSQL/MinIO proof belongs in M0-R05 only after M0-R02, M0-R03, M0-R04, and M0-R06 are complete; M0-R01 is a transitive prerequisite.
- No real company data is permitted before M0-R05 passes.

## Alternatives and rejected shortcuts

- **Hardcoded 365 days for every category:** rejected because it neither represents the accepted data-category policy nor supplies indefinite protection for permanent evidence.
- **Retention class only as a database label:** rejected because a database declaration is not storage enforcement.
- **Bucket-only classification:** rejected because categories with different semantics can share storage and because each protected version needs explicit evidence.
- **A far-future timestamp as the sole meaning of permanent:** rejected because any fixed timestamp expires and does not equal an indefinite hold.
- **Renewable fixed retention without legal hold:** rejected because renewal failure creates an unintended expiry path.
- **Trusting locally calculated deadlines:** rejected because only version-specific storage read-back proves what MinIO enforces.
- **Allowing normal runtime credentials to clear holds:** rejected because compromise or ordinary operational error could release permanent evidence.
- **Silently reclassifying or weakening legacy objects:** rejected because migration must preserve or increase protection and retain an accountable decision trail.
- **Treating a delete marker as evidence deletion:** rejected because the protected version can remain stored and recoverable.
- **Treating retention expiry as deletion authorization:** rejected because eligibility, approval, lifecycle action, and physical deletion are distinct events.

## Deferred implementation decisions

The following implementation choices remain deferred because repository and pinned-release proof is insufficient:

- exact versioned migrations, tables, constraints, and indexes;
- exact MinIO server, client, and SDK calls and policy mechanics for the pinned releases;
- deployment topology and custody of break-glass authority;
- the mechanism used to separate hold setting from hold clearing when the provider permission is shared;
- dual-approval workflow details;
- exact quarantine state names and transitions;
- lifecycle deletion implementation and eligibility scheduler;
- user interface and approval workflow;
- reconciliation batch sizing, recovery checkpoints, and operator tooling;
- alerting, monitoring, and operational dashboards.

Deferral of implementation mechanics does not weaken the semantic, authority, evidence, or fail-closed contracts in this ADR.

## Human-ratification record

### User/Program Owner

- Decision: `RATIFIED`
- Date: `2026-08-20`
- Rationale: The corrected personal-data retention contract—no implicit permanent default for customer, sales, or email data and fail-closed onboarding without an approved records policy—the exact-version storage timestamp anchor, explicit non-atomicity between MinIO and PostgreSQL with protected-orphan reconciliation, the original/manifest pair-consistency requirement, and legal-hold ON/OFF authority separation are accepted as written. The three canonical retention classes and all M0-R, M0-R05, and real-data gates are unchanged and remain binding.

### Architecture Reviewer

- Decision: `RATIFIED`
- Date: `2026-08-20`
- Rationale: Independently verified that `infra/minio/policies/app-bronze-write.json` grants `s3:PutObjectRetention` but not `s3:PutObjectLegalHold` or `s3:DeleteObject`, and that no legal-hold implementation currently exists in the repository. The revision resolves the personal-data default, timestamp-anchor, cross-system atomicity, original/manifest pair, and hold-authority findings without weakening ADR-0001 or the M0-T01/M0-T02 findings and gates. No inconsistency was found.

## References

- [`README.md`](../../../README.md)
- [`compose.yaml`](../../../compose.yaml)
- [`.env.example`](../../../.env.example)
- [`docs/architecture/PHASE_1_ARCHITECTURE.md`](../PHASE_1_ARCHITECTURE.md)
- [`docs/architecture/decisions/ADR-0001-master-roadmap-v2-scope-expansion-sequencing.md`](ADR-0001-master-roadmap-v2-scope-expansion-sequencing.md)
- [`docs/governance/BRONZE_IMMUTABILITY_POLICY.md`](../../governance/BRONZE_IMMUTABILITY_POLICY.md)
- [`docs/governance/PHASE_1_ACCEPTANCE_REPORT_TEMPLATE.md`](../../governance/PHASE_1_ACCEPTANCE_REPORT_TEMPLATE.md)
- [`infra/minio/bootstrap.sh`](../../../infra/minio/bootstrap.sh)
- [`infra/minio/policies/app-bronze-write.json`](../../../infra/minio/policies/app-bronze-write.json)
- [`infra/minio/policies/reviewer-read.json`](../../../infra/minio/policies/reviewer-read.json)
- [`infra/postgres/init.sql`](../../../infra/postgres/init.sql)
- [`apps/api/src/domain.py`](../../../apps/api/src/domain.py)
- [`apps/api/src/storage.py`](../../../apps/api/src/storage.py)
- [`apps/api/src/database.py`](../../../apps/api/src/database.py)
- [`apps/api/src/requirements.txt`](../../../apps/api/src/requirements.txt)
- [`apps/ocr-worker/src/requirements.txt`](../../../apps/ocr-worker/src/requirements.txt)
- [`apps/api/tests/test_acceptance.py`](../../../apps/api/tests/test_acceptance.py)
- [`apps/api/tests/fakes.py`](../../../apps/api/tests/fakes.py)
- *Unified Data & AI Platform — Master Roadmap & Foundational Design v2.0*, Section 6, August 2026.
- [MinIO AIStor: Object Locking and Immutability](https://docs.min.io/aistor/administration/object-locking-and-immutability/)
- [Amazon S3: Locking objects with Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)
- [Regulation (EU) 2016/679, Article 5(1)(e): storage limitation](https://eur-lex.europa.eu/eli/reg/2016/679/oj) — governing policy input for personal-data records design; this ADR is not legal advice.
