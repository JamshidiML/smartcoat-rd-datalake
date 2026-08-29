# Legal-hold authority boundary

## Architecture decision

SmartCoat uses `CONTROLLED_MEDIATION_REQUIRED` for legal-hold application. The pinned MinIO runtime does not reliably enforce an ON-only IAM condition for `s3:PutObjectLegalHold`, so no ordinary application identity receives that permission.

The `legal-hold-applier` service is an internal, non-root, backend-only boundary. It publishes no host port and is absent from the edge network. Its MinIO credential is injected only into the one-shot bootstrap and the mediator. API, ingestion, OCR, reviewer, backup, and web runtimes do not receive the MinIO mediator credential.

Calling the mediator is a separate authority. `LEGAL_HOLD_APPLIER_CALL_TOKEN` is required and must be a distinct high-entropy, single-line secret of at least 32 characters. Compose makes this caller token available only to the API and `legal-hold-applier`. OCR, reviewer, backup, web, MinIO bootstrap, migration, role-provisioning, and other runtime contexts must not receive it.

The mediator accepts only an exact approved target consisting of:

- an approved Bronze bucket;
- an approved `rd/` object key;
- an exact `version_id`.

`POST /apply` requires `Authorization: Bearer <token>`. It can only apply Legal Hold ON to that exact `bucket` + `object_key` + `version_id`, reads back the same version, and reports success only after ON is confirmed. `POST /status` requires the same Bearer token and performs read-only exact-version status observation. `GET /healthz` remains unauthenticated and exposes health only.

There is no unauthenticated `/apply` operation. A missing or wrong token returns `401` before request-body processing or any MinIO storage call, so it cannot cause a storage mutation. The mediator has no normal-runtime OFF operation, generic MinIO proxy, object write, delete, retention-change, bypass, or bucket-administration interface.

## Runtime configuration

Configure distinct values for these variable names without committing their values:

- `MINIO_HOLD_APPLIER_ACCESS_KEY`
- `MINIO_HOLD_APPLIER_SECRET_KEY`
- `LEGAL_HOLD_APPLIER_CALL_TOKEN`

The caller token must never be committed, logged, printed, placed in command output, or included in acceptance evidence. Supply it through the local secret environment only. Do not reuse either MinIO credential as the caller token.

Start through the normal Compose workflow. Verify the rendered `legal-hold-applier` service has only the `backend` network, an internal container exposure on `8090`, and no `ports` entry.

An authenticated API-side caller applies a hold with `POST http://legal-hold-applier:8090/apply`, an `Authorization: Bearer` header sourced from the environment, and an exact JSON object containing `bucket`, `object_key`, and `version_id`. Additional fields, including a requested hold status or generic operation, are rejected. Read-only observation uses the same exact JSON target with `POST http://legal-hold-applier:8090/status` and the same Bearer header.

## Break-glass OFF

Legal-hold clearing is not part of the mediator or normal runtime. Break-glass OFF remains a separate operator-only mechanism using a separately provisioned credential and the pinned MinIO client image. Never inject the break-glass credential into Compose services, and never treat the caller token as break-glass authority.

Provision the authority only through `infra/minio/provision_legal_hold_break_glass.sh` with its exact confirmation. The operator environment must explicitly provide the endpoint, root provisioning identity, distinct break-glass identity, and `LEGAL_HOLD_CONTROL_ROOT`. Do not use the repository `.env` as an operator credential source.

Clear a hold only through `infra/minio/legal_hold_break_glass.py` in the pinned `minio==7.2.16` operator image. The command requires a unique decision ID, actor, reason, caller-supplied UTC timestamp, approved bucket, exact key, exact version ID, and `CONFIRM_BREAK_GLASS_LEGAL_HOLD_CLEAR`.

The script fails unless it can read back an exact-version COMPLIANCE floor. It writes immutable versioned `REQUESTED` evidence before clearing, reads back OFF on the exact target version, and writes `COMPLETED` evidence. The break-glass policy explicitly denies object deletion, retention changes, and governance bypass. Clearing legal hold therefore cannot shorten or bypass the COMPLIANCE deadline.

## Acceptance and limitations

Run `infra/minio/tests/live_legal_hold_mediation_acceptance.py` only with its explicit disposable-synthetic authorization flag. It must use existing local pinned images, publish no ports, touch no existing stack or volume, and remove all owned resources.

This boundary owns authority mediation only. Full retention-class enforcement, storage timestamp assignment, Bronze pair commit, orphan reconciliation, legacy reconciliation, success constraints, and M0-R05 remain downstream work.
