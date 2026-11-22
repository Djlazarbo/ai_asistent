# Job Hunter — бриф для проверки проекта (GLM)

Документ для внешнего ревьюера/агента (GLM). Цель: быстро понять продукт, архитектуру, multi-user модель, prod-контекст и на что смотреть при проверке. **Секреты в репо/этот файл не класть.**

---

## 1. Что это за продукт

**Job Hunter** — Telegram-бот поиска работы (не Atol и не другие проекты).

Пайплайн:
1. Источники: **hh.ru** (Playwright), **Хабр**, **Getmatch**, **Telegram-каналы** (Telethon).
2. Вакансии попадают в общий пул Postgres.
3. **score_server** (замена n8n на prod): фильтр is_pm + DeepSeek-скоринг под профиль пользователя.
4. Персональная лента в боте + опциональные пуши «новая вакансия».

Репозиторий: `DjLazarbo/ai_asistent` (private). Версия: см. `VERSION` (сейчас **1.2.0**). Рабочая копия на ПК: `D:\Job`.

Боевой бот: **@aiHANTERj_bot** (не путать с тестовыми/Atol-ботами).

---

## 2. Окружения

| | Stage (ПК) | Prod |
|--|------------|------|
| Ветка | `develop` | `main` |
| `APP_ENV` | `stage` | `prod` |
| Compose | `docker-compose.yml` (+ локальный запуск) | `docker-compose.prod.yml` |
| Postgres | Docker, часто `:5433` | VPS, `127.0.0.1:5432` |
| AI filter | можно Ollama | **DeepSeek** (Ollama на маленькой VM не ставить) |
| Секреты | локальные `config.yaml` / `.env` | только на сервере `/opt/job-hunter` |

В git **не** коммитятся: `config.yaml`, `.env`, `*.session`, credentials, логи. Шаблоны: `config.example.yaml`, `.env.example`.

### Prod-сервер (где крутится бот)

| | |
|--|--|
| Хост / IP | `31.76.6.234` |
| OS | Ubuntu |
| SSH | `root@31.76.6.234` (ключ локально у владельца, **не** в git; historically `~/.ssh/yandex_djlazarboy32`) |
| Каталог проекта на сервере | `/opt/job-hunter` |
| Compose | `docker compose -f docker-compose.prod.yml …` из `/opt/job-hunter` |
| Postgres с хоста | `127.0.0.1:5432` (наружу не торчит) |
| Score health | `http://127.0.0.1:5679/health` (только localhost на VM) |
| Poller health | внутри контейнера `http://127.0.0.1:5680/health` (с хоста обычно через `docker exec`) |

Секреты (`config.yaml`, `.env`, Telethon session) лежат **только на этом сервере** в `/opt/job-hunter`, не в репозитории.

---

## 3. Сервисы prod (`docker-compose.prod.yml`)

| Контейнер | Образ | Роль |
|-----------|--------|------|
| `jobhunter_postgres` | postgres:16-alpine | БД |
| `jobhunter_score` | `jobhunter-app` | `score_server.py` — `/webhook/vacancy`, `/filter`, `/health` :5679 |
| `jobhunter_bot` | `jobhunter-app` | `watchdog.py` → `bot.py` |
| `jobhunter_poller` | `jobhunter-poller` (Playwright) | `hh_poller.py --daemon` — HH/Habr/Getmatch + HTTP API :5680 |
| `jobhunter_telethon` | `jobhunter-app` | `telethon_monitor.py` — каналы TG |

Host cron (вне Docker): `scripts/jh_monitor.py`, `jh_backup.sh`, `jh_digest.py`.

Ключевые URL внутри сети:
- Score webhook: `http://score:5679/webhook/vacancy` (env `N8N_WEBHOOK_URL` — имя историческое)
- Poller: `POLLER_URL=http://poller:5680` — `/health`, `/searchnow`, `/progress`, `/cancel`

---

## 4. Multi-user модель (критично понять)

**Одна БД, не отдельные БД на пользователя.** Изоляция логическая:

| Общее | Персональное |
|-------|----------------|
| `vacancies_pool` (карточка вакансии один раз) | `user_vacancies` (status/score/analysis на chat_id) |
| Poller / Telethon / Score процессы | `user_settings` (keywords, threshold, notify, monitoring…) |
| Очередь поиска (один Playwright за раз) | `user_channels`, `user_resumes` |

Флаги/код:
- `MULTI_USER` / `user_data.multi_user_on()`
- Контекст запроса: `db.set_request_chat_id(chat_id)` / `reset_request_chat_id`
- Fan-out: `score_fanout_opt.py` — только search-ready юзеры; TG → кто подписан на канал; HH/Habr/Getmatch → match по keywords
- Регистрация: invite codes (`multiuser.py`), `init_user_defaults(chat_id)`

**Ожидаемое поведение «как у владельца»:** свои keywords/каналы/резюме/порог, своя лента и пуши. Чужие настройки не применяются. Пересечение только на уровне общего pool (уже спарсенная вакансия).

Owner chat_id historically: `242107017`. Другие prod-пользователи (пример): `456399761` (Ilsiyar82), `49741770` (Ganick).

---

## 5. Карта важных файлов

| Файл | Зачем |
|------|--------|
| `bot.py` | UI, меню, «Искать сейчас», drain/retry scoring, команды |
| `watchdog.py` | Рестарт bot.py по heartbeat |
| `score_server.py` | Webhook скоринга / filter / health; `only_chat_id` для точечного backfill |
| `score_fanout_opt.py` | Кому слать score; ready = resume + (keywords\|channels) |
| `hh_poller.py` | HH scrape + daemon HTTP queue `/searchnow` |
| `habr_poller.py` / `getmatch_poller.py` / `light_hh.py` | Другие источники |
| `telethon_monitor.py` | TG ingest |
| `user_data.py` | Schema MU: pool, user_vacancies, settings, channels, resumes |
| `db_postgres.py` | DB API + MU overrides поверх legacy |
| `multiuser.py` | users, invites, `init_user_defaults` |
| `user_feed_backfill.py` | После поиска: привязать подходящие pool-карточки к ленте юзера |
| `title_filter.py` | Junk/PM title filters, `vacancy_matches_user` |
| `agents.py` / `agents.yaml` / `ai.py` | Провайдеры LLM (DeepSeek/Ollama) |
| `scripts/jh_monitor.py` | Host health → Telegram alerts |
| `test_*.py` | Pytest (CI без integration) |

Dockerfiles: `Dockerfile` (bot/score/telethon), `Dockerfile.poller` (Playwright pin **1.64.1**).

---

## 6. Поиск «Искать сейчас»

1. Юзер жмёт поиск в боте → `_execute_search_now(chat_id)` → POST poller `/searchnow` с `chat_id`.
2. Poller ставит в очередь, `set_request_chat_id`, гоняет HH→Habr→(Getmatch если `GETMATCH_ENABLED=1`).
3. «Новые» = ещё нет в `vacancies_pool`. Если pool уже забит owner/cron — сырых new мало.
4. Поэтому после поиска вызывается **`backfill_user_feed`**: матчинг recent pool → score только для этого `chat_id` (`only_chat_id`).
5. Личный cooldown поиска ~1 час на chat_id. `/cancel` / `/cancelsearch` для отмены.

`/searchnow` command в боте часто `@owner_only`; обычные юзеры идут через кнопку меню.

---

## 7. Скоринг и статусы ленты

- Статусы UV: `pending` → `new` | `skipped` | `filtered` | …
- В UI «вакансии» в основном **`new`** (score ≥ personal `score_threshold`, default 7; у Ilsiyar было 6).
- `skipped` с низким score в ленте «как у админа много карточек» не видны — это нормально.
- `is_monitoring_for_chat`: **должен** default’ить в `true` при отсутствии setting (баг «у других 0 вакансий» был из default `false`).
- Retry/drain pending: обязан передавать `channel` и chat context; иначе channel затирался в `"telegram"` и fan-out никого не находил.

---

## 8. Известные баги / фиксы (проверять регрессии)

Уже чинили на prod (ноябрь 2026) — убедиться, что в коде/образе не откатилось:

1. **Retry без `channel`** → score ставил `channel=source` (`telegram`) и портил pool → все skip «channel not theirs». Фикс: не fallback на source-имя; доставать `@channel` из t.me URL; upsert не затирать хороший channel мусором.
2. **`is_monitoring_for_chat` default false** → другие юзеры не скорились. Default `true`; `init_user_defaults` пишет monitoring/notify/threshold.
3. **Пустая лента при «0 новых»** → `user_feed_backfill` после search + `only_chat_id` в webhook.
4. **Stuck pending + silent_drain loop** → poison-pill skip; clear orphan pending при 0 eligible chats.
5. **Getmatch**: часто `no __NEXT_DATA__` → 0 new (сайт/парсер). Не считать это поломкой всего бота.
6. **Monitor false alarm**: `docker exec` health poller падает под нагрузкой Playwright — в `jh_monitor` нужен soft-fail (несколько подряд), не один flake.
7. Playwright версия: image и package **1.64.x**, не смешивать с 1.49.

Открытые задачи продукта: `docs/TASKS.md` (персональные пороги/опыт hh — только по запросу юзера; не ломать чужие роли marketing/security/non-PM).

---

## 9. Данные и безопасность

- Не коммитить и не печатать: bot token, DeepSeek key, TG api_id/hash, Postgres password, session files, SSH keys.
- Postgres на prod слушает только localhost.
- Stage и prod — **разные** bot token’ы.
- Чеклист: `docs/SECURITY.md`. Deploy notes: `docs/DEPLOY.md`.

---

## 10. Как проверять (чеклист для GLM)

### Код / архитектура
- [ ] MU-пути: keywords/settings/UV всегда scoped по `chat_id`, нет утечки ленты owner↔user.
- [ ] Fan-out TG vs boards корректен; junk/PM filters не режут легитимных non-PM юзеров без нужды.
- [ ] Search + backfill + cancel + cooldown безопасны при параллельных юзерах.
- [ ] Retry scoring не затирает channel; pending не крутится вечно.
- [ ] `init_user_defaults` / monitoring defaults согласованы (`is_monitoring_active` vs `is_monitoring_for_chat`).
- [ ] Dockerfile(s) копируют все нужные модули (`user_feed_backfill.py`, `score_fanout_opt.py` в poller image).

### Тесты
- [ ] `pytest` (unit, без integration) зелёный.
- [ ] Есть/нужны тесты на: channel overwrite, monitoring default, backfill only_chat_id, vacancy_matches_user.

### Prod-санитария (если доступ есть; иначе описать процедуру)
- [ ] `docker compose -f docker-compose.prod.yml ps` — все Up; score/poller health ok.
- [ ] У каждого активного юзера: keywords или channels, resume/profile, `monitoring_active`, ненулевой UV после search/backfill.
- [ ] Getmatch: зафиксировать статус парсера отдельно от HH/TG.
- [ ] Логи: нет бесконечных `retry_scoring timeout` на одном id.

### Не делать без явного запроса владельца
- Force-push, wipe DB, ротация чужих секретов в git, отключение Getmatch/мониторинга «на всякий», коммит `.env`/`config.yaml`.
- Менять глобальный опыт hh / порог score для всех вместо per-user setting.

---

## 11. Критерии «всё ок для других пользователей»

1. После «Искать сейчас» ответ включает fresh new **и/или** «из базы к вашей ленте».
2. В `user_vacancies` появляются строки **только** с их `chat_id`.
3. Пуши/карточки соответствуют их threshold и `notify_new`.
4. Owner лента не меняется от их поиска (кроме общего роста `vacancies_pool`).
5. Повторный поиск упирается в личный rate limit, не в чужой busy без очереди.

---

## 12. Краткий glossary

- **Pool** — `vacancies_pool`, общая карточка.
- **UV** — `user_vacancies`, персональный статус/score.
- **Fan-out** — рассылка скоринга по подходящим chat_id.
- **Backfill** — догон ленты из уже существующего pool.
- **n8n** — в README/старом коде; на prod заменён `score_server` (URL env всё ещё может называться N8N_*).

---

*Бриф составлен для поручения проверки GLM. При расхождении с кодом приоритет у актуальных файлов в корне репо (не у `_stage_app/` как источника правды, если не указано иначе).*
