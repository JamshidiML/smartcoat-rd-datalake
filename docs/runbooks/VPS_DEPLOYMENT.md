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

```bash
docker compose config
docker compose up --build -d
./scripts/create-pilot-users.sh
docker compose ps
```

Open `http://127.0.0.1:8080`. Confirm `docker compose ps` shows only host bindings beginning with `127.0.0.1`. Do not change them to `0.0.0.0`.

If a default host port is already occupied, change only the corresponding `HOST_*_PORT` value in `.env`; container-to-container endpoints remain unchanged. When changing the web or API port, keep `WEB_ORIGIN`, `API_ORIGIN`, and the web client's local API constant aligned.

## Service credentials and rotation

MinIO root credentials are consumed only by the one-shot bootstrap container. API, worker, and backup use their separate identities. To rotate a service credential, stop its consumer, change the secret through the MinIO console on localhost, update `.env`, recreate the consumer, and test its narrow operation. Record the date and operator below.

| Identity | Last rotation (UTC) | Operator |
| --- | --- | --- |
| `app-ingestion` |  |  |
| `ocr-worker` |  |  |
| `backup` |  |  |

## Operations

```bash
docker compose logs -f api ocr-worker
docker compose restart api ocr-worker
./scripts/verify-bronze-integrity.sh
./scripts/restore-drill.sh backup
```

Never run `docker compose down -v`; bind-mounted `.local-data` contains pilot state. Do not use MinIO root credentials for routine browsing or ingestion. Real inputs must be selected from outside the repository.

## Handover / future migration

Before adding a second reviewer, set `ALLOW_PHASE_1_SOLO_SELF_REVIEW=false` and replace local authentication. A later VPS deployment migrates MinIO objects and PostgreSQL data without changing Bronze/Silver contracts. That later phase requires a separate threat model, TLS, access controls, backup target, and migration acceptance test; none are Phase-1 operational steps.
