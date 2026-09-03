# WP10 synthetic end-to-end pilot report

## Decision

**Result: `PASS_SYNTHETIC_END_TO_END_PILOT`**

The result is a composition of two preserved, sanitized executions. The first
completed fixtures 1–10 and fault scenarios 1–3, then stopped on
`P0-16-DEFECT-02` at fault scenario 4. After the independently scoped WP13 fix,
the second execution resumed at fault scenario 4 and ran only scenarios 4–7.
Fixtures 1–10 and fault scenarios 1–3 were not rerun.

`P0-16-DEFECT-02` is closed. `PostgresRepository.ensure_ocr_queued()` now locks
the upload row and returns an existing OCR job before applying the state guard
that controls creation of a new job. The creation guard itself is unchanged:
without an existing job, OCR remains unqueueable unless the ingestion is
`BRONZE_COMMITTED` or `OCR_QUEUED`. This makes reconciliation read-idempotent
after lifecycle advancement without weakening the pre-commit boundary.

`P0-16-DEFECT-01` also remains closed: pre-run OCR failures were counted exactly
once, and the retry bound was enforced at three attempts in both relevant live
observations.

## 1. Execution identity

- Execution date UTC: `2026-09-03`
- Local report date (Europe/Berlin): `2026-09-04`
- Branch: `agent/wp10-synthetic-pilot`
- Fixture/fault-1–3 commit: `45a299ab76699d9da03a6b1e179ffef635ae8fee`
- Fault-4–7 candidate commit: `b030a0b7288f663ca9af85f051494c3d342d1a69`
- Integration base: `a824ad1d0bb9d3b36187162c5ba1ee2c800bbfab`
- Disposable project: `bronzepair-d6884a43cf6e`
- Synthetic data only: yes
- Existing containers, volumes, networks, `./.local-data/`, archives, and real
  company data accessed: no
- Fault-4–7 disposable project: `bronzepair-47e63b3673a9`

Immutable images:

- API: `sha256:fc1aee8b138354a98f30f064533a082bb73dfd72383b653f6c17fef906d051ec`
- WP13 API: `sha256:b6ebad7011dc3494ebabf5729d966f776ec8a3ff140ede49d9dc5e61acd25b4b`
- OCR worker: `sha256:fd8dfe1cb15204b3b0f956b491900ba6348e9ba94ccd52c8f59e1d0a69e54545`
- Legal Hold mediator: `sha256:bbb170b5a8eed05a179db672e7528b222e8c93c433ffdf79605fd9e8045d57ef`
- PostgreSQL: `sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94`
- MinIO: `sha256:d249d1fb6966de4d8ad26c04754b545205ff15a62e4fd19ebd0f26fa5baacbc0`
- MinIO client: `sha256:fb8f773eac8ef9d6da0486d5dec2f42f219358bcb8de579d1623d518c9ebd4cc`

Embedded candidate source hashes matched the repository:

- `apps/api/src/database.py` in the WP13 image:
  `c873bdf1de826d71f5a2895d03ee9461d0ed70f425ac96d219dff23b739dd7f9`
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

### 7.4 Retry already-completed ingestion — PASS after WP13

- Target state before the request: `VERIFIED`.
- Endpoint: `POST /api/uploads/{ingestion_id}/reconcile-bronze`.
- Result: HTTP `200`, `ALREADY_COMMITTED`.
- Pair rows before/after: `1 → 1`.
- Bronze object rows before/after: `2 → 2`.
- OCR jobs before/after: `1 → 1`.
- State before/after: `VERIFIED → VERIFIED`.
- The complete independently queried before/after snapshots were equal.

The regression was written before the fix and failed at `1e4a6f22`: the static
production-order assertion proved the existing-job lookup followed the state
guard. After the narrow ordering repair, the focused Bronze-pair suite passed
`13/13`. The same suite also reconciles `VERIFIED`, `REVIEW_REJECTED`, and
`OCR_FAILED` through the domain boundary and retains the negative pre-pair
case.

### 7.5 Concurrent reviews — PASS

- Responses: one HTTP `200`, one expected HTTP `422`.
- Persisted review decisions: exactly `1`.
- Persisted verified records: exactly `1`.
- Final state: `VERIFIED`.
- The rejected response retained the expected different-effective-decision
  classification.

### 7.6 Bounded OCR retry — PASS

- Fixture 2: attempts `1`, `2`, `3`, then HTTP `409`.
- Fixture 5: attempts `1`, `2`, `3`, then HTTP `409`.
- Terminal state for both: `OCR_FAILED`.
- Bronze evidence remained preserved.

The resumed fault-6 execution independently observed counts `1`, `2`, and `3`,
then an expected HTTP `409`; its final state was `OCR_FAILED`.

### 7.7 PostgreSQL killed after storage enforcement before evidence insert — PASS

Before reconciliation, the retained database snapshot showed `RECEIVED` with
zero pair, object, orphan, reconciliation, and OCR-job rows. The failed client
observed HTTP `500` after both exact MinIO versions had been stored and
protected.

The ordinary application identity then rediscovered the exact original and
manifest versions and reconciliation returned `RECONCILED`. The resulting state
contained exactly one pair, two Bronze object rows, two durable orphan-evidence
rows, one reconciliation row, and one OCR job. Repeated reconciliation returned
`ALREADY_COMMITTED` without changing those counts.

Both exact versions independently read back with matching SHA-256,
`COMPLIANCE` retention, a present retain-until value, and Legal Hold `ON`.
No invisible orphan was observed; the deferred orphan discovery sweep remains
P2.

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

The first execution retained the accepted fixture transition evidence. The
resume execution intentionally did not recreate fixtures 1–10 merely to produce
a combined aggregate; its fault-specific snapshots and counts are direct live
queries, not inferred results.

## 9. Structured correlation evidence

The protected-write orphan recovery retained one correlation chain with:

- Ingestion: `01a06960-b05a-7f51-b200-eeff22d45fdd`
- OCR job: `7e22a462-a0e9-476b-9d5f-28f21aff2ea9`
- First reconciliation: `RECONCILED`
- Repeated reconciliation: `ALREADY_COMMITTED`
- Pair identity and exact original/manifest versions retained in the sanitized
  evidence file.

The original completed-ingestion failure remains retained as defect evidence.
The repaired replay used ingestion
`01a06976-c8e6-795e-a63f-77c4fb1cbfd8` and returned the existing OCR job with
HTTP `200 ALREADY_COMMITTED` while leaving its exact snapshot unchanged.

No Bearer token, password, MinIO credential, database URL, or raw Authorization
header is present in the retained structured evidence.

## 10. Problems encountered

Before the decisive run, two evidence-instrument defects were corrected without
changing repository product code:

1. Expected `urllib.error.HTTPError` responses were not serialized because the
   print logic was incorrectly placed in a `try/else` block.
2. The protection query selected nonexistent `e.outcome` instead of migration
   `0007`'s `e.enforcement_verification_result`.

Both instrument changes were preserved as standalone diffs. The WP13 resume
instrument initially failed because a retained helper's default evidence path
was bound before the helper module was rebound. That generated project was
ownership-cleaned to zero resources before a rerun. The instrument was repaired
outside the repository to own its evidence path directly; no product code was
changed by that repair.

## 11. What this pilot did not prove

The completed pilot did not score OCR transcription accuracy. That was
explicitly outside this synthetic contract pilot's scope. It also does not
claim that the two preserved executions share one database; the authorized
resume used a fresh disposable project and carried forward the authenticated
first-run evidence rather than rerunning completed work.

## 12. Evidence preservation and cleanup

- Evidence directory: `/private/tmp/smartcoat-wp10-evidence-c0214622d581`
- Pre-cleanup partial evidence SHA-256:
  `090b62b77b5ad9810d512107f9374557c9c3f7ac405f95f23219b887efe74561`
- Pre-cleanup manifest SHA-256:
  `4cb43068bc7d41dbe0a057054abde9bf0572e9d5105ac84dc0d555f72067fbf0`
- Final sanitized evidence SHA-256:
  `120eb887c8e0fd79b90c2c380357c95b2dab53b876d99395db926eac2002682d`
- WP13 fault-4–7 evidence directory:
  `/private/tmp/smartcoat-wp13-evidence-e9b22ee55478`
- WP13 fault-4–7 final sanitized evidence SHA-256:
  `6530fba65898288a21315e78f08a33ff4996f56e217fc0ebef0899d065fcf9b2`
- WP13 resume-instrument file SHA-256:
  `c06a129a33a1ba7a32856c809c4746fa5a01684f41db2e50b33b8cb7cdb4755c`
- WP13 resume-instrument no-index diff: `592` lines, SHA-256
  `9050278372dfa73503ebf586de089f021d191c86fe74a1794ecc4ab69fbebcbf`

Docker inventory before and after cleanup:

- Containers: `11 → 11`
- Networks: `12 → 12`
- Volumes: `7 → 7`
- Inventory fingerprint before and after:
  `6a8679177eb1436031a46b8e6b948abd9f4e977373ac48e0cfa9b42287b81de6`
- Owned resources remaining: zero containers, zero networks, zero volumes

The WP13 resume execution independently observed the same `11/12/7` counts and
the same inventory fingerprint before and after cleanup. Its generated project
also had zero remaining containers, networks, and volumes.

## 13. Retained regression and CI evidence

- Solo-review focused acceptance: `15` passed
- P0-16 focused suite before fix at `a824ad1`: failed as expected
- P0-16 focused suite after attempt-accounting fix: `12` passed
- WP13 Bronze-pair regression before fix at `1e4a6f22`: `13` run, `1` expected
  failure in the production-order assertion
- WP13 Bronze-pair regression after fix: `13` passed
- API suite after WP13: `88` passed
- OCR suite: `12` passed
- PostgreSQL offline suite: `128` passed, `17` external checks skipped
- Python compilation: exit `0`
- Compose render: exit `0`
- Repository scan: `AT-14 passed`
- CI for candidate `45a299a`: success, run `33805174882`
- Offline repaired-client classification check: pass for expected HTTP `400`,
  `409`, `413`, `415`, and `422`; unexpected HTTP `500`; and transport failure

No test was weakened. WP13 changed only the existing-job/state-guard ordering
and its regression tests. No migration, policy, credential, state, transition
edge, unique constraint, or RBAC grant changed.

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
- Faults 7.4–7.7: `PASS`
- `P0-16-DEFECT-02`: `CLOSED`
- WP10: `PASS_SYNTHETIC_END_TO_END_PILOT`
- M0-R05: `NOT_STARTED`
- Platform: `BLOCKED`
- Real company data: `PROHIBITED`
