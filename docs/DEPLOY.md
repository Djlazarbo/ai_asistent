# Production deploy (Yandex VM)

## Prerequisites

1. SSH access works:
   ```bash
   ssh -i ~/.ssh/djlazarboy djlazarboy@YOUR_STATIC_IP
   ```
   Or add this PC pubkey in Yandex Console → VM → SSH keys:
   `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBB+fTcAGbGH7quDw4nYG6KNppd21E9rxbRsrNk2h71u drago@DJlazarenko`

2. Prefer a **static** public IP (Dynamic IP breaks CD).

3. Rotate secrets listed in [SECURITY.md](SECURITY.md).

## One-time bootstrap on the VM

```bash
# as djlazarboy
sudo mkdir -p /opt/job-hunter && sudo chown $USER:$USER /opt/job-hunter
git clone -b main https://github.com/Djlazarbo/ai_asistent.git /opt/job-hunter
cd /opt/job-hunter
bash scripts/server_bootstrap.sh
# edit .env (APP_ENV=prod, DEEPSEEK_API_KEY, POSTGRES_*) and config.yaml (prod bot token)
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobhunter-score jobhunter-bot jobhunter-poller jobhunter-telethon
bash scripts/deploy.sh
curl -s http://127.0.0.1:5679/health
```

Postgres is bound to `127.0.0.1:5432` via `docker-compose.prod.yml`.

`APP_ENV=prod` loads `agents.prod.yaml` (DeepSeek for PM filter — no Ollama).

## GitHub Actions CD secrets

Repo → Settings → Secrets and variables → Actions:

| Name | Value |
|------|--------|
| `SSH_HOST` | VM public IP |
| `SSH_USER` | `djlazarboy` |
| `SSH_KEY` | full private key PEM/OpenSSH contents |
| `DEPLOY_PATH` | `/opt/job-hunter` (optional) |

Also create Environment `production` (referenced by deploy workflow).

After secrets exist, push to `main` or run **Deploy prod** → Run workflow.

## Smoke checklist

- [ ] `systemctl is-active jobhunter-score jobhunter-bot jobhunter-poller`
- [ ] `curl -s localhost:5679/health` → ok
- [ ] Telegram `/start` on **prod** bot
- [ ] Stage PC still uses local `start.bat` + Ollama + n8n
