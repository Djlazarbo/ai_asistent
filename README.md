# Job Hunter Bot

Автоматизированная система поиска работы: мониторит Telegram-каналы и hh.ru,
фильтрует вакансии через AI и присылает релевантные в личный Telegram-бот.

Репозиторий: [DjLazarbo/ai_asistent](https://github.com/DjLazarbo/ai_asistent) (private).

---

## Окружения (stage / prod)

| | Stage (ПК) | Prod (сервер) |
|--|------------|---------------|
| Ветка | `develop` | `main` |
| `APP_ENV` | `stage` | `prod` |
| Секреты | локальные `.env` + `config.yaml` | отдельные на сервере |
| Telegram-бот | отдельный тестовый | боевой |
| Postgres | Docker `:5433` | свой инстанс на сервере |

В Git **не** коммитятся: `config.yaml`, `.env`, Telethon-сессии, `*.db`, логи.
В репо лежат шаблоны: `config.example.yaml`, `.env.example`.

```bash
cp config.example.yaml config.yaml   # Windows: copy
cp .env.example .env
# заполни реальные значения только локально
```

### CI/CD (GitHub Actions)

- **CI** (`.github/workflows/ci.yml`): на push/PR в `develop` и `main` — unit-тесты (`pytest`, без integration).
- **CD** (`.github/workflows/deploy.yml`): на push в `main` — SSH на prod VM → `git pull` + `scripts/deploy.sh`.

Секреты репозитория (Settings → Secrets → Actions):

| Secret | Пример |
|--------|--------|
| `SSH_HOST` | `51.250.104.136` (лучше статичный IP) |
| `SSH_USER` | `djlazarboy` |
| `SSH_KEY` | весь private key (файл `djlazarboy`) |
| `DEPLOY_PATH` | `/opt/job-hunter` (опционально) |

Первый раз на сервере: `bash scripts/server_bootstrap.sh`, заполнить `.env`/`config.yaml`, поставить systemd из `deploy/systemd/`.

На prod фильтр PM лучше через DeepSeek (`agents.yaml` → `is_pm_filter.provider: deepseek`), Ollama на 2 GB VM не ставить.

---

## Быстрый старт

### 1. Установка зависимостей

```bash
cd D:\Job
pip install -r requirements.txt
```

### 2. Заполни config.yaml

Скопируй `config.example.yaml` → `config.yaml` и замени все `YOUR_*` значения:

| Поле | Где взять |
|------|-----------|
| `telegram.api_id` / `api_hash` | https://my.telegram.org → Apps |
| `deepseek.api_key` | https://platform.deepseek.com |
| `telegram_bot.chat_id` | Напиши @userinfobot в Telegram |
| `google_sheets.spreadsheet_id` | Из URL таблицы Google Sheets |
| `google_sheets.credentials_file` | Google Cloud → Service Account JSON |

### 3. Google Sheets

1. Открой [Google Cloud Console](https://console.cloud.google.com)
2. Создай проект → включи Google Sheets API и Google Drive API
3. Создай Service Account → скачай JSON как `google_credentials.json` → положи рядом с проектом
4. Открой Google Sheets таблицу → поделись с email из `client_email` в JSON (роль Editor)

### 4. Ollama (локальный AI фильтр)

```bash
# Установка (Windows)
# Скачай установщик с https://ollama.com/download
# После установки:
ollama pull llama3
# Проверь:
ollama run llama3 "hello"
# Сервер стартует автоматически на http://localhost:11434
```

### 5. n8n (оркестратор)

```bash
# Установка через npm
npm install -g n8n

# Запуск
n8n start
# Откроется на http://localhost:5678
```

**Импорт workflow:**
1. Открой n8n → Settings → Import from File
2. Выбери `n8n_workflow.json`
3. В узле "Deepseek Analysis" замени `YOUR_DEEPSEEK_KEY` на реальный ключ
4. В узлах Google Sheets замени `YOUR_SPREADSHEET_ID`
5. В узле "Send Telegram Card" замени `YOUR_CHAT_ID`
6. Добавь Telegram credentials: Settings → Credentials → Add → Telegram → вставь токен бота
7. Активируй workflow (переключатель вверху)

### 6. Telethon сессия (авторизация)

```bash
cd D:\Job
python telethon_monitor.py
# Введёт телефон и код подтверждения — создаст файл telethon_session.session
# После авторизации запусти снова для мониторинга
```

### 7. Запуск всех компонентов

Открой 3 отдельных терминала:

**Терминал 1 — Telegram bot:**
```bash
cd D:\Job
python bot.py
```

**Терминал 2 — Telethon monitor:**
```bash
cd D:\Job
python telethon_monitor.py
```

**Терминал 3 — n8n (если не запущен):**
```bash
n8n start
```

hh.ru поллер запускается автоматически через n8n в 09:00 или по команде `/searchnow` в боте.

---

## Первые шаги в боте

1. Напиши `/start` — проверь что бот отвечает
2. Отправь `/resume` → прикрепи PDF резюме → Deepseek извлечёт профиль
3. Проверь профиль командой `/myprofile`
4. Каналы уже настроены из config.yaml — можно добавить через `/addchannel @название`

---

## Структура файлов

```
D:\Job\
├── config.yaml              # все настройки и секреты
├── google_credentials.json  # ключ Google Service Account (создай сам)
├── bot.py                   # Telegram бот
├── telethon_monitor.py      # мониторинг TG каналов
├── hh_poller.py             # парсер hh.ru API
├── ai.py                    # вызовы Ollama и Deepseek
├── db.py                    # SQLite: вакансии, каналы, настройки
├── sheets.py                # Google Sheets интеграция
├── prompts.py               # все промпты
├── n8n_workflow.json        # импортируй в n8n
├── requirements.txt
├── vacancies.db             # создаётся автоматически
└── job_hunter.log           # создаётся автоматически
```

---

## Как это работает

```
Telegram каналы → telethon_monitor.py → POST → n8n webhook
hh.ru API       → hh_poller.py        → POST → n8n webhook
                                                    ↓
                                          Ollama: "это TPM/PM?"
                                                    ↓ YES
                                          Deepseek: оценка 1–10
                                                    ↓ score ≥ 7
                                          Google Sheets + Telegram карточка
```

---

## Команды бота

| Команда | Описание |
|---------|----------|
| `/resume` | Загрузить PDF или текст резюме |
| `/myprofile` | Просмотреть текущий профиль |
| `/editprofile` | Отредактировать профиль вручную |
| `/channels` | Список отслеживаемых каналов |
| `/addchannel @name` | Добавить канал |
| `/removechannel @name` | Удалить канал |
| `/pausechannel @name` | Поставить канал на паузу |
| `/keywords` | Ключевые слова для hh.ru |
| `/addkeyword слово` | Добавить ключевое слово |
| `/removekeyword слово` | Удалить ключевое слово |
| `/searchnow` | Запустить поиск hh.ru немедленно |
| `/list` | Вакансии за последние 7 дней |
| `/stats` | Воронка: new/applied/interview/offer |
| `/status id статус` | Обновить статус вакансии |
| `/threshold N` | Изменить порог оценки (по умолчанию 7) |
| `/pause` | Приостановить мониторинг |
| `/unpause` | Возобновить мониторинг |
