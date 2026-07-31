# Production deploy (Yandex VM) — Docker images

## Stack

`docker compose -f docker-compose.prod.yml` builds/runs:

| Container | Image | Role |
|-----------|--------|------|
| `jobhunter_postgres` | postgres:16-alpine | DB on `127.0.0.1:5432` |
| `jobhunter_score` | `jobhunter-app` | filter + DeepSeek + `/webhook/vacancy` |
| `jobhunter_bot` | `jobhunter-app` | Telegram bot (watchdog) |
| `jobhunter_poller` | `jobhunter-poller` | hh/habr/getmatch (Playwright) |
| `jobhunter_telethon` | `jobhunter-app` | TG channels |

No n8n / Ollama on the VM. Pollers POST to `http://score:5679/webhook/vacancy`.

## Prerequisites

1. SSH access restored (see Yandex recovery docs if key lost).
2. Static public IP recommended.
3. Secrets: rotate items in [SECURITY.md](SECURITY.md).

## One-time bootstrap

```bash
ssh djlazarboy@YOUR_IP
sudo mkdir -p /opt/job-hunter && sudo chown $USER:$USER /opt/job-hunter
git clone -b main https://github.com/Djlazarbo/ai_asistent.git /opt/job-hunter
cd /opt/job-hunter
bash scripts/server_bootstrap.sh
# edit .env and config.yaml, then:
bash scripts/deploy.sh
curl -s http://127.0.0.1:5679/health
docker compose -f docker-compose.prod.yml ps
```

Telethon: create session once (local or interactive), put `data/telethon_session.session` on the server.

## GitHub Actions CD

Secrets: `SSH_HOST`, `SSH_USER`, `SSH_KEY`, optional `DEPLOY_PATH=/opt/job-hunter`.  
Deploy runs `git reset --hard origin/main` + `scripts/deploy.sh` → `docker compose up -d --build`.

## Smoke

- [ ] `docker compose -f docker-compose.prod.yml ps` — all Up
- [ ] `curl -s localhost:5679/health` → `{"status":"ok"}`
- [ ] Telegram `/start` on **prod** bot
- [ ] Stage PC unchanged: `docker-compose.yml` + `start.bat` + local n8n
