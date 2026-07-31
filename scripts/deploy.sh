#!/usr/bin/env bash
# Idempotent deploy: rebuild/restart Docker prod stack after git pull.
set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"

echo "==> Deploy in $APP_DIR"
git rev-parse --short HEAD

if [ ! -f .env ] || [ ! -f config.yaml ]; then
  echo "Missing .env or config.yaml — abort"
  exit 1
fi

# Ensure prod docker networking defaults in .env (without clobbering secrets)
grep -q '^APP_ENV=' .env || echo 'APP_ENV=prod' >> .env
grep -q '^POSTGRES_HOST=' .env || echo 'POSTGRES_HOST=postgres' >> .env
# Inside compose network Postgres listens on 5432
if grep -q '^POSTGRES_PORT=5433' .env; then
  sed -i 's/^POSTGRES_PORT=5433/POSTGRES_PORT=5432/' .env || true
fi

mkdir -p data
# Create empty session placeholder so Docker bind-mount works until first auth
if [ ! -f data/telethon_session.session ]; then
  touch data/telethon_session.session
fi

echo "==> Build & start Docker images"
docker compose -f docker-compose.prod.yml up -d --build --remove-orphans

echo "==> Health"
sleep 3
curl -fsS http://127.0.0.1:5679/health || true
docker compose -f docker-compose.prod.yml ps

echo "==> Deploy OK $(date -Is)"
