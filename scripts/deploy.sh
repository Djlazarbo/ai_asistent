#!/usr/bin/env bash
# Idempotent deploy step after git pull (called by GitHub Actions CD).
set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"

echo "==> Deploy in $APP_DIR"
git rev-parse --short HEAD

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [ ! -f .env ] || [ ! -f config.yaml ]; then
  echo "Missing .env or config.yaml — abort"
  exit 1
fi

echo "==> Postgres (prod compose, localhost only)"
export APP_ENV="${APP_ENV:-prod}"
if [ -f docker-compose.prod.yml ]; then
  docker compose -f docker-compose.prod.yml up -d
else
  docker compose up -d postgres
fi

# Optional systemd units if installed
for unit in jobhunter-score jobhunter-bot jobhunter-poller jobhunter-telethon; do
  if systemctl list-unit-files | grep -q "^${unit}.service"; then
    echo "==> Restart $unit"
    sudo systemctl restart "$unit"
  fi
done

echo "==> Deploy OK $(date -Is)"
