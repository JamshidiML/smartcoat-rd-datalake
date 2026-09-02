# Restore drill report — blocked at fresh-volume restore boundary

## Result

- UTC execution date: `2026-09-02`
- Local execution date (Europe/Berlin): `2026-09-03`
- Repository commit tested: `18139f68de674958a84d4648c226eef9669bf71b`
- Branch: `agent/wp7-positive-grant-audit`
- Final disposable project: `sc-wp6-rerun-18139f6`
- Classification: **BLOCKED — the repository restore procedure does not restore both systems onto destroyed, fresh, empty volumes**
- Platform status: **BLOCKED**
- Real company data: **PROHIBITED**

The source-state and backup stages completed with synthetic data after the
positive-path grant defect found by the first attempt was repaired in migration
`0010`. The drill then stopped before destruction because the existing restore
procedure cannot execute the recovery model required by WP6. It restores the
dump into a second database on the still-running PostgreSQL service and mounts
the copied MinIO directory read-only in an ad-hoc container. It does not restore
PostgreSQL and MinIO from backup onto two destroyed, fresh, empty volumes.

The central WP6 question therefore remains unanswered:

> Does COMPLIANCE retention state and Legal-Hold state survive a backup and
> restore onto fresh replacement storage?

No restore pass is claimed. No backup or restore behavior was changed, and no
manual recovery procedure was invented to manufacture a pass.

## Isolation and immutable images

Only synthetic fixtures were used. All disposable database, object-storage,
control, and backup files were under
`/tmp/smartcoat-wp6-rerun-e002275/`. The repository's `./.local-data/`
directory and `~/smartcoat-volume-archive-20260830.tar.gz` were never read,
mounted, copied, or modified.

No pre-existing container was stopped, started, restarted, joined, or modified.
The final project used unique Compose project and network names and bind-mounted
only its owned `/tmp` data directories.

Immutable image IDs:

| Component | Immutable image ID |
| --- | --- |
| PostgreSQL 17.6 | `sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94` |
| MinIO server | `sha256:d249d1fb6966de4d8ad26c04754b545205ff15a62e4fd19ebd0f26fa5baacbc0` |
| MinIO client/bootstrap | `sha256:fb8f773eac8ef9d6da0486d5dec2f42f219358bcb8de579d1623d518c9ebd4cc` |
| API/migration/role provisioning | `sha256:cd276d9b3b8c3c083bb037cc88be592306f531337b0268be85cd8e29a14a8d92` |
| Legal-Hold mediator | `sha256:bbb170b5a8eed05a179db672e7528b222e8c93c433ffdf79605fd9e8045d57ef` |
| OCR worker | `sha256:ad9d479532cfb309c38023b7a305176332a360c72af0a760b120f3511f378d46` |

## Source deployment

The fresh bootstrap database was explicitly adopted. The accepted migration
runner then reported:

```text
Adoption result: status=ADOPTED database=smartcoat_wp6_rerun2 oid=16384 evidence_inserted=true
Migration run complete: discovered=10 already_applied=1 applied_now=9
Runtime-role provisioning complete: roles=4 credentials_updated=4
```

The source ledger contained versions `0001` through `0010` exactly once, one
adoption-decision row, and 11 legal transition edges.

Synthetic source outcomes:

- Four distinct PNG uploads reached `VERIFIED` through real OCR and review API
  paths.
- A byte-identical repeat of the first PNG was linked as an exact SHA-256
  duplicate and reached `REVIEW_REJECTED` through the real review path.
- A validation-accepted malformed PDF reached `OCR_FAILED`, was retried once by
  the operator endpoint, and failed again with job `attempt_count=2`.
- An unsupported `.txt` upload was rejected with HTTP `422` and
  `UNSUPPORTED_TYPE`.
- Every accepted upload produced one committed Bronze pair with exact original
  and manifest version IDs.

Final source table counts:

| Relation | Rows |
| --- | ---: |
| `users` | 1 |
| `uploads` | 6 |
| `bronze_objects` | 12 |
| `bronze_pairs` | 6 |
| `bronze_protected_orphans` | 0 |
| `bronze_reconciliation_events` | 0 |
| `bronze_retention_assignments` | 12 |
| `bronze_retention_enforcement_evidence` | 12 |
| `ocr_jobs` | 6 |
| `ocr_runs` | 7 |
| `silver_drafts` | 5 |
| `review_decisions` | 5 |
| `silver_verified_records` | 4 |
| `audit_events` | 56 |
| `smartcoat_migrations.applied_migrations` | 10 |
| `smartcoat_migrations.adoption_decisions` | 1 |
| `smartcoat_state.legal_upload_transitions` | 11 |

The canonical captured source evidence, including the complete ordered
`bronze_pairs`, `bronze_retention_enforcement_evidence`, and `audit_events`
rows, had SHA-256
`5d1759f20926b09be81a57fed11d806e171ee61707de08c444c715a75367686b`.

## Pre-backup Bronze protection ground truth

The ordinary ingestion MinIO identity independently read and hashed each exact
version and read its retention. The authenticated mediator's read-only
`/status` operation independently read Legal-Hold state. All 12 objects matched
their database SHA-256, all were `COMPLIANCE`, and all had Legal Hold `ON`.

| Bucket/object | Exact version ID | SHA-256 | Retain until UTC | Hold |
| --- | --- | --- | --- | --- |
| `sc-rd-bronze-manifests/rd/2026/09/01a0646d-4526-7503-8496-6cac87ec33ad/manifest/v1.json` | `c1ba32fd-e92e-4add-95b4-9ad7f6b21e8e` | `20434d18ad96050e37aee30b6dc0e732bb5191b6f64648a37ebfb74010c7f38e` | `2036-09-02 23:21:17+00:00` | `ON` |
| `sc-rd-bronze-originals/rd/2026/09/01a0646d-4526-7503-8496-6cac87ec33ad/original/review-1.png` | `b65bd00b-ac52-466d-8f69-b136b38e0506` | `40d5e6002976f7e180d04b881e75c272bbc1bda48f50fa01c47d895b24e20fe2` | `2036-09-02 23:21:17+00:00` | `ON` |
| `sc-rd-bronze-manifests/rd/2026/09/01a0646d-45b3-7ca2-850b-4d47a0588004/manifest/v1.json` | `23a642e7-050f-4d72-8d9a-6725ce9c0a64` | `47d813ea64cb84eaecc9b222f8b47afc8b308ccf5f6915ce1aea56d4bfdeaac9` | `2036-09-02 23:21:18+00:00` | `ON` |
| `sc-rd-bronze-originals/rd/2026/09/01a0646d-45b3-7ca2-850b-4d47a0588004/original/review-2.png` | `81196c26-af55-42dc-a357-10ce2718d25c` | `67e7d2b8605b13133e71d68795bfe1e10ff9e91c0f5b626f29a68a054a4f83b5` | `2036-09-02 23:21:18+00:00` | `ON` |
| `sc-rd-bronze-manifests/rd/2026/09/01a0646d-45f3-7466-bd25-9e5c9170290e/manifest/v1.json` | `2983a0b2-46b4-46d9-a9bd-ce5150082014` | `6c5929d66521bb943dc96820e3352d50cb1d0a4ba76492ec6f7a23869792910c` | `2036-09-02 23:21:18+00:00` | `ON` |
| `sc-rd-bronze-originals/rd/2026/09/01a0646d-45f3-7466-bd25-9e5c9170290e/original/review-3.png` | `a31e585f-1911-48a8-9dca-db000a4c8b01` | `52647d3705b7c6d1a56771ba809f3361cf62584fe6609d4b83a1f44bfbe5e11a` | `2036-09-02 23:21:18+00:00` | `ON` |
| `sc-rd-bronze-manifests/rd/2026/09/01a0646d-462e-7583-a6fd-b361e3bf918e/manifest/v1.json` | `7baa25b0-11ea-4ce3-a0b9-9112ae51a904` | `2ddee1219bdebf4d1530abcb42e4153151c0d291011ba3eecad16d13ad787aa2` | `2036-09-02 23:21:18+00:00` | `ON` |
| `sc-rd-bronze-originals/rd/2026/09/01a0646d-462e-7583-a6fd-b361e3bf918e/original/review-4.png` | `15c7bb60-aa35-44d9-8650-411fc9996774` | `47470b8f02fed7dd7ccc396bb49d3a8250c851a5aa87e26022a30435484da0b2` | `2036-09-02 23:21:18+00:00` | `ON` |
| `sc-rd-bronze-manifests/rd/2026/09/01a0646d-4666-7ea2-be6f-044c42218197/manifest/v1.json` | `1d9cc626-835e-4075-83b7-a6aa0e428e25` | `f607f3194dbc3fd19d934188fb4471bc95d36fb298f9488cf938d53d23301e95` | `2036-09-02 23:21:18+00:00` | `ON` |
| `sc-rd-bronze-originals/rd/2026/09/01a0646d-4666-7ea2-be6f-044c42218197/original/review-1.png` | `81e5e907-c32c-4d45-b198-ecb33e9e37b8` | `40d5e6002976f7e180d04b881e75c272bbc1bda48f50fa01c47d895b24e20fe2` | `2036-09-02 23:21:18+00:00` | `ON` |
| `sc-rd-bronze-manifests/rd/2026/09/01a0646d-46a3-7e59-867d-abd2c08e3f72/manifest/v1.json` | `dd1d85a2-15db-46ea-876e-e81bbcb34371` | `335f2ec615aa0a2cfa242fde8fa8e357113557d53c09ef946a42e8bc34db26d2` | `2036-09-02 23:21:18+00:00` | `ON` |
| `sc-rd-bronze-originals/rd/2026/09/01a0646d-46a3-7e59-867d-abd2c08e3f72/original/ocr-failure.pdf` | `4c1e1c30-42b0-4578-9abd-08ac5e5fd6ac` | `eadef7418e14af08d4dab416d408d94121199f49e60eed1caa7a6bec3b16ebe0` | `2036-09-02 23:21:18+00:00` | `ON` |

## Backup

The repository's existing command was used unchanged:

```sh
ENV_FILE=[owned synthetic env] \
COMPOSE_PROJECT_NAME=sc-wp6-rerun-18139f6 \
COMPOSE_FILE=[repository compose]:[owned isolation override] \
scripts/restore-drill.sh backup
```

- Start: `2026-09-02T23:22:44Z`
- End: `2026-09-02T23:22:48Z`
- Exit: `0`
- Elapsed wall-clock interval: 4 seconds
- Total backup size: 584 KiB
- PostgreSQL dump size: 107,029 bytes (128 KiB allocated)
- PostgreSQL dump SHA-256:
  `0653eabf8e3d1ebf627f6b6a03ea7345cd2ff5a50784303390de3e1a78330740`
- MinIO tree size: 436 KiB
- Total files represented by the backup tree and checksum manifest: 109
- `SHA256SUMS` SHA-256:
  `5c33b4fa22fa5b9402b48f6787df1af767df76ffe660805791aaf1e1f70c00a1`

The copied MinIO tree contains `.minio.sys` with 80 files. It includes bucket
metadata for both Bronze buckets, configuration, IAM metadata, and per-object
`xl.meta` data. The procedure uses `cp -a "$minio_source/."`, so dot-prefixed
`.minio.sys` content is included.

This proves inclusion in this backup artifact; it does not prove that a fresh
MinIO service can successfully restore and enforce that metadata. The live
filesystem copy also has no snapshot, quiesce, or consistency boundary.

## Destruction and restore boundary

Filesystem destruction was **not run**. The drill failed closed before that
irreversible stage because there is no repository procedure that can complete
the required subsequent restore onto fresh empty PostgreSQL and MinIO volumes.

The current restore half of `scripts/restore-drill.sh`:

1. runs `dropdb`/`createdb` inside the still-running source PostgreSQL service;
2. restores the dump into `smartcoat_rd_restore_drill` in that same service;
3. starts an ad-hoc MinIO container with the backup tree mounted `:ro`;
4. checks only one original payload SHA-256 and one manifest stat;
5. does not independently verify exact version IDs, per-object retention,
   Legal Hold, protected-object delete denial, database equality, triggers,
   roles, grants, migration idempotency, or post-restore application behavior.

Executing that path would not satisfy WP6 §6.3–6.5 and could not answer the
central protection-survival question. Per the package boundary, it was not
repaired, supplemented, or presented as a restore.

- RTO: **NOT MEASURED**
- Restore artifact comparison: **NOT RUN**
- Post-restore delete denial: **NOT RUN**

## Verification matrix

| Required verification | Result | Evidence or reason |
| --- | --- | --- |
| At least three uploads reach `VERIFIED` before backup | PASS | Four reached `VERIFIED` through real API/OCR/review paths. |
| At least one reaches `REVIEW_REJECTED` | PASS | One real rejection completed. |
| At least one reaches `OCR_FAILED` and is retried | PASS | PDF failed, retry returned 200/QUEUED at attempt 1, then failed at attempt 2. |
| Validation rejection exercised | PASS | `.txt` returned 422/`UNSUPPORTED_TYPE`. |
| SHA-256 duplicate detection exercised | PASS | Exactly one `duplicate_of_ingestion_id` link existed. |
| Exact Bronze version IDs created | PASS | 12 non-null exact versions independently read. |
| Source SHA-256 values match | PASS | All 12 exact-version payload hashes matched. |
| Source retention is `COMPLIANCE` | PASS | All 12 read back `COMPLIANCE`. |
| Source Legal Hold is `ON` | PASS | Authenticated mediator `/status` returned `ON` for all 12. |
| Backup uses `smartcoat_backup` | PASS | `pg_dump -U smartcoat_backup` completed after the final grant fix. |
| Backup includes `.minio.sys` | PASS | 80 `.minio.sys` files captured, including both Bronze bucket metadata trees. |
| Backup has a coherent live-copy boundary | FAIL/UNPROVEN | No snapshot or quiesce contract exists. |
| Source filesystem destroyed before restore | NOT RUN | No compliant fresh-volume restore path exists. |
| Restore uses fresh empty PostgreSQL and MinIO volumes | FAIL/DESIGN GAP | Existing script uses running PostgreSQL and read-only MinIO bind mount. |
| Every exact version restored | NOT RUN | No compliant restore. |
| Every restored SHA-256 matches | NOT RUN | No compliant restore. |
| Restored retention remains `COMPLIANCE` | NOT RUN | Central question unanswered. |
| Restored retain-until timestamps match and are not shorter | NOT RUN | Central question unanswered. |
| Restored Legal Hold matches | NOT RUN | Central question unanswered. |
| Delete of restored protected version is denied | NOT RUN | Decisive test not reached. |
| All PostgreSQL row counts match | NOT RUN | No compliant restored database. |
| Bronze pairs/evidence/audit contents match | NOT RUN | Ground truth captured; no restored comparison. |
| Append-only triggers survive restore | NOT RUN | No compliant restore. |
| State graph restores as 11 legal / 79 illegal | NOT RUN | Source has 11 legal edges; restored graph not tested. |
| R02 grants and denials survive restore | NOT RUN | Source contract passed; restored contract not tested. |
| `smartcoat_app` remains `NOLOGIN` | NOT RUN | No compliant restore. |
| Ledger `0001`–`0010` restores; reapply is idempotent | NOT RUN | Source ledger correct; no restored ledger. |
| New ingestion reaches `VERIFIED` after restore | NOT RUN | No compliant restore. |
| OCR retry works after restore | NOT RUN | Source retry passed; restored retry not tested. |
| Authenticated mediation works after restore | NOT RUN | Source mediation passed; restored mediation not tested. |

## RPO

The repository backup is manual and on demand. There is no schedule,
automation, replication, or enforced recovery-point interval. The honest RPO is
**all data created since the last time an operator manually ran the backup**.
The maximum loss window is unbounded until a backup frequency is defined and
enforced.

## Command and incident record

Credential values, URLs containing credentials, raw PostgreSQL/MinIO logs, and
the rendered synthetic environment are intentionally omitted.

| Operation | Exit/result |
| --- | --- |
| Capture initial Docker inventory | 0; 11 containers, 12 networks, 7 volumes |
| Render isolated Compose configuration | 0 |
| Start final source PostgreSQL, MinIO, and bootstrap | 0 |
| Explicitly adopt fresh bootstrap | 0; `ADOPTED` |
| Apply migrations `0002`–`0010` | 0; `applied_now=9` |
| Provision four runtime roles | 0; `roles=4 credentials_updated=4` |
| Start mediator/API/OCR worker from immutable IDs | 0 |
| Submit six accepted fixtures | six HTTP 201 |
| Submit unsupported fixture | HTTP 422/`UNSUPPORTED_TYPE` |
| Retry failed OCR | HTTP 200/`QUEUED`; subsequent failure at attempt 2 |
| Four real approval calls | four HTTP 200/`VERIFIED` |
| One real rejection call | HTTP 200/`REVIEW_REJECTED` |
| Capture complete source ground truth | 0; 12 exact versions verified |
| Run unchanged backup, final attempt | 0; `23:22:44Z`–`23:22:48Z` |
| Inspect backup sizes/hashes and `.minio.sys` | 0 |
| Run existing restore path | NOT RUN; incompatible with required fresh-volume contract |
| Ownership-scoped final Compose cleanup | 0; completed `2026-09-02T23:24:02Z` |
| Verify owned Docker resources | 0; zero remain |
| Verify global Docker inventory | 0; exact initial fingerprints restored |

Problems encountered:

1. On the first WP6 rerun attempt at commit `e002275`, `pg_dump` failed with
   `permission denied for schema smartcoat_state`. The positive-path audit had
   omitted the state graph from the backup role's required reads. This was
   repaired narrowly in migration `0010` and the contract; no backup code was
   changed. Final commit `18139f6` passed live RBAC and CI.
2. The first backup invocation used an unquoted display name in the temporary
   synthetic shell environment and exited before backup. The temporary fixture
   was corrected; no repository configuration changed.
3. Two early read-only ground-truth helper attempts failed on an import path and
   an incorrect evidence-order column. Both failed before producing evidence;
   the temporary helper was corrected and the complete capture then passed.
4. The backup is a live MinIO filesystem copy without a coherent snapshot or
   quiesce contract.
5. The repository restore procedure does not implement fresh-volume disaster
   recovery and therefore blocks the drill's central verification.

Individual UTC timestamps were not retained for every setup command. The final
backup and cleanup boundaries are exact above; source object logs establish the
final population interval beginning `2026-09-02T23:21:17Z`. This limitation is
reported rather than reconstructed from memory.

## Cleanup proof

Initial global inventory:

- Containers: 11; SHA-256 fingerprint
  `07540472764d1c0e01482cc05b57dbe41698dbe610174f25a0770df5bec73281`
- Networks: 12; SHA-256 fingerprint
  `2b22622f892c25db97dace07e01d7cf3001795ecda10551461b836fb736a3534`
- Volumes: 7; SHA-256 fingerprint
  `4ab3c70a8f1cd170ddd66c1996e720ec9539d2389da82495b95e76464c3cde17`

After cleanup:

- WP6-owned containers: 0
- WP6-owned networks: 0
- WP6-owned volumes: 0
- Containers: 11; identical fingerprint
- Networks: 12; identical fingerprint
- Volumes: 7; identical fingerprint

After the report evidence was captured, the owned synthetic `/tmp` control,
source-data, and backup tree was removed. No drill credential or backup artifact
remains on disk.

## Protected repository hashes

Migrations `0001`–`0009` and governance documents remained unchanged:

| Path | SHA-256 |
| --- | --- |
| `infra/postgres/migrations/0001__validate_bootstrap_prerequisites.sql` | `7f34c9aba3819a49a5bb6c83f75bceaf436009d36c1c62eb46d0ddfa425529e5` |
| `infra/postgres/migrations/0002__separate_runtime_roles.sql` | `1f3f3b3faa3340c503bad0e844b08af6af5312546a35c5d2ab399fd6e105dffe` |
| `infra/postgres/migrations/0003__enforce_upload_state_transitions.sql` | `50f454fae23f36694466c69163f111c9f852f6af421b822225e6575f1666f9da` |
| `infra/postgres/migrations/0004__enforce_atomic_review_decisions.sql` | `6eb2819018134e8c790fbd463181a12d996ca0eb40bc4d73680b8f949782a8da` |
| `infra/postgres/migrations/0005__expand_retention_metadata.sql` | `f2b7b958df0a010ded58004d3027ead7f8f1e07ee80b0e25f5bb9f1de9e1c0bc` |
| `infra/postgres/migrations/0006__grant_review_audit_evidence_read.sql` | `6a7dd3d1d3c5b2f4059fb8116eef9da65de2bd0850b62372ed57e70e04659b31` |
| `infra/postgres/migrations/0007__record_retention_enforcement_evidence.sql` | `d41f94417e8c2c50a001ace2210a2e2ac2d0cee5aafbdbdf305285f11791e0f2` |
| `infra/postgres/migrations/0008__enforce_bronze_pair_commit_and_orphans.sql` | `fcda14c29c31ab05b832b4dd0e83d3de31ef7a9c40038e1855784c757356ec6d` |
| `infra/postgres/migrations/0009__add_operator_ocr_retry_transition.sql` | `5ca2d455a0c6f73dc614a9640ad4570ddc5e84984d1a54eb2105e69f703acdc7` |
| `docs/architecture/decisions/ADR-0001-master-roadmap-v2-scope-expansion-sequencing.md` | `afb78304621b383c2e187698beaaf0017037fa7c450063f326a05a9f71e5eaeb` |
| `docs/architecture/decisions/ADR-0002-retention-semantics-and-enforcement-contract.md` | `307ce9d9484b3819d16c5178a3dc61fb56e257376779e679e4923b1e7f5beb37` |
| `docs/architecture/M0_CONTRACT_FREEZE_ACCEPTANCE_MATRIX.md` | `cece377662dcb5224fa70226e4200f14017745615a6b31024c792cdc9d33de12` |

Migration `0010` tested SHA-256:
`499af9f6590a24a64f9241e9b407dd9e52056faa2c9d2d8552f947d971dd4f71`.

## What this drill did not prove

This run did not prove a coherent MinIO backup point, recovery onto destroyed
fresh volumes, exact-version restoration, COMPLIANCE retention survival,
retain-until equality, Legal-Hold survival, post-restore deletion denial,
PostgreSQL restore equality, trigger/grant/ledger survival, RTO, or
post-restore end-to-end behavior.

The next work must be a separately reviewed backup/restore design ticket. WP6
did not authorize that repair. M0-R05, the synthetic pilot, and the fresh-volume
switch remain unstarted.
