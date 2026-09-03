#!/bin/sh
set -eu

confirmation=${SMARTCOAT_DISASTER_RECOVERY_CONFIRM:-}
expected_confirmation=DESTROYED_FRESH_VOLUMES_SYNTHETIC_ONLY

fail() {
  printf '%s\n' "DISASTER_RECOVERY_FAILED: $1" >&2
  exit 1
}

test "$confirmation" = "$expected_confirmation" ||
  fail "explicit authorization is required"

command=${1:-}
backup_dir=${2:-}
test "$command" = backup || test "$command" = restore || test "$command" = verify-backup ||
  fail "usage: disaster-recovery.sh {backup|restore|verify-backup} ABSOLUTE_BACKUP_DIR"
test -n "$backup_dir" || fail "an absolute backup directory is required"
case "$backup_dir" in /*) ;; *) fail "backup directory must be absolute" ;; esac

env_file=${ENV_FILE:-}
test -n "$env_file" && test -f "$env_file" || fail "ENV_FILE must name a readable file"
case "$env_file" in */.env|.env) fail "the repository .env is not an accepted drill input" ;; esac

set -a
# shellcheck disable=SC1090
. "$env_file"
set +a

project=${COMPOSE_PROJECT_NAME:-}
scope_root=${DISASTER_RECOVERY_SCOPE_ROOT:-}
postgres_data=${POSTGRES_DATA_DIR:-}
minio_data=${MINIO_DATA_DIR:-}
test -n "$project" || fail "COMPOSE_PROJECT_NAME is required"
test -n "$scope_root" || fail "DISASTER_RECOVERY_SCOPE_ROOT is required"
case "$scope_root" in /*) ;; *) fail "scope root must be absolute" ;; esac
case "$postgres_data" in /*) ;; *) fail "POSTGRES_DATA_DIR must be absolute" ;; esac
case "$minio_data" in /*) ;; *) fail "MINIO_DATA_DIR must be absolute" ;; esac

scope_root=${scope_root%/}
case "$postgres_data" in "$scope_root"/*) ;; *) fail "PostgreSQL data is outside the owned scope" ;; esac
case "$minio_data" in "$scope_root"/*) ;; *) fail "MinIO data is outside the owned scope" ;; esac
case "$backup_dir" in "$scope_root"/*) ;; *) fail "backup is outside the owned scope" ;; esac
test -f "$scope_root/.smartcoat-disaster-recovery-owned" ||
  fail "owned-scope marker is missing"

for required in POSTGRES_DB POSTGRES_USER POSTGRES_BACKUP_PASSWORD; do
  eval "value=\${$required:-}"
  test -n "$value" || fail "$required is required"
done

secret_file=$(mktemp "$scope_root/.dr-secrets.XXXXXX")
chmod 600 "$secret_file"
cleanup_secret_file() { rm -f "$secret_file"; }
trap cleanup_secret_file EXIT HUP INT TERM
for name in POSTGRES_PASSWORD POSTGRES_APP_PASSWORD POSTGRES_BACKUP_PASSWORD \
  POSTGRES_INGESTION_PASSWORD POSTGRES_OCR_PASSWORD POSTGRES_REVIEW_PASSWORD \
  MINIO_ROOT_USER MINIO_ROOT_PASSWORD MINIO_APP_ACCESS_KEY MINIO_APP_SECRET_KEY \
  MINIO_OCR_ACCESS_KEY MINIO_OCR_SECRET_KEY MINIO_BACKUP_ACCESS_KEY \
  MINIO_BACKUP_SECRET_KEY MINIO_HOLD_APPLIER_ACCESS_KEY \
  MINIO_HOLD_APPLIER_SECRET_KEY LEGAL_HOLD_APPLIER_CALL_TOKEN SESSION_SECRET \
  LOCAL_USER_PASSWORD; do
  eval "value=\${$name:-}"
  test -z "$value" || printf '%s\t%s\n' "$name" "$value" >> "$secret_file"
done

assert_secret_free() {
  output_file=$1
  operation=${2:-operation}
  while IFS="$(printf '\t')" read -r secret_name secret; do
    test -z "$secret" || ! grep -F "$secret" "$output_file" >/dev/null 2>&1 ||
      fail "$secret_name appeared in captured output for $operation"
  done < "$secret_file"
}

compose() {
  operation="${1:-compose}:${2:-none}:${3:-none}:${4:-none}"
  output_file=$(mktemp "$scope_root/.dr-output.XXXXXX")
  if docker compose --project-name "$project" "$@" >"$output_file" 2>&1; then
    status=0
  else
    status=$?
  fi
  assert_secret_free "$output_file" "$operation"
  if test "$status" -ne 0; then
    sed -n '1,80p' "$output_file" >&2
    rm -f "$output_file"
    fail "Docker Compose operation failed"
  fi
  rm -f "$output_file"
}

compose_quiet() {
  operation="${1:-compose}:${2:-none}:${3:-none}:${4:-none}"
  if ! docker compose --project-name "$project" "$@" >/dev/null 2>&1; then
    fail "silent Docker Compose operation failed for $operation"
  fi
}

verify_backup() {
  test -f "$backup_dir/postgres.dump" || fail "PostgreSQL dump is missing"
  test -d "$backup_dir/minio-data" || fail "MinIO backup tree is missing"
  test -f "$backup_dir/SHA256SUMS" || fail "backup checksum manifest is missing"
  (cd "$backup_dir" && shasum -a 256 -c SHA256SUMS >/dev/null) ||
    fail "backup checksum verification failed"
  test -d "$backup_dir/minio-data/.minio.sys" ||
    fail "MinIO system metadata is missing from the backup"
}

if test "$command" = verify-backup; then
  verify_backup
  printf '%s\n' "DISASTER_RECOVERY_BACKUP_VERIFIED"
  exit 0
fi

if test "$command" = backup; then
  test ! -e "$backup_dir" || fail "backup destination already exists"
  test -d "$minio_data" || fail "source MinIO data directory is missing"
  mkdir -m 700 "$backup_dir"
  mkdir -m 700 "$backup_dir/minio-data"

  paused=
  resume_paused() {
    if test -n "$paused"; then
      # shellcheck disable=SC2086
      docker compose --project-name "$project" unpause $paused >/dev/null 2>&1 || true
      paused=
    fi
  }
  trap 'resume_paused; cleanup_secret_file' EXIT HUP INT TERM
  for service in api ocr-worker legal-hold-applier; do
    if docker compose --project-name "$project" ps --services --filter status=running |
      grep -Fx "$service" >/dev/null 2>&1; then
      compose pause "$service"
      paused="$paused $service"
    fi
  done
  compose stop minio

  dump_output=$(mktemp "$scope_root/.dr-dump-output.XXXXXX")
  if PGPASSWORD="$POSTGRES_BACKUP_PASSWORD" docker compose --project-name "$project" \
    exec -T -e PGPASSWORD postgres pg_dump --format=custom --no-owner \
    -h 127.0.0.1 -U smartcoat_backup -d "$POSTGRES_DB" \
    >"$backup_dir/postgres.dump" 2>"$dump_output"; then
    dump_status=0
  else
    dump_status=$?
  fi
  assert_secret_free "$dump_output" "pg_dump"
  if test "$dump_status" -ne 0; then
    sed -n '1,80p' "$dump_output" >&2
    rm -f "$dump_output"
    fail "PostgreSQL backup failed"
  fi
  rm -f "$dump_output"
  cp -a "$minio_data/." "$backup_dir/minio-data/"
  (cd "$backup_dir" && find . -type f ! -name SHA256SUMS -print0 |
    sort -z | xargs -0 shasum -a 256 > SHA256SUMS)
  verify_backup

  compose start minio
  resume_paused
  trap cleanup_secret_file EXIT HUP INT TERM
  printf '%s\n' "DISASTER_RECOVERY_BACKUP_CREATED"
  exit 0
fi

verify_backup
test -d "$postgres_data" && test -d "$minio_data" ||
  fail "fresh replacement data directories must already exist"
test -z "$(find "$postgres_data" -mindepth 1 -maxdepth 1 -print -quit)" ||
  fail "PostgreSQL replacement directory is not empty"
test -z "$(find "$minio_data" -mindepth 1 -maxdepth 1 -print -quit)" ||
  fail "MinIO replacement directory is not empty"

restore_started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
cp -a "$backup_dir/minio-data/." "$minio_data/"

# A fresh PostgreSQL cluster runs init.sql once.  Explicit adoption and the
# governed migrations create the cluster-level runtime roles before pg_restore
# replays ACLs that reference them.  --clean then replaces bootstrap objects.
compose up -d --wait postgres
compose run --rm postgres-migrate adopt "$POSTGRES_DB"
compose run --rm postgres-migrate apply
compose run --rm postgres-role-provision

restore_output=$(mktemp "$scope_root/.dr-restore-output.XXXXXX")
if docker compose --project-name "$project" exec -T postgres pg_restore \
  --clean --if-exists --exit-on-error --no-owner \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  <"$backup_dir/postgres.dump" >"$restore_output" 2>&1; then
  restore_status=0
else
  restore_status=$?
fi
assert_secret_free "$restore_output" "pg_restore"
if test "$restore_status" -ne 0; then
  sed -n '1,80p' "$restore_output" >&2
  rm -f "$restore_output"
  fail "PostgreSQL restore failed"
fi
rm -f "$restore_output"

# Validate the restored ACL contract, rotate in the supplied runtime secrets,
# and prove the restored migration ledger is complete and idempotent.
compose run --rm postgres-role-provision
compose run --rm postgres-migrate apply

# MinIO first sees the complete restored tree only after it has been copied into
# the fresh directory.  Bootstrap is rerun idempotently to restore policy files
# from source without substituting for the restored version/retention metadata.
compose up -d --wait minio
compose_quiet run --rm minio-bootstrap

restore_completed=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\n' "DISASTER_RECOVERY_RESTORE_COMPLETE start=$restore_started end=$restore_completed"
