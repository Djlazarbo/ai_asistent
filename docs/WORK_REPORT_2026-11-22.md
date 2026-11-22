# Отчёт о работах — 22.11.2026 (GLM-аудит и релизы 1.2.1–1.2.2)

Полный лог сессии: аудит multi-user, фиксы, деплой, аудит безопасности. Все правки в git (`develop` + `main`, HEAD на момент отчёта `38dee71`), прод работает на **1.2.2**.

---

## 1. Аудит кода — что нашлось

### P0 (исправлено в 1.2.1)
- **Утечка данных между юзерами**: блок-дубликат в `db_postgres.py` переопределял MU-безопасные `save_cover_example`/`get_cover_examples`/`save_interview`/`get_interview_history` chat-blind-версиями → заметки интервью и письма видны всем, сохранение письма портило данные (external_id вместо текста), сохранение интервью падало (TypeError). Удалено; MU-обёртки принимают явный `chat_id=`, в multi-user без chat_id — RuntimeError (fail-closed).
- **`/drainpending` мог крутиться вечно**: без poison-pill «вечная» pending-вакансия обрабатывалась бесконечно; DB-шибка убивала drain-задачу молча; счётчик считал неудачи. Теперь: skip после 2 фейлов, стоп после 3 залипаний, try/except, честный счётчик (также `_silent_drain`).
- **`/feed` и `/list`** отдавали глобальную legacy-ленту без скоупинга и проверки доступа → скоупинг + auth-гейт.

### P1 (исправлено в 1.2.1)
- **`tg_notify.py` не существовал**, хотя `score_server` импортирует его с 1.2.0 → мгновенные пуши умирали на ImportError. Модуль создан (Bot API, HTML-экранирование, токен из env/config, fail-closed) и добавлен в оба Dockerfile.
- **Telethon-гейт мониторинга** читал глобальный флаг владельца → пауза одного юзера останавливала TG-ингест всем. Теперь гейт = «у любого активного юзера monitoring ON».
- 409 «поиск уже в очереди» проваливался во второй (лёгкий) поиск; лёгкий fallback без кулдауна. Исправлено (+ `LIGHT_SEARCH_COOLDOWN_SEC`).
- Мелочи: `"unknown"` в channel-blacklist; hasattr-гард вместо вызова несуществующей `update_vacancy_status_for_chat`; `/retryscoring` проверяет занятый drain.

### P0 UX (исправлено в 1.2.2)
- **Бот «замерзал» на всё время поиска для всех юзеров**: PTB обрабатывает апдейты последовательно по умолчанию, а хендлеры ждали 10–30-минутный поиск инлайном. Теперь: `concurrent_updates(True)` + `_spawn_search()` (фон, прогресс/результат редактируют сообщение, повторный клик — «поиск уже идёт»).

### Auth-гейты (задеплоено после 1.2.2)
- Все роутеры (`handle_message`, `handle_menu`, `handle_callback`, `handle_detail`, `handle_favorites_callback`, `/feed`, `/list`) проверяют `_is_allowed` (fail-closed при ошибке БД).

### Тесты
- `test_mu_regressions.py` — 19 юнит-тестов (channel overwrite, monitoring/notify default, backfill `only_chat_id`, fan-out изоляция, сигнатуры cover/interview, poison-skip, 409-fallthrough, кулдаун light, tg_notify, фоновый поиск, concurrent_updates).
- Итог: **354 passed** (`python -m pytest -q -m "not integration and not slow"`). Интеграционные (живые PG/score) — отдельно: `-m integration`.

---

## 2. Проверка прода (31.76.6.234)

Было (до работ): 5/5 контейнеров Up, БД здорова, зависшего pending нет, инжест 4 источников живой (hh=32/habr=25/tg=11/gm=5 за сутки).

Нашёл и исправил:
1. **`docker logs jobhunter_score` «ослеп» с 18.11** (лог-файл подменён снаружи, API отдавал старицу) → вылечено пересозданием контейнера при деплое.
2. **`scripts/jh_digest.py` падал каждый день с установки** (`GROUP BY 1` указывал на выражение с агрегатом) → хостовый digest владельцу не отправлялся ни разу. SQL исправлен (`GROUP BY source/status`), проверен на прод-БД.

---

## 3. Деплой

- Всё недостающее добавлено в git: `multiuser.py`, `light_hh.py`, `user_feed_backfill.py`, `tg_notify.py`, cron-скрипты (`jh_monitor.py`, `jh_digest.py`, `jh_backup.sh`, `install_host_cron.sh`), бриф, тесты. **main больше не отстаёт от прода.**
- Сервер не git-клон → деплой вручную зеркалом Actions-скрипта: `git -c core.autocrlf=false archive main | ssh … tar -x` + `bash scripts/deploy.sh` (в `deploy.sh` добавлен fallback `sha=manual` для деплоя без .git).
- **Poller-образ не пересобирается**: `mcr.microsoft.com` недоступен с VM (TLS-таймаут), локального базового Playwright-образа нет. Текущий poller-образ функционально идентичен (код poller'а в 1.2.1/1.2.2 не менялся). Варианты: proxy для dockerd или `docker save | ssh docker load` с ПК.
- Версия на проде: `/health` → `{"status":"ok","version":"1.2.2","git":"manual"}`.

## 4. Безопасность (детали — docs/SECURITY.md)

Применено (по подтверждению владельца):
- SSH: `PasswordAuthentication no`, `PermitRootLogin prohibit-password` (`/etc/ssh/sshd_config.d/00-hardening.conf`; имя с `00-` обязательно — в sshd побеждает первое вхождение, `50-cloud-init.conf` иначе перекрывает). До фикса: **4043 неудачных попытки входа за сутки**. Проверено: ключ работает, пароль отклоняется.
- ufw: active, входящий только SSH.
- Auth-гейты бота (см. выше).

Осталось (на решение владельца): GitHub 2FA + branch protection на `main` (в Actions лежит SSH-ключ прода); chmod 600 на config.yaml/session/backups; удалить локальные `_deploy.tar`/`_deploy_env` (содержат пароль БД и DeepSeek-ключ, вне git); pin версий requirements.txt.

## 5. Как проверять

```bash
# локально
cd D:\Job && python -m pytest -q -m "not integration and not slow"   # 354 passed
# прод
ssh -i ~/.ssh/yandex_djlazarboy32 root@31.76.6.234
curl -s localhost:5679/health            # version 1.2.2
docker ps --format '{{.Names}}: {{.Status}}' | grep jobhunter
```
Смоук в боте: «Искать сейчас» (бот отвечает сразу, прогресс в фоне, другие юзеры не блокируются); /feed — только своя лента.
