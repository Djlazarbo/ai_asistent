from dotenv import load_dotenv
load_dotenv()

import json
import os
import re
import asyncpg
import yaml
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger

with open("config.yaml", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

_pool = None
_BOARD_SOURCES = ("hh", "habr", "getmatch")


async def _get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", 5433)),
            database=os.getenv("POSTGRES_DB", "jobhunter"),
            user=os.getenv("POSTGRES_USER", "jobhunter"),
            password=os.getenv("POSTGRES_PASSWORD", "jobhunter_secret"),
            min_size=2,
            max_size=10,
        )
    return _pool


def _row_to_dict(row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


# ── Schema ─────────────────────────────────────────────────────────────────────

_CREATE_VACANCIES = """
CREATE TABLE IF NOT EXISTS vacancies (
    id BIGSERIAL PRIMARY KEY,
    source TEXT,
    external_id TEXT UNIQUE,
    channel TEXT,
    text TEXT,
    title TEXT,
    company TEXT,
    salary TEXT,
    url TEXT,
    score INTEGER,
    analysis TEXT,
    status TEXT DEFAULT 'new',
    viewed INTEGER DEFAULT 0,
    is_favorite BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)
"""

_CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

_CREATE_CHANNELS = """
CREATE TABLE IF NOT EXISTS channels (
    username TEXT PRIMARY KEY,
    title TEXT,
    active INTEGER DEFAULT 1,
    added_at TIMESTAMP
)
"""

_CREATE_INTERVIEW_HISTORY = """
CREATE TABLE IF NOT EXISTS interview_history (
    id BIGSERIAL PRIMARY KEY,
    company TEXT,
    vacancy_id TEXT,
    notes TEXT,
    questions_list TEXT DEFAULT '[]',
    created_at TIMESTAMP
)
"""

_CREATE_COVER_EXAMPLES = """
CREATE TABLE IF NOT EXISTS cover_examples (
    id BIGSERIAL PRIMARY KEY,
    external_id TEXT,
    text TEXT,
    status TEXT DEFAULT 'worked',
    created_at TIMESTAMP
)
"""

_INITIAL_SETTINGS = [
    ("hh_keywords", json.dumps(_config["hh"]["keywords"], ensure_ascii=False)),
    ("score_threshold", "7"),
    ("monitoring_active", "true"),
    ("analysis_model", _config["ollama"]["analysis_model"]),
]


async def init_db():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_CREATE_VACANCIES)
        await conn.execute(_CREATE_SETTINGS)
        await conn.execute(_CREATE_CHANNELS)
        await conn.execute(_CREATE_INTERVIEW_HISTORY)
        await conn.execute(_CREATE_COVER_EXAMPLES)

        # Migrations: add columns that may be missing in tables created with old schema
        for stmt in (
            "ALTER TABLE cover_examples ADD COLUMN IF NOT EXISTS external_id TEXT",
            "ALTER TABLE cover_examples ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'worked'",
            "ALTER TABLE cover_examples ADD COLUMN IF NOT EXISTS created_at TIMESTAMP",
            "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS viewed INTEGER DEFAULT 0",
            "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS analysis TEXT",
            "ALTER TABLE vacancies ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN DEFAULT FALSE",
        ):
            try:
                await conn.execute(stmt)
            except Exception:
                pass

        for key, value in _INITIAL_SETTINGS:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
                key, value,
            )
        # Only seed keywords if DB has none yet — never overwrite user-added keywords
        config_keywords = json.dumps(_config["hh"]["keywords"], ensure_ascii=False)
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('hh_keywords', $1) "
            "ON CONFLICT (key) DO NOTHING",
            config_keywords,
        )
        channels = _config["telegram"].get("channels", [])
        now = datetime.utcnow()
        for username in channels:
            await conn.execute(
                "INSERT INTO channels (username, active, added_at) VALUES ($1, 1, $2) "
                "ON CONFLICT (username) DO NOTHING",
                username, now,
            )
    logger.info("PostgreSQL initialized")


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_setting(key: str) -> Optional[str]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return row["value"] if row else None


async def set_setting(key: str, value: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value=$2",
            key, value,
        )


async def get_keywords() -> list[str]:
    raw = await get_setting("hh_keywords")
    return json.loads(raw) if raw else []


def _persist_keywords_to_config(keywords: list[str]):
    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["hh"]["keywords"] = keywords
    with open("config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


async def add_keyword(word: str):
    kws = await get_keywords()
    if word not in kws:
        kws.append(word)
        await set_setting("hh_keywords", json.dumps(kws, ensure_ascii=False))
        _persist_keywords_to_config(kws)


async def remove_keyword(word: str):
    kws = await get_keywords()
    kws = [k for k in kws if k.lower() != word.lower()]
    await set_setting("hh_keywords", json.dumps(kws, ensure_ascii=False))
    _persist_keywords_to_config(kws)


async def get_threshold() -> int:
    val = await get_setting("score_threshold")
    return int(val) if val else 7


async def set_threshold(value: int):
    await set_setting("score_threshold", str(value))


async def is_monitoring_active() -> bool:
    val = await get_setting("monitoring_active")
    return val == "true"


async def set_monitoring_active(active: bool):
    await set_setting("monitoring_active", "true" if active else "false")


async def get_analysis_model() -> str:
    val = await get_setting("analysis_model")
    return val or _config["ollama"]["analysis_model"]


async def set_analysis_model(model: str):
    await set_setting("analysis_model", model)


async def get_agent_overrides() -> dict:
    """Per-agent overrides as JSON. Empty dict if none set."""
    raw = await get_setting("agent_overrides")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


async def set_agent_override(role: str, provider: str, model: str):
    """Override one agent's provider+model. Pass provider=None to clear."""
    overrides = await get_agent_overrides()
    if provider is None:
        overrides.pop(role, None)
    else:
        overrides[role] = {"provider": provider, "model": model}
    await set_setting("agent_overrides", json.dumps(overrides, ensure_ascii=False))


async def get_active_profile() -> Optional[dict]:
    raw = await get_setting("active_profile")
    return json.loads(raw) if raw else None


async def save_profile(profile: dict):
    await set_setting("active_profile", json.dumps(profile, ensure_ascii=False))


async def save_resume_text(text: str):
    await set_setting("resume_text", text)


# ── Vacancies ──────────────────────────────────────────────────────────────────

async def get_pending_vacancies() -> list[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM vacancies WHERE status='pending' AND score IS NULL ORDER BY created_at DESC"
        )
        return [_row_to_dict(r) for r in rows]


async def vacancy_exists(external_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM vacancies WHERE external_id=$1", external_id
        )
        return row is not None


async def insert_vacancy(data: dict) -> int:
    now = datetime.utcnow()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO vacancies
               (source, external_id, channel, text, title, company, salary, url,
                score, analysis, status, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (external_id) DO NOTHING
               RETURNING id""",
            data.get("source"),
            data.get("external_id"),
            data.get("channel"),
            data.get("text"),
            data.get("title"),
            data.get("company"),
            data.get("salary"),
            data.get("url"),
            data.get("score"),
            data.get("analysis"),
            data.get("status", "new"),
            now,
            now,
        )
        return row["id"] if row else 0


async def update_vacancy_analysis(external_id: str, analysis: dict):
    now = datetime.utcnow()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vacancies SET analysis=$1, updated_at=$2 WHERE external_id=$3",
            json.dumps(analysis, ensure_ascii=False), now, external_id,
        )


async def update_vacancy_status(external_id: str, status: str):
    now = datetime.utcnow()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vacancies SET status=$1, updated_at=$2 WHERE external_id=$3",
            status, now, external_id,
        )


async def mark_viewed(external_id: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vacancies SET viewed=1 WHERE external_id=$1", external_id
        )


async def get_vacancy_by_external(external_id: str) -> Optional[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM vacancies WHERE external_id=$1", external_id
        )
        return _row_to_dict(row)


async def get_recent_vacancies(days: int = 7, source: str | None = None) -> list[dict]:
    pool = await _get_pool()
    since = datetime.utcnow() - timedelta(days=days)
    async with pool.acquire() as conn:
        if source in _BOARD_SOURCES:
            rows = await conn.fetch(
                "SELECT * FROM vacancies WHERE created_at >= $1 AND source=$2 ORDER BY created_at DESC",
                since, source,
            )
        elif source in ("tg", "channels"):
            rows = await conn.fetch(
                "SELECT * FROM vacancies WHERE created_at >= $1 AND NOT (source = ANY($2)) ORDER BY created_at DESC",
                since, list(_BOARD_SOURCES),
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM vacancies WHERE created_at >= $1 ORDER BY created_at DESC",
                since,
            )
        return [_row_to_dict(r) for r in rows]


async def get_stats() -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        stats = {}
        for status in ("new", "applied", "interview", "offer", "skipped", "rejected"):
            val = await conn.fetchval(
                "SELECT COUNT(*) FROM vacancies WHERE status=$1", status
            )
            stats[status] = val
        return stats


# ── Channels ──────────────────────────────────────────────────────────────────

async def get_active_channels() -> list[str]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT username FROM channels WHERE active=1")
        return [r["username"] for r in rows]


async def get_all_channels() -> list[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM channels ORDER BY added_at")
        return [_row_to_dict(r) for r in rows]


async def add_channel(username: str, title: str = ""):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO channels (username, title, active, added_at) VALUES ($1,$2,1,$3) "
            "ON CONFLICT (username) DO UPDATE SET title=$2, active=1, added_at=$3",
            username, title, datetime.utcnow(),
        )


async def set_channel_active(username: str, active: int):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE channels SET active=$1 WHERE username=$2", active, username
        )


async def channel_exists(username: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM channels WHERE username=$1", username
        )
        return row is not None


async def get_yesterday_stats() -> dict:
    pool = await _get_pool()
    since = datetime.utcnow() - timedelta(days=1)
    async with pool.acquire() as conn:
        new = await conn.fetchval(
            "SELECT COUNT(*) FROM vacancies WHERE created_at >= $1", since
        )
        viewed = await conn.fetchval(
            "SELECT COUNT(*) FROM vacancies WHERE viewed=1 AND updated_at >= $1", since
        )
        applied = await conn.fetchval(
            "SELECT COUNT(*) FROM vacancies WHERE status='applied' AND updated_at >= $1", since
        )
        return {"new": new, "viewed": viewed, "applied": applied}


async def get_top_vacancies_by_score(hours: int = 24, limit: int = 3) -> list[dict]:
    pool = await _get_pool()
    since = datetime.utcnow() - timedelta(hours=hours)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM vacancies WHERE created_at >= $1 AND score IS NOT NULL "
            "ORDER BY score DESC LIMIT $2",
            since, limit,
        )
        return [_row_to_dict(r) for r in rows]


async def get_stale_applied_vacancies(days: int = 3) -> list[dict]:
    pool = await _get_pool()
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM vacancies WHERE status='applied' AND updated_at < $1 "
            "ORDER BY updated_at ASC",
            cutoff,
        )
        return [_row_to_dict(r) for r in rows]


async def get_stats_30days() -> dict:
    pool = await _get_pool()
    since = datetime.utcnow() - timedelta(days=30)
    async with pool.acquire() as conn:
        result = {}
        for status in ("new", "skipped", "filtered", "pending", "applied", "interview", "offer", "rejected"):
            val = await conn.fetchval(
                "SELECT COUNT(*) FROM vacancies WHERE status=$1 AND created_at >= $2",
                status, since,
            )
            result[status] = val
        result["viewed"] = await conn.fetchval(
            "SELECT COUNT(*) FROM vacancies WHERE viewed=1 AND created_at >= $1", since
        )
        # "found" = evaluated by AI (new + skipped), excludes filtered/pending
        result["found"] = result["new"] + result["skipped"]
        result["total"] = sum(result[s] for s in ("new", "skipped", "filtered", "pending"))
        return result


async def save_interview(company: str, vacancy_id: str, notes: str, questions_list: list = None):
    now = datetime.utcnow()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO interview_history (company, vacancy_id, notes, questions_list, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            company, vacancy_id, notes,
            json.dumps(questions_list or [], ensure_ascii=False),
            now,
        )


async def get_interview_history(limit: int = 10) -> list[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT * FROM interview_history ORDER BY created_at DESC LIMIT $1", limit
            )
            return [_row_to_dict(r) for r in rows]
        except Exception:
            return []


async def get_frequent_questions(limit: int = 10) -> list[str]:
    history = await get_interview_history(limit=30)
    all_questions: list[str] = []
    for h in history:
        try:
            qs = json.loads(h.get("questions_list") or "[]")
            all_questions.extend(qs)
        except Exception:
            pass
    seen: set[str] = set()
    result: list[str] = []
    for q in all_questions:
        if q not in seen:
            seen.add(q)
            result.append(q)
        if len(result) >= limit:
            break
    return result


def _parse_salary_value(raw: str) -> Optional[int]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            frm = parsed.get("from")
            to = parsed.get("to")
            if frm and to:
                return (int(frm) + int(to)) // 2
            if frm:
                return int(frm)
            if to:
                return int(to)
            return None
    except Exception:
        pass
    nums = re.findall(r'\b\d[\d\s]{2,}\d\b', raw.replace('\xa0', ' ').replace(',', ''))
    values = []
    for n in nums:
        try:
            v = int(n.replace(' ', ''))
            if 10_000 <= v <= 50_000_000:
                values.append(v)
        except ValueError:
            pass
    return sum(values) // len(values) if values else None


async def get_salary_stats(days: int = 30) -> dict:
    pool = await _get_pool()
    since = datetime.utcnow() - timedelta(days=days)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT salary FROM vacancies WHERE salary IS NOT NULL AND salary != '' "
            "AND created_at >= $1",
            since,
        )
    values = [v for r in rows if (v := _parse_salary_value(r["salary"])) is not None]
    if not values:
        return {"count": 0, "total_with_salary": len(rows)}
    values.sort()
    n = len(values)
    median = (values[n // 2] + values[(n - 1) // 2]) // 2
    return {
        "count": n,
        "total_with_salary": len(rows),
        "median": median,
        "min": values[0],
        "max": values[-1],
        "range_from": values[n // 4],
        "range_to": values[3 * n // 4],
    }


async def get_top_companies(days: int = 30, limit: int = 5) -> list[dict]:
    pool = await _get_pool()
    since = datetime.utcnow() - timedelta(days=days)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT company, COUNT(*) AS cnt FROM vacancies "
            "WHERE company IS NOT NULL AND company != '' "
            "AND created_at >= $1 "
            "GROUP BY company ORDER BY cnt DESC LIMIT $2",
            since, limit,
        )
        return [{"company": r["company"], "count": r["cnt"]} for r in rows]


async def get_source_stats(days: int = 30) -> dict:
    pool = await _get_pool()
    since = datetime.utcnow() - timedelta(days=days)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source, COUNT(*) AS cnt FROM vacancies "
            "WHERE created_at >= $1 GROUP BY source",
            since,
        )
        return {(r["source"] or "other"): r["cnt"] for r in rows}


async def save_cover_example(external_id: str, text: str, status: str = "worked"):
    now = datetime.utcnow()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO cover_examples (external_id, text, status, created_at) VALUES ($1,$2,$3,$4)",
            external_id, text, status, now,
        )


async def get_cover_examples(limit: int = 5) -> str:
    """Возвращает сохранённые успешные письма в виде текста для подстановки в промпт."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT text FROM cover_examples WHERE status='worked' ORDER BY created_at DESC LIMIT $1",
            limit,
        )
    if not rows:
        return ""
    parts = []
    for i, r in enumerate(rows, 1):
        parts.append(f"Пример {i}:\n{r['text']}")
    return "\n\n".join(parts)


async def cleanup_old_vacancies(days: int = 30) -> int:
    """Удаляет вакансии старше N дней со статусом pending/skipped/filtered. Избранные не трогает."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM vacancies WHERE status IN ('pending', 'skipped', 'filtered') "
            "AND created_at < $1 AND is_favorite = FALSE",
            cutoff,
        )
    deleted = int(result.split()[-1])
    logger.info(f"Cleanup: удалено {deleted} вакансий старше {days} дней")
    return deleted


async def toggle_favorite(external_id: str) -> bool:
    """Переключает избранное. Возвращает True если добавлено, False если убрано."""
    now = datetime.utcnow()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        current = await conn.fetchval(
            "SELECT is_favorite FROM vacancies WHERE external_id=$1", external_id
        )
        new_value = not bool(current)
        await conn.execute(
            "UPDATE vacancies SET is_favorite=$1, updated_at=$2 WHERE external_id=$3",
            new_value, now, external_id,
        )
        return new_value


async def is_favorite(external_id: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT is_favorite FROM vacancies WHERE external_id=$1", external_id
        )
        return bool(result)


async def get_favorites(limit: int = 5, offset: int = 0) -> list[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM vacancies WHERE is_favorite=TRUE "
            "ORDER BY updated_at DESC LIMIT $1 OFFSET $2",
            limit, offset,
        )
        return [_row_to_dict(r) for r in rows]


async def count_favorites() -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM vacancies WHERE is_favorite=TRUE"
        )


async def save_vacancy_score(
    external_id: str, score, title: str, company: str,
    salary: str, analysis: str, status: str,
):
    now = datetime.utcnow()
    score_int = int(score) if score is not None else None
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE vacancies SET score=$1, title=$2, company=$3, salary=$4, "
            "analysis=$5, status=$6, updated_at=$7 WHERE external_id=$8",
            score_int, title, company, salary, analysis, status, now, external_id,
        )
