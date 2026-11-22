"""Multi-user accounts + invites (prod)."""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional

from loguru import logger

_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
  chat_id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ
)
"""

_CREATE_USER_SETTINGS = """
CREATE TABLE IF NOT EXISTS user_settings (
  chat_id BIGINT NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (chat_id, key)
)
"""

_CREATE_INVITES = """
CREATE TABLE IF NOT EXISTS invite_codes (
  code TEXT PRIMARY KEY,
  used_by BIGINT REFERENCES users(chat_id),
  used_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
)
"""

_INIT_USER_DEFAULTS = """
CREATE OR REPLACE FUNCTION init_user_defaults(p_chat_id BIGINT)
RETURNS void AS $$
BEGIN
  INSERT INTO users (chat_id, active)
  VALUES (p_chat_id, true)
  ON CONFLICT (chat_id) DO UPDATE SET active = true;

  -- Keep in sync with docker/postgres/init.sql (critical: monitoring ON by default)
  INSERT INTO user_settings (chat_id, key, value, updated_at) VALUES
    (p_chat_id, 'score_threshold', '7', NOW()),
    (p_chat_id, 'monitoring_active', 'true', NOW()),
    (p_chat_id, 'notify_new', 'true', NOW()),
    (p_chat_id, 'digest_time', '09:00', NOW()),
    (p_chat_id, 'follow_up_days', '3', NOW())
  ON CONFLICT (chat_id, key) DO NOTHING;
END;
$$ LANGUAGE plpgsql;
"""

DEFAULT_INVITE_COUNT = 20
MAX_USERS = 50


async def ensure_multiuser_schema(conn) -> None:
    await conn.execute(_CREATE_USERS)
    await conn.execute(_CREATE_USER_SETTINGS)
    await conn.execute(_CREATE_INVITES)
    await conn.execute(_INIT_USER_DEFAULTS)
    for stmt in (
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT",
    ):
        try:
            await conn.execute(stmt)
        except Exception:
            pass


async def seed_invite_codes(conn, n: int = DEFAULT_INVITE_COUNT) -> None:
    unused = await conn.fetchval(
        "SELECT COUNT(*) FROM invite_codes WHERE used_by IS NULL"
    )
    need = max(0, int(n) - int(unused or 0))
    for _ in range(need):
        code = secrets.token_urlsafe(8)
        try:
            await conn.execute(
                "INSERT INTO invite_codes (code) VALUES ($1) ON CONFLICT DO NOTHING",
                code,
            )
        except Exception as e:
            logger.warning(f"seed invite: {e}")


async def is_registered(conn, chat_id: int) -> bool:
    row = await conn.fetchval(
        "SELECT 1 FROM users WHERE chat_id=$1 AND COALESCE(active, true) = true",
        int(chat_id),
    )
    return bool(row)


async def list_active_chat_ids(conn) -> list[int]:
    rows = await conn.fetch(
        "SELECT chat_id FROM users WHERE COALESCE(active, true) = true ORDER BY chat_id"
    )
    return [int(r["chat_id"]) for r in rows]


async def touch_last_seen(conn, chat_id: int) -> None:
    await conn.execute(
        """
        UPDATE users SET last_seen_at = NOW()
        WHERE chat_id=$1
        """,
        int(chat_id),
    )


async def unused_invite_codes(conn) -> list[str]:
    rows = await conn.fetch(
        "SELECT code FROM invite_codes WHERE used_by IS NULL ORDER BY created_at"
    )
    return [str(r["code"]) for r in rows]


async def register_with_invite(
    conn,
    chat_id: int,
    code: str,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Returns (status, detail). status: ok|already|invalid|used|full|need_code|bad_code"""
    cid = int(chat_id)
    code = (code or "").strip()
    if not code:
        return "need_code", None

    if await is_registered(conn, cid):
        await touch_last_seen(conn, cid)
        return "already", None

    total = await conn.fetchval("SELECT COUNT(*) FROM users WHERE COALESCE(active, true)")
    if int(total or 0) >= MAX_USERS:
        return "full", None

    inv = await conn.fetchrow(
        "SELECT code, used_by FROM invite_codes WHERE code=$1",
        code,
    )
    if not inv:
        return "invalid", None
    if inv["used_by"] is not None:
        return "used", None

    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO users (chat_id, username, first_name, active, last_seen_at)
            VALUES ($1, $2, $3, true, NOW())
            ON CONFLICT (chat_id) DO UPDATE SET
              active = true,
              username = COALESCE(EXCLUDED.username, users.username),
              first_name = COALESCE(EXCLUDED.first_name, users.first_name),
              last_seen_at = NOW()
            """,
            cid,
            username,
            first_name,
        )
        await conn.execute(
            """
            UPDATE invite_codes
            SET used_by=$1, used_at=NOW()
            WHERE code=$2 AND used_by IS NULL
            """,
            cid,
            code,
        )
        try:
            await conn.execute("SELECT init_user_defaults($1)", cid)
        except Exception:
            pass
    return "ok", None
