#!/bin/sh
set -eu

env_file=${ENV_FILE:-./.env}
test -f "$env_file"

set -a
. "$env_file"
set +a

case "$BACKUP_ROOT" in
  '~/'*) backup_root="$HOME/${BACKUP_ROOT#~/}" ;;
  /*) backup_root="$BACKUP_ROOT" ;;
  *) backup_root="$HOME/$BACKUP_ROOT" ;;
esac

if [ "${1:-}" = "backup" ]; then
  umask 077
  case "$MINIO_DATA_DIR" in
    /*) minio_source="$MINIO_DATA_DIR" ;;
    *) minio_source="$(pwd)/${MINIO_DATA_DIR#./}" ;;
  esac
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  destination="$backup_root/$timestamp"
  mkdir -p "$destination/minio-data"
  docker compose exec -T -e PGPASSWORD="$POSTGRES_BACKUP_PASSWORD" postgres pg_dump \
    --format=custom --no-owner --no-privileges \
    -h 127.0.0.1 -U smartcoat_backup -d "$POSTGRES_DB" > "$destination/postgres.dump"
  cp -a "$minio_source/." "$destination/minio-data/"
  (cd "$destination" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 shasum -a 256 > SHA256SUMS)
  ln -sfn "$destination" "$backup_root/latest"
  echo "Local backup created: $destination"
  exit 0
fi

backup_dir=${1:-"$backup_root/latest"}
test -f "$backup_dir/postgres.dump"
test -d "$backup_dir/minio-data"
(cd "$backup_dir" && shasum -a 256 -c SHA256SUMS)

restore_db="smartcoat_rd_restore_drill"
docker compose exec -T postgres dropdb --if-exists -U "$POSTGRES_USER" "$restore_db"
docker compose exec -T postgres createdb -U "$POSTGRES_USER" "$restore_db"
docker compose exec -T postgres pg_restore --no-owner --no-privileges \
  -U "$POSTGRES_USER" -d "$restore_db" < "$backup_dir/postgres.dump"

record=$(docker compose exec -T postgres psql -At -F '|' -U "$POSTGRES_USER" -d "$restore_db" -c \
  "SELECT source_sha256, stored_object_key, manifest_object_key FROM uploads ORDER BY uploaded_at_utc LIMIT 1")
test -n "$record"
expected_sha=$(printf '%s' "$record" | cut -d '|' -f 1)
original_key=$(printf '%s' "$record" | cut -d '|' -f 2)
manifest_key=$(printf '%s' "$record" | cut -d '|' -f 3)

restore_container="sc-rd-minio-restore-drill"
docker rm -f "$restore_container" >/dev/null 2>&1 || true
docker run -d --rm --name "$restore_container" \
  -e MINIO_ROOT_USER="$MINIO_ROOT_USER" \
  -e MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD" \
  -v "$backup_dir/minio-data:/data:ro" \
  minio/minio:RELEASE.2025-07-23T15-54-02Z server /data >/dev/null
trap 'docker rm -f "$restore_container" >/dev/null 2>&1 || true; docker compose exec -T postgres dropdb --if-exists -U "$POSTGRES_USER" "$restore_db" >/dev/null' EXIT
sleep 3

actual_sha=$(docker run --rm --network "container:$restore_container" \
  --entrypoint /bin/sh minio/mc:RELEASE.2025-07-21T05-28-08Z -c \
  "mc alias set restore http://127.0.0.1:9000 '$MINIO_ROOT_USER' '$MINIO_ROOT_PASSWORD' >/dev/null && mc cat 'restore/sc-rd-bronze-originals/$original_key' | sha256sum | cut -d ' ' -f 1")
test "$actual_sha" = "$expected_sha"
docker run --rm --network "container:$restore_container" \
  --entrypoint /bin/sh minio/mc:RELEASE.2025-07-21T05-28-08Z -c \
  "mc alias set restore http://127.0.0.1:9000 '$MINIO_ROOT_USER' '$MINIO_ROOT_PASSWORD' >/dev/null && mc stat 'restore/sc-rd-bronze-manifests/$manifest_key' >/dev/null"

echo "AT-13 restore drill passed: database provenance, original SHA-256, and manifest match."
