# WP10 synthetic end-to-end pilot report

## Decision

**Result: `FAIL_SYNTHETIC_END_TO_END_PILOT`**

The repaired evidence instrument completed all ten fixture paths and the first
three fault scenarios. It then stopped at the first new product-contract
failure, as required.

Defect identifier: `P0-16-DEFECT-02`.

Calling Bronze reconciliation for an already `VERIFIED` ingestion returned HTTP
`409` with `OCR cannot be queued before a successful Bronze pair commit`, rather
than the intended idempotent `ALREADY_COMMITTED` result. Source inspection
confirms the cause: `BronzeIngestionService.reconcile()` detects the existing
pair but calls `ensure_ocr_queued()` before returning `ALREADY_COMMITTED`.
`ensure_ocr_queued()` rejects every state except `BRONZE_COMMITTED` and
`OCR_QUEUED` before it checks for the existing OCR job. Consequently the replay
contract becomes unreachable once a completed ingestion advances to
`SILVER_DRAFT_READY`, `UNDER_HUMAN_REVIEW`, `VERIFIED`, or `REVIEW_REJECTED`.

This is `FAIL_PRODUCT_CONTRACT`, not a harness failure. No product fix was made,
and the pilot was not rerun after the finding. The remaining fault scenarios
were not executed.

`P0-16-DEFECT-01` remains closed: pre-run OCR failures were counted exactly
once, and the retry bound was enforced at three attempts in this live run.

## 1. Execution identity

- Execution date UTC: `2026-09-03`
- Local report date (Europe/Berlin): `2026-09-04`
- Branch: `agent/wp10-synthetic-pilot`
- Candidate commit: `45a299ab76699d9da03a6b1e179ffef635ae8fee`
- Integration base: `a824ad1d0bb9d3b36187162c5ba1ee2c800bbfab`
- Disposable project: `bronzepair-d6884a43cf6e`
- Synthetic data only: yes
- Existing containers, volumes, networks, `./.local-data/`, archives, and real
  company data accessed: no

Immutable images:

- API: `sha256:fc1aee8b138354a98f30f064533a082bb73dfd72383b653f6c17fef906d051ec`
- OCR worker: `sha256:fd8dfe1cb15204b3b0f956b491900ba6348e9ba94ccd52c8f59e1d0a69e54545`
- Legal Hold mediator: `sha256:bbb170b5a8eed05a179db672e7528b222e8c93c433ffdf79605fd9e8045d57ef`
- PostgreSQL: `sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94`
- MinIO: `sha256:d249d1fb6966de4d8ad26c04754b545205ff15a62e4fd19ebd0f26fa5baacbc0`
- MinIO client: `sha256:fb8f773eac8ef9d6da0486d5dec2f42f219358bcb8de579d1623d518c9ebd4cc`

Embedded candidate source hashes matched the repository:

- `apps/api/src/database.py`: `4189a4863c84736d2638c5462ac41909795c54ea544cbe65eab67adc0203d005`
- `apps/api/src/domain.py`: `3159f4860a760375724f16017cb8fc65ebd09fe53f09be9ded12841195aa36d5`
- `apps/api/src/main.py`: `0678fb4dc5814d92e46ced6d2984e18acf17da86d10355e2887654d0e72e280c`
- `apps/api/src/storage.py`: `b40b13f3a217d04e1216ddfdc517af028e3f97c8ba653863e594e110f9e8f087`
- `apps/ocr-worker/src/jobs/worker.py`: `c1dcef0f47596b4651de825bcb401a8f5ce6a83f183c6585251d332bb3bb3acf`

## 2. P0-16 attempt-accounting result

The accepted repair conditionally increments in `mark_ocr_failed()` when the
pre-update job status is `QUEUED`. A `RUNNING` job was already counted by
`start_ocr_run()` and is not incremented again. The statement remains atomic
under the existing locked job row.

No migration was required. `ocr_jobs_one_per_ingestion` remains sufficient, and
the existing R02 `smartcoat_ocr` column-level UPDATE grant already includes
`attempt_count`; no grant was widened.

The regression failed against `a824ad1` with expected count `1`, actual count
`0`, and passed after the repair with 12 focused tests. The live pilot then
observed attempts `1`, `2`, and `3` and an expected HTTP `409` rejection of the
next retry.

## 3. Reproducible fixtures and hashes

The standard-library generator is
`scripts/generate-synthetic-pilot-fixtures.py`. Generated binaries remained
outside Git.

1. `01-clean-handwritten-lab-note.png` — `20099` bytes —
   `8b160b2fd7554e53d1f5f5e340efc7c509eeff5a04efb9d5bc07cf39f2479c81`
2. `02-poor-light-skewed-lab-note.png` — `26067` bytes —
   `7cfc784b0ec868d3022e0ec48ef79475b6b92deaf0e4954ddbfb6e18f8f7a2f9`
3. `03-rotated-90-scan.png` — `30906` bytes —
   `6ace82fca186fce857ceeaec9f307c75f9865fa2fa215c99a3be1dd39a2a1d2b`
4. `04-multipage-technical-report.pdf` — `1282` bytes —
   `506a7985b9a7ae4c9b79bb3900f564ab514b953266f1c2e5a7912c93a94dab96`
5. `05-measurement-sheet.xlsx` — `1632` bytes —
   `3bb6da33c0bcf0a598b8b2353fb157711f93d801c9e83ab1457aafbd020ff1f4`
6. `06-photo-of-screen.png` — `7872` bytes —
   `6cf7cc4b64d8edb5c279c23ebee29919a93483262d00c3da6f977e9749449067`
7. `07-byte-identical-duplicate.png` — `20099` bytes —
   `8b160b2fd7554e53d1f5f5e340efc7c509eeff5a04efb9d5bc07cf39f2479c81`
8. `08-over-50mb.jpg` — `52428801` bytes —
   `d824d08a7160201a7318d1da8cef127849bf91a92f0eea5cce384bae760a25b7`
9. `09-unsupported.txt` — `37` bytes —
   `b3245727f2f1a5af5e8a2fded09d9042557d0d480c7669631ffdbb4828598b98`
10. `10-corrupt-valid-extension.pdf` — `61` bytes —
    `d5dc50276b1920beb87d39063a1f76518b3eec12651942d089fa260319615516`

## 4. Expected versus actual outcome

- Fixture 1: expected `VERIFIED` or bounded `OCR_FAILED`; actual `VERIFIED` in
  one OCR attempt and one explicit human review.
- Fixture 2: expected `VERIFIED` or bounded `OCR_FAILED`; actual `OCR_FAILED`
  after exactly three deliberately injected pre-run retrieval failures. Further
  retry returned HTTP `409` with `EXPECTED_REJECTION`.
- Fixture 3: expected `VERIFIED` or bounded `OCR_FAILED`; actual `VERIFIED` in
  one OCR attempt and one explicit human review.
- Fixture 4: expected `VERIFIED` or bounded `OCR_FAILED`; actual `VERIFIED` in
  one extraction attempt and one explicit human review. The PDF path executed.
- Fixture 5: expected `VERIFIED` or bounded `OCR_FAILED`; actual `OCR_FAILED`
  after exactly three attempts. Further retry returned HTTP `409`. The Excel
  extraction path executed and failed honestly without fabricated Silver fact.
- Fixture 6: expected `VERIFIED` or bounded `OCR_FAILED`; actual `VERIFIED`.
  Two concurrent reviews produced exactly one HTTP `200` decision and one
  expected HTTP `422` conflict.
- Fixture 7: expected a second, independently protected provenance pair linked
  to fixture 1; actual `VERIFIED`, linked through
  `duplicate_of_ingestion_id=01a0695f-22b5-7d28-a9ed-12e5c9c17116`.
- Fixture 8: expected size rejection; actual audited `FILE_TOO_LARGE`, HTTP
  `422`, no Bronze growth.
- Fixture 9: expected type rejection; actual audited `UNSUPPORTED_TYPE`, HTTP
  `422`, no Bronze growth.
- Fixture 10: expected validation rejection or bounded `OCR_FAILED`; actual
  audited `CORRUPT_FILE`, HTTP `422`, no Bronze growth.

An additional synthetic copy of fixture 3 exercised human review rejection and
reached `REVIEW_REJECTED` through the application review boundary.

## 5. Corrected fixture-7 provenance result

Fixture 7 created a second original-plus-manifest pair rather than deduplicating
away the upload event:

- Payload SHA-256 equal to fixture 1: yes
- `duplicate_of_ingestion_id` points to fixture 1: yes
- Fixture-1 pair identity:
  `565e01259b4d3b6d18aad5a093c8135dce1bf9f54a80cf29eb9da30b195e673b`
- Fixture-7 pair identity:
  `7a3b80a4d448641538009b9a1ec134fc0df4fc3a6f397c479325261f1fdcc871`
- Pair identities distinct: yes
- Stored objects across the two ingestion events: `4`

This proves two distinct provenance events with independent protection
obligations, not an identity collision.

## 6. Base-set protection evidence

For fixtures 1–7, the database returned 14 exact object records and storage
read-back returned the same 14 exact versions:

- Original objects: `7`
- Manifest objects: `7`
- Retention class: `permanent` for all
- Database retention mode: `COMPLIANCE` for all
- Database retain-until value present: yes for all
- Enforcement verification result: `SUCCESS` for all
- Storage retention mode: `COMPLIANCE` for all
- Storage retain-until value present: yes for all
- Storage Legal Hold: `ON` for all
- Exact-version SHA-256 read-back matched: yes for all

No Bronze object was deleted, shortened, or released by any normal-runtime
operation during the completed portion of the pilot.

## 7. Fault-injection results

### 7.1 PostgreSQL killed before Bronze commit — PASS

- Client observed HTTP `500`.
- Matching upload rows after PostgreSQL recovery: `0`.
- Result: no half-recorded ingestion and no Bronze commit.
- Discoverable: yes.
- Recoverable: yes; ordinary retry can create a new ingestion event.

### 7.2 PostgreSQL killed after two protected writes before pair commit — PASS

Before reconciliation:

- State: `RECEIVED`
- Pair rows: `0`
- Bronze object rows: `0`
- Durable orphan rows: `0`
- Reconciliation rows: `0`
- OCR jobs: `0`

The ordinary API reconciliation path rediscovered the exact versions through
the application MinIO identity, independently re-read and protected both, and
returned `RECONCILED`. After reconciliation:

- Pair rows: `1`
- Bronze object rows: `2`
- Durable orphan rows: `2`
- Reconciliation rows: `1`
- OCR jobs: `1`
- State: `OCR_QUEUED`

Repeated reconciliation returned `ALREADY_COMMITTED` with the same OCR job and
identical counts. Both exact versions read back as `COMPLIANCE`, retain-until
present, and Legal Hold `ON`.

Explicit orphan-window decision: **no invisible orphan was observed, and this
run does not escalate the deferred discovery sweep to P1.** Exact-version
discovery and recovery succeeded through the ordinary application boundary.

### 7.3 MinIO killed after RECEIVED and before original write — PASS

- Persisted state: `RECEIVED`
- Pair/object/orphan/reconciliation/job counts: all `0`
- Reconciliation returned expected HTTP `409` with `Protected Bronze pair is
  incomplete`; no partial record was treated as fact.
- After MinIO recovery, the documented operator re-upload created one new
  ingestion event with one pair, two objects, and one OCR job.
- The failed `RECEIVED` ingestion remained discoverable.
- Recoverable on the same ingestion: no.
- Recoverable by explicit provenance-preserving re-upload: yes.

### 7.4 Retry already-completed ingestion — FAIL

- Target state before the request: `VERIFIED`.
- Endpoint: `POST /api/uploads/{ingestion_id}/reconcile-bronze`.
- Actual result: HTTP `409`.
- Actual classification: `StateConflict`.
- Actual reason: `OCR cannot be queued before a successful Bronze pair commit`.
- Expected result: idempotent `ALREADY_COMMITTED` with the existing OCR job and
  no additional pair, object, or job.

The source path rejects before returning its documented existing-pair result.
The disposable database was cleaned after evidence preservation, so this report
does not substitute a post-cleanup inference for a retained after-state query.

### 7.5 Concurrent reviews — product boundary observed before stop

- One review returned HTTP `200` and created the verified outcome.
- The competing review returned expected HTTP `422` with `The Silver draft
  already has a different effective review decision`.
- The later dedicated database count was not reached because fault 7.4 stopped
  execution; no stronger exactly-one database claim is made here.

### 7.6 Bounded OCR retry — PASS

- Fixture 2: attempts `1`, `2`, `3`, then HTTP `409`.
- Fixture 5: attempts `1`, `2`, `3`, then HTTP `409`.
- Terminal state for both: `OCR_FAILED`.
- Bronze evidence remained preserved.

### 7.7 PostgreSQL killed after storage enforcement before evidence insert

`NOT_RUN_AFTER_FAIL_CLOSED_STOP`.

## 8. Observed state transitions and lineage

The retained structured logs observed only accepted edges, including:

- `null → RECEIVED`
- `RECEIVED → BRONZE_COMMITTED`
- `BRONZE_COMMITTED → OCR_QUEUED`
- `OCR_QUEUED → OCR_RUNNING`
- `OCR_RUNNING → SILVER_DRAFT_READY`
- `OCR_RUNNING → OCR_FAILED`
- `OCR_FAILED → OCR_QUEUED`
- `SILVER_DRAFT_READY → UNDER_HUMAN_REVIEW`
- `UNDER_HUMAN_REVIEW → VERIFIED`
- `UNDER_HUMAN_REVIEW → REVIEW_REJECTED`

The final aggregate transition and lineage queries were after the failing fault
and were not reached. No complete-graph or final-lineage claim is made.

## 9. Structured correlation evidence

The protected-write orphan recovery retained one correlation chain with:

- Ingestion: `01a06960-b05a-7f51-b200-eeff22d45fdd`
- OCR job: `7e22a462-a0e9-476b-9d5f-28f21aff2ea9`
- First reconciliation: `RECONCILED`
- Repeated reconciliation: `ALREADY_COMMITTED`
- Pair identity and exact original/manifest versions retained in the sanitized
  evidence file.

The completed-ingestion replay failure retained correlation ID
`b0bae51a-1a87-4556-8247-30ef8240f94b`, the `StateConflict` classification,
reason, request path, and HTTP `409` completion record.

No Bearer token, password, MinIO credential, database URL, or raw Authorization
header is present in the retained structured evidence.

## 10. Problems encountered

Before the decisive run, two evidence-instrument defects were corrected without
changing repository product code:

1. Expected `urllib.error.HTTPError` responses were not serialized because the
   print logic was incorrectly placed in a `try/else` block.
2. The protection query selected nonexistent `e.outcome` instead of migration
   `0007`'s `e.enforcement_verification_result`.

Both instrument changes were preserved as standalone diffs. The final run then
stopped on `P0-16-DEFECT-02`; that product defect was not fixed.

## 11. What this pilot did not prove

The stopped run did not prove:

- idempotent completed-ingestion reconciliation;
- a final database count of exactly one effective concurrent-review decision;
- fault 7.7, the retention-evidence insertion failure window;
- the final all-object protection audit after every fault-created ingestion;
- a final aggregate lineage result or complete transition trace;
- OCR transcription accuracy, which was explicitly outside scope.

## 12. Evidence preservation and cleanup

- Evidence directory: `/private/tmp/smartcoat-wp10-evidence-c0214622d581`
- Pre-cleanup partial evidence SHA-256:
  `090b62b77b5ad9810d512107f9374557c9c3f7ac405f95f23219b887efe74561`
- Pre-cleanup manifest SHA-256:
  `4cb43068bc7d41dbe0a057054abde9bf0572e9d5105ac84dc0d555f72067fbf0`
- Final sanitized evidence SHA-256:
  `120eb887c8e0fd79b90c2c380357c95b2dab53b876d99395db926eac2002682d`

Docker inventory before and after cleanup:

- Containers: `11 → 11`
- Networks: `12 → 12`
- Volumes: `7 → 7`
- Inventory fingerprint before and after:
  `6a8679177eb1436031a46b8e6b948abd9f4e977373ac48e0cfa9b42287b81de6`
- Owned resources remaining: zero containers, zero networks, zero volumes

## 13. Retained regression and CI evidence

- Solo-review focused acceptance: `15` passed
- P0-16 focused suite before fix at `a824ad1`: failed as expected
- P0-16 focused suite after fix: `12` passed
- API suite: `85` passed
- OCR suite: `12` passed
- PostgreSQL offline suite: `128` passed, `17` external checks skipped
- Python compilation: exit `0`
- Compose render: exit `0`
- Repository scan: `AT-14 passed`
- CI for candidate `45a299a`: success, run `33805174882`
- Offline repaired-client classification check: pass for expected HTTP `400`,
  `409`, `413`, `415`, and `422`; unexpected HTTP `500`; and transport failure

No test was weakened. No migration, production source, policy, credential,
state, or transition edge was changed by the pilot.

## 14. Protected hashes

- `0001`: `7f34c9aba3819a49a5bb6c83f75bceaf436009d36c1c62eb46d0ddfa425529e5`
- `0002`: `1f3f3b3faa3340c503bad0e844b08af6af5312546a35c5d2ab399fd6e105dffe`
- `0003`: `50f454fae23f36694466c69163f111c9f852f6af421b822225e6575f1666f9da`
- `0004`: `6eb2819018134e8c790fbd463181a12d996ca0eb40bc4d73680b8f949782a8da`
- `0005`: `f2b7b958df0a010ded58004d3027ead7f8f1e07ee80b0e25f5bb9f1de9e1c0bc`
- `0006`: `6a7dd3d1d3c5b2f4059fb8116eef9da65de2bd0850b62372ed57e70e04659b31`
- `0007`: `d41f94417e8c2c50a001ace2210a2e2ac2d0cee5aafbdbdf305285f11791e0f2`
- `0008`: `fcda14c29c31ab05b832b4dd0e83d3de31ef7a9c40038e1855784c757356ec6d`
- `0009`: `5ca2d455a0c6f73dc614a9640ad4570ddc5e84984d1a54eb2105e69f703acdc7`
- `0010`: `99bfa918ae725f948a499d4285e6609a1a90f239113ea9cc94f354bd636b1391`
- ADR-0001: `afb78304621b383c2e187698beaaf0017037fa7c450063f326a05a9f71e5eaeb`
- ADR-0002: `307ce9d9484b3819d16c5178a3dc61fb56e257376779e679e4923b1e7f5beb37`
- Contract-freeze matrix:
  `cece377662dcb5224fa70226e4200f14017745615a6b31024c792cdc9d33de12`

## 15. Gate status

- `P0-16-DEFECT-01`: `CLOSED`
- WP10 harness repair: `COMPLETE`
- Fixtures 1–10: `EXECUTED`
- Base Bronze exact-version protection: `PASS`
- Corrected fixture-7 provenance: `PASS`
- Faults 7.1–7.3: `PASS`
- Fault 7.4: `FAIL_PRODUCT_CONTRACT`
- Faults 7.5–7.6: `OBSERVED_BEFORE_STOP`
- Fault 7.7: `NOT_RUN`
- `P0-16-DEFECT-02`: `OPEN`
- WP10: `BLOCKED`
- M0-R05: `NOT_STARTED`
- Platform: `BLOCKED`
- Real company data: `PROHIBITED`
