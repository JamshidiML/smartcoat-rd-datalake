# WP10 synthetic end-to-end pilot report

## Decision

**Result: `FAIL_SYNTHETIC_END_TO_END_PILOT`**

The pilot stopped at the first product-contract failure, as required. The real
OCR worker can fail before `start_ocr_run()` increments `ocr_jobs.attempt_count`.
Those failures enter `OCR_FAILED`, but the counter remains zero, so the bounded
operator-retry limit is never reached. A fourth operator retry was accepted with
HTTP 200 and `attempt_count: 0` after three observed worker failures.

Defect identifier: `P0-16-DEFECT-01`.

No production code, governance document, migration, state, transition edge,
OCR engine, or preprocessing configuration was changed. The remaining fixture
and fault scenarios were not run after this failure.

## 1. Execution identity

- Date: `2026-09-03`
- Starting branch: `integration/m0-stage1-parallel-20260827`
- Starting commit: `a824ad1d0bb9d3b36187162c5ba1ee2c800bbfab`
- Execution branch: `agent/wp10-synthetic-pilot`
- Disposable project: `bronzepair-ac37cadd2ff5`
- Synthetic data only: yes
- Existing containers, volumes, networks, `./.local-data/`, and archives used:
  no

Immutable images:

- API: `sha256:cd276d9b3b8c3c083bb037cc88be592306f531337b0268be85cd8e29a14a8d92`
- OCR worker: `sha256:ad9d479532cfb309c38023b7a305176332a360c72af0a760b120f3511f378d46`
- Legal Hold mediator: `sha256:bbb170b5a8eed05a179db672e7528b222e8c93c433ffdf79605fd9e8045d57ef`
- PostgreSQL: `sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94`
- MinIO: `sha256:d249d1fb6966de4d8ad26c04754b545205ff15a62e4fd19ebd0f26fa5baacbc0`
- MinIO client: `sha256:fb8f773eac8ef9d6da0486d5dec2f42f219358bcb8de579d1623d518c9ebd4cc`

The API image contained byte-identical copies of the candidate `database.py`,
`domain.py`, `storage.py`, and `main.py`. The OCR image contained byte-identical
copies of `database.py`, `domain.py`, `storage.py`, and `jobs/worker.py`.

## 2. Reproducible fixture set

The generator is `scripts/generate-synthetic-pilot-fixtures.py`. It uses only
the Python standard library. Two independent generations produced identical
file and manifest hashes. Generated binaries remain outside Git.

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
   - Expected: a distinct, independently protected Bronze pair linked to
     fixture 1 through `duplicate_of_ingestion_id`
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
    - Expected: `CORRUPT_FILE` validation rejection or bounded `OCR_FAILED`

## 3. Expected versus actual outcome

- Fixture 1: `NOT_RUN_AFTER_FAIL_CLOSED_STOP`
- Fixture 2: `FAIL`
  - Upload HTTP status: `201`
  - Ingestion: `01a06702-b29d-7ccf-83e9-a39c37c0006f`
  - OCR job: `6a66f672-fa8f-4f4d-a744-02e1c4b2458c`
  - Upload progressed through `RECEIVED`, `BRONZE_COMMITTED`, and
    `OCR_QUEUED`.
  - The worker was intentionally given an invalid synthetic MinIO identity so
    exact-version source retrieval failed through the real worker exception
    boundary.
  - Three worker failures were observed as `OCR_FAILED`; the first two were
    returned to `OCR_QUEUED` by the authenticated operator endpoint.
  - After the third failure, the expected fourth call was an HTTP 409 retry-limit
    rejection. Actual result: HTTP 200, `status: QUEUED`, `max_attempts: 3`,
    `attempt_count: 0`.
- Fixtures 3–10: `NOT_RUN_AFTER_FAIL_CLOSED_STOP`

## 4. Retained Bronze protection evidence

The fixture-2 upload response retained:

- Original bucket: `sc-rd-bronze-originals`
- Original key:
  `rd/2026/09/01a06702-b29d-7ccf-83e9-a39c37c0006f/original/02-poor-light-skewed-lab-note.png`
- Exact original version:
  `312dd697-3592-4872-8dc8-d68903555c48`
- Original SHA-256:
  `7cfc784b0ec868d3022e0ec48ef79475b6b92deaf0e4954ddbfb6e18f8f7a2f9`
- Retention class: `permanent`
- Retention policy: `smartcoat_retention_2026_08_v1`

The upload reached `BRONZE_COMMITTED`, which the database transition guard
requires to have a committed pair, before it reached `OCR_QUEUED`. However, the
temporary harness stopped before it copied the manifest version, pair identity,
enforcement row, retain-until value, and independent storage Legal Hold
read-back into retained evidence. Those values are therefore **not claimed** by
this report. The disposable database and MinIO volume were then cleaned.

## 5. Product defect evidence

`apps/ocr-worker/src/jobs/worker.py` performs these operations in order:

1. `claim_next_job()` selects a queued job.
2. `process_job()` reads the exact source version from MinIO.
3. Only after that read succeeds does the domain service call
   `start_ocr_run()`.

`apps/api/src/database.py::claim_next_job()` only selects the job. It does not
claim it by changing its status or increment the attempt counter.
`PostgresRepository.start_ocr_run()` increments `attempt_count`, but that method
is downstream of the exact-version MinIO read. The worker catches a retrieval
failure and calls `mark_ocr_failed()`, which records the unchanged zero counter.
`retry_failed_ocr()` consequently evaluates `0 >= 3` as false forever.

The existing unit test uses `MemoryRepository` and explicitly calls
`OCRDomainService.start()` before each injected failure. That increments the
fake counter and proves only post-start failures. It does not cover the real
worker's pre-start source-read failure boundary.

This is a product defect, not an OCR-quality failure and not an evidence-harness
failure. A MinIO permission error, exact-version retrieval error, or temporary
storage outage can therefore create unlimited operator retries despite the
configured bound.

## 6. Fault-injection status

- OCR failure, bounded retry, and exhaustion: `FAIL`
  - Resulting state was discoverable through the upload and OCR job.
  - Bronze evidence was not mutated by retries.
  - The configured retry bound was not enforced.
- PostgreSQL kill before Bronze commit: `NOT_RUN_AFTER_FAIL_CLOSED_STOP`
- Kill after successful object writes and before database pair commit:
  `NOT_RUN_AFTER_FAIL_CLOSED_STOP`
- MinIO kill mid-ingest: `NOT_RUN_AFTER_FAIL_CLOSED_STOP`
- Retry completed ingestion: `NOT_RUN_AFTER_FAIL_CLOSED_STOP`
- Concurrent review: `NOT_RUN_AFTER_FAIL_CLOSED_STOP`
- Kill after MinIO retention and before enforcement-evidence insertion:
  `NOT_RUN_AFTER_FAIL_CLOSED_STOP`

## 7. Orphan-window decision

The decisive orphan-window scenario was not reached because the pilot stopped
at `P0-16-DEFECT-01`. This run provides no new evidence for or against an orphan
discovery sweep. The previously ratified deferred status remains unchanged; the
sweep is neither implemented nor escalated by this incomplete run.

## 8. State transitions observed

The retained API records show:

- `null → RECEIVED`
- `RECEIVED → BRONZE_COMMITTED`
- `BRONZE_COMMITTED → OCR_QUEUED`
- `OCR_FAILED → OCR_QUEUED`, repeated for the accepted retry calls

Every observed transition is in the accepted 11-edge graph. The complete
transition set was not exercised because the pilot stopped early.

## 9. Structured correlation evidence

The upload correlation ID and OCR job ID were both
`6a66f672-fa8f-4f4d-a744-02e1c4b2458c`. Retained structured events include:

```json
{"actor_id":"usr_wp10_synthetic","correlation_id":"6a66f672-fa8f-4f4d-a744-02e1c4b2458c","event":"state.transition","ingestion_id":"01a06702-b29d-7ccf-83e9-a39c37c0006f","level":"INFO","service":"api","state_from":null,"state_to":"RECEIVED"}
{"actor_id":"system","correlation_id":"6a66f672-fa8f-4f4d-a744-02e1c4b2458c","event":"state.transition","ingestion_id":"01a06702-b29d-7ccf-83e9-a39c37c0006f","level":"INFO","service":"api","state_from":"RECEIVED","state_to":"BRONZE_COMMITTED"}
{"correlation_id":"6a66f672-fa8f-4f4d-a744-02e1c4b2458c","event":"ocr.job.queued","ingestion_id":"01a06702-b29d-7ccf-83e9-a39c37c0006f","level":"INFO","ocr_job_id":"6a66f672-fa8f-4f4d-a744-02e1c4b2458c","service":"api"}
{"actor_id":"usr_wp10_synthetic","attempt_count":0,"event":"ocr.job.queued","ingestion_id":"01a06702-b29d-7ccf-83e9-a39c37c0006f","level":"INFO","max_attempts":3,"ocr_job_id":"6a66f672-fa8f-4f4d-a744-02e1c4b2458c","recovery":true,"service":"api"}
```

Thirty-six sanitized API records were retained. No Bearer token, database URL,
password, MinIO credential, or raw Authorization header is present in this
report.

## 10. Problems encountered

Before the actual pilot, a disposable harness-validation attempt falsely treated
the synthetic user ID and display name as secret material. It stopped before a
fixture upload and restored the exact Docker inventory. The temporary detector
was narrowed to actual generated secrets. This was a harness issue and did not
exercise or alter product behavior.

The subsequent actual pilot encountered `P0-16-DEFECT-01` and stopped without a
second product attempt. The defect was not fixed or bypassed.

## 11. What this pilot did not prove

Because the fail-closed stop occurred during fixture 2, this run did not prove:

- end-to-end OCR and human verification for fixtures 1 and 3–7;
- PDF or Excel extraction;
- duplicate provenance linkage and distinct pair identities;
- size, unsupported-type, or corrupt-file rejection;
- review rejection or concurrent-review behavior;
- the PostgreSQL, MinIO, pair-commit, retention-evidence, or completed-retry
  fault scenarios;
- the decisive orphan-window outcome;
- independent per-object retention and Legal Hold read-back for the pilot set;
- an end-to-end lineage query;
- a complete state-transition trace.

No claim of `P0-16` readiness is made.

## 12. Preserved evidence and cleanup

Sanitized diagnostic evidence was written before Docker cleanup:

- Evidence directory:
  `/private/tmp/smartcoat-wp10-evidence-f6e35dadc528`
- Pre-cleanup partial evidence SHA-256:
  `7f8fe2ca4741c5a5023fabcc904be87a289a0c35a63299a14b8e3c0100a86f5f`
- Pre-cleanup manifest SHA-256:
  `41a7e62e99f481156b797efac2675f59eb20e6fba765c8fdfd6485f3383ee437`
- Final sanitized evidence SHA-256:
  `e1fc4a18843f2057fb979822f6285f3fa9782e687ad278aa59041d0a287d9c15`

Docker inventory before and after cleanup:

- Containers: `11 → 11`
- Networks: `12 → 12`
- Volumes: `7 → 7`
- Inventory fingerprint:
  `6a8679177eb1436031a46b8e6b948abd9f4e977373ac48e0cfa9b42287b81de6`
  before and after
- Owned resources remaining: zero containers, zero networks, zero volumes

No Bronze object was deleted, shortened, or released through a normal runtime
operation. The entire owned disposable MinIO volume was removed only during the
mandatory acceptance cleanup after evidence preservation.

## 13. Gate status

- `P0-16`: `FAILED_OPEN_DEFECT`
- `P0-16-DEFECT-01`: `OPEN`
- Platform: `BLOCKED`
- Real company data: `PROHIBITED`
- M0-R05, fresh-volume switch, `main` merge, and PR: not started

## 14. Non-live regression results

These checks ran once after the fail-closed stop; the live pilot was not rerun.

- `ruff 0.12.11 check apps scripts infra`: exit `0`, all checks passed
- MinIO offline suite: exit `0`, `23` passed
- Network offline suite: exit `0`, `14` passed
- PostgreSQL offline suite: exit `0`, `128` passed, `17` skipped external
- Migration lifecycle focused regression: exit `0`
- Migration rollback focused regression: exit `0`
- Migration lock focused regression: exit `0`
- Migration checksum/name-drift focused regression: exit `0`
- Migration history-drift focused regression: exit `0`
- Review atomicity focused regression: exit `0`
- State-transition focused regression: exit `0`
- API suite: exit `0`, `82` passed
- OCR suite: exit `0`, `12` passed
- Generator Python compilation: exit `0`
- `docker compose --env-file .env.example config --quiet`: exit `0`
- `git diff --check`: exit `0`

The green offline suites do not override the live failure. In particular, the
existing retry test injects failure only after `start_ocr_run()` and therefore
does not exercise `P0-16-DEFECT-01`.

Protected migration and governance hashes remained unchanged:

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
