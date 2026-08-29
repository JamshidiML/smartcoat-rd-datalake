# Legacy Bronze 365-day reconciliation

This runbook covers the controlled, one-time reconciliation candidate for legacy
R&D Bronze evidence created under the historical 365-day default. It does not
change the ordinary upload, OCR, review, or Bronze-pair paths. It must not be run
against real company data before the governing acceptance gate authorizes that
data.

## Authority boundary

The operator is a separate, temporary control-plane identity. Do not put its
credentials into Compose or any long-running application service.

- PostgreSQL: `LEGACY_RECONCILIATION_DATABASE_URL` must name the explicit
  migration/operator identity. `DATABASE_URL` is never a fallback.
- MinIO: `MINIO_LEGACY_RECONCILIATION_ACCESS_KEY` and
  `MINIO_LEGACY_RECONCILIATION_SECRET_KEY` must be a dedicated identity governed
  by `infra/minio/policies/legacy-reconciliation.json`.
- Legal Hold: the operator has no direct Legal Hold authority. Hold ON is applied
  and read through the accepted mediator using `LEGAL_HOLD_APPLIER_URL` and the
  distinct `LEGAL_HOLD_APPLIER_CALL_TOKEN`.
- Root/admin MinIO credentials are only for separately controlled provisioning.
  They must not be present when the reconciliation process runs.
- Backup receives `SELECT` only on reconciliation evidence. Ingestion, OCR,
  review, and the disabled legacy application identity receive no reconciliation
  write authority.
- `LEGACY_RECONCILIATION_OPERATOR_IMAGE_ID` must be an already-built immutable
  `sha256:` API dependency image, and `LEGACY_RECONCILIATION_DOCKER_NETWORK` must
  name the controlled internal backend network. The launcher never pulls or
  builds an image and publishes no port.

The dedicated MinIO policy permits complete version inventory, exact-version
reads, and extension of COMPLIANCE retention. It explicitly denies object writes,
deletes, governance bypass, and direct Legal Hold access. Provision it using the
normal privileged MinIO administration procedure, attach it only to the temporary
operator identity, then remove or disable that identity when the controlled run is
closed.

Never commit, print, log, or place operator credentials, URLs containing
credentials, the mediator token, or rendered real configuration in review
evidence.

## What the operator does

Both `sc-rd-bronze-originals` and `sc-rd-bronze-manifests` are inventoried with
all versions and delete markers. Every relevant PostgreSQL `bronze_objects` row
and every exact storage version receives one durable outcome:

- `RECONCILED`, after an exact version has matching SHA-256, COMPLIANCE retention
  is at least the stronger of its existing floor and ten UTC calendar years after
  reconciliation, Legal Hold is ON, and exact-version readback succeeds; or
- `QUARANTINED`, with an explicit ambiguity, hash mismatch, database orphan,
  storage orphan, contradictory metadata, or enforcement/readback failure.

A null database version ID is recoverable only when the exact-version inventory
and content hash identify one candidate. Multiple valid candidates are
quarantined. Delete markers are retained and recorded; they do not prove that a
protected historical version was deleted.

The operator never updates `bronze_objects`, upload state, the M0-R03 transition
graph, Bronze content, or delete markers. It never creates replacement object
versions, shortens retention, clears Legal Hold, or converts unresolved evidence
to success.

## Preflight

1. Verify the repository is on the independently reviewed candidate commit and
   the worktree is clean.
2. Verify migrations `0001` through `0009` are present and unchanged. Apply
   `0009__record_legacy_bronze_reconciliation.sql` through the accepted migration
   runner before invoking this operator.
3. Confirm the dedicated MinIO identity has exactly the policy above and the
   mediator is healthy on its internal network.
4. Set the six environment variables named in the authority section without
   reading the repository `.env`.
5. Record a sanitized pre-run Docker and database inventory through the governed
   review procedure.

## Mandatory dry run

Run dry mode first through the hardened one-shot launcher. This is necessary
because the Legal Hold mediator is intentionally not published to the host:

```bash
infra/legacy/run_legacy_reconciliation.sh --dry-run
```

Require exit `0` and `DRY_RUN_COMPLETE_NO_MUTATION`. Review deterministic counts
for database rows, original versions, manifest versions, delete markers, exact and
null-version candidates, ambiguities, mismatches, storage/database orphans,
contradictory metadata, and total unresolved/quarantined entities. Dry mode reads
PostgreSQL and exact MinIO versions but does not insert database evidence or alter
retention or Legal Hold.

If the inventory is incomplete, credentials are broader than documented, or the
counts cannot be explained, stop. Do not proceed by guessing a mapping.

## Explicit apply

After the dry-run evidence is reviewed and approved, use both mutation switches:

```bash
infra/legacy/run_legacy_reconciliation.sh \
  --apply \
  --confirm-legacy-365-day-reconciliation
```

Apply must end with `PASS_LEGACY_RECONCILED_OR_QUARANTINED`. A nonzero exit or
missing marker is not acceptance. Do not expose raw exception output if it could
contain a credential or connection URL.

## Retry and interruption

Evidence inserts are transactional and append-only. If enforcement fails for an
exact version, the inventory entity is durably quarantined rather than silently
omitted. Repeating the same complete inventory reuses its run and success facts,
revalidates exact-version retention and Hold ON, preserves the original policy
assignment, and does not create duplicate success evidence. Any missing success
fact or changed final classification fails closed.

After an interruption, run dry mode again. Compare the new inventory hash and
counts with retained evidence before deciding whether the same controlled apply
is safe. Never edit a committed reconciliation fact.

## Closeout

Verify that:

- every database row and exact storage version is represented exactly once;
- every success has exact version ID, matching SHA-256, COMPLIANCE readback,
  non-shortened retain-until, and Legal Hold ON;
- quarantine classifications and failure codes are explicit;
- ordinary runtime roles cannot insert, update, or delete reconciliation evidence;
- backup can only read it;
- repeated execution does not add a run or success row;
- no Bronze object, object version, or delete marker was deleted or overwritten;
- no operator secret appears in logs or evidence.

Remove or disable the temporary operator identities through the separate
administrative process. This candidate does not authorize the later
`SUCCESS_CONSTRAINT_VALIDATED` ticket, M0-R05, or real company data.
