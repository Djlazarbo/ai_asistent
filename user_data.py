"""
Per-user data isolation for prod (server-only).
Used when APP_ENV=prod / MULTI_USER=1.
"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

_request_chat_id: ContextVar[Optional[int]] = ContextVar("request_chat_id", default=None)


def multi_user_on() -> bool:
    return os.getenv("APP_ENV", "").lower() == "prod" or os.getenv("MULTI_USER", "").lower() in (
        "1",
        "true",
        "yes",
    )


def set_request_chat_id(chat_id: Optional[int]):
    return _request_chat_id.set(int(chat_id) if chat_id is not None else None)


def reset_request_chat_id(token) -> None:
    _request_chat_id.reset(token)


def current_chat_id(explicit: Optional[int] = None) -> Optional[int]:
    if explicit is not None:
        return int(explicit)
    return _request_chat_id.get()


_CREATE_POOL = """
CREATE TABLE IF NOT EXISTS vacancies_pool (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  title TEXT,
  company TEXT,
  salary TEXT,
  location TEXT,
  url TEXT,
  text TEXT,
  channel TEXT,
  shared_analysis JSONB,
  analyzed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
)
"""

_CREATE_USER_VACANCIES = """
CREATE TABLE IF NOT EXISTS user_vacancies (
  id SERIAL PRIMARY KEY,
  chat_id BIGINT REFERENCES users(chat_id) ON DELETE CASCADE,
  vacancy_id TEXT REFERENCES vacancies_pool(id) ON DELETE CASCADE,
  personal_score INTEGER,
  status TEXT DEFAULT 'new',
  cover_letter TEXT,
  analysis TEXT,
  is_favorite BOOLEAN DEFAULT FALSE,
  viewed INTEGER DEFAULT 0,
  viewed_at TIMESTAMPTZ,
  applied_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(chat_id, vacancy_id)
)
"""

_CREATE_USER_RESUMES = """
CREATE TABLE IF NOT EXISTS user_resumes (
  chat_id BIGINT PRIMARY KEY REFERENCES users(chat_id) ON DELETE CASCADE,
  raw_text TEXT,
  profile JSONB,
  updated_at TIMESTAMPTZ DEFAULT NOW()
)
"""

_CREATE_USER_CHANNELS = """
CREATE TABLE IF NOT EXISTS user_channels (
  chat_id BIGINT REFERENCES users(chat_id) ON DELETE CASCADE,
  username TEXT NOT NULL,
  title TEXT,
  active BOOLEAN DEFAULT true,
  added_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (chat_id, username)
)
"""

_CREATE_DIGEST = """
CREATE TABLE IF NOT EXISTS digest_log (
  chat_id BIGINT PRIMARY KEY REFERENCES users(chat_id) ON DELETE CASCADE,
  last_sent_at TIMESTAMPTZ,
  last_vacancy_ids JSONB
)
"""


async def ensure_user_data_schema(conn) -> None:
    await conn.execute(_CREATE_POOL)
    await conn.execute(_CREATE_USER_RESUMES)
    await conn.execute(_CREATE_USER_CHANNELS)
    await conn.execute(_CREATE_USER_VACANCIES)
    await conn.execute(_CREATE_DIGEST)
    for stmt in (
        "ALTER TABLE user_vacancies ADD COLUMN IF NOT EXISTS analysis TEXT",
        "ALTER TABLE user_vacancies ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN DEFAULT FALSE",
        "ALTER TABLE user_vacancies ADD COLUMN IF NOT EXISTS viewed INTEGER DEFAULT 0",
        "ALTER TABLE vacancies_pool ADD COLUMN IF NOT EXISTS channel TEXT",
        "ALTER TABLE cover_examples ADD COLUMN IF NOT EXISTS chat_id BIGINT",
        "ALTER TABLE interview_history ADD COLUMN IF NOT EXISTS chat_id BIGINT",
    ):
        try:
            await conn.execute(stmt)
        except Exception:
            pass


async def migrate_admin_from_global(conn, admin_id: int, get_global_setting) -> None:
    if not admin_id:
        return
    await conn.execute(
        """
        INSERT INTO users (chat_id, username, first_name, active)
        VALUES ($1, 'admin', 'Admin', true)
        ON CONFLICT (chat_id) DO UPDATE SET active = true
        """,
        admin_id,
    )
    try:
        await conn.execute("SELECT init_user_defaults($1)", admin_id)
    except Exception:
        pass

    has_profile = await conn.fetchval(
        "SELECT profile FROM user_resumes WHERE chat_id=$1", admin_id
    )
    if not has_profile:
        raw_profile = await get_global_setting("active_profile")
        raw_resume = await get_global_setting("resume_text")
        if raw_profile or raw_resume:
            await conn.execute(
                """
                INSERT INTO user_resumes (chat_id, raw_text, profile, updated_at)
                VALUES ($1, $2, $3::jsonb, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET
                  raw_text = COALESCE(EXCLUDED.raw_text, user_resumes.raw_text),
                  profile = COALESCE(EXCLUDED.profile, user_resumes.profile),
                  updated_at = NOW()
                """,
                admin_id,
                raw_resume,
                raw_profile,
            )
            logger.info(f"Migrated admin profile/resume chat_id={admin_id}")

    for key in ("hh_keywords", "score_threshold", "monitoring_active", "notify_new"):
        exists = await conn.fetchval(
            "SELECT 1 FROM user_settings WHERE chat_id=$1 AND key=$2", admin_id, key
        )
        if exists:
            continue
        val = await get_global_setting(key)
        if val is not None:
            await conn.execute(
                """
                INSERT INTO user_settings (chat_id, key, value, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (chat_id, key) DO NOTHING
                """,
                admin_id,
                key,
                val,
            )

    ch_count = await conn.fetchval(
        "SELECT COUNT(*) FROM user_channels WHERE chat_id=$1", admin_id
    )
    if not ch_count:
        try:
            rows = await conn.fetch("SELECT username, title, active FROM channels")
            for r in rows:
                await conn.execute(
                    """
                    INSERT INTO user_channels (chat_id, username, title, active)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT DO NOTHING
                    """,
                    admin_id,
                    r["username"],
                    r["title"] or "",
                    bool(r["active"]),
                )
        except Exception as e:
            logger.warning(f"channel migrate: {e}")


async def get_user_setting(conn, chat_id: int, key: str) -> Optional[str]:
    row = await conn.fetchrow(
        "SELECT value FROM user_settings WHERE chat_id=$1 AND key=$2", chat_id, key
    )
    return row["value"] if row else None


async def set_user_setting(conn, chat_id: int, key: str, value: str) -> None:
    await conn.execute(
        """
        INSERT INTO user_settings (chat_id, key, value, updated_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (chat_id, key) DO UPDATE SET value=$3, updated_at=NOW()
        """,
        chat_id,
        key,
        value,
    )


async def get_user_profile(conn, chat_id: int) -> Optional[dict]:
    row = await conn.fetchrow(
        "SELECT profile FROM user_resumes WHERE chat_id=$1", chat_id
    )
    if not row or row["profile"] is None:
        return None
    p = row["profile"]
    return p if isinstance(p, dict) else json.loads(p)


async def save_user_profile(conn, chat_id: int, profile: dict) -> None:
    await conn.execute(
        """
        INSERT INTO user_resumes (chat_id, profile, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (chat_id) DO UPDATE SET profile=$2::jsonb, updated_at=NOW()
        """,
        chat_id,
        json.dumps(profile, ensure_ascii=False),
    )


async def save_user_resume_text(conn, chat_id: int, text: str) -> None:
    await conn.execute(
        """
        INSERT INTO user_resumes (chat_id, raw_text, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (chat_id) DO UPDATE SET raw_text=$2, updated_at=NOW()
        """,
        chat_id,
        text,
    )


async def get_user_resume_text(conn, chat_id: int) -> Optional[str]:
    row = await conn.fetchrow(
        "SELECT raw_text FROM user_resumes WHERE chat_id=$1", chat_id
    )
    return row["raw_text"] if row else None


async def get_keywords_union(conn) -> list[str]:
    rows = await conn.fetch(
        "SELECT value FROM user_settings WHERE key='hh_keywords'"
    )
    seen = set()
    out = []
    for r in rows:
        try:
            kws = json.loads(r["value"] or "[]")
        except Exception:
            continue
        for k in kws:
            lk = k.lower()
            if lk not in seen:
                seen.add(lk)
                out.append(k)
    return out


async def get_active_channels_union(conn) -> list[str]:
    rows = await conn.fetch(
        "SELECT DISTINCT username FROM user_channels WHERE active = true"
    )
    return [r["username"] for r in rows]


async def pool_exists(conn, vacancy_id: str) -> bool:
    row = await conn.fetchrow("SELECT 1 FROM vacancies_pool WHERE id=$1", vacancy_id)
    return row is not None



def normalize_vacancy_url(url: str | None) -> str:
    from urllib.parse import urlparse, urlunparse
    u = (url or "").strip()
    if not u:
        return ""
    try:
        p = urlparse(u)
        host = (p.netloc or "").lower().removeprefix("www.")
        path = (p.path or "").rstrip("/")
        return urlunparse(("https", host, path, "", "", ""))
    except Exception:
        return u.lower().rstrip("/")


async def upsert_pool_vacancy(conn, data: dict) -> str:
    """Insert into pool or return existing canonical id on dedup (url / title+company)."""
    from loguru import logger as _lg
    eid = str(data.get("external_id") or "")
    if not eid:
        raise ValueError("upsert_pool_vacancy: empty external_id")
    norm = normalize_vacancy_url(data.get("url"))
    if norm:
        exists = await conn.fetchval(
            "SELECT id FROM vacancies_pool WHERE url=$1 OR url=$2 LIMIT 1",
            data.get("url"),
            norm,
        )
        if exists and str(exists) != eid:
            _lg.info(f"pool dedup reuse url={norm!r} existing={exists} new={eid}")
            return str(exists)
        title = (data.get("title") or "").strip().lower()
        company = (data.get("company") or "").strip().lower()
        src = data.get("source") or "unknown"
        if title and company:
            exists2 = await conn.fetchval(
                """
                SELECT id FROM vacancies_pool
                WHERE source=$1 AND lower(coalesce(title,''))=$2 AND lower(coalesce(company,''))=$3
                LIMIT 1
                """,
                src, title, company,
            )
            if exists2 and str(exists2) != eid:
                _lg.info(f"pool dedup reuse title+company existing={exists2} new={eid}")
                return str(exists2)
    await conn.execute(
        """
        INSERT INTO vacancies_pool (id, source, title, company, salary, url, text, channel, created_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW())
        ON CONFLICT (id) DO UPDATE SET
          title = COALESCE(EXCLUDED.title, vacancies_pool.title),
          company = COALESCE(EXCLUDED.company, vacancies_pool.company),
          salary = COALESCE(EXCLUDED.salary, vacancies_pool.salary),
          url = COALESCE(EXCLUDED.url, vacancies_pool.url),
          text = COALESCE(EXCLUDED.text, vacancies_pool.text),
          channel = COALESCE(EXCLUDED.channel, vacancies_pool.channel)
        """,
        eid,
        data.get("source") or "unknown",
        data.get("title"),
        data.get("company"),
        data.get("salary"),
        data.get("url"),
        data.get("text"),
        data.get("channel"),
    )
    return eid


async def upsert_user_vacancy(
    conn,
    chat_id: int,
    vacancy_id: str,
    *,
    score=None,
    status: str = "pending",
    analysis: Optional[str] = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO user_vacancies
          (chat_id, vacancy_id, personal_score, status, analysis, created_at, updated_at)
        VALUES ($1,$2,$3,$4,$5,NOW(),NOW())
        ON CONFLICT (chat_id, vacancy_id) DO UPDATE SET
          personal_score = COALESCE(EXCLUDED.personal_score, user_vacancies.personal_score),
          status = CASE
            WHEN user_vacancies.status IN (
              'skipped', 'rejected', 'applied', 'interview', 'offer',
              'skipped_archived', 'archived_legacy', 'filtered'
            ) THEN user_vacancies.status
            ELSE EXCLUDED.status
          END,
          analysis = COALESCE(EXCLUDED.analysis, user_vacancies.analysis),
          updated_at = NOW()
        """,
        chat_id,
        vacancy_id,
        score,
        status,
        analysis,
    )


def _row_dates(d: dict) -> dict:
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


async def list_user_vacancies(
    conn, chat_id: int, days: int | None = None, source: str | None = None
) -> list[dict]:
    import os as _os
    if days is None:
        days = int(_os.getenv('FEED_TTL_DAYS') or '7')
    since = datetime.utcnow() - timedelta(days=days)
    args: list = [chat_id, since]
    src_sql = ""
    if source == "hh":
        src_sql = "AND p.source = 'hh'"
    elif source in ("tg", "channels", "telegram"):
        # PC parity: UI button source_tg_* passes "tg"; telethon writes source="telegram"
        src_sql = "AND p.source = 'telegram'"
    elif source in ("habr", "getmatch"):
        src_sql = "AND p.source = $3"
        args.append(source)
    elif source and source != "all":
        src_sql = "AND p.source = $3"
        args.append(source)

    rows = await conn.fetch(
        f"""
        SELECT uv.vacancy_id AS external_id, uv.personal_score AS score, uv.status,
               uv.analysis, uv.is_favorite, uv.viewed, uv.created_at, uv.updated_at,
               p.source, p.title, p.company, p.salary, p.url, p.text, p.channel
        FROM user_vacancies uv
        JOIN vacancies_pool p ON p.id = uv.vacancy_id
        WHERE uv.chat_id=$1 AND uv.created_at >= $2
          AND uv.status NOT IN ('filtered', 'skipped', 'rejected', 'skipped_archived', 'archived_legacy')
          {src_sql}
        ORDER BY uv.created_at DESC
        """,
        *args,
    )
    return [_row_dates(dict(r)) for r in rows]


async def get_user_vacancy(conn, chat_id: int, vacancy_id: str) -> Optional[dict]:
    row = await conn.fetchrow(
        """
        SELECT uv.vacancy_id AS external_id, uv.personal_score AS score, uv.status,
               uv.analysis, uv.is_favorite, uv.viewed, uv.cover_letter,
               uv.created_at, uv.updated_at,
               p.source, p.title, p.company, p.salary, p.url, p.text, p.channel
        FROM user_vacancies uv
        JOIN vacancies_pool p ON p.id = uv.vacancy_id
        WHERE uv.chat_id=$1 AND uv.vacancy_id=$2
        """,
        chat_id,
        vacancy_id,
    )
    return _row_dates(dict(row)) if row else None


async def update_user_vacancy_status(conn, chat_id: int, vacancy_id: str, status: str) -> None:
    if status == "applied":
        await conn.execute(
            """
            UPDATE user_vacancies SET status=$3, applied_at=NOW(), updated_at=NOW()
            WHERE chat_id=$1 AND vacancy_id=$2
            """,
            chat_id,
            vacancy_id,
            status,
        )
    else:
        await conn.execute(
            """
            UPDATE user_vacancies SET status=$3, updated_at=NOW()
            WHERE chat_id=$1 AND vacancy_id=$2
            """,
            chat_id,
            vacancy_id,
            status,
        )


async def set_user_favorite(conn, chat_id: int, vacancy_id: str, fav: bool) -> None:
    await conn.execute(
        """
        UPDATE user_vacancies SET is_favorite=$3, updated_at=NOW()
        WHERE chat_id=$1 AND vacancy_id=$2
        """,
        chat_id,
        vacancy_id,
        fav,
    )


async def is_user_favorite(conn, chat_id: int, vacancy_id: str) -> bool:
    row = await conn.fetchrow(
        "SELECT is_favorite FROM user_vacancies WHERE chat_id=$1 AND vacancy_id=$2",
        chat_id,
        vacancy_id,
    )
    return bool(row and row["is_favorite"])


async def get_user_favorites(conn, chat_id: int, limit: int = 5, offset: int = 0) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT uv.vacancy_id AS external_id, uv.personal_score AS score, uv.status,
               uv.analysis, uv.is_favorite, p.title, p.company, p.salary, p.url, p.source, p.channel
        FROM user_vacancies uv
        JOIN vacancies_pool p ON p.id = uv.vacancy_id
        WHERE uv.chat_id=$1 AND uv.is_favorite = true
        ORDER BY uv.updated_at DESC
        LIMIT $2 OFFSET $3
        """,
        chat_id,
        limit,
        offset,
    )
    return [dict(r) for r in rows]


async def get_pending_user_vacancies(conn, chat_id: Optional[int] = None) -> list[dict]:
    if chat_id is None:
        rows = await conn.fetch(
            """
            SELECT uv.chat_id, uv.vacancy_id AS external_id, uv.status,
                   p.source, p.title, p.company, p.salary, p.url, p.text, p.channel
            FROM user_vacancies uv
            JOIN vacancies_pool p ON p.id = uv.vacancy_id
            WHERE uv.status='pending' AND uv.personal_score IS NULL
            ORDER BY uv.created_at DESC
            """
        )
    else:
        rows = await conn.fetch(
            """
            SELECT uv.chat_id, uv.vacancy_id AS external_id, uv.status,
                   p.source, p.title, p.company, p.salary, p.url, p.text, p.channel
            FROM user_vacancies uv
            JOIN vacancies_pool p ON p.id = uv.vacancy_id
            WHERE uv.chat_id=$1 AND uv.status='pending' AND uv.personal_score IS NULL
            ORDER BY uv.created_at DESC
            """,
            chat_id,
        )
    return [dict(r) for r in rows]


async def user_stats(conn, chat_id: int) -> dict:
    rows = await conn.fetch(
        "SELECT status, COUNT(*) AS c FROM user_vacancies WHERE chat_id=$1 GROUP BY status",
        chat_id,
    )
    stats = {"new": 0, "applied": 0, "skipped": 0, "pending": 0, "total": 0, "viewed": 0}
    for r in rows:
        stats[r["status"]] = r["c"]
        stats["total"] += r["c"]
    return stats




async def user_stats_30days(conn, chat_id: int, days: int = 30) -> dict:
    """Per-user funnel for /stats — same keys as legacy get_stats_30days."""
    since = datetime.utcnow() - timedelta(days=days)
    result = {}
    for status in ("new", "skipped", "filtered", "pending", "applied", "interview", "offer", "rejected"):
        val = await conn.fetchval(
            """
            SELECT COUNT(*) FROM user_vacancies
            WHERE chat_id=$1 AND status=$2 AND created_at >= $3
            """,
            chat_id, status, since,
        )
        result[status] = int(val or 0)
    result["viewed"] = int(await conn.fetchval(
        """
        SELECT COUNT(*) FROM user_vacancies
        WHERE chat_id=$1 AND viewed=1 AND created_at >= $2
        """,
        chat_id, since,
    ) or 0)
    result["found"] = result["new"] + result["skipped"]
    result["total"] = sum(result[s] for s in ("new", "skipped", "filtered", "pending"))
    return result

async def list_user_channels(conn, chat_id: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT username, title, active, added_at
        FROM user_channels WHERE chat_id=$1 ORDER BY added_at
        """,
        chat_id,
    )
    out = []
    for r in rows:
        d = dict(r)
        d["active"] = 1 if d.get("active") else 0
        if isinstance(d.get("added_at"), datetime):
            d["added_at"] = d["added_at"].isoformat()
        out.append(d)
    return out


async def add_user_channel(conn, chat_id: int, username: str, title: str = "") -> None:
    await conn.execute(
        """
        INSERT INTO user_channels (chat_id, username, title, active)
        VALUES ($1, $2, $3, true)
        ON CONFLICT (chat_id, username) DO UPDATE SET active = true, title = COALESCE(EXCLUDED.title, user_channels.title)
        """,
        chat_id,
        username,
        title,
    )


async def set_user_channel_active(conn, chat_id: int, username: str, active: bool) -> None:
    await conn.execute(
        """
        UPDATE user_channels SET active=$3 WHERE chat_id=$1 AND username=$2
        """,
        chat_id,
        username,
        active,
    )


async def get_follow_up_days(conn, chat_id: int, default: int = 3) -> int:
    from followup_meta import parse_follow_up_days

    raw = await get_user_setting(conn, chat_id, "follow_up_days")
    return parse_follow_up_days(raw, default=default)


async def get_followup_meta(conn, chat_id: int) -> dict:
    from followup_meta import parse_followup_meta

    raw = await get_user_setting(conn, chat_id, "followup_meta")
    return parse_followup_meta(raw)


async def save_followup_meta(conn, chat_id: int, meta: dict) -> None:
    from followup_meta import dump_followup_meta

    await set_user_setting(conn, chat_id, "followup_meta", dump_followup_meta(meta))


async def patch_followup_meta(conn, chat_id: int, mutator) -> dict:
    """Load meta, apply mutator(meta) -> meta, save, return meta."""
    meta = await get_followup_meta(conn, chat_id)
    meta = mutator(meta) or meta
    await save_followup_meta(conn, chat_id, meta)
    return meta


async def stale_applied(conn, chat_id: int, days: int = 3) -> list[dict]:
    from followup_meta import should_send_followup_reminder

    since = datetime.utcnow() - timedelta(days=days)
    rows = await conn.fetch(
        """
        SELECT uv.vacancy_id AS external_id,
               COALESCE(uv.applied_at, uv.updated_at) AS updated_at,
               uv.applied_at, uv.status,
               p.title, p.company, p.url
        FROM user_vacancies uv
        JOIN vacancies_pool p ON p.id = uv.vacancy_id
        WHERE uv.chat_id=$1 AND uv.status='applied'
          AND COALESCE(uv.applied_at, uv.updated_at) < $2
        ORDER BY COALESCE(uv.applied_at, uv.updated_at)
        """,
        chat_id,
        since,
    )
    meta = await get_followup_meta(conn, chat_id)
    out = []
    for r in rows:
        d = _row_dates(dict(r))
        vid = str(d.get("external_id") or "")
        if not should_send_followup_reminder(meta.get(vid)):
            continue
        out.append(d)
    return out


async def top_by_score(conn, chat_id: int, hours: int = 24, limit: int = 3) -> list[dict]:
    since = datetime.utcnow() - timedelta(hours=hours)
    rows = await conn.fetch(
        """
        SELECT uv.vacancy_id AS external_id, uv.personal_score AS score,
               p.title, p.company, p.url
        FROM user_vacancies uv
        JOIN vacancies_pool p ON p.id = uv.vacancy_id
        WHERE uv.chat_id=$1 AND uv.created_at >= $2
          AND uv.personal_score IS NOT NULL
          AND uv.status IN ('new', 'applied')
        ORDER BY uv.personal_score DESC NULLS LAST
        LIMIT $3
        """,
        chat_id,
        since,
        limit,
    )
    return [dict(r) for r in rows]


async def yesterday_stats(conn, chat_id: int) -> dict:
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    end = start + timedelta(days=1)
    row = await conn.fetchrow(
        """
        SELECT
          COUNT(*) FILTER (WHERE status='new') AS new,
          COUNT(*) FILTER (WHERE viewed > 0) AS viewed,
          COUNT(*) FILTER (WHERE status='applied') AS applied
        FROM user_vacancies
        WHERE chat_id=$1 AND created_at >= $2 AND created_at < $3
        """,
        chat_id,
        start,
        end,
    )
    return {"new": row["new"] or 0, "viewed": row["viewed"] or 0, "applied": row["applied"] or 0}


# === OPENFIX / STAGE_A helpers ===
async def mark_user_viewed(conn, chat_id: int, vacancy_id: str) -> None:
    await conn.execute(
        """
        UPDATE user_vacancies
        SET viewed = 1, viewed_at = NOW(), updated_at = NOW()
        WHERE chat_id=$1 AND vacancy_id=$2
        """,
        chat_id, vacancy_id,
    )

async def update_user_vacancy_analysis(conn, chat_id: int, vacancy_id: str, analysis: str) -> None:
    await conn.execute(
        """
        UPDATE user_vacancies
        SET analysis=$3, updated_at=NOW()
        WHERE chat_id=$1 AND vacancy_id=$2
        """,
        chat_id, vacancy_id, analysis,
    )

async def get_pool_vacancy(conn, vacancy_id: str):
    row = await conn.fetchrow(f"SELECT * FROM vacancies_pool WHERE id=$1", vacancy_id)
    if not row:
        return None
    d = dict(row)
    d["external_id"] = d.get("id")
    return _row_dates(d)

async def ensure_user_vacancy_from_pool(conn, chat_id: int, vacancy_id: str):
    pool = await get_pool_vacancy(conn, vacancy_id)
    if not pool:
        return None
    await upsert_user_vacancy(conn, chat_id, vacancy_id, status="new")
    return await get_user_vacancy(conn, chat_id, vacancy_id)

async def save_user_cover_example(conn, chat_id: int, external_id: str, text: str, status: str = "worked") -> None:
    await conn.execute(
        """
        INSERT INTO cover_examples (chat_id, external_id, vacancy_id, text, status, created_at)
        VALUES ($1, $2, $2, $3, $4, NOW())
        """,
        chat_id, external_id, text, status,
    )

async def get_user_cover_examples(conn, chat_id: int, limit: int = 5) -> str:
    rows = await conn.fetch(
        """
        SELECT text FROM cover_examples
        WHERE chat_id=$1 AND status='worked'
        ORDER BY created_at DESC LIMIT $2
        """,
        chat_id, limit,
    )
    if not rows:
        return ""
    return "\n\n".join(f"Пример {i}:\n{r['text']}" for i, r in enumerate(rows, 1))

async def save_user_interview(conn, chat_id: int, company: str, vacancy_id: str, notes: str, questions_list=None) -> None:
    import json as _json
    await conn.execute(
        """
        INSERT INTO interview_history (chat_id, company, vacancy_id, notes, questions, created_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
        """,
        chat_id, company, vacancy_id, notes, _json.dumps(questions_list or [], ensure_ascii=False),
    )

async def get_user_interview_history(conn, chat_id: int, limit: int = 10) -> list:
    import json as _json
    rows = await conn.fetch(
        """
        SELECT id, chat_id, company, vacancy_id, notes, questions, created_at, result, interview_date
        FROM interview_history WHERE chat_id=$1 ORDER BY created_at DESC LIMIT $2
        """,
        chat_id, limit,
    )
    out = []
    for r in rows:
        d = dict(r)
        q = d.get("questions")
        if q is not None:
            d["questions_list"] = q if isinstance(q, str) else _json.dumps(q, ensure_ascii=False)
        out.append(d)
    return out

async def count_user_favorites(conn, chat_id: int) -> int:
    return int(await conn.fetchval(
        f"SELECT COUNT(*) FROM user_vacancies WHERE chat_id=$1 AND is_favorite = true", chat_id,
    ) or 0)


async def list_digest_candidates(
    conn, chat_id: int, *, since, threshold: int = 7, limit: int = 12
) -> list[dict]:
    """New (unseen-ish) scored vacancies for ears digest."""
    rows = await conn.fetch(
        """
        SELECT uv.vacancy_id AS external_id, uv.personal_score AS score,
               uv.status, p.title, p.company, p.url, p.source, p.channel,
               uv.created_at
        FROM user_vacancies uv
        JOIN vacancies_pool p ON p.id = uv.vacancy_id
        WHERE uv.chat_id=$1
          AND uv.created_at >= $2
          AND uv.personal_score IS NOT NULL
          AND uv.personal_score >= $3
          AND COALESCE(uv.status, 'new') IN ('new')
        ORDER BY uv.personal_score DESC NULLS LAST, uv.created_at DESC
        LIMIT $4
        """,
        chat_id,
        since,
        int(threshold),
        int(limit),
    )
    return [dict(r) for r in rows]


async def get_digest_meta(conn, chat_id: int) -> dict:
    row = await conn.fetchrow(
        "SELECT last_sent_at, last_vacancy_ids FROM digest_log WHERE chat_id=$1",
        chat_id,
    )
    if not row:
        return {"last_sent_at": None, "last_vacancy_ids": []}
    ids = row["last_vacancy_ids"] or []
    if isinstance(ids, str):
        import json as _json
        try:
            ids = _json.loads(ids)
        except Exception:
            ids = []
    return {"last_sent_at": row["last_sent_at"], "last_vacancy_ids": ids}


async def touch_digest_meta(conn, chat_id: int, vacancy_ids: list) -> None:
    import json as _json
    await conn.execute(
        """
        INSERT INTO digest_log (chat_id, last_sent_at, last_vacancy_ids)
        VALUES ($1, NOW(), $2::jsonb)
        ON CONFLICT (chat_id) DO UPDATE
          SET last_sent_at=NOW(), last_vacancy_ids=$2::jsonb
        """,
        chat_id,
        _json.dumps(vacancy_ids or [], ensure_ascii=False),
    )

