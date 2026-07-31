# App image: bot, score_server, telethon (no browser).
FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agents.py agents.yaml agents.prod.yaml ai.py bot.py \
     db_postgres.py db.py prompts.py score_server.py sheets.py \
     telethon_monitor.py watchdog.py hh_poller.py habr_poller.py \
     getmatch_poller.py retry_pending.py run_bot.py ./

ENV APP_ENV=prod \
    PYTHONUNBUFFERED=1 \
    POSTGRES_HOST=postgres \
    POSTGRES_PORT=5432

CMD ["python", "score_server.py"]
