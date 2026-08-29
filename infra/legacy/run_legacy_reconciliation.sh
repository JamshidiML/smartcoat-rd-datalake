#!/bin/sh
set -eu

blocked() {
    printf '%s\n' "BLOCKED_LEGACY_RECONCILIATION_OPERATOR_BOUNDARY" >&2
    exit 2
}

case "${1:-}" in
    --dry-run)
        [ "$#" -eq 1 ] || blocked
        ;;
    --apply)
        [ "$#" -eq 2 ] || blocked
        [ "${2:-}" = "--confirm-legacy-365-day-reconciliation" ] || blocked
        ;;
    *)
        blocked
        ;;
esac

required_names="
LEGACY_RECONCILIATION_OPERATOR_IMAGE_ID
LEGACY_RECONCILIATION_DOCKER_NETWORK
LEGACY_RECONCILIATION_DATABASE_URL
MINIO_ENDPOINT
MINIO_LEGACY_RECONCILIATION_ACCESS_KEY
MINIO_LEGACY_RECONCILIATION_SECRET_KEY
LEGAL_HOLD_APPLIER_URL
LEGAL_HOLD_APPLIER_CALL_TOKEN
"

for variable_name in $required_names; do
    eval "variable_value=\${$variable_name:-}"
    [ -n "$variable_value" ] || blocked
    case "$variable_value" in
        *"
"*|*""*) blocked ;;
    esac
done

case "$LEGACY_RECONCILIATION_OPERATOR_IMAGE_ID" in
    sha256:????????????????????????????????????????????????????????????????) ;;
    *) blocked ;;
esac

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_root=$(CDPATH= cd -- "$script_directory/../.." && pwd)

exec docker run --rm --pull=never --read-only \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --network "$LEGACY_RECONCILIATION_DOCKER_NETWORK" \
    --env PYTHONPATH=/workspace/apps/api/src \
    --env LEGACY_RECONCILIATION_DATABASE_URL \
    --env MINIO_ENDPOINT \
    --env MINIO_SECURE \
    --env MINIO_LEGACY_RECONCILIATION_ACCESS_KEY \
    --env MINIO_LEGACY_RECONCILIATION_SECRET_KEY \
    --env LEGAL_HOLD_APPLIER_URL \
    --env LEGAL_HOLD_APPLIER_CALL_TOKEN \
    --mount "type=bind,src=$repository_root/infra/legacy,dst=/workspace/infra/legacy,readonly" \
    --mount "type=bind,src=$repository_root/apps/api/src,dst=/workspace/apps/api/src,readonly" \
    --entrypoint python \
    "$LEGACY_RECONCILIATION_OPERATOR_IMAGE_ID" \
    /workspace/infra/legacy/legacy_reconciliation.py "$@"
