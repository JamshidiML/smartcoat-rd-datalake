#!/bin/sh
set -eu

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 /absolute/path/to/local-pilot-files" >&2
  exit 1
fi
case "$1" in
  /*) pilot_dir="$1" ;;
  *) echo "Pilot directory must be an absolute path outside this repository." >&2; exit 1 ;;
esac

repository_root=$(git rev-parse --show-toplevel)
case "$pilot_dir/" in
  "$repository_root"/*) echo "Real pilot files must never be placed inside the Git repository." >&2; exit 1 ;;
esac

set -a
. "$repository_root/.env"
set +a

login_payload=$(printf '{"email":"%s","password":"%s"}' "$LOCAL_USER_EMAIL" "$LOCAL_USER_PASSWORD")
token=$(curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  -d "$login_payload" http://127.0.0.1:8000/api/auth/login |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

find "$pilot_dir" -maxdepth 1 -type f \( \
  -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.heic' -o \
  -iname '*.pdf' -o -iname '*.xlsx' -o -iname '*.xls' \) -print | while IFS= read -r file; do
  echo "Uploading local-only source: $(basename "$file")"
  curl --fail --silent --show-error \
    -H "Authorization: Bearer $token" \
    -F "file=@$file" \
    -F 'document_category=OTHER' \
    -F 'context_note=Authorized Phase-1 R&D pilot batch source.' \
    http://127.0.0.1:8000/api/uploads
  echo
done
