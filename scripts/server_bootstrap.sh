#!/usr/bin/env bash
# One-time bootstrap on the Yandex Cloud VM (Ubuntu) — Docker-only prod.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/job-hunter}"
REPO_URL="${REPO_URL:-https://github.com/Djlazarbo/ai_asistent.git}"
BRANCH="${BRANCH:-main}"

echo "==> Installing packages"
sudo apt-get update -y
sudo apt-get install -y git curl ca-certificates

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
  echo "Log out/in (or newgrp docker) so docker works without sudo, then re-run this script."
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
mkdir -p data

if [ ! -f .env ]; then
  cp .env.example .env
  cat >> .env <<'EOF'

# --- prod docker overrides ---
APP_ENV=prod
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
N8N_WEBHOOK_URL=http://score:5679/webhook/vacancy
EOF
  echo "EDIT $APP_DIR/.env (DEEPSEEK_API_KEY, POSTGRES_PASSWORD)"
fi

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "EDIT $APP_DIR/config.yaml (prod bot token, chat_id, telegram api_id/hash)"
fi

if [ ! -f data/telethon_session.session ]; then
  touch data/telethon_session.session
fi

echo "==> Build & start stack"
docker compose -f docker-compose.prod.yml up -d --build

echo "==> Done."
echo "  docker compose -f docker-compose.prod.yml ps"
echo "  curl -s http://127.0.0.1:5679/health"
echo "  Telethon: first login may need interactive session on host, then copy *.session into data/"
echo "  Add GitHub secrets SSH_HOST / SSH_USER / SSH_KEY for CD"
