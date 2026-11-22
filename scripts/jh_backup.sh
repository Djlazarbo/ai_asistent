#!/usr/bin/env bash
# Daily backup: Postgres dump + telethon session. Keep 7 days.
set -euo pipefail
ROOT="${JH_ROOT:-/opt/job-hunter}"
DEST="${JH_BACKUP_DIR:-/opt/job-hunter/backups}"
STAMP="$(date -u +%Y%m%d_%H%M%S)"
DAY_DIR="$DEST/$STAMP"
mkdir -p "$DAY_DIR"

echo "== postgres =="
docker exec jobhunter_postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$DAY_DIR/postgres.dump"

echo "== telethon session =="
if [[ -f "$ROOT/data/telethon_session.session" ]]; then
  cp -a "$ROOT/data/telethon_session.session" "$DAY_DIR/telethon_session.session"
fi

echo "== config keys (no secrets dump of .env) =="
cp -a "$ROOT/VERSION" "$DAY_DIR/VERSION" 2>/dev/null || true
# store checksums only for env presence
if [[ -f "$ROOT/.env" ]]; then
  sha256sum "$ROOT/.env" | awk '{print $1}' > "$DAY_DIR/env.sha256"
fi

# prune >7 days
find "$DEST" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +

echo "BACKUP_OK $DAY_DIR"
ls -la "$DAY_DIR"
