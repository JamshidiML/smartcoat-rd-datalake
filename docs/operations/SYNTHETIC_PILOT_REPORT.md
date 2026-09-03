# WP10 synthetic end-to-end pilot report

## Decision

**Result: `FAIL_SYNTHETIC_END_TO_END_PILOT`**

The resumed pilot stopped at its first acceptance-harness failure, as required.
It did not reveal a new product-contract defect. The production-bound API
recorded three pre-run OCR failures for fixture 2 and rejected the next retry
with HTTP `409` and the reason `OCR retry limit reached (3 attempts); operator
review is required`. This is direct evidence that `P0-16-DEFECT-01` is fixed.

The temporary HTTP evidence client mishandled expected non-2xx responses. Its
`urllib.error.HTTPError` handler populated a result, but the serialization and
print logic was in the `try` statement's `else` block. Python does not execute
that block after a handled exception. The client therefore returned no stdout
for the expected `409`, and the harness classified it as
`NO_CLIENT_RESULT`. The API's sanitized structured log retained the actual
`409` outcome.

This is `FAIL_VERIFICATION_HARNESS`, not `FAIL_PRODUCT_CONTRACT`. WP10 says to
stop and report after a pilot failure, so the harness was not repaired and the
live pilot was not rerun. Fixtures 3–10 and the seven fault scenarios remain
unexecuted in this run.

## 1. Execution identity and checkpoints

- Date: `2026-09-03`
- Integration base: `a824ad1d0bb9d3b36187162c5ba1ee2c800bbfab`
- Execution branch: `agent/wp10-synthetic-pilot`
- Fixture/report checkpoint: `f363eae`
- Solo-review governance prerequisite: `3a343a3`
- P0-16 repair checkpoint: `8d1531f3f055d1eb3af6babff99bacc2be6ee82a`
- Disposable project: `bronzepair-a534a9d2ae19`
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

Candidate source hashes embedded in the built images matched the repository:

- `apps/api/src/database.py`: `4189a4863c84736d2638c5462ac41909795c54ea544cbe65eab67adc0203d005`
- `apps/api/src/domain.py`: `3159f4860a760375724f16017cb8fc65ebd09fe53f09be9ded12841195aa36d5`
- `apps/api/src/main.py`: `0678fb4dc5814d92e46ced6d2984e18acf17da86d10355e2887654d0e72e280c`
- `apps/api/src/storage.py`: `b40b13f3a217d04e1216ddfdc517af028e3f97c8ba653863e594e110f9e8f087`
- `apps/ocr-worker/src/jobs/worker.py`: `c1dcef0f47596b4651de825bcb401a8f5ce6a83f183c6585251d332bb3bb3acf`

## 2. P0-16 repair and regression

The repair uses conditional accounting in
`PostgresRepository.mark_ocr_failed()`. A job that is still `QUEUED` failed
before `start_ocr_run()` and is incremented there. A `RUNNING` job was already
incremented by `start_ocr_run()` and is not incremented again. This is the
narrowest change that counts every failed worker attempt exactly once without
double-counting the normal path.

No migration was required. The existing `ocr_jobs_one_per_ingestion` constraint
still guarantees one job per ingestion. The existing R02 column-level
`smartcoat_ocr` grant already permits `attempt_count` along with the other OCR
status columns; no grant was widened.

The new regression injects failure before `start_ocr_run()`. Against
`a824ad1d0bb9d3b36187162c5ba1ee2c800bbfab`, it failed with expected `1`, actual
`0`, and the production SQL lacked conditional accounting. After `8d1531f`, the
focused recovery suite passed all `12` tests. It proves counts `1`, `2`, and `3`
and rejects the next retry at `OCR_MAX_ATTEMPTS=3`.

## 3. Reproducible fixture set and declared outcomes

The generator is `scripts/generate-synthetic-pilot-fixtures.py`. Generated
binaries remain outside Git.

1. `01-clean-handwritten-lab-note.png`
   - Bytes: `20099`
   - SHA-256: `8b160b2fd7554e53d1f5f5e340efc7c509eeff5a04efb9d5bc07cf39f2479c81`
   - Expected: `VERIFIED` or bounded `OCR_FAILED`
2. `02-poor-light-skewed-lab-note.png`
   - Bytes: `26067`
   - SHA-256: `7cfc784b0ec868d3022e0ec48ef79475b6b92deaf0e4954ddbfb6e18f8f7a2f9`
   - Expected: `VERIFIED` or bounded `OCR_FAILED`
3. `03-rotated-90-scan.png`
   - Bytes: `30906`
   - SHA-256: `6ace82fca186fce857ceeaec9f307c75f9865fa2fa215c99a3be1dd39a2a1d2b`
   - Expected: `VERIFIED` or bounded `OCR_FAILED`
4. `04-multipage-technical-report.pdf`
   - Bytes: `1282`
   - SHA-256: `506a7985b9a7ae4c9b79bb3900f564ab514b953266f1c2e5a7912c93a94dab96`
   - Expected: `VERIFIED` or bounded `OCR_FAILED`
5. `05-measurement-sheet.xlsx`
   - Bytes: `1632`
   - SHA-256: `3bb6da33c0bcf0a598b8b2353fb157711f93d801c9e83ab1457aafbd020ff1f4`
   - Expected: `VERIFIED` or bounded `OCR_FAILED`
6. `06-photo-of-screen.png`
   - Bytes: `7872`
   - SHA-256: `6cf7cc4b64d8edb5c279c23ebee29919a93483262d00c3da6f977e9749449067`
   - Expected: `VERIFIED` or bounded `OCR_FAILED`
7. `07-byte-identical-duplicate.png`
   - Bytes: `20099`
   - SHA-256: `8b160b2fd7554e53d1f5f5e340efc7c509eeff5a04efb9d5bc07cf39f2479c81`
   - Expected: a second independently version-addressed, retention-protected,
     Legal-Hold-protected Bronze pair linked to fixture 1 by
     `duplicate_of_ingestion_id`; four stored objects across the two events;
     distinct `pair_identity_sha256` values despite equal payload hashes
8. `08-over-50mb.jpg`
   - Bytes: `52428801`
   - SHA-256: `d824d08a7160201a7318d1da8cef127849bf91a92f0eea5cce384bae760a25b7`
   - Expected: `FILE_TOO_LARGE`, audited, no Bronze object
9. `09-unsupported.txt`
   - Bytes: `37`
   - SHA-256: `b3245727f2f1a5af5e8a2fded09d9042557d0d480c7669631ffdbb4828598b98`
   - Expected: `UNSUPPORTED_TYPE`, audited, no Bronze object
10. `10-corrupt-valid-extension.pdf`
    - Bytes: `61`
    - SHA-256: `d5dc50276b1920beb87d39063a1f76518b3eec12651942d089fa260319615516`
    - Expected: `CORRUPT_FILE` or bounded `OCR_FAILED`

## 4. Expected versus actual outcomes

- Fixture 1: `PARTIAL_PASS`
  - Upload HTTP status: `201`
  - Ingestion: `01a06760-a165-7d14-9963-3f87e4c161cf`
  - OCR job: `8de951ea-f8e6-4d31-8c32-e2d9c4c2a918`
  - OCR attempt count: `1`
  - Reached: `SILVER_DRAFT_READY`
  - Human review was not run before the fail-closed stop.
- Fixture 2: `PRODUCT_PATH_PASS_HARNESS_CLASSIFICATION_FAIL`
  - Upload HTTP status: `201`
  - Ingestion: `01a06761-41aa-7279-b32f-57d10a65d0e9`
  - OCR job: `61b4df29-ff2d-42e0-81e6-5b4fbeee44b3`
  - Three intentionally injected pre-run exact-version retrieval failures were
    counted.
  - Retry log records exposed attempt counts `1` and `2`.
  - The next request was rejected by the API at count `3` with HTTP `409`.
  - The temporary client emitted no result for that expected HTTP error and
    stopped the harness.
- Fixtures 3–10: `NOT_RUN_AFTER_FAIL_CLOSED_STOP`

Because the stop preceded the protection query, this run does not claim a
complete original-plus-manifest protection table. The upload responses retained
the original object keys, exact original version IDs, hashes, retention class,
and policy, but independent storage read-back was not reached.

## 5. Sanitized retry-bound evidence

The retained API log contains these decisive records:

```json
{"actor_id":"usr_wp10_synthetic","attempt_count":1,"correlation_id":"4f7d4a56-2ad7-4029-9145-336d3b1e551d","event":"ocr.job.queued","ingestion_id":"01a06761-41aa-7279-b32f-57d10a65d0e9","level":"INFO","max_attempts":3,"ocr_job_id":"61b4df29-ff2d-42e0-81e6-5b4fbeee44b3","recovery":true,"service":"api"}
{"actor_id":"usr_wp10_synthetic","attempt_count":2,"correlation_id":"4cea2ace-c4dc-4013-b186-4dd03dd031ef","event":"ocr.job.queued","ingestion_id":"01a06761-41aa-7279-b32f-57d10a65d0e9","level":"INFO","max_attempts":3,"ocr_job_id":"61b4df29-ff2d-42e0-81e6-5b4fbeee44b3","recovery":true,"service":"api"}
{"actor_id":"usr_wp10_synthetic","correlation_id":"a8cbc4e1-e6c8-4972-8e1a-b9881d278941","error_type":"StateConflict","event":"upload.state_conflict.rejected","ingestion_id":"01a06761-41aa-7279-b32f-57d10a65d0e9","level":"WARNING","reason":"OCR retry limit reached (3 attempts); operator review is required","service":"api"}
{"correlation_id":"a8cbc4e1-e6c8-4972-8e1a-b9881d278941","duration_ms":16.905,"event":"request.completed","level":"INFO","method":"POST","path":"/api/uploads/01a06761-41aa-7279-b32f-57d10a65d0e9/retry-ocr","service":"api","status_code":409}
```

No Bearer token, password, MinIO credential, database URL, raw Authorization
header, raw Docker output, or raw PostgreSQL output is present in the retained
structured evidence.

## 6. Fault-injection and orphan-window status

The seven WP10 fault scenarios were not reached after the fail-closed stop:

- PostgreSQL killed before Bronze commit: `NOT_RUN`
- PostgreSQL killed after object writes and before pair commit: `NOT_RUN`
- MinIO killed mid-ingest: `NOT_RUN`
- Retry of an already-completed ingestion: `NOT_RUN`
- Two concurrent reviews of one draft: `NOT_RUN`
- OCR failure, bounded retry, and exhaustion: product boundary observed, but
  harness outcome invalidated by the client defect
- PostgreSQL killed after MinIO enforcement and before evidence insertion:
  `NOT_RUN`

The decisive orphan-window scenario was not reached. This run provides no new
evidence for or against escalation of the orphan discovery sweep. Its ratified
deferred status remains unchanged.

## 7. State transitions and lineage

Fixture 1 reached `RECEIVED → BRONZE_COMMITTED → OCR_QUEUED → OCR_RUNNING →
SILVER_DRAFT_READY`. Fixture 2 reached `RECEIVED → BRONZE_COMMITTED →
OCR_QUEUED`, then repeated the legal `OCR_FAILED → OCR_QUEUED` recovery edge
until the configured bound rejected further retry. All observed edges belong to
the accepted transition graph.

The run stopped before human review and the final lineage query. It therefore
does not claim `VERIFIED`, review rejection, duplicate lineage, or end-to-end
lineage coverage.

## 8. Problems encountered

Two OCR image build invocations were inadvertently active concurrently while
the execution tool was returning an asynchronous session identifier. Both
completed with the same candidate tag; no pilot resources existed yet and no
repository file changed. The final immutable OCR image ID was authenticated
before the pilot.

The live pilot then exposed the temporary client defect described in the
decision section. This was outside repository and production code. In accordance
with the no-iteration rule, it was not changed after the run and the pilot was
not repeated.

## 9. What this pilot did not prove

This run did not prove:

- human verification of fixture 1;
- end-to-end outcomes for fixtures 3–10;
- PDF and Excel extraction;
- corrected fixture-7 duplicate linkage, four-object count, or distinct pair
  identities;
- validation rejection and audit behavior for fixtures 8–10;
- review rejection or concurrent-review behavior;
- the PostgreSQL, MinIO, orphan-window, completed-retry, or retention-evidence
  fault scenarios;
- final independent per-object retention and Legal Hold read-back;
- a complete lineage query or transition trace.

No claim of `P0-16` pilot readiness is made.

## 10. Preserved evidence and cleanup

Sanitized evidence was written before Docker cleanup:

- Evidence directory: `/private/tmp/smartcoat-wp10-evidence-3fcaebe0c6ec`
- Pre-cleanup partial evidence SHA-256:
  `5593eaa0b0df14e6c6a7095530741ee6754f0ed3450f4d2a5901b4e996491b0a`
- Pre-cleanup manifest SHA-256:
  `89ca72bd64d61e9764961d37d1a6ee6b8ccf399113f12e242c718c6807252cec`
- Final sanitized evidence SHA-256:
  `920bf7ec06be557cb85abe2573a59e55eb0ff05500beeb5eaef6fdee1904e0e3`

Docker inventory before and after cleanup:

- Containers: `11 → 11`
- Networks: `12 → 12`
- Volumes: `7 → 7`
- Inventory fingerprint before and after:
  `6a8679177eb1436031a46b8e6b948abd9f4e977373ac48e0cfa9b42287b81de6`
- Owned resources remaining: zero containers, zero networks, zero volumes

No normal-runtime delete, shortening, or Legal Hold release was invoked. The
owned disposable MinIO volume was removed only by mandatory finalization after
evidence preservation.

## 11. Verification results retained before the pilot

- Solo-review focused acceptance: `15` passed
- P0-16 focused OCR recovery suite before fix at `a824ad1`: failed as expected
  with attempt count `0` instead of `1`
- P0-16 focused OCR recovery suite after fix: `12` passed
- API suite after fix: `85` passed
- OCR suite after fix: `12` passed
- PostgreSQL offline suite after fix: `128` passed, `17` external checks skipped
- Python compilation of affected files: exit `0`
- Compose render: exit `0`
- Repository scan: `AT-14 passed`
- `git diff --check`: exit `0`
- Ruff was not installed in the local host environment and was not run

No baseline command was rerun after the live pilot stopped.

## 12. Protected hashes

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

## 13. Gate status

- WP11 prerequisite: `COMPLETE`
- `P0-16-DEFECT-01`: `FIXED_AND_REGRESSION_VERIFIED`
- WP10 live pilot: `BLOCKED_VERIFICATION_HARNESS`
- Fixture 1: `IN_PROGRESS_AT_SILVER_DRAFT_READY_WHEN_DISPOSABLE_RUN_ENDED`
- Fixture 2: `BOUNDED_OCR_FAILURE_PRODUCT_PATH_VERIFIED`
- Fixtures 3–10: `NOT_STARTED_IN_RESUMED_RUN`
- Orphan-window decision: `UNRESOLVED`; existing deferral unchanged
- M0-R05: `NOT_STARTED`
- Platform: `BLOCKED`
- Real company data: `PROHIBITED`
