# M0 Contract-Freeze Acceptance Matrix

## Document control

- Status: `ACCEPTED`
- Date: `2026-08-20`
- Scope: Design and traceability only.
- Approval boundary: This document is not implementation approval, remediation authorization, executable verification, or real-data approval.
- Repository baseline: Repository Pilot Slice on `codex/fix-ocr-onednn` at `88ec398a3dd38cfb5e86c85943d063bd02989057`.

## Authority and interpretation

- The accepted ADRs are normative. Repository code, configuration, policies, tests, and runbooks are evidence of current state, not substitutes for accepted contracts.
- ADR-0001 governs program scope, terminology, stage order, remediation dependencies, and real-data admission.
- ADR-0002 governs retention classes, version-specific enforcement evidence, original/manifest pairing, cross-system failure semantics, and legal-hold authority.
- Master Roadmap v2 authorizes the broader program direction. Where its older retention wording is less precise, ADR-0002 supplies the ratified fail-closed personal-data contract.
- Legacy documents that still describe 365-day retention or PaddlePaddle 3.3.1 are current-documentation discrepancies, not accepted-ADR conflicts.
- A documented claim is not runtime proof. `CONFIRMED_IMPLEMENTED` is used only where current static evidence completely expresses the applicable current-stage contract; executable proof can still remain assigned to a verification owner.

## Classification vocabularies

- Current evidence state: `CONFIRMED_IMPLEMENTED`, `PARTIALLY_IMPLEMENTED`, `CONTRADICTED`, `UNPROVEN`, `NOT_IMPLEMENTED`, or `NOT_APPLICABLE_CURRENT_STAGE`.
- Implementation ownership state: `OWNED`, `PARTIALLY_OWNED`, `UNOWNED_MANDATORY_REMEDIATION`, or `DEFERRED_LATER_STAGE`.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`, `BLOCKING_BEFORE_EXPANSION`, `NON_BLOCKING_TECHNICAL_DEBT`, or `INFORMATIONAL`.

## Coverage result

- Overall coverage result: `BLOCKED_PENDING_REMEDIATION_OWNERSHIP`.
- Product baseline: `BLOCKED`.
- Deterministic derivation: Contracts CF-002 through CF-016 are `PARTIALLY_OWNED` or `UNOWNED_MANDATORY_REMEDIATION`; therefore the ready result is prohibited.
- Contract-conflict determination: No contradiction exists between ADR-0001 and ADR-0002. Lower-authority implementation and documentation discrepancies are recorded below and do not change the deterministic result to `BLOCKED_BY_CONTRACT_CONFLICT`.
- Real-data admission: No real company data is permitted before M0-R05 passes after its required remediation dependencies.
- Expansion admission: Controlled 5–10-file R&D acceptance must follow M0-R05 and precede multi-department expansion.

## Contract records

### CF-001 — Program terminology boundary

- Contract: The current repository is the Repository Pilot Slice; unqualified “Phase 1” is deprecated, while Master Roadmap Program expansion remains authorized.
- Normative source: ADR-0001, “Decision,” “Terminology,” and Stages 0–6.
- Current evidence: `README.md` and `docs/architecture/PHASE_1_ARCHITECTURE.md` still use “Phase 1”; ADR-0001 supplies the accepted replacement vocabulary.
- Current evidence state: `PARTIALLY_IMPLEMENTED`
- Frozen target: All new program decisions distinguish Repository Pilot Slice from Master Roadmap Program stages without rewriting historical evidence.
- Observable acceptance criterion: New governance and ticket documents use the accepted terms, preserve Stage 0–6 sequencing, and identify historical “Phase 1” text as legacy terminology when cited.
- Implementation owner: Architecture documentation owner under ADR-0001.
- Implementation ownership state: `OWNED`
- Verification owner: Architecture Reviewer.
- Dependency: ADR-0001 acceptance; no product-code dependency.
- Gate effect: `INFORMATIONAL`
- Deferred details: Historical-document cleanup is separate documentation work.
- Conflict or coverage note: Terminology drift is present but does not reopen the authorized expansion decision.

### CF-002 — Immutable versioned Bronze original

- Contract: Each accepted source is stored as an immutable, versioned, content-hash-linked Bronze original.
- Normative source: ADR-0001 mandatory invariant 1; ADR-0002 original/manifest pair invariant; `docs/governance/BRONZE_IMMUTABILITY_POLICY.md`.
- Current evidence: `IngestionService.ingest` uses `put_once(..., locked=True)`; `MinioObjectStorage.put_once` rejects an existing key; `bootstrap.sh` creates the originals bucket with Object Lock and versioning; AT-01, AT-03, and AT-12 use `MemoryStorage` rather than pinned MinIO.
- Current evidence state: `PARTIALLY_IMPLEMENTED`
- Frozen target: The exact returned original version is protected according to its per-record policy and remains retrievable and verifiable by version.
- Observable acceptance criterion: Pinned-MinIO integration evidence proves non-null version capture, active protection, version-specific read-back, overwrite denial, explicit protected-version delete denial, and SHA-256 equality.
- Implementation owner: Existing ingestion implementation plus Candidate Retention Enforcement and Read-Back package.
- Implementation ownership state: `PARTIALLY_OWNED`
- Verification owner: M0-R05.
- Dependency: M0-R01 and the candidate retention implementation; M0-R02 where authority changes are required.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Exact pinned-SDK calls and version-specific metadata mapping.
- Conflict or coverage note: Versioning and an intended retention request do not alone prove immutability of the exact version.

### CF-003 — Immutable versioned provenance manifest

- Contract: Every accepted original has an immutable, versioned provenance manifest that records identity, lineage, metadata, and source checksum.
- Normative source: ADR-0001 mandatory invariant 1; ADR-0002 original/manifest pair invariant; Bronze immutability policy.
- Current evidence: `IngestionService.ingest` serializes `manifest/v1.json`, writes it locked, and compares latest-key bytes; `bootstrap.sh` enables Object Lock and versioning on the manifest bucket; AT-02 and AT-03 are memory-backed.
- Current evidence state: `PARTIALLY_IMPLEMENTED`
- Frozen target: The exact manifest version is independently protected, version-addressable, hash-linked, and subject to a policy consistent with its original.
- Observable acceptance criterion: Integration evidence retrieves the returned manifest version, verifies its bytes and checksum, and proves its retention and legal-hold state without relying on the latest-key view.
- Implementation owner: Existing ingestion implementation plus Candidate Retention Enforcement and Read-Back package.
- Implementation ownership state: `PARTIALLY_OWNED`
- Verification owner: M0-R05.
- Dependency: M0-R01 and the candidate retention implementation.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Manifest schema evolution and exact enforcement-evidence storage shape.
- Conflict or coverage note: Current latest-key verification is not exact-version enforcement read-back.

### CF-004 — Original/manifest pair-consistency boundary

- Contract: A Bronze commit succeeds only when original and manifest exact versions both satisfy the same declared retention class and policy version, unless an approved policy explicitly strengthens the manifest.
- Normative source: ADR-0002, “Original/manifest pair invariant” and “Source of truth and fail-closed behavior.”
- Current evidence: `IngestionService.ingest` writes original, then manifest, calls `commit_bronze`, transitions state, and queues OCR; it has no policy-pair read-back, quarantine boundary, or protected-member recovery path.
- Current evidence state: `PARTIALLY_IMPLEMENTED`
- Frozen target: Pair verification and PostgreSQL success evidence are one fail-closed commit boundary before OCR queueing.
- Observable acceptance criterion: Tests inject failure after either member and prove no `BRONZE_COMMITTED` or OCR queue, protected-member discoverability, consistent pair metadata, one PostgreSQL success transaction, and idempotent reconciliation.
- Implementation owner: Candidate Bronze Pair Commit and Protected-Orphan Reconciliation package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05.
- Dependency: M0-R01; `RETENTION_ENFORCEMENT_READY`; M0-R03 only if this candidate introduces a new legally enforced state.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Exact quarantine names, recovery checkpoints, and orchestration mechanism.
- Conflict or coverage note: M0-R04 is limited to review/verification and cannot absorb Bronze commit orchestration.

### CF-005 — Non-null storage version IDs

- Contract: Both Bronze pair members have non-null exact storage version IDs linked to their enforcement evidence.
- Normative source: ADR-0002 record-level metadata contract, pair invariant, and required integration evidence.
- Current evidence: `bronze_objects.object_version_id` is nullable; `put_once` returns the SDK value, but neither domain nor repository rejects a missing value.
- Current evidence state: `CONTRADICTED`
- Frozen target: Successful Bronze evidence cannot exist without non-null original and manifest version IDs.
- Observable acceptance criterion: A phased versioned migration first adds compatible metadata; new successful Bronze evidence fails closed unless both exact original and manifest version IDs are present; after legacy null or ambiguous rows and protected versions are reconciled or quarantined, the final success-record invariant is validated against fresh and upgraded volumes.
- Implementation owner: Candidate Retention Metadata and Policy Assignment package owns `METADATA_EXPAND` and `SUCCESS_CONSTRAINT_VALIDATED`; Candidate Legacy Bronze 365-Day Reconciliation package owns `LEGACY_RECONCILED_OR_QUARANTINED`.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05.
- Dependency: Candidate sequencing is M0-R01 to `METADATA_EXPAND`, then `BRONZE_PAIR_READY` to `LEGACY_RECONCILED_OR_QUARANTINED` to `SUCCESS_CONSTRAINT_VALIDATED`.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Whether enforcement uses the existing table, a version-specific evidence table, or an explicitly mapped equivalent.
- Conflict or coverage note: M0-R01 owns migration machinery, not candidate metadata, backfill, or business-policy implementation. Compatible metadata expansion may precede legacy cleanup, but final success-constraint validation cannot precede reconciliation or quarantine of legacy nulls.

### CF-006 — Content-hash and manifest verification

- Contract: Stored source bytes match the manifest SHA-256, and stored manifest bytes match the committed canonical manifest.
- Normative source: ADR-0001 mandatory invariant 1; Bronze immutability policy; acceptance criteria AT-02 and AT-03.
- Current evidence: `IngestionService.ingest` computes SHA-256, reads the source back, compares its digest, writes a canonical JSON manifest, and compares returned manifest bytes; AT-02 and AT-03 prove the domain behavior in memory.
- Current evidence state: `PARTIALLY_IMPLEMENTED`
- Frozen target: Verification is exact-version-aware and is part of the successful pair boundary.
- Observable acceptance criterion: Existing unit tests remain green; candidate implementation verifies the exact returned versions and keeps both hash checks inside the successful original/manifest pair boundary; M0-R05 recomputes both checks against pinned-MinIO versions and persisted PostgreSQL lineage.
- Implementation owner: Existing ingestion implementation owns the current source-hash and canonical-manifest checks; Candidate Retention Enforcement and Read-Back owns exact-version verification; Candidate Bronze Pair Commit and Protected-Orphan Reconciliation owns placement of that verification inside the successful pair boundary.
- Implementation ownership state: `PARTIALLY_OWNED`
- Verification owner: M0-R05.
- Dependency: `METADATA_EXPAND`, `RETENTION_ENFORCEMENT_READY`, and `BRONZE_PAIR_READY` as defined by the proposed candidate-phase dependency model.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Exact canonical-manifest schema migration strategy.
- Conflict or coverage note: Current memory tests prove logic but not pinned object-store behavior; M0-R05 verifies the completed implementation and does not own either missing adaptation.

### CF-007 — Exactly three retention classes

- Contract: The canonical classes are exactly `permanent`, `long_term_10y`, and `short_90d`, assigned by approved data category rather than file type, modality, bucket, or department alone.
- Normative source: ADR-0002, “Standing policy direction” and “Data-category assignment.”
- Current evidence: No schema field or domain assignment exists; current bootstrap, storage, and database code use one hardcoded 365-day COMPLIANCE policy.
- Current evidence state: `NOT_IMPLEMENTED`
- Frozen target: Every protected exact version records one canonical class and an immutable retention-policy version.
- Observable acceptance criterion: Versioned constraints reject any fourth class; assignment tests cover approved R&D, operational logs, personal-data policy input, and unknown-category fail-closed behavior.
- Implementation owner: Candidate Retention Metadata and Policy Assignment package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05 for runtime enforcement; Architecture Reviewer for policy vocabulary.
- Dependency: M0-R01 and approved data-category policy inputs.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Physical schema and policy-engine representation.
- Conflict or coverage note: A database label without storage enforcement cannot satisfy this contract.

### CF-008 — Personal-data retention-policy onboarding gate

- Contract: Personal-data-bearing customer, sales, contact-person, and email sources have no implicit permanent default and cannot onboard without an approved, versioned records policy with purpose and legal-basis classification.
- Normative source: ADR-0002, “Data-category assignment.”
- Current evidence: The Repository Pilot Slice accepts only R&D categories and has no records-policy registry, classification gate, or quarantine implementation.
- Current evidence state: `NOT_APPLICABLE_CURRENT_STAGE`
- Frozen target: Later onboarding rejects or restricts classification when the required policy is absent; it never silently selects `permanent`.
- Observable acceptance criterion: Onboarding tests prove rejection or restricted quarantine for absent policy, exact policy-version linkage when present, and no file-type-derived fallback.
- Implementation owner: Candidate Retention Metadata and Policy Assignment package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: Architecture Reviewer and the later department-onboarding verifier.
- Dependency: M0-R01, M0-R03 for legal quarantine transitions, and an approved records policy before the applicable source is admitted.
- Gate effect: `BLOCKING_BEFORE_EXPANSION`
- Deferred details: Second department, lawful-purpose decisions, records schedule, and UI.
- Conflict or coverage note: This architecture contract is not legal advice and does not select a department.

### CF-009 — Permanent legal hold and COMPLIANCE floor

- Contract: `permanent` exact versions receive legal hold ON, a ten-UTC-calendar-year COMPLIANCE safety floor, and no automatic deletion.
- Normative source: ADR-0002, “permanent semantics” and required implementation evidence.
- Current evidence: Current code sets only 365-day COMPLIANCE retention; no legal-hold call, read-back, permission, field, or test exists.
- Current evidence state: `NOT_IMPLEMENTED`
- Frozen target: Legal hold remains effective after the floor expires, clearing a hold does not bypass an active floor, and ordinary runtime identities cannot release evidence.
- Observable acceptance criterion: Pinned-MinIO tests prove hold ON plus the calendar floor on both pair versions, no lifecycle auto-delete, explicit deletion denial, and independent hold/floor behavior.
- Implementation owner: Candidate Retention Enforcement and Read-Back package plus Candidate Legal-Hold Authority Mediation package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05.
- Dependency: CF-007, CF-012, CF-013, and CF-016.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Exact SDK calls, mediation topology, and custody mechanism.
- Conflict or coverage note: Fixed-duration retention is not an indefinite legal hold.

### CF-010 — Ten-calendar-year and leap-day semantics

- Contract: `long_term_10y` uses the accepted exact-version storage timestamp plus ten UTC calendar years, with February 29 mapping to the last valid February day while preserving time.
- Normative source: ADR-0002, “long_term_10y semantics.”
- Current evidence: No canonical class or calendar-year calculation exists; current storage and database clocks independently add 365 days.
- Current evidence state: `NOT_IMPLEMENTED`
- Frozen target: Active COMPLIANCE protection cannot be shortened or downgraded, and a separate case-specific hold does not alter the class deadline.
- Observable acceptance criterion: Deterministic boundary tests cover ordinary dates, February 29, timezone normalization, existing stronger protection, and independent case holds.
- Implementation owner: Candidate Retention Metadata and Policy Assignment package plus Candidate Retention Enforcement and Read-Back package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05.
- Dependency: CF-012 and CF-013.
- Gate effect: `BLOCKING_BEFORE_EXPANSION`
- Deferred details: Calculation library and persisted representation.
- Conflict or coverage note: Ten calendar years must not be approximated as a fixed day count.

### CF-011 — Exact 2,160-hour short retention

- Contract: `short_90d` means exactly 2,160 hours from the normalized authoritative storage timestamp; expiry creates eligibility only and does not authorize or execute deletion.
- Normative source: ADR-0002, “short_90d semantics” and delete-marker/lifecycle semantics.
- Current evidence: No short class or lifecycle eligibility implementation exists; current code uses 365 days.
- Current evidence state: `NOT_IMPLEMENTED`
- Frozen target: UTC arithmetic is independent of daylight-saving and calendar boundaries, and any case hold remains independently effective.
- Observable acceptance criterion: Tests cover DST and calendar boundaries, exactly 2,160 hours, no automatic lifecycle action, and hold behavior after expiry.
- Implementation owner: Candidate Retention Metadata and Policy Assignment package plus Candidate Retention Enforcement and Read-Back package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05.
- Dependency: CF-012 and CF-013.
- Gate effect: `BLOCKING_BEFORE_EXPANSION`
- Deferred details: Future lifecycle authorization and deletion scheduler.
- Conflict or coverage note: Retention expiry, deletion authorization, and physical deletion are separate events.

### CF-012 — Version-specific MinIO enforcement read-back

- Contract: MinIO read-back for the exact returned version is authoritative for storage timestamp, retention mode, retain-until, and legal-hold status.
- Normative source: ADR-0002, record-level metadata contract, retention-duration anchor, and source-of-truth contract.
- Current evidence: `MinioObjectStorage.get` selects no version and exposes no retention or legal-hold metadata; database deadlines are locally calculated; current policies do not provide the complete write/read authority shape.
- Current evidence state: `NOT_IMPLEMENTED`
- Frozen target: PostgreSQL records declared policy and append-only storage-observed enforcement evidence, and mismatches fail closed.
- Observable acceptance criterion: Pinned-MinIO integration tests read exact-version metadata for both pair members, compare normalized values, persist results, and reject missing, unavailable, or contradictory evidence.
- Implementation owner: Candidate Retention Enforcement and Read-Back package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05.
- Dependency: M0-R01 for versioned schema support and M0-R02 for least-privilege database grants.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Pinned server/client/SDK call sequence and alternative server-time contract if required.
- Conflict or coverage note: Bucket defaults, requested values, upload success, and local calculations are not enforcement proof.

### CF-013 — Exact-version storage timestamp anchor

- Contract: `accepted_storage_at_utc` is the exact-version storage-service-observed Last-Modified instant, normalized to UTC `Z` at whole-second precision by truncation.
- Normative source: ADR-0002, “Retention-duration anchor.”
- Current evidence: Domain, storage, and database use independent application or PostgreSQL clocks; no exact-version Last-Modified read-back is persisted.
- Current evidence state: `NOT_IMPLEMENTED`
- Frozen target: Class deadlines derive only from a verified authoritative server-time contract.
- Observable acceptance criterion: Pinned-MinIO tests prove exact-version timestamp acquisition and normalization or explicitly block implementation until an alternative server-time contract is ratified and verified.
- Implementation owner: Candidate Retention Enforcement and Read-Back package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05.
- Dependency: CF-005 and CF-012.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Provider response field or alternative authoritative server-time mechanism.
- Conflict or coverage note: An upload-request, application-host, or PostgreSQL timestamp cannot substitute silently.

### CF-014 — Cross-system non-atomicity and protected orphans

- Contract: MinIO and PostgreSQL do not share an atomic transaction; storage success followed by database failure creates a protected, discoverable orphan reconciled idempotently without weakening storage protection.
- Normative source: ADR-0002, “Source of truth and fail-closed behavior.”
- Current evidence: Sequential object writes precede separate `commit_bronze`, transition, queue, and transition transactions; no protected-orphan registry, discovery job, or retry identity exists.
- Current evidence state: `NOT_IMPLEMENTED`
- Frozen target: PostgreSQL-local policy, pair evidence, state, and audit success commit atomically; OCR is queued only after commit; cross-system failures remain recoverable and fail closed.
- Observable acceptance criterion: Fault-injection tests cover every write boundary, discover every protected orphan, prove monotonic idempotent replay, prevent duplicate commits, and prevent premature OCR.
- Implementation owner: Candidate Bronze Pair Commit and Protected-Orphan Reconciliation package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05.
- Dependency: M0-R01, CF-012, and M0-R03 only if the candidate introduces a new legally enforced state.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Reconciliation scheduling, checkpoints, quarantine naming, and operator tooling.
- Conflict or coverage note: This contract does not claim or require a distributed transaction.

### CF-015 — Legacy 365-day-object reconciliation

- Contract: Every existing Bronze version is inventoried and non-destructively reconciled to the accepted retention contract with monotonic, idempotent enforcement.
- Normative source: ADR-0002, “Existing 365-day-object compatibility.”
- Current evidence: Current objects and rows were designed around 365-day COMPLIANCE; no all-version inventory, null-version repair, legal-hold migration, ambiguity quarantine, or retryable reconciliation exists.
- Current evidence state: `NOT_IMPLEMENTED`
- Frozen target: Every storage version and database row reconciles unambiguously or remains explicitly unresolved; permanent versions receive hold ON and no weaker than the required floor.
- Observable acceptance criterion: A dry-run inventory and controlled integration fixture prove all-version coverage, hash reconciliation, ambiguity quarantine, monotonic retries, and no content overwrite, version collapse, delete-marker removal, or shortened protection.
- Implementation owner: Candidate Legacy Bronze 365-Day Reconciliation package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05.
- Dependency: `BRONZE_PAIR_READY`; this contract precedes `SUCCESS_CONSTRAINT_VALIDATED` and does not require that final constraint to exist first.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Batch sizing, checkpoints, operator interface, and recovery reporting.
- Conflict or coverage note: M0-R01 provides migration machinery but does not own this storage/data reconciliation.

### CF-016 — Legal-hold authority isolation and break-glass clearing

- Contract: Legal-hold ON/OFF authority is unavailable to ordinary API, OCR-worker, reviewer, backup, and ingestion identities; clearing uses a separately governed, audited break-glass boundary.
- Normative source: ADR-0002, “Authority and exception model.”
- Current evidence: No legal-hold implementation or `s3:PutObjectLegalHold` grant exists; therefore neither hold application nor required mediated clearing exists.
- Current evidence state: `NOT_IMPLEMENTED`
- Frozen target: Permanent hold application is isolated or mediated; OFF requires accountable decision evidence and should require dual approval; active COMPLIANCE remains effective.
- Observable acceptance criterion: Permission tests deny ordinary identities, controlled tests exercise mediated ON and break-glass OFF, and immutable decision/audit evidence captures actor, reason, time, and exact object versions.
- Implementation owner: Candidate Legal-Hold Authority Mediation package.
- Implementation ownership state: `UNOWNED_MANDATORY_REMEDIATION`
- Verification owner: M0-R05 for permissions and runtime behavior; Architecture Reviewer for authority design.
- Dependency: M0-R02 only for PostgreSQL role changes; MinIO mediation remains a separate package.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Provider-policy feasibility, broker topology, credential custody, dual approval, and monitoring.
- Conflict or coverage note: M0-R02 cannot absorb MinIO legal-hold mediation because it owns PostgreSQL grants only.

### CF-017 — Versioned migration foundation

- Contract: Database-changing remediation uses a versioned, repeatable migration foundation that safely handles existing volumes.
- Normative source: ADR-0001 Stage 1 and mandatory dependency graph.
- Current evidence: Only `infra/postgres/init.sql` is mounted as first-initialization SQL; no migration directory, migration tool, or lock exists.
- Current evidence state: `NOT_IMPLEMENTED`
- Frozen target: M0-R01 completes before M0-R02, M0-R03, or M0-R04 and before candidate packages make database changes.
- Observable acceptance criterion: M0-R01 supplies ordered migrations, applied-version tracking, upgrade/rollback policy, existing-volume fixture proof, and failure recovery without rebuilding data volumes.
- Implementation owner: M0-R01.
- Implementation ownership state: `OWNED`
- Verification owner: M0-R01 acceptance reviewer; M0-R05 consumes the migrated fixture.
- Dependency: None among remediation tickets; it is the prerequisite foundation.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Migration technology and exact rollout procedure.
- Conflict or coverage note: M0-R01 does not automatically own every schema or data migration described by other contracts.

### CF-018 — Distinct PostgreSQL roles and least privilege

- Contract: API, OCR, review, backup, and administrative database authorities are distinct and restricted to their workflow boundaries.
- Normative source: ADR-0001 mandatory invariant 5 and M0-R02 scope.
- Current evidence: API and OCR worker share `DATABASE_URL` for `smartcoat_app`; that role can insert `silver_verified_records`, `review_decisions`, and `audit_events`; no separate reviewer or backup database role is configured.
- Current evidence state: `CONTRADICTED`
- Frozen target: OCR cannot create verified records, ingestion cannot exercise review authority, backup is read-only, and administrative authority is not used by runtime services.
- Observable acceptance criterion: Migration and grant inspection plus runtime negative tests prove distinct identities and denied cross-boundary operations on existing and fresh volumes.
- Implementation owner: M0-R02.
- Implementation ownership state: `OWNED`
- Verification owner: M0-R05.
- Dependency: M0-R01.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Final role names and credential-delivery mechanism.
- Conflict or coverage note: Distinct MinIO identities do not compensate for shared PostgreSQL authority.

### CF-019 — Centrally enforced legal transition graph

- Contract: The accepted ingestion, OCR, review, verification, rejection, and revision edges are centrally enforced independent of caller-supplied previous and next states.
- Normative source: ADR-0001 mandatory invariant 6 and M0-R03 scope.
- Current evidence: `PostgresRepository.transition` checks only that the current state equals the caller-provided previous state; no allowed-edge table, constraint, trigger, or central graph exists.
- Current evidence state: `CONTRADICTED`
- Frozen target: Illegal edges are impossible through application, worker, direct runtime-role SQL, retries, and concurrent calls.
- Observable acceptance criterion: M0-R03 defines one authoritative graph and exhaustive positive, negative, concurrency, and existing-volume tests prove it.
- Implementation owner: M0-R03.
- Implementation ownership state: `OWNED`
- Verification owner: M0-R05.
- Dependency: M0-R01.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Constraint, trigger, procedure, or equivalent mechanism and any ADR-0002 quarantine-state names.
- Conflict or coverage note: Enumerating valid state values is not legal-edge enforcement.

### CF-020 — OCR and extraction create drafts only

- Contract: OCR and native extraction may create only `DRAFT_UNVERIFIED`; no machine path can create factual `VERIFIED` evidence.
- Normative source: ADR-0001 mandatory invariant 2; Silver review policy; acceptance criterion AT-08.
- Current evidence: `OCRDomainService.complete` and `PostgresRepository.complete_ocr_run` create only `DRAFT_UNVERIFIED`; AT-08 proves the domain path, but the shared PostgreSQL role can directly insert verified rows.
- Current evidence state: `PARTIALLY_IMPLEMENTED`
- Frozen target: Both application logic and database authority prevent OCR from creating or transitioning to verified evidence.
- Observable acceptance criterion: Unit tests retain draft-only behavior and M0-R02/M0-R05 permission tests prove the OCR identity cannot insert verified/review rows or perform verified transitions.
- Implementation owner: Existing OCR implementation plus M0-R02.
- Implementation ownership state: `OWNED`
- Verification owner: M0-R05.
- Dependency: M0-R01, M0-R02, and M0-R03.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Exact database role and stored-operation interface.
- Conflict or coverage note: Application-path discipline alone is insufficient while the worker holds direct verified-write authority.

### CF-021 — Human review before verified Silver

- Contract: A reviewer compares the immutable source, edits or confirms text, selects a decision, and explicitly confirms comparison before a verified revision can exist.
- Normative source: ADR-0001 mandatory invariant 2; Silver review policy; AT-09 and AT-10.
- Current evidence: `ReviewService.review` requires an unverified draft, an approval decision, non-empty text, explicit confirmation, and self-review evidence; the web opens source and draft side by side; shared database grants can bypass this path.
- Current evidence state: `PARTIALLY_IMPLEMENTED`
- Frozen target: Only the review boundary, with enforced identity and decision evidence, can create verified Silver.
- Observable acceptance criterion: Applicable API/domain behavior, direct PostgreSQL permission-denial tests, explicit review/decision evidence, relevant self-review safeguards, transition enforcement, and concurrency/idempotency tests prove verified creation is impossible without complete review evidence.
- Implementation owner: Existing review implementation plus M0-R02, M0-R03, and M0-R04.
- Implementation ownership state: `OWNED`
- Verification owner: M0-R05 and controlled R&D acceptance.
- Dependency: M0-R01 through M0-R04 as defined by ADR-0001.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Multi-user reviewer assignment and later department RBAC.
- Conflict or coverage note: The Phase 1 solo self-review exception remains visible and audited; it is not silent separation of duties. Browser/E2E automation remains F-017 nonblocking technical debt and controlled Stage 2 usability evidence after M0-R05; it is not an M0-R05 pass or real-data-admission condition.

### CF-022 — Atomic and idempotent review/verification

- Contract: Review decision, draft disposition, verified revision, state transition, and audit evidence commit atomically and retries are idempotent.
- Normative source: ADR-0001 mandatory invariant 7 and M0-R04 scope.
- Current evidence: `create_review_decision` performs three writes and an audit in one transaction, but `ReviewService.review` transitions to review before it and performs the final transition in a later transaction; no idempotency key protects retries.
- Current evidence state: `CONTRADICTED`
- Frozen target: One transactional boundary produces one effective outcome and replay cannot duplicate or conflict.
- Observable acceptance criterion: Fault-injection and concurrent-retry tests prove all-or-nothing behavior, stable response replay, no duplicate revisions or decisions, and one final legal state.
- Implementation owner: M0-R04.
- Implementation ownership state: `OWNED`
- Verification owner: M0-R05.
- Dependency: M0-R01.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Idempotency-key source and exact transaction API.
- Conflict or coverage note: Transactional SQL inside `create_review_decision` does not make the whole service operation atomic. Compatibility with the M0-R03 legal transition graph is an M0-R05 integration requirement after M0-R03 and M0-R04 complete independently; this creates no M0-R03-to-M0-R04 prerequisite, and their parallel-versus-sequential scheduling remains undecided.

### CF-023 — Unique effective review decision per draft

- Contract: A Silver draft has at most one effective review decision and cannot yield competing verified outcomes.
- Normative source: ADR-0001 mandatory invariant 7 and M0-R04 scope.
- Current evidence: `review_decisions.silver_draft_id` is a non-unique foreign key; application status checks are raceable; only `(ingestion_id, silver_revision)` is unique for verified records.
- Current evidence state: `CONTRADICTED`
- Frozen target: Database enforcement and idempotent service semantics admit one effective draft decision.
- Observable acceptance criterion: A versioned uniqueness mechanism rejects concurrent distinct decisions while exact retries resolve to the original outcome.
- Implementation owner: M0-R04.
- Implementation ownership state: `OWNED`
- Verification owner: M0-R05.
- Dependency: M0-R01.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Constraint shape if rejected/reopened drafts require an explicit lifecycle model.
- Conflict or coverage note: Primary-key uniqueness on decision ID does not constrain decisions per draft.

### CF-024 — Append-only verified, review, and audit evidence

- Contract: Bronze object records, verified revisions, review decisions, and audit events cannot be updated or deleted through runtime authority.
- Normative source: ADR-0001 mandatory invariants 3 and 4; Phase 1 architecture trust boundary.
- Current evidence: `init.sql` defines exactly four `BEFORE UPDATE OR DELETE` triggers using `reject_immutable_mutation`; static grants omit update/delete on those tables, but no current database integration suite proves triggers and grants on fresh and upgraded volumes.
- Current evidence state: `PARTIALLY_IMPLEMENTED`
- Frozen target: Append-only behavior survives migrations, runtime-role misuse, concurrent operations, and restore.
- Observable acceptance criterion: M0-R05 inspects installed triggers and grants and proves insert-allowed/update-denied/delete-denied behavior for each runtime identity and each protected table.
- Implementation owner: Existing schema plus M0-R02 for corrected grants.
- Implementation ownership state: `OWNED`
- Verification owner: M0-R05.
- Dependency: M0-R01 and M0-R02.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Whether additional enforcement-evidence tables require the same trigger pattern.
- Conflict or coverage note: Static SQL is implementation evidence, not proof that existing volumes have the triggers or correct grants.

### CF-025 — Backend and edge network boundary

- Contract: Backend data services are isolated; only explicitly required localhost-facing entrypoints participate in the non-internal edge network.
- Normative source: ADR-0001 mandatory security invariants and M0-R06 scope; Phase 1 architecture network statement.
- Current evidence: `backend` is internal and all published ports bind `127.0.0.1`, but PostgreSQL, MinIO, and API also join non-internal `edge`; the architecture claims the edge exists only for forwarding while data services participate directly.
- Current evidence state: `CONTRADICTED`
- Frozen target: Network membership and localhost publication expose no broader path than required, with database and object storage unreachable from untrusted edge peers.
- Observable acceptance criterion: M0-R06 topology and reachability tests prove allowed and denied service paths, host bindings, DNS access, and absence of unintended egress.
- Implementation owner: M0-R06.
- Implementation ownership state: `OWNED`
- Verification owner: M0-R05 for integration prerequisites and M0-R06 acceptance reviewer for topology.
- Dependency: M0-R06 is a direct prerequisite for M0-R05.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Exact proxy or host-forwarding topology.
- Conflict or coverage note: Loopback host binding does not make a shared non-internal Docker network internal-only.

### CF-026 — Pinned PostgreSQL/MinIO integration verification

- Contract: M0-R05 verifies repaired contracts against the pinned PostgreSQL, MinIO server, MinIO client image, and Python SDK; it implements no fixes.
- Normative source: ADR-0001 dependency graph and Stage 1 exit; ADR-0002 required implementation and integration evidence.
- Current evidence: Tests are memory-backed or static; no test connects to PostgreSQL or MinIO; M0-T02 baseline remains `BLOCKED`.
- Current evidence state: `UNPROVEN`
- Frozen target: M0-R05 runs only after M0-R02, M0-R03, M0-R04, and M0-R06 complete; M0-R01 is transitive; all gate contracts receive executable evidence.
- Observable acceptance criterion: A reviewable suite reports pinned image/SDK identities, fresh and upgraded volume results, positive and negative permission tests, failure injection, retention read-back, pair/orphan behavior, and clean reruns.
- Implementation owner: M0-R05, verification-only.
- Implementation ownership state: `OWNED`
- Verification owner: M0-R05 acceptance reviewer and Architecture Reviewer.
- Dependency: M0-R01 before M0-R02/M0-R03/M0-R04; M0-R02/M0-R03/M0-R04/M0-R06 before M0-R05.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Test harness mechanics and fixture topology.
- Conflict or coverage note: M0-R05 must reject unmet prerequisites rather than repair them during verification.

### CF-027 — No real company data before M0-R05

- Contract: Real company data is prohibited until M0-R05 passes after all required remediation dependencies.
- Normative source: ADR-0001 Stage 1 exit and dependency graph; ADR-0002 dependencies and compatibility gate.
- Current evidence: Accepted ADRs state the prohibition; repository scanning covers tracked files only and cannot prove all local operator behavior.
- Current evidence state: `NOT_APPLICABLE_CURRENT_STAGE`
- Frozen target: No ingestion authorization, pilot batch, or acceptance claim precedes a recorded M0-R05 pass.
- Observable acceptance criterion: Admission records show completed prerequisites and M0-R05 approval before the first authorized controlled-data event.
- Implementation owner: User/Program Owner as admission authority; M0-R05 supplies the technical prerequisite evidence.
- Implementation ownership state: `OWNED`
- Verification owner: Architecture Reviewer.
- Dependency: CF-017 through CF-026 as applicable.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Deferred details: Controlled acceptance authorization record and data handling logistics.
- Conflict or coverage note: Earlier legacy pilot authorization language is superseded by the accepted ADR gate.

### CF-028 — Controlled 5–10-file R&D acceptance

- Contract: After M0-R05, a controlled 5–10-file real R&D batch exercises difficult photos, technical content, tables/numbers/units, Bronze, OCR/extraction, human correction, verified Silver, accuracy, integrity, and restore evidence.
- Normative source: ADR-0001 Stage 2; `PHASE_1_ACCEPTANCE_REPORT_TEMPLATE.md`; OCR evaluation and backup/restore runbooks.
- Current evidence: The template and scripts exist; synthetic unit tests do not constitute the controlled batch, and no accepted completed report is present.
- Current evidence state: `NOT_APPLICABLE_CURRENT_STAGE`
- Frozen target: Failures remain included, every fact is human-reviewed or rejected, and compatibility of the Repository Pilot Slice is demonstrated before expansion.
- Observable acceptance criterion: An authorized report records per-file word and numeric/unit accuracy, correction rate, usability, Bronze integrity, audit, and isolated restore evidence for 5–10 files.
- Implementation owner: Stage 2 controlled R&D acceptance executor under User/Program Owner authorization.
- Implementation ownership state: `OWNED`
- Verification owner: Architecture Reviewer and User/Program Owner.
- Dependency: Passing M0-R05.
- Gate effect: `BLOCKING_BEFORE_EXPANSION`
- Deferred details: Actual files, reviewers, date, and acceptance thresholds within the ratified template.
- Conflict or coverage note: No real file belongs in Git or may leave the local environment.

### CF-029 — Gold consumes verified Silver only

- Contract: Gold and feature-layer outputs consume only verified Silver inputs and retain lineage to those sources.
- Normative source: ADR-0001 mandatory invariant 9 and Stage 4.
- Current evidence: The Repository Pilot Slice has no Gold implementation; this is intentionally deferred.
- Current evidence state: `NOT_APPLICABLE_CURRENT_STAGE`
- Frozen target: No unverified draft or raw OCR output can become a Gold fact.
- Observable acceptance criterion: Later Gold schemas and jobs enforce verified-source foreign lineage and reject draft-only inputs in unit and integration tests.
- Implementation owner: Future Stage 4 Gold design and implementation package.
- Implementation ownership state: `DEFERRED_LATER_STAGE`
- Verification owner: Future Stage 4 acceptance reviewer.
- Dependency: Controlled R&D acceptance, Stage 3 governed expansion inputs, and verified Silver readiness.
- Gate effect: `INFORMATIONAL`
- Deferred details: Gold schemas, KPIs, metrics, technology, and schedule.
- Conflict or coverage note: This freeze records the invariant without selecting a Gold design.

### CF-030 — AI and embeddings remain derived evidence

- Contract: AI and embedding outputs remain derived, cite verified sources, preserve provenance, and do not silently become facts.
- Normative source: ADR-0001 mandatory invariant 10 and Stage 5; Master Roadmap v2 AI direction.
- Current evidence: No AI, embedding, vector-search, or semantic-search implementation exists in this slice.
- Current evidence state: `NOT_APPLICABLE_CURRENT_STAGE`
- Frozen target: Later AI operates only after verified Silver and Gold readiness with explicit source citations and uncertainty.
- Observable acceptance criterion: Later evaluation and lineage tests reject unsupported outputs and prove every result resolves to verified source evidence.
- Implementation owner: Future Stage 5 AI design and implementation package.
- Implementation ownership state: `DEFERRED_LATER_STAGE`
- Verification owner: Future Stage 5 acceptance reviewer.
- Dependency: Verified Silver, Stage 4 Gold readiness, governance, and approved technology decisions.
- Gate effect: `INFORMATIONAL`
- Deferred details: Models, embeddings, vector database, evaluation framework, UX, and schedule.
- Conflict or coverage note: No technology selection is made by this matrix.

### CF-031 — Separation from SmartCoat Knowledge Object v2

- Contract: This data-lake repository remains architecturally separate from SmartCoat Knowledge Object v2 unless a later accepted decision defines a source-feed contract.
- Normative source: ADR-0001 mandatory invariant 11 and consequences.
- Current evidence: No repository component or contract defines an SKO v2 dependency or shared persistence boundary.
- Current evidence state: `CONFIRMED_IMPLEMENTED`
- Frozen target: Future integration, if any, is one governed feed from verified data and never an implicit merge of repositories or authority.
- Observable acceptance criterion: Architecture review finds no hidden shared schema, credential, deployment, or bidirectional write path; any future feed has its own accepted ADR.
- Implementation owner: Architecture governance owner.
- Implementation ownership state: `OWNED`
- Verification owner: Architecture Reviewer.
- Dependency: A future accepted source-feed decision if integration is proposed.
- Gate effect: `INFORMATIONAL`
- Deferred details: Whether a feed is needed, its payload, direction, cadence, and transport.
- Conflict or coverage note: The separation decision does not prohibit a later explicitly governed source feed.

## Ownership-gap summary

- `OWNED` (14): CF-001, CF-017 through CF-028, and CF-031.
- `PARTIALLY_OWNED` (3): CF-002, CF-003, and CF-006.
- `UNOWNED_MANDATORY_REMEDIATION` (12): CF-004, CF-005, CF-007, CF-008, CF-009, CF-010, CF-011, CF-012, CF-013, CF-014, CF-015, and CF-016.
- `DEFERRED_LATER_STAGE` (2): CF-029 and CF-030.
- Existing remediation boundaries remain unchanged: M0-R01 is migration foundation only; M0-R02 is PostgreSQL roles and grants only; M0-R03 is legal transition enforcement; M0-R04 is review/verification atomicity, uniqueness, and idempotency; M0-R05 is verification-only; M0-R06 is network segmentation.
- The execution relationship among M0-R02, M0-R03, and M0-R04 remains undecided.

## Candidate remediation packages

- Governance status: The five package boundaries below and their candidate-phase sequencing were ratified on 2026-08-21 by the User/Program Owner and Architecture Reviewer. Their `Candidate` names remain descriptive labels because no final M0-R identifiers or executable implementation-ticket owners are assigned. Existing M0-R01–M0-R06 boundaries remain unchanged, detailed implementation-ticket ownership remains unresolved, and this documentation acceptance does not authorize implementation.

### Ratified candidate-phase dependency model

- Sequencing authority: These phases and edges are ratified candidate sequencing. Ratification does not assign new M0-R identifiers, expand any M0-R scope, authorize implementation under this documentation ticket, or decide whether M0-R02, M0-R03, and M0-R04 execute in parallel or sequentially.
- Phase `METADATA_EXPAND`: After M0-R01, introduce compatible metadata and schema primitives without requiring legacy reconciliation to have completed.
- Phase `LEGAL_HOLD_AUTHORITY_READY`: Establish the candidate authority capability required for permanent retention. Provider capability must be validated; any candidate PostgreSQL or schema change still follows M0-R01 without inventing a new ratified M0-R ordering.
- Phase `RETENTION_ENFORCEMENT_READY`: Implement exact-version retention and enforcement read-back using `METADATA_EXPAND` and `LEGAL_HOLD_AUTHORITY_READY`.
- Phase `BRONZE_PAIR_READY`: Implement the fail-closed original/manifest pair and protected-orphan success boundary using the available metadata and enforcement primitives. M0-R03 is additionally required only if this candidate introduces a new legally enforced state.
- Phase `LEGACY_RECONCILED_OR_QUARANTINED`: Use the operational primitives to reconcile or explicitly quarantine legacy null or ambiguous rows and existing protected versions.
- Phase `SUCCESS_CONSTRAINT_VALIDATED`: Validate the final successful-record invariant only after legacy data is safely reconciled or quarantined. Implementation may use a success-only constraint or separate evidence structure, but successful Bronze evidence must be impossible without exact original and manifest version IDs.
- Candidate dependency edge: M0-R01 -> METADATA_EXPAND
- Candidate dependency edge: METADATA_EXPAND -> RETENTION_ENFORCEMENT_READY
- Candidate dependency edge: LEGAL_HOLD_AUTHORITY_READY -> RETENTION_ENFORCEMENT_READY
- Candidate dependency edge: RETENTION_ENFORCEMENT_READY -> BRONZE_PAIR_READY
- Candidate dependency edge: BRONZE_PAIR_READY -> LEGACY_RECONCILED_OR_QUARANTINED
- Candidate dependency edge: LEGACY_RECONCILED_OR_QUARANTINED -> SUCCESS_CONSTRAINT_VALIDATED
- Acyclicity statement: The documented candidate edges form a directed acyclic graph; they do not prescribe a migration framework, schema shape, quarantine table, SDK call, or enforcement mechanism.

### Candidate Retention Metadata and Policy Assignment package

- Problem boundary: Supply versioned schema and policy behavior for exact-version identity, canonical retention classes, data-category assignment, personal-data onboarding denial, and class-duration calculations.
- Included contracts: CF-005, CF-007, CF-008, CF-010, and CF-011.
- Explicit non-goals: MinIO enforcement calls, legal-hold mediation, Bronze orchestration, legacy-object mutation, transition-graph design, and integration verification.
- Prerequisites: M0-R01 precedes `METADATA_EXPAND`; approved policy inputs are required for each onboarded data category. `SUCCESS_CONSTRAINT_VALIDATED` occurs only after `LEGACY_RECONCILED_OR_QUARANTINED`; completion of final metadata hardening is not a prerequisite for legacy reconciliation.
- Downstream verification: M0-R05 for current-scope runtime behavior and later department-onboarding verification for personal-data sources.
- Why it cannot be absorbed safely into an existing M0-R ticket: M0-R01 supplies migration machinery but not every schema or policy migration; M0-R02 through M0-R06 have separate ratified boundaries.

### Candidate Retention Enforcement and Read-Back package

- Problem boundary: Apply and verify exact-version retention/legal-hold policy for both Bronze members using storage-observed timestamps and append-only enforcement evidence.
- Included contracts: CF-002, CF-003, CF-006 for exact-version verification, CF-009, CF-010, CF-011, CF-012, and CF-013.
- Explicit non-goals: Policy authorship, legal-hold OFF authorization, cross-system orphan orchestration, legacy inventory, database role remediation, and verification ownership.
- Prerequisites: `METADATA_EXPAND` and `LEGAL_HOLD_AUTHORITY_READY`; M0-R01 remains required before any candidate database change.
- Downstream verification: M0-R05 against the pinned MinIO server, client image, Python SDK, and PostgreSQL image.
- Why it cannot be absorbed safely into an existing M0-R ticket: No existing M0-R owns MinIO retention implementation; M0-R02 is PostgreSQL-only and M0-R05 cannot implement fixes.

### Candidate Bronze Pair Commit and Protected-Orphan Reconciliation package

- Problem boundary: Make the original/manifest enforcement pair a fail-closed PostgreSQL success boundary and recover cross-system protected orphans idempotently.
- Included contracts: CF-004, CF-006 to the extent exact-version verification belongs inside the successful pair boundary, and CF-014, including post-commit OCR queueing.
- Explicit non-goals: Distributed transactions, review/verification writes, retention-policy semantics, legal-hold custody, and legacy bulk migration.
- Prerequisites: `RETENTION_ENFORCEMENT_READY`; M0-R01 remains required before any candidate database change; M0-R03 is required only if this candidate introduces a new legally enforced state.
- Downstream verification: M0-R05 fault injection across object write, enforcement read-back, PostgreSQL commit, audit, and OCR queue boundaries.
- Why it cannot be absorbed safely into an existing M0-R ticket: M0-R04 is expressly review/verification-only, M0-R03 owns legal edges rather than orchestration, and M0-R05 is verification-only.

### Candidate Legal-Hold Authority Mediation package

- Problem boundary: Separate permanent-hold application and hold-clearing authority from all ordinary runtime identities and define governed break-glass evidence.
- Included contracts: CF-009 and CF-016.
- Explicit non-goals: PostgreSQL service-role design, retention class assignment, lifecycle deletion, user-interface design, and ordinary M0-R05 test implementation.
- Prerequisites: Provider-capability validation for the pinned MinIO release; any candidate database or schema change follows M0-R01; M0-R02 applies only to related PostgreSQL grants. These conditions establish `LEGAL_HOLD_AUTHORITY_READY` without changing the ratified M0-R ordering.
- Downstream verification: M0-R05 permission matrices and controlled ON/OFF integration behavior.
- Why it cannot be absorbed safely into an existing M0-R ticket: M0-R02 is bounded to PostgreSQL roles and cannot safely claim MinIO mediation or break-glass custody.

### Candidate Legacy Bronze 365-Day Reconciliation package

- Problem boundary: Inventory all existing original and manifest versions and reconcile them non-destructively to the accepted policy with ambiguity quarantine and monotonic retries.
- Included contracts: CF-005 and CF-015.
- Explicit non-goals: Rebuilding volumes, overwriting content, collapsing versions, removing delete markers, shortening retention, or designing normal ingestion.
- Prerequisites: `BRONZE_PAIR_READY`, which already depends on the required operational metadata, enforcement, and authority phases. This package produces `LEGACY_RECONCILED_OR_QUARANTINED` before the metadata package validates `SUCCESS_CONSTRAINT_VALIDATED`.
- Downstream verification: M0-R05 with synthetic legacy volumes containing null IDs, multiple versions, delete markers, orphans, hash mismatches, and interrupted retries.
- Why it cannot be absorbed safely into an existing M0-R ticket: M0-R01 is the migration foundation but does not own object-store inventory, classification decisions, or legacy enforcement repair.

## Finding-disposition ledger

### F-001 — No versioned migration foundation

- Finding ID: F-001
- Finding: Only first-initialization SQL exists; no versioned migration tool or directory was found.
- Evidence: `compose.yaml` mounts `infra/postgres/init.sql` at `001-init.sql`; repository path search found no migration framework or lock.
- Disposition: Mandatory remediation retained by CF-017.
- Owner or candidate owner: M0-R01.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Existing-volume migration acceptance evidence before database-changing remediation.
- Reason it is not silently dropped: Every later database repair depends on a safe versioned foundation.

### F-002 — Shared PostgreSQL runtime authority

- Finding ID: F-002
- Finding: API and OCR worker share the `smartcoat_app` database role.
- Evidence: Both Compose services receive the same `DATABASE_URL`; `.env.example` resolves it to `smartcoat_app`.
- Disposition: Mandatory remediation retained by CF-018 and CF-020.
- Owner or candidate owner: M0-R02.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Runtime identities and negative permission tests against fresh and upgraded volumes.
- Reason it is not silently dropped: Shared authority defeats workflow separation even when application call paths differ.

### F-003 — Direct verified-record insert authority

- Finding ID: F-003
- Finding: `smartcoat_app` can insert directly into verified Silver, review-decision, and audit tables.
- Evidence: `infra/postgres/init.sql` grants `SELECT, INSERT` on `silver_verified_records`, `review_decisions`, and `audit_events` to `smartcoat_app`.
- Disposition: Mandatory remediation retained by CF-018, CF-020, and CF-021.
- Owner or candidate owner: M0-R02.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: OCR and ingestion identities are denied verified/review writes while the review boundary retains only necessary authority.
- Reason it is not silently dropped: Draft-only application logic is bypassable with current database authority.

### F-004 — Runtime trigger and grant proof missing

- Finding ID: F-004
- Finding: Exactly four append-only triggers exist in static SQL, but installed trigger and effective-grant behavior is unproven on runtime and upgraded volumes.
- Evidence: `bronze_objects_append_only`, `verified_records_append_only`, `review_decisions_append_only`, and `audit_events_append_only` are declared in `init.sql`; no PostgreSQL integration test connects to a database.
- Disposition: Mandatory verification retained by CF-024 and CF-026; implementation changes are not presumed.
- Owner or candidate owner: M0-R05 after M0-R01 and M0-R02.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Catalog inspection and per-role insert/update/delete positive and negative tests.
- Reason it is not silently dropped: Static initialization text does not prove existing volumes installed or retained the controls.

### F-005 — Hardcoded 365-day retention and absent legal hold

- Finding ID: F-005
- Finding: Bucket bootstrap, application storage, and database metadata independently hardcode 365-day COMPLIANCE; no legal-hold implementation exists.
- Evidence: `bootstrap.sh` uses `COMPLIANCE 365d`; `storage.py` adds 365 days; `database.py` adds a 365-day interval; repository search finds no legal-hold implementation outside ADR-0002.
- Disposition: Mandatory ownership gap retained by CF-007 and CF-009 through CF-016.
- Owner or candidate owner: Candidate retention, authority, orchestration, and legacy-reconciliation packages.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Completed candidate ownership, implementation, and M0-R05 pinned integration evidence.
- Reason it is not silently dropped: The current behavior contradicts the accepted retention semantics and permanent-evidence requirement.

### F-006 — Nullable Bronze version identity

- Finding ID: F-006
- Finding: `bronze_objects.object_version_id` is nullable and missing IDs are not rejected.
- Evidence: `infra/postgres/init.sql` declares `object_version_id text`; domain and repository accept `None` from the SDK result.
- Disposition: Mandatory ownership gap retained by CF-005 and legacy reconciliation CF-015.
- Owner or candidate owner: Candidate Retention Metadata and Policy Assignment package and Candidate Legacy Bronze 365-Day Reconciliation package.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Enforced non-null success records and safe reconciliation or quarantine of legacy nulls.
- Reason it is not silently dropped: Exact-version enforcement and recovery cannot be proven without version identity.

### F-007 — No central allowed-edge transition graph

- Finding ID: F-007
- Finding: State changes compare a supplied previous state but do not validate the requested edge against a central graph.
- Evidence: `PostgresRepository.transition` executes a conditional `UPDATE`; schema constrains values only.
- Disposition: Mandatory remediation retained by CF-019.
- Owner or candidate owner: M0-R03.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Exhaustive legal/illegal edge and concurrency tests against central enforcement.
- Reason it is not silently dropped: Caller-chosen old and new states can form an otherwise illegal edge.

### F-008 — Review operation is not end-to-end atomic

- Finding ID: F-008
- Finding: Enter-review transition, review/verified writes, and final transition use separate transactions.
- Evidence: `ReviewService.review` calls `transition`, then `create_review_decision`, then `transition`; each repository call opens its own connection context.
- Disposition: Mandatory remediation retained by CF-022.
- Owner or candidate owner: M0-R04.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Fault-injection and retry tests demonstrate one all-or-nothing outcome.
- Reason it is not silently dropped: A mid-operation failure can leave evidence and state inconsistent.

### F-009 — Review-decision uniqueness missing

- Finding ID: F-009
- Finding: `review_decisions.silver_draft_id` has no uniqueness constraint.
- Evidence: `init.sql` defines only a non-unique foreign key; no equivalent effective-decision guard exists.
- Disposition: Mandatory remediation retained by CF-023.
- Owner or candidate owner: M0-R04.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Concurrent distinct decisions are rejected and exact retries are idempotent.
- Reason it is not silently dropped: Application status checks do not prevent a database race.

### F-010 — Network-isolation contradiction

- Finding ID: F-010
- Finding: PostgreSQL, MinIO, and API join the non-internal edge network despite the documented backend-isolation boundary.
- Evidence: `compose.yaml` assigns those services to both `backend` and `edge`; only `backend` has `internal: true`.
- Disposition: Mandatory remediation retained by CF-025.
- Owner or candidate owner: M0-R06.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Allowed/denied network reachability and egress evidence after segmentation.
- Reason it is not silently dropped: Loopback publication does not eliminate container-to-container exposure on edge.

### F-011 — MinIO Object Lock integration proof missing

- Finding ID: F-011
- Finding: Current AT-12 is memory-backed and policy-static; it does not prove pinned-MinIO retention, exact-version deletion denial, delete-marker behavior, or legal hold.
- Evidence: `test_at_12_application_cannot_delete_or_overwrite_bronze` uses `MemoryStorage`; no test imports or creates a MinIO client.
- Disposition: Mandatory verification and implementation ownership gaps retained by CF-002 through CF-016 and CF-026.
- Owner or candidate owner: Candidate packages for implementation; M0-R05 for verification.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Pinned-MinIO version-specific positive and negative integration suite.
- Reason it is not silently dropped: In-memory permission exceptions do not prove server-side WORM semantics.

### F-012 — No PostgreSQL/MinIO repaired-contract integration suite

- Finding ID: F-012
- Finding: Current tests do not connect to PostgreSQL or MinIO and cannot prove cross-system repaired contracts.
- Evidence: Test-source search finds no `psycopg.connect`, `Minio(`, browser driver, or service orchestration; M0-T02 product baseline is `BLOCKED`.
- Disposition: Mandatory verification retained by CF-026.
- Owner or candidate owner: M0-R05, verification-only.
- Gate effect: `BLOCKING_BEFORE_REAL_DATA`
- Required proof: Integration suite only after all direct prerequisites complete.
- Reason it is not silently dropped: Unit/static success cannot promote the product baseline or authorize real data.

### F-013 — Dependency reproducibility gaps

- Finding ID: F-013
- Finding: Direct Python dependencies are version-pinned, but there are no hash-locked transitive environments; base images and GitHub actions are not digest/SHA pinned, and apt packages are unversioned.
- Evidence: Requirements contain `==`; no recognized lockfile exists; Dockerfiles use tagged bases and unversioned apt installs; CI uses `actions/checkout@v4` and `actions/setup-python@v5`.
- Disposition: Retained as technical debt; promotion to the real-data gate requires both ratification roles.
- Owner or candidate owner: Build/release engineering owner to be assigned; no remediation ticket is ratified.
- Gate effect: `NON_BLOCKING_TECHNICAL_DEBT`
- Required proof: Rebuild comparison, dependency inventory, and approved pinning policy.
- Reason it is not silently dropped: Version drift can invalidate reproducibility without changing repository source.

### F-014 — PaddlePaddle documentation conflict

- Finding ID: F-014
- Finding: Architecture and acceptance template state PaddlePaddle 3.3.1, while requirements, worker tests, engine identity, and OCR runbook use 3.2.2 because of a recorded 3.3.x regression.
- Evidence: `PHASE_1_ARCHITECTURE.md` and the acceptance template name 3.3.1; `requirements.txt`, `test_worker_contract.py`, `paddle_engine.py`, and `OCR_EVALUATION.md` name 3.2.2.
- Disposition: Retained as documentation correction debt; runtime source of truth remains the pinned build input until corrected.
- Owner or candidate owner: Architecture and acceptance-document owner.
- Gate effect: `NON_BLOCKING_TECHNICAL_DEBT`
- Required proof: One reconciled documented version matching the built image and OCR evidence.
- Reason it is not silently dropped: Conflicting version claims make evidence reports ambiguous.

### F-015 — Authentication and authorization enforcement incomplete

- Finding ID: F-015
- Finding: API data routes require a signed local session, but one configured user is accepted for upload, review, source access, audit access, and revision without role-based endpoint enforcement.
- Evidence: `current_actor` accepts only `LOCAL_USER_ID`; every protected route depends on it; database user roles exist but are not loaded or checked by route capability.
- Disposition: Retained as technical debt for the single-user Repository Pilot Slice; promotion to the real-data gate requires both ratification roles.
- Owner or candidate owner: Repository Pilot security owner to be assigned; no remediation ticket is ratified.
- Gate effect: `NON_BLOCKING_TECHNICAL_DEBT`
- Required proof: Threat model, capability matrix, negative route tests, session tests, and explicit solo-pilot exception review.
- Reason it is not silently dropped: Authentication presence does not prove least-privilege authorization.

### F-016 — Privileged-operation audit coverage incomplete

- Finding ID: F-016
- Finding: Domain events are audited, but login attempts, source reads/downloads, readiness checks, local-user upserts, backup operations, policy/bootstrap changes, and break-glass operations lack one complete audited contract.
- Evidence: `_audit` call sites cover upload, state, OCR queue/recovery, review, and edit paths; `main.py` read/auth routes and `restore-drill.sh` do not append corresponding audit events.
- Disposition: Retained as technical debt; break-glass audit evidence is separately mandatory under CF-016; any broader gate promotion requires both ratification roles.
- Owner or candidate owner: Audit-governance owner to be assigned; Candidate Legal-Hold Authority Mediation package owns hold-clearing audit evidence.
- Gate effect: `NON_BLOCKING_TECHNICAL_DEBT`
- Required proof: Approved event catalog and completeness tests across defined privileged operations.
- Reason it is not silently dropped: Append-only storage does not establish that every security-relevant event is recorded.

### F-017 — Real PDF, Excel, and browser-path evidence missing

- Finding ID: F-017
- Finding: Worker code routes PDF pages to PaddleOCR and Excel to `openpyxl`, and web code renders or downloads source types, but tests do not execute real PDF, Excel, or browser review flows.
- Evidence: Worker contract tests inspect source strings; API acceptance fixtures only use synthetic JPEG/PNG bytes plus invalid protected-file signatures; no browser driver test exists.
- Disposition: Retained as nonblocking technical debt and for controlled Stage 2 R&D acceptance usability evidence after M0-R05; it is not a condition of M0-R05 passing or real-data admission.
- Owner or candidate owner: M0-R05 may collect nonblocking synthetic integration evidence; the Stage 2 controlled R&D acceptance executor owns authorized real-file usability evidence.
- Gate effect: `NON_BLOCKING_TECHNICAL_DEBT`
- Required proof: Nonblocking closure evidence consists of synthetic real-format PDF/Excel integration fixtures and an executed authenticated browser upload/review path, followed later by controlled acceptance evidence.
- Reason it is not silently dropped: Static routing proves wiring intent, not end-to-end extraction or review usability.

### F-018 — Backup and restore evidence status

- Finding ID: F-018
- Finding: A restore-drill script and runbook exist, while AT-13 is an in-memory temporary-directory copy rather than a PostgreSQL/MinIO isolated restore; no accepted completed drill report is present.
- Evidence: `scripts/restore-drill.sh` defines PostgreSQL dump/restore and isolated MinIO checks; `test_at_13_backup_restore_preserves_source_manifest_and_provenance` copies memory-backed bytes and JSON.
- Disposition: Retained as technical debt and Stage 2 acceptance evidence; promotion to the real-data gate before existing gates requires both ratification roles.
- Owner or candidate owner: Backup/restore runbook owner and Stage 2 controlled R&D acceptance executor.
- Gate effect: `NON_BLOCKING_TECHNICAL_DEBT`
- Required proof: Timestamped isolated restore evidence preserving database provenance, original SHA-256, exact versions, manifests, and append-only controls.
- Reason it is not silently dropped: A script and simulated unit test do not prove recoverability of actual service state.

### F-019 — M0-T02 execution-telemetry conflict

- Finding ID: F-019
- Finding: M0-T02 recorded Docker image completion evidence while the captured shell/tool duration conflicted with a user-observed 2h23m+ Codex UI execution and failed-stop event.
- Evidence: The closed M0-T02 reconciliation classified the build `PASS_WITH_ORCHESTRATION_INCIDENT` and the location of the extra delay `UNRESOLVED_EXECUTION_TELEMETRY_CONFLICT`; its overall product baseline remained `BLOCKED`.
- Disposition: Preserved as orchestration evidence uncertainty; it does not invalidate image existence and does not establish a 31-second end-to-end duration.
- Owner or candidate owner: Codex/tooling execution telemetry owner outside repository remediation; M0-R05 must capture independent command and artifact timing evidence.
- Gate effect: `INFORMATIONAL`
- Required proof: Correlated Docker, tool-sandbox, result-delivery, and UI timestamps in a future execution environment.
- Reason it is not silently dropped: Build completion and orchestration end-to-end duration are different claims.

## Dependency freeze

- M0-R01 completes before M0-R02.
- M0-R01 completes before M0-R03.
- M0-R01 completes before M0-R04.
- This document does not decide whether M0-R02, M0-R03, and M0-R04 execute in parallel or sequentially.
- M0-R06 is a hard prerequisite for M0-R05.
- M0-R05 begins only after M0-R02, M0-R03, M0-R04, and M0-R06 complete; M0-R01 is therefore a transitive prerequisite.
- M0-R05 is verification-only and must not implement fixes.
- M0-R05 precedes controlled real-data R&D acceptance.
- Controlled R&D acceptance precedes multi-department expansion.
- Real company data remains prohibited until M0-R05 passes.

## Deferred program decisions

- The second department is not selected.
- Gold schemas, KPIs, metrics, technology, and rollout timing are not selected.
- AI models, embedding models, vector databases, evaluation technology, and rollout timing are not selected.
- SmartCoat Knowledge Object v2 integration is not selected.
- Lifecycle deletion, legal-hold custody details, and remediation-candidate ticket numbering are not selected.

## Human-ratification record

### User/Program Owner

- Decision: `RATIFIED`
- Date: `2026-08-21`
- Rationale: The 31-contract matrix, its 19 finding dispositions, and the ownership classification of 14 `OWNED`, 3 `PARTIALLY_OWNED`, 12 `UNOWNED_MANDATORY_REMEDIATION`, and 2 `DEFERRED_LATER_STAGE` are accepted. All five future remediation packages are explicitly accepted beyond the existing M0-R01–M0-R06 boundaries: Candidate Retention Metadata and Policy Assignment; Candidate Retention Enforcement and Read-Back; Candidate Bronze Pair Commit and Protected-Orphan Reconciliation; Candidate Legal-Hold Authority Mediation; and Candidate Legacy Bronze 365-Day Reconciliation. The `METADATA_EXPAND -> RETENTION_ENFORCEMENT_READY -> BRONZE_PAIR_READY -> LEGACY_RECONCILED_OR_QUARANTINED -> SUCCESS_CONSTRAINT_VALIDATED` candidate-phase sequence, with `LEGAL_HOLD_AUTHORITY_READY` feeding `RETENTION_ENFORCEMENT_READY`, is accepted as candidate sequencing. This acceptance does not assign final M0-R identifiers or authorize implementation under this ticket. Real company data remains prohibited until M0-R05 passes.

### Architecture Reviewer

- Decision: `RATIFIED`
- Date: `2026-08-21`
- Rationale: Independently verified resolution of all four prior blocking findings: CF-022 no longer introduces an `M0-R03 -> M0-R04` dependency; CF-006 is `PARTIALLY_OWNED` with implementation responsibility split between the appropriate retention-enforcement and Bronze-pair packages; CF-021 does not require browser automation before real-data admission while F-017 remains `NON_BLOCKING_TECHNICAL_DEBT`; and CF-005 uses a phased, acyclic legacy-reconciliation model. Independently verified that the documented candidate-phase edges pass `tsort` with exit code 0 and that the 14/3/12/2 ownership counts match the ownership-gap summary. No inconsistency exists with accepted ADR-0001 or ADR-0002.

## Source set

- `README.md`
- `docs/architecture/PHASE_1_ARCHITECTURE.md`
- `docs/architecture/decisions/ADR-0001-master-roadmap-v2-scope-expansion-sequencing.md`
- `docs/architecture/decisions/ADR-0002-retention-semantics-and-enforcement-contract.md`
- `docs/governance/BRONZE_IMMUTABILITY_POLICY.md`
- `docs/governance/SILVER_REVIEW_POLICY.md`
- `docs/governance/PHASE_1_ACCEPTANCE_REPORT_TEMPLATE.md`
- `docs/runbooks/VPS_DEPLOYMENT.md`
- `docs/runbooks/OCR_EVALUATION.md`
- `docs/runbooks/BACKUP_RESTORE.md`
- `compose.yaml`
- `.env.example`
- `infra/postgres/init.sql`
- `infra/minio/bootstrap.sh`
- `infra/minio/policies/app-bronze-write.json`
- `infra/minio/policies/reviewer-read.json`
- `apps/api/src/domain.py`
- `apps/api/src/database.py`
- `apps/api/src/storage.py`
- `apps/api/tests/test_acceptance.py`
- `apps/api/tests/fakes.py`
- *Unified Data & AI Platform — Master Roadmap & Foundational Design v2.0*, August 2026.
