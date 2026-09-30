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
    ("notify_new", "true"),
    ("ears_freq", "hourly"),
    ("quiet_hours", "on"),
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
    
    try:
        import multiuser
        import user_data
        async with pool.acquire() as conn:
            await multiuser.ensure_multiuser_schema(conn)
            await user_data.ensure_user_data_schema(conn)
            await multiuser.seed_invite_codes(conn)
            admin_id = int(_config.get("telegram_bot", {}).get("chat_id") or 0)
            if admin_id:
                async def _gset(key):
                    row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
                    return row["value"] if row else None
                await user_data.migrate_admin_from_global(conn, admin_id, _gset)
    except Exception as e:
        logger.warning(f"multiuser/user_data init skipped: {e}")

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
    """Best-effort mirror into config.yaml. On prod the file is often read-only."""
    try:
        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["hh"]["keywords"] = keywords
        with open("config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    except OSError as e:
        # soft-fail config persist (Docker RO bind-mount)
        logger.warning(f"skip config.yaml keywords persist: {e}")


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


async def is_notify_new_enabled() -> bool:
    val = await get_setting("notify_new")
    return (val or "false") == "true"


async def set_notify_new_enabled(active: bool):
    await set_setting("notify_new", "true" if active else "false")


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
    """Salary stats from vacancies_pool (live multiuser store), fallback to legacy vacancies."""
    pool = await _get_pool()
    since = datetime.utcnow() - timedelta(days=days)
    async with pool.acquire() as conn:
        rows = []
        try:
            rows = await conn.fetch(
                "SELECT salary FROM vacancies_pool WHERE salary IS NOT NULL AND salary != '' "
                "AND created_at >= $1",
                since,
            )
        except Exception:
            rows = []
        if not rows:
            rows = await conn.fetch(
                "SELECT salary FROM vacancies WHERE salary IS NOT NULL AND salary != '' "
                "AND created_at >= $1",
                since,
            )
    values = []
    for r in rows:
        raw = r["salary"]
        # skip explicit empty markers
        if isinstance(raw, str) and raw.strip().lower() in ("не указана", "не указано", "-", "n/a"):
            continue
        v = _parse_salary_value(raw if isinstance(raw, str) else str(raw or ""))
        if v is not None:
            values.append(v)
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
    """Top companies from vacancies_pool (live), fallback to legacy vacancies."""
    pool = await _get_pool()
    since = datetime.utcnow() - timedelta(days=days)
    async with pool.acquire() as conn:
        rows = []
        try:
            rows = await conn.fetch(
                "SELECT company, COUNT(*) AS cnt FROM vacancies_pool "
                "WHERE company IS NOT NULL AND company != '' "
                "AND lower(company) NOT IN ('не указана','не указано','n/a','unknown','-') "
                "AND created_at >= $1 "
                "GROUP BY company ORDER BY cnt DESC LIMIT $2",
                since, limit,
            )
        except Exception:
            rows = []
        if not rows:
            rows = await conn.fetch(
                "SELECT company, COUNT(*) AS cnt FROM vacancies "
                "WHERE company IS NOT NULL AND company != '' "
                "AND lower(company) NOT IN ('не указана','не указано','n/a','unknown','-') "
                "AND created_at >= $1 "
                "GROUP BY company ORDER BY cnt DESC LIMIT $2",
                since, limit,
            )
        return [{"company": r["company"], "count": r["cnt"]} for r in rows]


async def get_source_stats(days: int = 30) -> dict:
    """Source breakdown from vacancies_pool (live), fallback to legacy vacancies."""
    pool = await _get_pool()
    since = datetime.utcnow() - timedelta(days=days)
    async with pool.acquire() as conn:
        rows = []
        try:
            rows = await conn.fetch(
                "SELECT source, COUNT(*) AS cnt FROM vacancies_pool "
                "WHERE created_at >= $1 GROUP BY source",
                since,
            )
        except Exception:
            rows = []
        if not rows:
            rows = await conn.fetch(
                "SELECT source, COUNT(*) AS cnt FROM vacancies "
                "WHERE created_at >= $1 GROUP BY source",
                since,
            )
        out = {}
        for r in rows:
            src = (r["source"] or "other").lower()
            if src in ("tg", "telegram"):
                src = "telegram"
            out[src] = out.get(src, 0) + int(r["cnt"])
        return out


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
    """Удаляет старые вакансии: user_vacancies (multi-user) + legacy vacancies + orphan pool."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    _keep = ("applied", "interview", "offer")
    _purge = (
        "pending", "skipped", "filtered", "rejected", "new",
        "skipped_archived", "archived_legacy",
    )
    pool = await _get_pool()
    deleted_uv = deleted_legacy = deleted_pool = 0
    async with pool.acquire() as conn:
        uv_res = await conn.execute(
            """
            DELETE FROM user_vacancies
            WHERE created_at < $1
              AND COALESCE(is_favorite, FALSE) = FALSE
              AND status = ANY($2::text[])
              AND status <> ALL($3::text[])
            """,
            cutoff,
            list(_purge),
            list(_keep),
        )
        deleted_uv = int(uv_res.split()[-1])

        leg_res = await conn.execute(
            "DELETE FROM vacancies WHERE status IN ('pending', 'skipped', 'filtered') "
            "AND created_at < $1 AND COALESCE(is_favorite, FALSE) = FALSE",
            cutoff,
        )
        deleted_legacy = int(leg_res.split()[-1])

        pool_res = await conn.execute(
            """
            DELETE FROM vacancies_pool vp
            WHERE vp.created_at < $1
              AND NOT EXISTS (
                SELECT 1 FROM user_vacancies uv WHERE uv.vacancy_id = vp.id
              )
            """,
            cutoff,
        )
        deleted_pool = int(pool_res.split()[-1])

    deleted = deleted_uv + deleted_legacy + deleted_pool
    logger.info(
        f"Cleanup: удалено {deleted} (user_vacancies={deleted_uv}, "
        f"legacy={deleted_legacy}, pool_orphans={deleted_pool}) старше {days} дн."
    )
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


# ── Multi-user helpers (prod) ──────────────────────────────────────────────────

async def _mu_conn_call(coro_factory):
    import multiuser
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await coro_factory(conn)


async def is_registered_user(chat_id: int) -> bool:
    import multiuser
    return await _mu_conn_call(lambda c: multiuser.is_registered(c, chat_id))


async def list_active_user_chat_ids() -> list[int]:
    import multiuser
    return await _mu_conn_call(multiuser.list_active_chat_ids)


async def register_user_with_invite(chat_id: int, code: str, username=None, first_name=None):
    import multiuser
    return await _mu_conn_call(
        lambda c: multiuser.register_with_invite(c, chat_id, code, username, first_name)
    )


async def unused_invites() -> list[str]:
    import multiuser
    return await _mu_conn_call(multiuser.unused_invite_codes)


async def touch_user(chat_id: int) -> None:
    import multiuser
    await _mu_conn_call(lambda c: multiuser.touch_last_seen(c, chat_id))


# ── Per-user routing (prod) ────────────────────────────────────────────────────

def set_request_chat_id(chat_id):
    import user_data
    return user_data.set_request_chat_id(chat_id)


def reset_request_chat_id(token):
    import user_data
    user_data.reset_request_chat_id(token)


def _mu():
    import user_data
    return user_data.multi_user_on()


def _cid(explicit=None):
    import user_data
    return user_data.current_chat_id(explicit)


async def _conn_call(fn, *args, **kwargs):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await fn(conn, *args, **kwargs)


_orig_get_setting = get_setting
_orig_set_setting = set_setting
_orig_get_keywords = get_keywords
_orig_add_keyword = add_keyword
_orig_remove_keyword = remove_keyword
_orig_get_threshold = get_threshold
_orig_set_threshold = set_threshold
_orig_is_monitoring_active = is_monitoring_active
_orig_set_monitoring_active = set_monitoring_active
_orig_get_active_profile = get_active_profile
_orig_save_profile = save_profile
_orig_save_resume_text = save_resume_text
_orig_get_pending = get_pending_vacancies
_orig_vacancy_exists = vacancy_exists
_orig_insert_vacancy = insert_vacancy
_orig_update_status = update_vacancy_status
_orig_get_by_ext = get_vacancy_by_external
_orig_get_recent = get_recent_vacancies
_orig_get_stats = get_stats
_orig_get_stats_30days = get_stats_30days
_orig_get_active_channels = get_active_channels
_orig_get_all_channels = get_all_channels
_orig_channel_exists = channel_exists
_orig_add_channel = add_channel
_orig_set_channel_active = set_channel_active
_orig_get_yesterday = get_yesterday_stats
_orig_get_top = get_top_vacancies_by_score
_orig_get_stale = get_stale_applied_vacancies
_orig_get_favorites = get_favorites
_orig_is_favorite = is_favorite
_orig_toggle_favorite = toggle_favorite
_orig_count_favorites = count_favorites
_orig_save_cover_example = save_cover_example
_orig_get_cover_examples = get_cover_examples
_orig_save_interview = save_interview
_orig_get_interview_history = get_interview_history
_orig_mark_viewed = mark_viewed
_orig_update_vacancy_analysis = update_vacancy_analysis


async def get_setting(key: str):
    cid = _cid()
    if _mu() and cid is not None and key in (
        "hh_keywords", "score_threshold", "monitoring_active", "notify_new", "active_profile", "resume_text", "companies_block", "companies_want", "ears_freq", "feed_ttl_days", "quiet_hours", "salary_from", "follow_up_days", "followup_meta"
    ):
        import user_data
        if key == "active_profile":
            profile = await _conn_call(user_data.get_user_profile, cid)
            return json.dumps(profile, ensure_ascii=False) if profile else None
        if key == "resume_text":
            return await _conn_call(user_data.get_user_resume_text, cid)
        val = await _conn_call(user_data.get_user_setting, cid, key)
        if val is not None:
            return val
    return await _orig_get_setting(key)


async def set_setting(key: str, value: str):
    cid = _cid()
    if _mu() and cid is not None and key in (
        "hh_keywords", "score_threshold", "monitoring_active", "notify_new", "active_profile", "resume_text", "companies_block", "companies_want", "ears_freq", "feed_ttl_days", "quiet_hours", "salary_from", "follow_up_days", "followup_meta"
    ):
        import user_data
        if key == "active_profile":
            await _conn_call(user_data.save_user_profile, cid, json.loads(value))
            return
        if key == "resume_text":
            await _conn_call(user_data.save_user_resume_text, cid, value)
            return
        await _conn_call(user_data.set_user_setting, cid, key, value)
        return
    await _orig_set_setting(key, value)


async def get_keywords():
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        raw = await _conn_call(user_data.get_user_setting, cid, "hh_keywords")
        return json.loads(raw) if raw else []
    if _mu() and cid is None:
        import user_data
        kws = await _conn_call(user_data.get_keywords_union)
        if kws:
            return kws
    return await _orig_get_keywords()


async def add_keyword(word: str):
    cid = _cid()
    if _mu() and cid is not None:
        kws = await get_keywords()
        if word not in kws:
            kws.append(word)
            await set_setting("hh_keywords", json.dumps(kws, ensure_ascii=False))
        return
    await _orig_add_keyword(word)


async def remove_keyword(word: str):
    cid = _cid()
    if _mu() and cid is not None:
        kws = [k for k in await get_keywords() if k.lower() != word.lower()]
        await set_setting("hh_keywords", json.dumps(kws, ensure_ascii=False))
        return
    await _orig_remove_keyword(word)


async def get_threshold() -> int:
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        val = await _conn_call(user_data.get_user_setting, cid, "score_threshold")
        return int(val) if val else 7
    return await _orig_get_threshold()


async def set_threshold(value: int):
    if _mu() and _cid() is not None:
        await set_setting("score_threshold", str(value))
        return
    await _orig_set_threshold(value)


async def is_monitoring_active() -> bool:
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        val = await _conn_call(user_data.get_user_setting, cid, "monitoring_active")
        return (val or "true") == "true"
    return await _orig_is_monitoring_active()


async def set_monitoring_active(active: bool):
    if _mu() and _cid() is not None:
        await set_setting("monitoring_active", "true" if active else "false")
        return
    await _orig_set_monitoring_active(active)


_orig_is_notify_new_enabled = is_notify_new_enabled
_orig_set_notify_new_enabled = set_notify_new_enabled


async def is_notify_new_enabled() -> bool:
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        val = await _conn_call(user_data.get_user_setting, cid, "notify_new")
        return (val or "false") == "true"
    return await _orig_is_notify_new_enabled()


async def set_notify_new_enabled(active: bool):
    if _mu() and _cid() is not None:
        await set_setting("notify_new", "true" if active else "false")
        return
    await _orig_set_notify_new_enabled(active)


async def get_active_profile():
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        return await _conn_call(user_data.get_user_profile, cid)
    return await _orig_get_active_profile()


async def save_profile(profile: dict):
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        await _conn_call(user_data.save_user_profile, cid, profile)
        return
    await _orig_save_profile(profile)


async def save_resume_text(text: str):
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        await _conn_call(user_data.save_user_resume_text, cid, text)
        return
    await _orig_save_resume_text(text)


async def vacancy_exists(external_id: str) -> bool:
    if _mu():
        import user_data
        return await _conn_call(user_data.pool_exists, external_id)
    return await _orig_vacancy_exists(external_id)


async def insert_vacancy(data: dict):
    """MU: returns canonical vacancies_pool id (may differ from payload external_id after dedup)."""
    if _mu():
        import user_data
        return await _conn_call(user_data.upsert_pool_vacancy, data)
    return await _orig_insert_vacancy(data)


async def get_pending_vacancies():
    if _mu():
        import user_data
        return await _conn_call(user_data.get_pending_user_vacancies, _cid())
    return await _orig_get_pending()


async def update_vacancy_status(external_id: str, status: str):
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        await _conn_call(user_data.update_user_vacancy_status, cid, external_id, status)
        return
    await _orig_update_status(external_id, status)


async def get_vacancy_by_external(external_id: str):
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        v = await _conn_call(user_data.get_user_vacancy, cid, external_id)
        if v:
            return v
        v = await _conn_call(user_data.ensure_user_vacancy_from_pool, cid, external_id)
        if v:
            # Fresh personal row — no foreign analysis.
            return v
        # Last resort: pool card without another user's analysis text.
        pool = await _conn_call(user_data.get_pool_vacancy, external_id)
        if pool:
            pool = dict(pool)
            pool.pop("analysis", None)
            return pool
        return None
    return await _orig_get_by_ext(external_id)


async def get_recent_vacancies(days: int = 7, source: str | None = None):
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        return await _conn_call(user_data.list_user_vacancies, cid, days, source)
    return await _orig_get_recent(days, source)


async def get_stats():
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        return await _conn_call(user_data.user_stats, cid)
    return await _orig_get_stats()


async def get_stats_30days():
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        return await _conn_call(user_data.user_stats_30days, cid)
    return await _orig_get_stats_30days()


async def get_active_channels():
    if _mu():
        import user_data
        cid = _cid()
        if cid is not None:
            ch = await _conn_call(user_data.list_user_channels, cid)
            return [c["username"] for c in ch if c.get("active")]
        return await _conn_call(user_data.get_active_channels_union)
    return await _orig_get_active_channels()


async def get_all_channels():
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        return await _conn_call(user_data.list_user_channels, cid)
    return await _orig_get_all_channels()


async def add_channel(username: str, title: str = ""):
    cid = _cid()
    u = (username or "").strip()
    if u and not u.startswith("@"):
        u = "@" + u
    if _mu() and cid is not None:
        import user_data
        await _conn_call(user_data.add_user_channel, cid, u, title)
        return
    await _orig_add_channel(u or username, title)


async def set_channel_active(username: str, active: int):
    cid = _cid()
    u = (username or "").strip()
    if u and not u.startswith("@"):
        u = "@" + u
    if _mu() and cid is not None:
        import user_data
        if bool(active):
            # activate must CREATE row if missing (legacy channel_exists lied before)
            await _conn_call(user_data.add_user_channel, cid, u, "")
        else:
            await _conn_call(user_data.set_user_channel_active, cid, u, False)
        return
    await _orig_set_channel_active(u or username, active)


async def channel_exists(username: str) -> bool:
    """MU: check current user's channels; legacy: global channels table."""
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        ch = await _conn_call(user_data.list_user_channels, cid)
        u = (username or "").strip()
        if not u.startswith("@"):
            u = "@" + u
        return any((c.get("username") or "").lower() == u.lower() for c in ch)
    return await _orig_channel_exists(username)


async def remove_channel(username: str):
    """Deactivate/remove channel for current user (MU) or legacy table."""
    u = (username or "").strip()
    if not u.startswith("@") and u:
        u = "@" + u
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        # hard-delete so it disappears from the list
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_channels WHERE chat_id=$1 AND username=$2",
                int(cid), u,
            )
        return
    await set_channel_active(u, 0)



async def get_yesterday_stats():
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        return await _conn_call(user_data.yesterday_stats, cid)
    return await _orig_get_yesterday()


async def get_top_vacancies_by_score(hours: int = 24, limit: int = 3):
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        return await _conn_call(user_data.top_by_score, cid, hours, limit)
    return await _orig_get_top(hours, limit)


async def get_stale_applied_vacancies(days: int = 3):
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        return await _conn_call(user_data.stale_applied, cid, days)
    return await _orig_get_stale(days)


async def get_follow_up_days_for_chat(chat_id: int | None = None) -> int:
    import user_data
    cid = int(chat_id) if chat_id is not None else _cid()
    if cid is None:
        return 3
    return await _conn_call(user_data.get_follow_up_days, int(cid))


async def followup_dismiss(chat_id: int, vacancy_id: str) -> None:
    import user_data
    from followup_meta import mark_dismissed

    def _mut(meta):
        return mark_dismissed(meta, vacancy_id)

    await _conn_call(user_data.patch_followup_meta, int(chat_id), _mut)


async def followup_snooze(chat_id: int, vacancy_id: str, days: int = 7) -> None:
    import user_data
    from followup_meta import mark_snoozed

    def _mut(meta):
        return mark_snoozed(meta, vacancy_id, days=days)

    await _conn_call(user_data.patch_followup_meta, int(chat_id), _mut)


async def followup_mark_reminded(chat_id: int, vacancy_id: str) -> None:
    import user_data
    from followup_meta import mark_reminded

    def _mut(meta):
        return mark_reminded(meta, vacancy_id)

    await _conn_call(user_data.patch_followup_meta, int(chat_id), _mut)


async def followup_clear(chat_id: int, vacancy_id: str) -> None:
    import user_data
    from followup_meta import clear_entry

    def _mut(meta):
        return clear_entry(meta, vacancy_id)

    await _conn_call(user_data.patch_followup_meta, int(chat_id), _mut)


async def get_favorites(limit: int = 5, offset: int = 0):
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        return await _conn_call(user_data.get_user_favorites, cid, limit, offset)
    return await _orig_get_favorites(limit, offset)


async def is_favorite(external_id: str) -> bool:
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        return await _conn_call(user_data.is_user_favorite, cid, external_id)
    return await _orig_is_favorite(external_id)


async def toggle_favorite(external_id: str) -> bool:
    """MULTI_USER requires request chat_id."""
    cid = _cid()
    if _mu():
        if cid is None:
            raise RuntimeError("toggle_favorite requires chat_id in multi-user mode")
        import user_data
        cur = await _conn_call(user_data.is_user_favorite, cid, external_id)
        await _conn_call(user_data.set_user_favorite, cid, external_id, not cur)
        return not cur
    return await _orig_toggle_favorite(external_id)


async def save_vacancy_score_for_user(chat_id: int, external_id: str, score, title, company, salary, analysis, status):
    import user_data
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await user_data.upsert_user_vacancy(
            conn, chat_id, external_id, score=score, status=status, analysis=analysis
        )
        await conn.execute(
            """
            UPDATE vacancies_pool SET
              title = COALESCE($2, title),
              company = COALESCE($3, company),
              salary = COALESCE($4, salary)
            WHERE id=$1
            """,
            external_id, title or None, company or None, salary or None,
        )


async def list_active_users_for_scoring():
    import multiuser
    return await _conn_call(multiuser.list_active_chat_ids)


async def get_profile_for_chat(chat_id: int):
    import user_data
    return await _conn_call(user_data.get_user_profile, chat_id)


async def get_threshold_for_chat(chat_id: int) -> int:
    import user_data
    val = await _conn_call(user_data.get_user_setting, chat_id, "score_threshold")
    return int(val) if val else 7


async def is_monitoring_for_chat(chat_id: int) -> bool:
    import user_data
    val = await _conn_call(user_data.get_user_setting, chat_id, "monitoring_active")
    return (val or "false") == "true"


async def is_notify_new_for_chat(chat_id: int) -> bool:
    import user_data
    val = await _conn_call(user_data.get_user_setting, chat_id, "notify_new")
    return (val or "false") == "true"

# === OPENFIX_MU_OVERRIDES ===
async def mark_viewed(external_id: str):
    cid = _cid()
    if _mu() and cid is not None:
        import user_data
        await _conn_call(user_data.mark_user_viewed, cid, external_id)
        return
    await _orig_mark_viewed(external_id)


async def update_vacancy_analysis(external_id: str, analysis: dict):
    cid = _cid()
    payload = json.dumps(analysis, ensure_ascii=False)
    if _mu() and cid is not None:
        import user_data
        await _conn_call(user_data.update_user_vacancy_analysis, cid, external_id, payload)
        return
    await _orig_update_vacancy_analysis(external_id, analysis)


async def count_favorites() -> int:
    cid = _cid()
    if _mu():
        if cid is None:
            raise RuntimeError("count_favorites requires chat_id in multi-user mode")
        import user_data
        return await _conn_call(user_data.count_user_favorites, cid)
    return await _orig_count_favorites()


async def save_cover_example(external_id: str, text: str, status: str = "worked"):
    cid = _cid()
    if _mu():
        if cid is None:
            raise RuntimeError("save_cover_example requires chat_id in multi-user mode")
        import user_data
        await _conn_call(user_data.save_user_cover_example, cid, external_id, text, status)
        return
    await _orig_save_cover_example(external_id, text, status)


async def get_cover_examples(limit: int = 5) -> str:
    cid = _cid()
    if _mu():
        if cid is None:
            raise RuntimeError("get_cover_examples requires chat_id in multi-user mode")
        import user_data
        return await _conn_call(user_data.get_user_cover_examples, cid, limit)
    return await _orig_get_cover_examples(limit)


async def save_interview(company: str, vacancy_id: str, notes: str, questions_list: list = None):
    cid = _cid()
    if _mu():
        if cid is None:
            raise RuntimeError("save_interview requires chat_id in multi-user mode")
        import user_data
        await _conn_call(user_data.save_user_interview, cid, company, vacancy_id, notes, questions_list)
        return
    now = datetime.utcnow()
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO interview_history (company, vacancy_id, notes, questions, created_at) VALUES ("
            + D + "1, " + D + "2, " + D + "3, " + D + "4::jsonb, " + D + "5)",
            company, vacancy_id, notes, json.dumps(questions_list or [], ensure_ascii=False), now,
        )


async def get_interview_history(limit: int = 10) -> list[dict]:
    cid = _cid()
    if _mu():
        if cid is None:
            raise RuntimeError("get_interview_history requires chat_id in multi-user mode")
        import user_data
        return await _conn_call(user_data.get_user_interview_history, cid, limit)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT * FROM interview_history ORDER BY created_at DESC LIMIT " + D + "1", limit
            )
            out = []
            for r in rows:
                d = _row_to_dict(r)
                if "questions_list" not in d and d.get("questions") is not None:
                    q = d["questions"]
                    d["questions_list"] = q if isinstance(q, str) else json.dumps(q, ensure_ascii=False)
                out.append(d)
            return out
        except Exception:
            return []

# ── chat_id scoped cover / interview (prod multi-user) ───────────────────────

async def _ensure_chat_scoped_columns(conn) -> None:
    try:
        await conn.execute(
            "ALTER TABLE cover_examples ADD COLUMN IF NOT EXISTS chat_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE interview_history ADD COLUMN IF NOT EXISTS chat_id BIGINT"
        )
    except Exception:
        pass


async def save_cover_example(text: str, chat_id: int | None = None, **kwargs) -> None:
    """Save cover example scoped to chat_id when multi-user."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _ensure_chat_scoped_columns(conn)
        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='cover_examples'"
        )
        names = {r["column_name"] for r in cols}
        if "chat_id" in names and chat_id is not None:
            # Prefer generic insert; fall back to (text, chat_id)
            try:
                await conn.execute(
                    "INSERT INTO cover_examples (text, chat_id) VALUES ($1, $2)",
                    text, int(chat_id),
                )
            except Exception:
                await conn.execute(
                    "INSERT INTO cover_examples (content, chat_id) VALUES ($1, $2)",
                    text, int(chat_id),
                )
        else:
            try:
                await conn.execute("INSERT INTO cover_examples (text) VALUES ($1)", text)
            except Exception:
                await conn.execute("INSERT INTO cover_examples (content) VALUES ($1)", text)


async def get_cover_examples(chat_id: int | None = None, limit: int = 20):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _ensure_chat_scoped_columns(conn)
        has = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='cover_examples' AND column_name='chat_id'"
        )
        if has and chat_id is not None:
            rows = await conn.fetch(
                "SELECT * FROM cover_examples WHERE chat_id=$1 "
                "ORDER BY id DESC NULLS LAST LIMIT $2",
                int(chat_id), limit,
            )
        elif has and chat_id is not None:
            rows = []
        else:
            rows = await conn.fetch(
                "SELECT * FROM cover_examples ORDER BY id DESC NULLS LAST LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]


async def save_interview(question: str, answer: str, chat_id: int | None = None, **kwargs) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _ensure_chat_scoped_columns(conn)
        has = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='interview_history' AND column_name='chat_id'"
        )
        if has and chat_id is not None:
            try:
                await conn.execute(
                    "INSERT INTO interview_history (question, answer, chat_id) "
                    "VALUES ($1, $2, $3)",
                    question, answer, int(chat_id),
                )
            except Exception:
                await conn.execute(
                    "INSERT INTO interview_history (q, a, chat_id) VALUES ($1, $2, $3)",
                    question, answer, int(chat_id),
                )
        else:
            try:
                await conn.execute(
                    "INSERT INTO interview_history (question, answer) VALUES ($1, $2)",
                    question, answer,
                )
            except Exception:
                pass


async def get_interview_history(chat_id: int | None = None, limit: int = 50):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await _ensure_chat_scoped_columns(conn)
        has = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='interview_history' AND column_name='chat_id'"
        )
        if has and chat_id is not None:
            rows = await conn.fetch(
                "SELECT * FROM interview_history WHERE chat_id=$1 "
                "ORDER BY id DESC NULLS LAST LIMIT $2",
                int(chat_id), limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM interview_history ORDER BY id DESC NULLS LAST LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]


# === EARS_FREQ ===
_EARS_ALLOWED = ("hourly", "daily", "twice")



async def get_quiet_hours() -> str:
    val = await get_setting("quiet_hours")
    v = (val or "on").strip().lower()
    return "off" if v in ("off", "0", "false", "no") else "on"


async def set_quiet_hours(enabled: bool | str) -> str:
    if isinstance(enabled, bool):
        v = "on" if enabled else "off"
    else:
        v = "off" if str(enabled).strip().lower() in ("off", "0", "false", "no") else "on"
    await set_setting("quiet_hours", v)
    return v


async def get_quiet_hours_for_chat(chat_id: int) -> str:
    import user_data
    val = await _conn_call(user_data.get_user_setting, int(chat_id), "quiet_hours")
    v = (val or "on").strip().lower()
    return "off" if v in ("off", "0", "false", "no") else "on"


async def set_quiet_hours_for_chat(chat_id: int, enabled: bool | str) -> str:
    import user_data
    if isinstance(enabled, bool):
        v = "on" if enabled else "off"
    else:
        v = "off" if str(enabled).strip().lower() in ("off", "0", "false", "no") else "on"
    await _conn_call(user_data.set_user_setting, int(chat_id), "quiet_hours", v)
    try:
        cid = _cid()
        if cid is not None and int(cid) == int(chat_id):
            await set_setting("quiet_hours", v)
    except Exception:
        pass
    return v

async def get_ears_freq() -> str:
    val = await get_setting("ears_freq")
    v = (val or "hourly").strip().lower()
    return v if v in _EARS_ALLOWED else "hourly"


async def set_ears_freq(freq: str) -> str:
    v = (freq or "hourly").strip().lower()
    if v not in _EARS_ALLOWED:
        v = "hourly"
    await set_setting("ears_freq", v)
    return v


async def get_ears_freq_for_chat(chat_id: int) -> str:
    import user_data
    val = await _conn_call(user_data.get_user_setting, int(chat_id), "ears_freq")
    v = (val or "hourly").strip().lower()
    return v if v in _EARS_ALLOWED else "hourly"


async def set_ears_freq_for_chat(chat_id: int, freq: str) -> str:
    v = (freq or "hourly").strip().lower()
    if v not in _EARS_ALLOWED:
        v = "hourly"
    # set_setting is chat-scoped under MULTI_USER when _cid is set; force via user_data
    import user_data
    await _conn_call(user_data.set_user_setting, int(chat_id), "ears_freq", v)
    try:
        tok = set_request_chat_id(int(chat_id))
        try:
            await set_setting("ears_freq", v)
        finally:
            reset_request_chat_id(tok)
    except Exception:
        pass
    return v


async def list_digest_candidates_for_chat(
    chat_id: int, *, since, threshold: int = 7, limit: int = 12
) -> list:
    import user_data
    return await _conn_call(
        user_data.list_digest_candidates,
        int(chat_id),
        since=since,
        threshold=threshold,
        limit=limit,
    )


async def get_digest_meta_for_chat(chat_id: int) -> dict:
    import user_data
    return await _conn_call(user_data.get_digest_meta, int(chat_id))


async def touch_digest_meta_for_chat(chat_id: int, vacancy_ids: list) -> None:
    import user_data
    await _conn_call(user_data.touch_digest_meta, int(chat_id), vacancy_ids)


# === COMPANIES_BLOCK_WANT ===
import re as _re_co


def _normalize_company_name(name: str) -> str:
    s = (name or "").lower().replace("ё", "е").strip()
    for junk in (
        "ооо", "ао", "пао", "зао", "оао", "llc", "ltd", "inc", "gmbh",
        "«", "»", '"', "'", "(", ")", ",", ".",
    ):
        s = s.replace(junk, " ")
    s = _re_co.sub(r"\s+", " ", s).strip()
    return s


def company_name_matches(vacancy_company: str, patterns: list) -> bool:
    vc = _normalize_company_name(vacancy_company)
    if not vc:
        return False
    for p in patterns or []:
        pn = _normalize_company_name(str(p))
        if not pn:
            continue
        if pn in vc or vc in pn:
            return True
    return False


async def _load_company_list(key: str) -> list:
    import json as _json
    raw = await get_setting(key)
    if not raw:
        return []
    try:
        data = _json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return []


async def _save_company_list(key: str, items: list) -> list:
    import json as _json
    cleaned = []
    seen = set()
    for x in items:
        name = str(x).strip()
        if not name:
            continue
        norm = _normalize_company_name(name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        cleaned.append(name)
    await set_setting(key, _json.dumps(cleaned, ensure_ascii=False))
    return cleaned


async def get_companies_block() -> list:
    return await _load_company_list("companies_block")


async def get_companies_want() -> list:
    return await _load_company_list("companies_want")


async def add_company_block(name: str) -> list:
    items = await get_companies_block()
    items.append(name)
    # remove from want if present
    want = await get_companies_want()
    want = [w for w in want if _normalize_company_name(w) != _normalize_company_name(name)]
    await _save_company_list("companies_want", want)
    return await _save_company_list("companies_block", items)


async def add_company_want(name: str) -> list:
    items = await get_companies_want()
    items.append(name)
    block = await get_companies_block()
    block = [b for b in block if _normalize_company_name(b) != _normalize_company_name(name)]
    await _save_company_list("companies_block", block)
    return await _save_company_list("companies_want", items)


async def remove_company_block(index: int) -> list:
    items = await get_companies_block()
    if 0 <= index < len(items):
        items.pop(index)
    return await _save_company_list("companies_block", items)


async def remove_company_want(index: int) -> list:
    items = await get_companies_want()
    if 0 <= index < len(items):
        items.pop(index)
    return await _save_company_list("companies_want", items)


async def get_companies_block_for_chat(chat_id: int) -> list:
    import json as _json
    import user_data
    raw = await _conn_call(user_data.get_user_setting, int(chat_id), "companies_block")
    if not raw:
        return []
    try:
        data = _json.loads(raw)
        return [str(x).strip() for x in data if str(x).strip()] if isinstance(data, list) else []
    except Exception:
        return []


async def get_companies_want_for_chat(chat_id: int) -> list:
    import json as _json
    import user_data
    raw = await _conn_call(user_data.get_user_setting, int(chat_id), "companies_want")
    if not raw:
        return []
    try:
        data = _json.loads(raw)
        return [str(x).strip() for x in data if str(x).strip()] if isinstance(data, list) else []
    except Exception:
        return []


# === FEED_TTL_DAYS ===
_FEED_TTL_ALLOWED = (7, 14, 30)


async def get_feed_ttl_days() -> int:
    val = await get_setting("feed_ttl_days")
    try:
        d = int(str(val or "7").strip())
    except Exception:
        d = 7
    return d if d in _FEED_TTL_ALLOWED else 7


async def set_feed_ttl_days(days: int) -> int:
    try:
        d = int(days)
    except Exception:
        d = 7
    if d not in _FEED_TTL_ALLOWED:
        d = 7
    await set_setting("feed_ttl_days", str(d))
    return d


async def get_feed_ttl_days_for_chat(chat_id: int) -> int:
    import user_data
    val = await _conn_call(user_data.get_user_setting, int(chat_id), "feed_ttl_days")
    try:
        d = int(str(val or "7").strip())
    except Exception:
        d = 7
    return d if d in _FEED_TTL_ALLOWED else 7

# === KEYWORDS FROM RESUME ===

def keywords_from_profile(profile: dict | None, limit: int = 12) -> list[str]:
    """Extract search keywords from saved resume profile (role / skills / domains)."""
    if not isinstance(profile, dict):
        return []
    out: list[str] = []

    def _add(v):
        if v is None:
            return
        if isinstance(v, (list, tuple)):
            for x in v:
                _add(x)
            return
        s = str(v).strip()
        if len(s) < 2 or len(s) > 80:
            return
        # skip noisy long sentences
        if s.count(" ") > 6:
            return
        low = s.lower()
        if any(x.lower() == low for x in out):
            return
        out.append(s)

    for key in ("role", "desired_role", "target_role", "position", "title"):
        _add(profile.get(key))
    for key in ("key_skills", "tech_stack", "domains", "methodologies"):
        _add(profile.get(key))
    return out[:limit]


async def merge_keywords_from_profile(profile: dict | None) -> list[str]:
    """Merge profile-derived keywords into user list; keep manually added ones."""
    added: list[str] = []
    before = await get_keywords()
    before_l = {str(b).lower() for b in before}
    for kw in keywords_from_profile(profile):
        if kw.lower() in before_l:
            continue
        await add_keyword(kw)
        before_l.add(kw.lower())
        added.append(kw)
    return added

