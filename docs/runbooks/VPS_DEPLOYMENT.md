# Local deployment runbook

The filename is retained from the authorized repository map; there is no VPS deployment in Phase 1. Do not configure DNS, a reverse proxy, TLS, firewall rules, public ports, or remote access.

## Prerequisites

- Apple-Silicon MacBook with current Docker Desktop and at least 16 GB available RAM.
- FileVault enabled for local storage and backups.
- Repository checkout and an external, access-restricted real-pilot source folder.

## First start

```bash
cp .env.example .env
chmod 600 .env
```

Replace every `change-me` value. Use independently generated values of at least 32 characters for PostgreSQL, MinIO, service credentials, the local password, and session secret. Then:

M0-R02 separates PostgreSQL workflow authority. `MIGRATION_DATABASE_URL` and `POSTGRES_ROLE_ADMIN_URL` must be set explicitly in the protected `.env`; do not print, paste into shell history, or commit their values. Both one-shot operations currently authenticate as the configured PostgreSQL administrative/bootstrap identity: their username and password correspond to `POSTGRES_USER` and `POSTGRES_PASSWORD` (`smartcoat_admin` in `.env.example`). `MIGRATION_DATABASE_URL` is consumed only by the migration runner. `POSTGRES_ROLE_ADMIN_URL` is consumed only by the credential provisioner after migration 0002 has installed and validated the fixed runtime-role contract. Percent-encode URI-reserved characters in URL usernames or passwords.

For the first start of an unmanaged bootstrap database, start PostgreSQL by itself and wait until its existing healthcheck reports healthy:

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d postgres
docker compose --env-file .env ps postgres
```

Explicitly adopt only the intended database name. For the example configuration, that exact name is `smartcoat_rd`:

```bash
docker compose --env-file .env run --rm --no-deps postgres-migrate adopt smartcoat_rd
```

Adoption is never a default command, dependency action, or retry fallback. Once adoption succeeds, run the full stack normally:

```bash
docker compose --env-file .env up --build -d
./scripts/create-pilot-users.sh
docker compose --env-file .env ps
```

Normal startup runs ordinary `apply`, then the backend-only `postgres-role-provision` one-shot operation. Successful completion of both gates new API and OCR-worker startup. To run the same ordinary migration operation independently after PostgreSQL is healthy:

```bash
docker compose --env-file .env run --rm --no-deps postgres-migrate apply
```

The controls fail closed:

- Missing or empty `MIGRATION_DATABASE_URL` prevents Compose configuration or migration invocation.
- An unmanaged database makes ordinary `apply` fail; the operator must invoke the separate adoption command deliberately.
- A bootstrap recognition mismatch rejects adoption without repair or bypass.
- A migration failure rolls back that migration and blocks dependent new application startup.
- Applied-history checksum, name, or ordering drift rejects ordinary migration execution.

The completion gate applies when Compose creates or recreates dependent containers; a failure does not stop API or OCR-worker containers that were already running before that deployment attempt. Repeat deployment never adopts an existing volume automatically.

Open `http://127.0.0.1:8080`. Confirm `docker compose ps` shows only host bindings beginning with `127.0.0.1`. Do not change them to `0.0.0.0`.

If a default host port is already occupied, change only the corresponding `HOST_*_PORT` value in `.env`; container-to-container endpoints remain unchanged. When changing the web or API port, keep `WEB_ORIGIN`, `API_ORIGIN`, and the web client's local API constant aligned.

## Service credentials and rotation

MinIO root credentials are consumed only by the one-shot bootstrap container. API, worker, and backup use their separate identities. To rotate a service credential, stop its consumer, change the secret through the MinIO console on localhost, update `.env`, recreate the consumer, and test its narrow operation. Record the date and operator below.

PostgreSQL runtime authority is split across fixed identities: `smartcoat_ingestion` serves upload/Bronze/queue and API reads, `smartcoat_ocr` may create only OCR evidence and unverified drafts, `smartcoat_review` owns review and verified-record writes, and `smartcoat_backup` is read-only. The historical `smartcoat_app` role is bootstrap compatibility only and migration 0002 disables its login and revokes its authority. The API receives separate ingestion and review URLs; the OCR worker receives only its OCR URL; the backup script receives only the backup password. Runtime services never receive either administrative URL. To rotate PostgreSQL runtime passwords, stop the affected consumers, replace the four distinct password and URL values in `.env`, run `docker compose --env-file .env run --rm --no-deps postgres-role-provision`, recreate the consumers, and exercise both an allowed operation and a cross-boundary denial.

| Identity | Last rotation (UTC) | Operator |
| --- | --- | --- |
| `app-ingestion` |  |  |
| `ocr-worker` |  |  |
| `backup` |  |  |
| `postgres-ingestion` |  |  |
| `postgres-ocr` |  |  |
| `postgres-review` |  |  |
| `postgres-backup` |  |  |

## Operations

```bash
docker compose logs -f api ocr-worker
docker compose restart api ocr-worker
./scripts/verify-bronze-integrity.sh
./scripts/restore-drill.sh backup
```

Never run `docker compose down -v`; bind-mounted `.local-data` contains pilot state. Do not use MinIO root credentials for routine browsing or ingestion. Real inputs must be selected from outside the repository.

The platform remains `BLOCKED`. Real company data is prohibited until M0-R05 passes; these migration operations do not change that gate.

## Handover / future migration

Before adding a second reviewer, set `ALLOW_PHASE_1_SOLO_SELF_REVIEW=false` and replace local authentication. A later VPS deployment migrates MinIO objects and PostgreSQL data without changing Bronze/Silver contracts. That later phase requires a separate threat model, TLS, access controls, backup target, and migration acceptance test; none are Phase-1 operational steps.
