# Security checklist (do NOT commit secrets here — this file is documentation only)

## Rotate if ever pasted in chat / screenshots

- [ ] GitHub account password
- [ ] All fine-grained / classic PATs used in chat — delete in GitHub Settings
- [ ] Telegram bot token — @BotFather → /revoke → update local config.yaml only
- [ ] Deepseek API key if exposed
- [ ] Telegram api_id / api_hash if exposed

## Repo hygiene

- [x] config.yaml, .env, sessions, workflow_* with tokens in .gitignore
- [x] Only config.example.yaml and .env.example in Git
- [x] docker-compose.prod.yml binds Postgres to 127.0.0.1 only
- [x] GitHub Actions secrets: SSH_HOST, SSH_USER, SSH_KEY (private key never in repo)
- [ ] Prefer static public IP on Yandex VM for CD

## Server (audited 2026-11-22)

- [x] SSH: `PasswordAuthentication no`, `PermitRootLogin prohibit-password`
      (`/etc/ssh/sshd_config.d/00-hardening.conf` — must sort BEFORE 50-cloud-init.conf:
      in sshd the first occurrence wins). Root login by key only.
- [x] ufw active: incoming allow OpenSSH only (Postgres/score bind 127.0.0.1,
      poller :5680 not published).
- [x] Bot routers auth-gated: handle_message, handle_menu, handle_callback,
      handle_detail, handle_favorites_callback, /feed, /list (fail-closed).
- [x] Multi-user isolation: user_vacancies/settings/channels scoped by chat_id
      (see CHANGELOG 1.2.1: removed shadowed chat-blind wrappers).
- [ ] chmod 600 on /opt/job-hunter/config.yaml, data/telethon_session.session,
      backups/ (declined 2026-11-22 — only root user on host; revisit if more
      users/services appear).
- [ ] Delete local D:\Job\_deploy.tar and _deploy_env (contain DB password and
      DeepSeek key; not in git).
- [ ] GitHub: 2FA on account + branch protection on main (Actions holds the
      deploy SSH key).
- [ ] Deps: pin requirements.txt versions (currently `>=` ranges).

## Stage vs prod bots

Use a separate Telegram bot token for stage (PC) and prod (server) so test /searchnow never hits the live chat.
