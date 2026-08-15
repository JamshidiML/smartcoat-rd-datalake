#!/bin/sh
set -eu

if [ ! -f .env ]; then
  echo "Missing .env; copy .env.example and replace all placeholders." >&2
  exit 1
fi

set -a
. ./.env
set +a

docker compose exec -T postgres psql \
  -v ON_ERROR_STOP=1 \
  -v user_id="$LOCAL_USER_ID" \
  -v display_name="$LOCAL_USER_DISPLAY_NAME" \
  -v email="$LOCAL_USER_EMAIL" \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'SQL'
INSERT INTO users (user_id, display_name, email, role, active, created_at_utc)
VALUES (:'user_id', :'display_name', :'email', 'ADMIN_REVIEWER', true, now())
ON CONFLICT (user_id) DO UPDATE
SET display_name = EXCLUDED.display_name, email = EXCLUDED.email, active = true;
SQL

echo "Local pilot user is ready: $LOCAL_USER_ID"
