# Security checklist (do NOT commit secrets here — this file is documentation only)

## Rotate if ever pasted in chat / screenshots

- [ ] GitHub account password
- [ ] All fine-grained / classic PATs used in chat — delete in GitHub Settings
- [ ] Telegram bot token — @BotFather → /revoke → update local config.yaml only
- [ ] n8n API key — recreate in n8n, never commit
- [ ] Deepseek API key if exposed
- [ ] Telegram api_id / api_hash if exposed

## Repo hygiene

- [x] config.yaml, .env, sessions, workflow_* with tokens in .gitignore
- [x] Only config.example.yaml and .env.example in Git
- [x] docker-compose.prod.yml binds Postgres to 127.0.0.1 only
- [ ] GitHub Actions secrets: SSH_HOST, SSH_USER, SSH_KEY (private key never in repo)
- [ ] Prefer static public IP on Yandex VM for CD

## Stage vs prod bots

Use a separate Telegram bot token for stage (PC) and prod (server) so test /searchnow never hits the live chat.
