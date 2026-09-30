# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).
Версия лежит в файле `VERSION`. Сборка: `scripts/deploy.sh` пишет тег образа `jobhunter-app:X.Y.Z`.
В боте: `/version`. Health: `curl localhost:5679/health`.

## [Unreleased]

### Только по запросу конкретного человека
- Опыт hh: 3–6 + 6+ без 1–3 (не включать глобально)
- Порог 8 вместо 7 (не включать глобально)
- Пересчёт уже лежащих карточек в ленте под новый фильтр

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
