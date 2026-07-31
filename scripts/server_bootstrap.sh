#!/usr/bin/env bash
# One-time bootstrap on the Yandex Cloud VM (Ubuntu).
# Usage (as deploy user with sudo):
#   curl/git clone then: bash scripts/server_bootstrap.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/job-hunter}"
REPO_URL="${REPO_URL:-https://github.com/Djlazarbo/ai_asistent.git}"
BRANCH="${BRANCH:-main}"

echo "==> Installing packages"
sudo apt-get update -y
sudo apt-get install -y git curl ca-certificates python3 python3-venv python3-pip

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi

echo "==> Clone repo to $APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo chown "$USER:$USER" "$APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  cd "$APP_DIR"
  git fetch origin
  git checkout "$BRANCH"
  git reset --hard "origin/$BRANCH"
fi

cd "$APP_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "EDIT $APP_DIR/.env before starting (DEEPSEEK_API_KEY, POSTGRES_PASSWORD, APP_ENV=prod)"
fi

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "EDIT $APP_DIR/config.yaml (bot token, chat_id, n8n webhook, telegram api)"
fi

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Start Postgres"
docker compose up -d postgres

echo "==> Done. Next:"
echo "  1) Fill .env and config.yaml"
echo "  2) Set is_pm_filter to deepseek in agents.yaml or bot settings"
echo "  3) bash scripts/deploy.sh"
echo "  4) Add GitHub secrets SSH_HOST / SSH_USER / SSH_KEY for CD"
