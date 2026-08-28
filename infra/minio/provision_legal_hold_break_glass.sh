#!/bin/sh
set -eu

readonly ALIAS_NAME="smartcoat-hold-provision"
readonly AUDIT_BUCKET="sc-rd-legal-hold-audit"
readonly CONFIRMATION="CONFIRM_CREATE_LEGAL_HOLD_BREAK_GLASS"

fail() {
  printf '{"classification":"%s","message":"%s"}\n' "$1" "$2" >&2
  exit 2
}

require_value() {
  variable_name="$1"
  eval "variable_value=\${$variable_name:-}"
  [ -n "$variable_value" ] || fail "BLOCKED_AUTHORITY_BOUNDARY" "required value absent: $variable_name"
}

[ "${1:-}" = "--confirm-create-legal-hold-break-glass" ] \
  || fail "BLOCKED_AUTHORITY_BOUNDARY" "explicit provisioning authorization absent"
[ "$#" -eq 1 ] || fail "BLOCKED_AUTHORITY_BOUNDARY" "unexpected provisioning arguments"
[ "${LEGAL_HOLD_BREAK_GLASS_PROVISIONING_CONFIRMATION:-}" = "$CONFIRMATION" ] \
  || fail "BLOCKED_AUTHORITY_BOUNDARY" "exact provisioning confirmation absent"

for variable_name in \
  MINIO_HOLD_ENDPOINT \
  MINIO_PROVISIONING_ROOT_USER \
  MINIO_PROVISIONING_ROOT_PASSWORD \
  MINIO_HOLD_BREAK_GLASS_ACCESS_KEY \
  MINIO_HOLD_BREAK_GLASS_SECRET_KEY; do
  require_value "$variable_name"
done
[ "$MINIO_PROVISIONING_ROOT_USER" != "$MINIO_HOLD_BREAK_GLASS_ACCESS_KEY" ] \
  || fail "BLOCKED_AUTHORITY_BOUNDARY" "root and break-glass identities must differ"
[ "$MINIO_PROVISIONING_ROOT_PASSWORD" != "$MINIO_HOLD_BREAK_GLASS_SECRET_KEY" ] \
  || fail "BLOCKED_AUTHORITY_BOUNDARY" "root and break-glass secrets must differ"
command -v mc >/dev/null 2>&1 || fail "BLOCKED_ENVIRONMENT" "mc is unavailable"

policy_path="${LEGAL_HOLD_CONTROL_ROOT:-/control}/policies/legal-hold-break-glass.json"
[ -f "$policy_path" ] || fail "BLOCKED_IMPLEMENTATION_BOUNDARY" "break-glass policy is unavailable"

mc alias set --quiet "$ALIAS_NAME" "$MINIO_HOLD_ENDPOINT" \
  "$MINIO_PROVISIONING_ROOT_USER" "$MINIO_PROVISIONING_ROOT_PASSWORD" >/dev/null 2>&1 \
  || fail "BLOCKED_ENVIRONMENT" "provisioning alias failed"
mc mb --quiet --ignore-existing --with-lock "$ALIAS_NAME/$AUDIT_BUCKET" >/dev/null 2>&1 \
  || fail "BLOCKED_ENVIRONMENT" "audit bucket creation failed"
mc version enable --quiet "$ALIAS_NAME/$AUDIT_BUCKET" >/dev/null 2>&1 \
  || fail "BLOCKED_ENVIRONMENT" "audit bucket versioning failed"
mc retention set --quiet --default COMPLIANCE 365d "$ALIAS_NAME/$AUDIT_BUCKET" >/dev/null 2>&1 \
  || fail "BLOCKED_ENVIRONMENT" "audit COMPLIANCE floor failed"
mc anonymous set none "$ALIAS_NAME/$AUDIT_BUCKET" >/dev/null 2>&1 \
  || fail "BLOCKED_ENVIRONMENT" "audit anonymous-access denial failed"
mc admin policy create "$ALIAS_NAME" legal-hold-break-glass "$policy_path" >/dev/null 2>&1 \
  || fail "BLOCKED_ENVIRONMENT" "break-glass policy creation failed"
mc admin user add "$ALIAS_NAME" "$MINIO_HOLD_BREAK_GLASS_ACCESS_KEY" \
  "$MINIO_HOLD_BREAK_GLASS_SECRET_KEY" >/dev/null 2>&1 \
  || fail "BLOCKED_ENVIRONMENT" "break-glass identity creation failed"
mc admin policy attach "$ALIAS_NAME" legal-hold-break-glass \
  --user "$MINIO_HOLD_BREAK_GLASS_ACCESS_KEY" >/dev/null 2>&1 \
  || fail "BLOCKED_ENVIRONMENT" "break-glass policy attachment failed"

printf '%s\n' '{"classification":"PASS_LEGAL_HOLD_BREAK_GLASS_PROVISIONED","policy":"legal-hold-break-glass","audit_bucket":"sc-rd-legal-hold-audit"}'
