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
     getmatch_poller.py retry_pending.py run_bot.py \
     title_filter.py score_fanout_opt.py user_data.py \
     followup_meta.py tg_post_parse.py version.py VERSION CHANGELOG.md ./

ARG APP_VERSION=dev
ARG APP_GIT_SHA=
ENV APP_ENV=prod \
    PYTHONUNBUFFERED=1 \
    POSTGRES_HOST=postgres \
    POSTGRES_PORT=5432 \
    APP_VERSION=$APP_VERSION \
    APP_GIT_SHA=$APP_GIT_SHA
LABEL org.opencontainers.image.version=$APP_VERSION

CMD ["python", "score_server.py"]
