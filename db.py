import json
import re
import aiosqlite
from datetime import datetime
from typing import Optional
import yaml


with open("config.yaml", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

DB_PATH = _config["db"]["path"]

CREATE_CHANNELS = """
CREATE TABLE IF NOT EXISTS channels (
    username TEXT PRIMARY KEY,
    title TEXT,
    active INTEGER DEFAULT 1,
    added_at DATETIME
)
"""

CREATE_VACANCIES = """
CREATE TABLE IF NOT EXISTS vacancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    created_at DATETIME,
    updated_at DATETIME
)
"""

CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

INITIAL_SETTINGS = [
    ("hh_keywords", json.dumps(_config["hh"]["keywords"], ensure_ascii=False)),
    ("score_threshold", "7"),
    ("monitoring_active", "true"),
    ("analysis_model", _config["ollama"]["analysis_model"]),
]


async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=10000")
        await db.execute(CREATE_CHANNELS)
        await db.execute(CREATE_VACANCIES)
        await db.execute(CREATE_SETTINGS)
        for key, value in INITIAL_SETTINGS:
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        # Always sync hh_keywords from config.yaml so edits take effect on restart
        config_keywords = json.dumps(_config["hh"]["keywords"], ensure_ascii=False)
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('hh_keywords', ?)",
            (config_keywords,),
        )
        # Migration: add viewed column if it doesn't exist yet
        try:
            await db.execute("ALTER TABLE vacancies ADD COLUMN viewed INTEGER DEFAULT 0")
        except Exception:
            pass
        channels = _config["telegram"].get("channels", [])
        for username in channels:
            await db.execute(
                "INSERT OR IGNORE INTO channels (username, active, added_at) VALUES (?, 1, ?)",
                (username, datetime.utcnow().isoformat()),
            )
        await db.commit()


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
        await db.commit()


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


async def get_active_profile() -> Optional[dict]:
    raw = await get_setting("active_profile")
    return json.loads(raw) if raw else None


async def save_profile(profile: dict):
    await set_setting("active_profile", json.dumps(profile, ensure_ascii=False))


async def save_resume_text(text: str):
    await set_setting("resume_text", text)


# ── Vacancies ──────────────────────────────────────────────────────────────────

async def get_pending_vacancies() -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM vacancies WHERE status='pending' AND score IS NULL ORDER BY created_at DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def vacancy_exists(external_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        async with db.execute(
            "SELECT 1 FROM vacancies WHERE external_id=?", (external_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def insert_vacancy(data: dict) -> int:
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        cur = await db.execute(
            """INSERT OR IGNORE INTO vacancies
               (source, external_id, channel, text, title, company, salary, url,
                score, analysis, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
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
            ),
        )
        await db.commit()
        return cur.lastrowid


async def update_vacancy_analysis(external_id: str, analysis: dict):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE vacancies SET analysis=?, updated_at=? WHERE external_id=?",
            (json.dumps(analysis, ensure_ascii=False), now, external_id),
        )
        await db.commit()


async def update_vacancy_status(external_id: str, status: str):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE vacancies SET status=?, updated_at=? WHERE external_id=?",
            (status, now, external_id),
        )
        await db.commit()


async def mark_viewed(external_id: str):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE vacancies SET viewed=1 WHERE external_id=?", (external_id,)
        )
        await db.commit()


async def get_vacancy_by_external(external_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM vacancies WHERE external_id=?", (external_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


_BOARD_SOURCES = ("hh", "habr", "getmatch")

async def get_recent_vacancies(days: int = 7, source: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        date_filter = f"-{days} days"
        if source in _BOARD_SOURCES:
            query = "SELECT * FROM vacancies WHERE created_at >= datetime('now', ?) AND source=? ORDER BY created_at DESC"
            params = (date_filter, source)
        elif source in ("tg", "channels"):
            placeholders = ",".join("?" * len(_BOARD_SOURCES))
            query = f"SELECT * FROM vacancies WHERE created_at >= datetime('now', ?) AND source NOT IN ({placeholders}) ORDER BY created_at DESC"
            params = (date_filter, *_BOARD_SOURCES)
        else:  # "all" or None
            query = "SELECT * FROM vacancies WHERE created_at >= datetime('now', ?) ORDER BY created_at DESC"
            params = (date_filter,)
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        stats = {}
        for status in ("new", "applied", "interview", "offer", "skipped", "rejected"):
            async with db.execute(
                "SELECT COUNT(*) FROM vacancies WHERE status=?", (status,)
            ) as cur:
                row = await cur.fetchone()
                stats[status] = row[0]
        return stats


# ── Channels ──────────────────────────────────────────────────────────────────

async def get_active_channels() -> list[str]:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        async with db.execute(
            "SELECT username FROM channels WHERE active=1"
        ) as cur:
            rows = await cur.fetchall()
            return [r[0] for r in rows]


async def get_all_channels() -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM channels ORDER BY added_at") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def add_channel(username: str, title: str = ""):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "INSERT OR REPLACE INTO channels (username, title, active, added_at) VALUES (?,?,1,?)",
            (username, title, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def set_channel_active(username: str, active: int):
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            "UPDATE channels SET active=? WHERE username=?", (active, username)
        )
        await db.commit()


async def channel_exists(username: str) -> bool:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        async with db.execute(
            "SELECT 1 FROM channels WHERE username=?", (username,)
        ) as cur:
            return await cur.fetchone() is not None


async def get_yesterday_stats() -> dict:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        result = {}
        async with db.execute(
            "SELECT COUNT(*) FROM vacancies WHERE created_at >= datetime('now', '-1 day')"
        ) as cur:
            result["new"] = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM vacancies WHERE viewed=1 AND updated_at >= datetime('now', '-1 day')"
        ) as cur:
            result["viewed"] = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM vacancies WHERE status='applied' AND updated_at >= datetime('now', '-1 day')"
        ) as cur:
            result["applied"] = (await cur.fetchone())[0]
        return result


async def get_top_vacancies_by_score(hours: int = 24, limit: int = 3) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM vacancies WHERE created_at >= datetime('now', ?) AND score IS NOT NULL "
            "ORDER BY score DESC LIMIT ?",
            (f"-{hours} hours", limit),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_stale_applied_vacancies(days: int = 3) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM vacancies WHERE status='applied' AND updated_at < datetime('now', ?) "
            "ORDER BY updated_at ASC",
            (f"-{days} days",),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_stats_30days() -> dict:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        result = {}
        async with db.execute(
            "SELECT COUNT(*) FROM vacancies WHERE created_at >= datetime('now', '-30 days')"
        ) as cur:
            result["found"] = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM vacancies WHERE viewed=1 AND created_at >= datetime('now', '-30 days')"
        ) as cur:
            result["viewed"] = (await cur.fetchone())[0]
        for status in ("applied", "interview", "offer"):
            async with db.execute(
                "SELECT COUNT(*) FROM vacancies WHERE status=? AND created_at >= datetime('now', '-30 days')",
                (status,),
            ) as cur:
                result[status] = (await cur.fetchone())[0]
        return result


async def save_interview(company: str, vacancy_id: str, notes: str, questions_list: list = None):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS interview_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company TEXT,
                vacancy_id TEXT,
                notes TEXT,
                questions_list TEXT DEFAULT '[]',
                created_at DATETIME
            )"""
        )
        await db.execute(
            "INSERT INTO interview_history (company, vacancy_id, notes, questions_list, created_at) VALUES (?,?,?,?,?)",
            (company, vacancy_id, notes, json.dumps(questions_list or [], ensure_ascii=False), now),
        )
        await db.commit()


async def get_interview_history(limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                "SELECT * FROM interview_history ORDER BY created_at DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        async with db.execute(
            "SELECT salary FROM vacancies WHERE salary IS NOT NULL AND salary != '' "
            "AND created_at >= datetime('now', ?)",
            (f"-{days} days",),
        ) as cur:
            rows = await cur.fetchall()
    values = [v for (raw,) in rows if (v := _parse_salary_value(raw)) is not None]
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
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        async with db.execute(
            "SELECT company, COUNT(*) AS cnt FROM vacancies "
            "WHERE company IS NOT NULL AND company != '' "
            "AND created_at >= datetime('now', ?) "
            "GROUP BY company ORDER BY cnt DESC LIMIT ?",
            (f"-{days} days", limit),
        ) as cur:
            rows = await cur.fetchall()
            return [{"company": r[0], "count": r[1]} for r in rows]


async def get_source_stats(days: int = 30) -> dict:
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        async with db.execute(
            "SELECT source, COUNT(*) FROM vacancies "
            "WHERE created_at >= datetime('now', ?) "
            "GROUP BY source",
            (f"-{days} days",),
        ) as cur:
            rows = await cur.fetchall()
            return {(r[0] or "other"): r[1] for r in rows}


async def save_cover_example(external_id: str, text: str, status: str = "worked"):
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=15) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS cover_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT,
                text TEXT,
                status TEXT DEFAULT 'worked',
                created_at DATETIME
            )"""
        )
        await db.execute(
            "INSERT INTO cover_examples (external_id, text, status, created_at) VALUES (?,?,?,?)",
            (external_id, text, status, now),
        )
        await db.commit()
