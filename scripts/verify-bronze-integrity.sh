#!/bin/sh
set -eu

set -a
. ./.env
set +a

docker compose run --rm --no-deps \
  -e MC_HOST_verify="http://${MINIO_BACKUP_ACCESS_KEY}:${MINIO_BACKUP_SECRET_KEY}@minio:9000" \
  --entrypoint /bin/sh minio-bootstrap -c '
    set -eu
    mc find verify/sc-rd-bronze-manifests --name "v1.json" --exec "mc cat {}" |
      while IFS= read -r manifest; do
        key=$(printf "%s" "$manifest" | sed -n "s/.*\"stored_object_key\":\"\([^\"]*\)\".*/\1/p")
        expected=$(printf "%s" "$manifest" | sed -n "s/.*\"sha256\":\"\([0-9a-f]*\)\".*/\1/p")
        actual=$(mc cat "verify/sc-rd-bronze-originals/$key" | sha256sum | cut -d " " -f 1)
        test "$actual" = "$expected"
        echo "verified $key"
      done
  '
