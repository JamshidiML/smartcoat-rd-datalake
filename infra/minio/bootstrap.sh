#!/bin/sh
set -eu

alias_name="local"
mc alias set "$alias_name" http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"

mc mb --ignore-existing --with-lock "$alias_name/sc-rd-bronze-originals"
mc mb --ignore-existing --with-lock "$alias_name/sc-rd-bronze-manifests"
mc mb --ignore-existing "$alias_name/sc-rd-ocr-artifacts"
mc version enable "$alias_name/sc-rd-bronze-originals"
mc version enable "$alias_name/sc-rd-bronze-manifests"
mc version enable "$alias_name/sc-rd-ocr-artifacts"
mc retention set --default COMPLIANCE 365d "$alias_name/sc-rd-bronze-originals"
mc retention set --default COMPLIANCE 365d "$alias_name/sc-rd-bronze-manifests"

mc admin policy create "$alias_name" app-bronze-write /bootstrap/policies/app-bronze-write.json
mc admin policy create "$alias_name" reviewer-read /bootstrap/policies/reviewer-read.json
mc admin policy create "$alias_name" legal-hold-applier /bootstrap/policies/legal-hold-applier.json

mc admin policy create "$alias_name" ocr-worker /bootstrap/policies/ocr-worker.json

mc admin user add "$alias_name" "$MINIO_APP_ACCESS_KEY" "$MINIO_APP_SECRET_KEY"
mc admin user add "$alias_name" "$MINIO_OCR_ACCESS_KEY" "$MINIO_OCR_SECRET_KEY"
mc admin user add "$alias_name" "$MINIO_BACKUP_ACCESS_KEY" "$MINIO_BACKUP_SECRET_KEY"
mc admin user add "$alias_name" "$MINIO_HOLD_APPLIER_ACCESS_KEY" "$MINIO_HOLD_APPLIER_SECRET_KEY"
mc admin policy attach "$alias_name" app-bronze-write --user "$MINIO_APP_ACCESS_KEY"
mc admin policy attach "$alias_name" ocr-worker --user "$MINIO_OCR_ACCESS_KEY"
mc admin policy attach "$alias_name" reviewer-read --user "$MINIO_BACKUP_ACCESS_KEY"
mc admin policy attach "$alias_name" legal-hold-applier --user "$MINIO_HOLD_APPLIER_ACCESS_KEY"

mc anonymous set none "$alias_name/sc-rd-bronze-originals"
mc anonymous set none "$alias_name/sc-rd-bronze-manifests"
mc anonymous set none "$alias_name/sc-rd-ocr-artifacts"
