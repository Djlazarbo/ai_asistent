# Job Hunter — открытые задачи

Не путать с Atol PM-списком. Это только бот поиска работы.
Глобальные правки не должны ломать чужие роли (marketing, безопасность, не-PM).

## Сделано (1.2.0, 2026-09-30)

- [x] Follow-up: dismiss / snooze / interview / reject + `followup_meta`
- [x] HH: `moreThan6` + hybrid Moscow; boolean OR/NOT для PM
- [x] Заголовки Telegram-постов + digest pre-filter
- [x] Понятный ответ при rate limit «Искать сейчас»
- [x] `companies_want` / `companies_block` в score/fanout
- [x] Патчи search-quality в Docker-образе / git
- [x] Меню follow-up: персональный `follow_up_days` + snooze/закрыть
- [x] Версии сборок (`VERSION` + `CHANGELOG` + `/version`)

## Сделано ранее (1.1.0, 2026-09-14)

- [x] Персональная лента по своим keywords
- [x] Фильтр мусорных названий на hh
- [x] Жёстче YES/NO для IT PM (без ломки marketing/безопасности)
- [x] Чистка мусорных keywords у двух пользователей

## Надо доделать

### Только по запросу конкретного человека
- [ ] Опыт hh: 3–6 + 6+, без 1–3 (сейчас 1–3 оставлен для остальных)
- [ ] Порог 8 вместо 7
- [ ] Пересчитать/спрятать старый мусор в уже открытой ленте
