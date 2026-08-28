# Local backup and restore

## Backup

`scripts/restore-drill.sh backup` creates a timestamped directory under `~/SmartCoatRDLakeBackups` by default. It writes a PostgreSQL custom-format dump using the read-only `smartcoat_backup` identity, copies the entire MinIO data directory, creates per-file SHA-256 checksums, applies owner-only permissions via `umask 077`, and moves the `latest` symlink.

```bash
./scripts/restore-drill.sh backup
```

The backup target must be local encrypted storage. Do not synchronize it to a consumer cloud account. Because MinIO should not be copied while objects are actively changing, pause uploads and wait for OCR jobs before the acceptance restore drill.

## Restore drill

```bash
./scripts/restore-drill.sh
# or: ./scripts/restore-drill.sh /absolute/path/to/timestamped-backup
```

The drill verifies backup checksums, restores PostgreSQL into an isolated temporary database, launches an isolated read-only MinIO container over the copied data, selects a restored upload, recomputes its source digest, and verifies the paired manifest object exists. The temporary database/container are removed on exit; the live system is not overwritten.

AT-13 passes only when database provenance, original SHA-256, and manifest all match. Record the command output, timestamp, selected ingestion ID, and result in the acceptance report.

## Recovery notes

A real recovery is intentionally not automated because it overwrites system state. Stop Compose, preserve the damaged directories, restore the MinIO directory into the configured `MINIO_DATA_DIR`, recreate the PostgreSQL database, and apply `pg_restore`. Resolve exact source and destination paths before any destructive operation, then run the integrity verifier over every manifest.
