# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).
Версия лежит в файле `VERSION`. Сборка: `scripts/deploy.sh` пишет тег образа `jobhunter-app:X.Y.Z`.
В боте: `/version`. Health: `curl localhost:5679/health`.

## [Unreleased]

### Только по запросу конкретного человека
- Опыт hh: 3–6 + 6+ без 1–3 (не включать глобально)
- Порог 8 вместо 7 (не включать глобально)
- Пересчёт уже лежащих карточек в ленте под новый фильтр

## [1.2.1] — 2026-11-22
> Multi-user audit: изоляция лент, стабильность drain, tg_notify, host-cron digest

### Fixed
- **Утечка данных между юзерами**: дубликаты `save_cover_example`/`get_cover_examples`/`save_interview`/`get_interview_history` в `db_postgres.py` затирали MU-версии — заметки интервью и письма были видны всем, сохранение письма портило данные, сохранение интервью падало. Итог: единственные MU-обёртки с явным `chat_id=`, fail-closed в multi-user.
- **`/drainpending` мог крутиться вечно**: нет poison-pill на «вечной» pending-вакансии; один DB-шибка убивал drain-задачу молча; `sent_total` считал неудачи. Теперь: skip после 2 фейлов, жёсткий стоп после 3 залипаний, try/except на каждый шаг, честный счётчик; то же в `_silent_drain`.
- **`/feed` и `/list` показывали глобальную legacy-ленту** любому чату (без скоупинга и проверки). Теперь скоупинг по chat_id.
- **409 «поиск уже в очереди» запускал второй (лёгкий) поиск поверх** стоящего в очереди; лёгкий fallback не имел кулдауна при лежащем poller. Теперь: 409-тексты распознаются, локальный кулдаун `LIGHT_SEARCH_COOLDOWN_SEC` (1 ч).
- **`tg_notify.py` не существовал**, хотя `score_server` импортирует его с 1.2.0 → мгновенные пуши умирали на ImportError. Модуль добавлен (Bot API, HTML, fail-closed без токена) и включён в оба образа.
- **Telethon-гейт мониторинга** читал глобальный флаг владельца: один юзер с выключенным мониторингом останавливал TG-ингест для всех. Теперь гейт = «у любого активного юзера monitoring ON».
- **`scripts/jh_digest.py`**: SQL `GROUP BY 1` указывал на выражение с `count(*)` → host-cron digest падал каждый день с установки. Исправлено (`GROUP BY source/status`).
- Мелочи: `"unknown"` в blacklist каналов; `hasattr`-гард вместо вызова несуществующей `update_vacancy_status_for_chat`; `/retryscoring` теперь проверяет занятый drain (гонка двойного скоринга).

### Added
- `test_mu_regressions.py` — 16 unit-тестов: channel overwrite, monitoring/notify default true, backfill `only_chat_id`, fan-out изоляция, сигнатуры cover/interview, poison-skip, 409-fallthrough, формат tg_notify.
- Ручные drain/retry видят pending **всех** юзеров (`_pending_all_users`), не только вызвавшего.

### Ops
- Недостающие модули добавлены в git: `multiuser.py`, `light_hh.py`, `user_feed_backfill.py`, `tg_notify.py`, host-cron скрипты (`jh_monitor.py`, `jh_digest.py`, `jh_backup.sh`, `install_host_cron.sh`), бриф `docs/GLM_PROJECT_BRIEF.md`.

## [1.2.0] — 2026-09-30
> Follow-up UX, чище поиск hh/TG, компании want/block

### Added
- Follow-up: snooze / «ответили» / «не актуально» / закрыть напоминания (`followup_meta` в user_settings)
- HH: `moreThan6` + hybrid Moscow; boolean OR/NOT для PM-поиска; опциональный `salary_from`
- Telegram: заголовок из поста + digest pre-filter (`tg_post_parse`, `title_filter`)
- `companies_want` / `companies_block` в fanout и score (boost / отсев)
- Модули в Docker-образе: `followup_meta`, `tg_post_parse`, `title_filter`, `score_fanout_opt`, `version`
- `/version` и `health.version` из файла `VERSION`

### Fixed
- «Закрыть» у follow-up реально гасит повторные пуши (раньше только убирало кнопки)
- Меню «Требует внимания» учитывает персональный `follow_up_days`, не жёсткие 3 дня
- В меню у stale-вакансий: письмо / snooze / выкл. напоминаний
- «Искать сейчас»: понятный ответ при личном rate limit (1 раз/час) и очереди poller
- Мусорные admin-keywords убраны на prod без глобального сужения чужих ролей

### Changed
- Poller / bot / score / telethon синхронизированы под multi-user queue и search-quality
- Prod: Docker Compose (systemd unit-файлы в репо сняты — деплой через compose)

## [1.1.0] — 2026-09-14
> Поиск: своя лента, без чужих keywords

### Fixed
- В ленту пользователя больше не попадают вакансии по чужим ключевым словам
- hh отсекает клининг / стройку / школу / продажи / аккаунт до AI
- Для PM-поиска: IT PM / TPM / delivery; не Product Owner, Team Lead, маркетинг
- Убраны мусорные keywords (обязанности, «аккредитованная», опечатка «рукодитель»)
- Чужие целевые запросы (marketing, экономическая безопасность) сохранены их авторам

## [1.0.0] — 2026-07-31
> Prod baseline: multi-user, hh / Хабр / Getmatch / Telegram
