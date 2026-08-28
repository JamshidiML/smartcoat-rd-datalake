# Legal-hold authority boundary

## Architecture decision

SmartCoat uses `CONTROLLED_MEDIATION_REQUIRED` for legal-hold application. The pinned MinIO runtime does not reliably enforce an ON-only IAM condition for `s3:PutObjectLegalHold`, so no ordinary application identity receives that permission.

The `legal-hold-applier` service is an internal, non-root, backend-only boundary. It publishes no host port and is absent from the edge network. Its MinIO credential is injected only into the one-shot bootstrap and the mediator. API, ingestion, OCR, reviewer, backup, and web runtimes do not receive it.

The mediator accepts only:

- approved Bronze bucket;
- `rd/` object key;
- exact version ID.

It exposes no status or operation parameter. Its implementation always calls legal-hold ON, reads back the same version, and reports success only after ON is confirmed. It has no object write, delete, retention-change, bypass, bucket-administration, or legal-hold OFF interface.

## Runtime configuration

Configure distinct values for these variable names without committing their values:

- `MINIO_HOLD_APPLIER_ACCESS_KEY`
- `MINIO_HOLD_APPLIER_SECRET_KEY`

Start through the normal Compose workflow. Verify the rendered `legal-hold-applier` service has only the `backend` network, an internal container exposure on `8090`, and no `ports` entry.

An internal caller applies a hold with `POST http://legal-hold-applier:8090/apply` and an exact JSON object containing `bucket`, `object_key`, and `version_id`. Additional fields, including `status`, are rejected.

## Break-glass OFF

Legal-hold clearing is not part of the mediator or normal runtime. It is an operator-only mechanism using a separately provisioned credential and the pinned MinIO client image. Never inject the break-glass credential into Compose services.

Provision the authority only through `infra/minio/provision_legal_hold_break_glass.sh` with its exact confirmation. The operator environment must explicitly provide the endpoint, root provisioning identity, distinct break-glass identity, and `LEGAL_HOLD_CONTROL_ROOT`. Do not use the repository `.env` as an operator credential source.

Clear a hold only through `infra/minio/legal_hold_break_glass.py` in the pinned `minio==7.2.16` operator image. The command requires a unique decision ID, actor, reason, caller-supplied UTC timestamp, approved bucket, exact key, exact version ID, and `CONFIRM_BREAK_GLASS_LEGAL_HOLD_CLEAR`.

The script fails unless it can read back an exact-version COMPLIANCE floor. It writes immutable versioned `REQUESTED` evidence before clearing, reads back OFF on the exact target version, and writes `COMPLETED` evidence. The break-glass policy explicitly denies object deletion, retention changes, and governance bypass. Clearing legal hold therefore cannot shorten or bypass the COMPLIANCE deadline.

## Acceptance and limitations

Run `infra/minio/tests/live_legal_hold_mediation_acceptance.py` only with its explicit disposable-synthetic authorization flag. It must use existing local pinned images, publish no ports, touch no existing stack or volume, and remove all owned resources.

This boundary owns authority mediation only. Full retention-class enforcement, storage timestamp assignment, Bronze pair commit, orphan reconciliation, legacy reconciliation, success constraints, and M0-R05 remain downstream work.
