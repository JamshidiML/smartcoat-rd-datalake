# Restore drill report — blocked before backup

## Result

- Date: 2026-09-02 UTC
- Repository commit tested: `3a4c4b40457d2b9b5d88b0bc622a76ed3224c32f`
- Branch: `agent/wp6-restore-drill`
- Disposable project: `sc-wp6-restore-20260902`
- Classification: **FAIL — source deployment cannot reach the required review outcomes**
- Platform status: **BLOCKED**
- Real company data: **PROHIBITED**

The drill stopped before backup, destruction, or restore. The isolated runtime
role `smartcoat_review` cannot read `public.bronze_objects`, but the review
service calls `PostgresRepository.get_upload()`, which joins that table. Every
attempt to approve or reject a synthetic Silver draft therefore returned HTTP
500 with PostgreSQL `InsufficientPrivilege`.

The central WP6 question — whether COMPLIANCE retention and Legal Hold survive
backup and restore — is **not answered**. No pass is claimed.

No product code, migration, policy, Compose file, or backup behavior was changed
to work around the failure.

## Isolation and immutable images

Only synthetic fixtures were used. The disposable PostgreSQL and MinIO data
directories were under `/tmp/smartcoat-wp6-restore.KeQOEK/`. The repository's
`./.local-data/` directory and the archive
`~/smartcoat-volume-archive-20260830.tar.gz` were never read, mounted, copied,
or modified.

No pre-existing container was stopped, started, restarted, joined, or modified.
All created containers and networks carried the disposable Compose project
identity.

Immutable image IDs:

| Component | Immutable image ID | Architecture |
| --- | --- | --- |
| PostgreSQL 17.6 | `sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94` | `linux/arm64` |
| MinIO server | `sha256:d249d1fb6966de4d8ad26c04754b545205ff15a62e4fd19ebd0f26fa5baacbc0` | `linux/arm64` |
| MinIO client/bootstrap | `sha256:fb8f773eac8ef9d6da0486d5dec2f42f219358bcb8de579d1623d518c9ebd4cc` | `linux/arm64` |
| API/migration/role provisioning | `sha256:cd276d9b3b8c3c083bb037cc88be592306f531337b0268be85cd8e29a14a8d92` | `linux/arm64` |
| Legal-hold mediator | `sha256:bbb170b5a8eed05a179db672e7528b222e8c93c433ffdf79605fd9e8045d57ef` | `linux/arm64` |
| OCR worker | `sha256:ad9d479532cfb309c38023b7a305176332a360c72af0a760b120f3511f378d46` | `linux/amd64` |

## Existing backup and restore procedure assessment

The repository contains `scripts/restore-drill.sh`.

Its backup path covers both systems:

1. PostgreSQL is captured with a custom-format `pg_dump` using the read-only
   `smartcoat_backup` identity.
2. The MinIO data root is copied with:

   ```sh
   cp -a "$minio_source/." "$destination/minio-data/"
   ```

The source disposable MinIO data root contained `.minio.sys`, including bucket
metadata paths for `sc-rd-bronze-originals`, `sc-rd-bronze-manifests`, and
`sc-rd-ocr-artifacts`. Because `cp -a source/.` includes dot-prefixed entries,
the procedure is designed to include `.minio.sys` rather than only visible
object payloads.

However, the backup command was not executed because the earlier source-state
gate failed. Therefore no backup artifact exists and this run does not prove
that the resulting archive actually contains complete, coherent lock,
retention, or Legal-Hold metadata.

The existing procedure copies the live MinIO filesystem without an explicit
snapshot or quiesce boundary. That creates an unverified consistency risk if
MinIO metadata changes during the copy. The restore half also restores
PostgreSQL into a new database on the currently running PostgreSQL service and
mounts the copied MinIO tree read-only; it is not itself proof of recovery onto
two destroyed, fresh empty volumes. These observations were not repaired in
WP6.

## Source deployment construction

### Migration and role state

The first ordinary migration attempt correctly failed closed because the fresh
bootstrap database was unmanaged:

```text
Migration error: Database is unmanaged; M0-R01.2 adoption is required before ordinary migration apply
```

The required explicit adoption then succeeded, followed by migrations
`0002`–`0009` and role provisioning:

```text
Adoption result: status=ADOPTED database=smartcoat_wp6 evidence_inserted=true
Migration run complete: discovered=9 already_applied=1 applied_now=8
```

The migration ledger contained versions `0001`–`0009` exactly once and the
state graph contained 11 legal edges.

### Synthetic input attempts

- Four synthetic Excel workbooks were accepted by upload validation but all
  failed in the OCR worker with `InvalidFileException` before a draft could be
  produced.
- One exact-byte duplicate Excel upload was detected and linked through
  `duplicate_of_ingestion_id`; it encountered the same OCR failure.
- One deliberately malformed but upload-valid PDF reached `OCR_FAILED` with
  `PDFPageCountError`.
- One `.txt` upload was rejected at validation with HTTP 422 and
  `UNSUPPORTED_TYPE`.
- Four synthetic PNGs successfully reached `SILVER_DRAFT_READY`.

The Excel failures were retained as a problem encountered. No product change
was made. PNG fixtures were added only to continue constructing the required
reviewable synthetic source state.

### Partial source-state snapshot at the stop point

| Upload state | Count |
| --- | ---: |
| `OCR_FAILED` | 6 |
| `SILVER_DRAFT_READY` | 4 |
| `VERIFIED` | 0 |
| `REVIEW_REJECTED` | 0 |
| Total persisted uploads | 10 |

| Table | Rows |
| --- | ---: |
| `uploads` | 10 |
| `users` | 1 |
| `bronze_objects` | 20 |
| `bronze_pairs` | 10 |
| `bronze_protected_orphans` | 0 |
| `bronze_reconciliation_events` | 0 |
| `bronze_retention_assignments` | 20 |
| `bronze_retention_enforcement_evidence` | 20 |
| `ocr_jobs` | 10 |
| `ocr_runs` | 10 |
| `silver_drafts` | 4 |
| `review_decisions` | 0 |
| `silver_verified_records` | 0 |
| `audit_events` | 61 |
| `smartcoat_migrations.applied_migrations` | 9 |
| `smartcoat_migrations.adoption_decisions` | 1 |
| `smartcoat_state.legal_upload_transitions` | 11 |

The first six accepted uploads returned real exact original version IDs and a
`permanent` retention assignment:

| Fixture | Ingestion ID | Original version ID | SHA-256 | Duplicate link |
| --- | --- | --- | --- | --- |
| Excel 1 | `01a06242-6ded-77e9-a547-ca7cbacb573b` | `067fc8bc-b075-49e4-ab6b-46e0451cd12a` | `b761789bfd20b04fc32e3fb31b6d5105214e2f468550cc5ab09e42a195f7a4eb` | none |
| Excel 2 | `01a06242-6f33-7780-81ce-7bb4159aae50` | `3228d2f3-1a5a-46af-a972-abdfc272e296` | `e9966575a0ac88e16df658fa044a7c22880b78215cbceead98249576cbab13aa` | none |
| Excel 3 | `01a06242-6fb0-7b3f-adb5-399250cb2fda` | `d131598d-d1e6-4d98-b730-bf9d83ebde64` | `fc25fbcd128059218c9fea75279bd56942c3dcd3c9b1ac84902c813117250c3c` | none |
| Excel 4 | `01a06242-702e-79ed-aa98-ceb1ed77ffe6` | `74fefe6d-d82c-42f6-a4ac-22aa181f4068` | `e1613ace8a0e4c069d497169fb7dd396e14002f1572e78b50b9f4495fda66890` | none |
| Exact duplicate | `01a06242-70b0-7b9e-abeb-96abdaeb166b` | `c3e067f4-da8c-41f5-ba5c-8db5cd2ec3ae` | `b761789bfd20b04fc32e3fb31b6d5105214e2f468550cc5ab09e42a195f7a4eb` | `01a06242-6ded-77e9-a547-ca7cbacb573b` |
| Failure PDF | `01a06242-7122-796f-a40a-291d2ccbbd66` | `e8a40e14-b306-45ad-8206-dde7c6841c31` | `b8274cd2f933ba897ac5a626e96063aa6ed45d4389b105279661a55aedd2bc6f` | none |

A complete pre-backup per-object protection snapshot was intentionally not
declared complete: the drill stopped before the backup gate once source-state
construction failed.

## Blocking failure

All three intended approvals and the intended rejection returned HTTP 500.
The owned API log showed:

```text
psycopg.errors.InsufficientPrivilege: permission denied for table bronze_objects
```

Independent catalog evidence from the disposable database was:

```text
review_bronze_objects_select|f
```

The failing call path was:

```text
ReviewService.review()
  -> PostgresRepository.get_upload()
  -> SELECT ... JOIN bronze_objects ...
```

This prevents the runtime review boundary from producing either `VERIFIED` or
`REVIEW_REJECTED` on the current integrated deployment. Administrative SQL was
not used to fabricate those outcomes, and the missing privilege was not added.

## Command and timestamp record

Secrets and connection URLs are intentionally omitted. All environment values
were disposable synthetic values stored outside the repository.

| UTC time | Command or operation | Exit/result |
| --- | --- | --- |
| Before construction | `docker compose ... config --quiet` | `0` |
| Before construction | Docker inventory capture | `0`; fingerprint `a75add2563804defbca7ce47e4d4e511adafd7db92eb4cfa88700233a87730bd` |
| 13:12 UTC | `docker compose ... up -d --no-build postgres minio minio-bootstrap postgres-migrate postgres-role-provision legal-hold-applier api` | migration dependency failed because adoption was required |
| 13:13:32Z | `docker compose ... run --rm --no-deps postgres-migrate adopt smartcoat_wp6` | `0`; `ADOPTED` |
| 13:13 UTC | `docker compose ... run --rm --no-deps postgres-migrate apply` | `0`; eight newly applied |
| 13:13 UTC | `docker compose ... up -d --no-build postgres-role-provision legal-hold-applier api` | `0` |
| 13:13:46Z | API/source stack startup interval ended | API started |
| 13:15:15Z–13:15:16Z | Six accepted synthetic uploads plus one rejected validation request | accepted requests `201`; rejection `422` |
| 13:15 UTC | `docker compose ... up -d --no-build ocr-worker` | `0` |
| 13:16 UTC | OCR processing of Excel/PDF inputs | six `OCR_FAILED` |
| 13:17 UTC | Four synthetic PNG uploads and OCR processing | four `SILVER_DRAFT_READY` |
| 13:18:51Z–13:18:54Z | Four `POST /api/drafts/{draft_id}/review` calls | four HTTP `500` failures |
| 13:19:43Z | Catalog, ledger, table-count, `.minio.sys`, and owned-resource evidence capture | `0` |
| 13:20:04Z | `docker compose ... down --remove-orphans` | `0` |
| 13:20:07Z | Cleanup verification | zero owned resources; original inventory restored |

Exploratory read-only repository commands before construction did not have
individual timestamps captured. That evidence-recording limitation is explicit
rather than reconstructed from memory.

## Backup, destruction, and restore

| Stage | Status | Evidence |
| --- | --- | --- |
| Backup | **NOT RUN** | Source-state gate failed first; backup artifact count was zero. |
| Filesystem destruction | **NOT RUN** | It would be misleading to destroy and restore a source deployment that failed the required population contract. |
| Restore to fresh volumes | **NOT RUN** | No backup existed. |
| RTO | **NOT MEASURED** | Restore never started. |

## Verification matrix

| Required verification | Result | Evidence or reason |
| --- | --- | --- |
| At least three uploads reach `VERIFIED` before backup | **FAIL** | Four review calls returned HTTP 500; count remained zero. |
| At least one reaches `REVIEW_REJECTED` | **FAIL** | Rejection call returned HTTP 500; count remained zero. |
| At least one reaches `OCR_FAILED` and is retried | **NOT COMPLETED** | Six reached `OCR_FAILED`; execution stopped before retry after the blocking review failure. |
| Validation rejection exercised | **PASS** | Unsupported `.txt` returned HTTP 422 / `UNSUPPORTED_TYPE`. |
| SHA-256 duplicate detection exercised | **PASS** | Duplicate linked to `01a06242-6ded-77e9-a547-ca7cbacb573b`. |
| Exact Bronze version IDs created | **PASS for captured uploads** | Six returned exact non-null original version IDs; 20 Bronze object rows existed. |
| Retention enforcement evidence created | **PASS for source writes** | 20 append-only enforcement-evidence rows existed. |
| Permanent Legal Hold applied through mediator | **PARTIALLY OBSERVED** | Ingestion completed through the authenticated mediator and evidence rows existed; no independent pre-backup object-by-object read-back table was completed before stop. |
| Backup includes `.minio.sys` | **NOT EXECUTED** | Source had `.minio.sys`; static command copies dot entries, but no artifact was created. |
| Exact versions restored | **NOT RUN** | No restore. |
| Restored SHA-256 values match | **NOT RUN** | No restore. |
| Restored retention mode is `COMPLIANCE` | **NOT RUN** | Central question unanswered. |
| Retain-until timestamps match and are not shorter | **NOT RUN** | Central question unanswered. |
| Restored Legal-Hold state matches | **NOT RUN** | Central question unanswered. |
| Delete of restored protected version is denied | **NOT RUN** | Decisive central test not reached. |
| All PostgreSQL row counts match | **NOT RUN** | No restored database. |
| Bronze pairs/evidence/audit contents match exactly | **NOT RUN** | No restored database. |
| Append-only triggers survive restore | **NOT RUN** | No restored database. |
| State graph restores as 11 legal / 79 illegal | **NOT RUN** | Source ledger had 11 legal edges; restored graph not tested. |
| R02 runtime permission matrix survives restore | **NOT RUN** | Source runtime already exposed the blocking missing review read. |
| `smartcoat_app` remains `NOLOGIN` after restore | **NOT RUN** | No restored database. |
| Ledger `0001`–`0009`, reapply `applied_now=0` | **PARTIAL** | Source ledger was correct; post-restore reapply not run. |
| New post-restore ingestion reaches `VERIFIED` | **NOT RUN** | No restore. |
| Post-restore OCR retry works | **NOT RUN** | No restore. |
| Post-restore authenticated mediation works | **NOT RUN** | No restore. |

## RPO

The repository procedure is manual and on demand. It defines no schedule,
automatic trigger, replication, or continuously updated recovery point.
Therefore the honest RPO is **all data created since the last time an operator
manually ran the backup command** — colloquially, “everything since the last
time someone remembered.” The maximum loss window is unbounded until an
operational backup frequency is established and enforced.

## Cleanup proof

Initial inventory:

- Containers: 11
- Networks: 12
- Volumes: 7
- Fingerprint: `a75add2563804defbca7ce47e4d4e511adafd7db92eb4cfa88700233a87730bd`

After cleanup:

- WP6-owned containers: 0
- WP6-owned networks: 0
- WP6-owned volumes: 0
- Total containers: 11
- Total networks: 12
- Total volumes: 7
- Fingerprint: `a75add2563804defbca7ce47e4d4e511adafd7db92eb4cfa88700233a87730bd`

The owned `/tmp/smartcoat-wp6-restore.KeQOEK` tree was removed after Docker
cleanup and inventory verification.

## Problems encountered

1. Ordinary migration apply correctly refused the unmanaged bootstrap database;
   explicit baseline adoption was required before applying `0002`–`0009`.
2. The host Python environment did not contain `openpyxl`; fixture generation
   was moved into the already-approved immutable OCR image.
3. Valid upload-accepted Excel fixtures failed in the OCR worker with
   `InvalidFileException`. This was not repaired.
4. The blocking product-contract failure: `smartcoat_review` cannot read
   `bronze_objects`, so every approval and rejection through the API fails with
   HTTP 500. This was not repaired or bypassed.
5. The existing MinIO backup is a live filesystem copy without an explicit
   snapshot/quiesce contract. Its consistency remains unproved.
6. Individual timestamps were not captured for the initial exploratory
   read-only commands.

## What this drill did not prove

This run did not prove backup artifact completeness, `.minio.sys` restoration,
exact-version recovery, COMPLIANCE retention survival, retain-until equality,
Legal-Hold survival, post-restore delete denial, PostgreSQL restore fidelity,
append-only trigger survival, runtime grant survival, RTO, or post-restore
end-to-end behavior.

The restore drill must be rerun from a separately reviewed fix for the review
runtime permission contract. Any backup/restore consistency remediation also
requires its own ticket. WP6 does not authorize either fix.
