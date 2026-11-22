"""Attach recent pool vacancies to a user who missed them (multi-user search)."""
from __future__ import annotations

import json
import os
from typing import Any

from loguru import logger


async def _candidate_pool_rows(conn, chat_id: int, days: int, fetch_limit: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT p.id, p.source, p.title, p.company, p.salary, p.url, p.text, p.channel, p.created_at
        FROM vacancies_pool p
        LEFT JOIN user_vacancies uv
          ON uv.vacancy_id = p.id AND uv.chat_id = $1
        WHERE p.created_at > NOW() - ($2::text || ' days')::interval
          AND (
            uv.vacancy_id IS NULL
            OR (uv.status = 'pending' AND uv.personal_score IS NULL)
          )
        ORDER BY p.created_at DESC
        LIMIT $3
        """,
        int(chat_id),
        str(int(days)),
        int(fetch_limit),
    )
    return [dict(r) for r in rows]


async def select_matching_for_user(
    chat_id: int,
    *,
    days: int = 7,
    limit: int = 40,
) -> list[dict]:
    """Recent pool cards this user should see but does not have scored yet."""
    import db_postgres as db
    import score_fanout_opt as sfo
    import user_data
    from title_filter import parse_keywords, vacancy_matches_user

    if not await sfo.user_is_search_ready(int(chat_id)):
        return []

    raw = await db._conn_call(user_data.get_user_setting, int(chat_id), "hh_keywords")
    kws = parse_keywords(raw)
    rows = await db._conn_call(_candidate_pool_rows, int(chat_id), int(days), max(limit * 8, 120))
    out: list[dict] = []
    for r in rows:
        src = (r.get("source") or "").lower()
        title = r.get("title") or ""
        text = r.get("text") or ""
        channel = r.get("channel")
        if src in ("telegram", "tg", "channels"):
            if not await sfo.user_owns_channel(int(chat_id), channel):
                continue
        else:
            if not kws or not vacancy_matches_user(kws, title, text):
                continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


async def score_rows_for_user(
    chat_id: int,
    rows: list[dict],
    *,
    notify: bool = False,
) -> dict[str, Any]:
    """POST existing pool vacancies through score webhook for one chat only."""
    import urllib.request

    base = (os.getenv("SCORE_URL") or os.getenv("N8N_WEBHOOK_URL") or "http://score:5679").rstrip("/")
    if base.endswith("/webhook/vacancy"):
        url = base
    else:
        url = base + "/webhook/vacancy"

    ok = 0
    err = 0
    for r in rows:
        payload = {
            "source": r.get("source") or "",
            "id": r.get("id"),
            "external_id": r.get("id"),
            "title": r.get("title") or "",
            "company": r.get("company") or "",
            "salary": r.get("salary") or "",
            "url": r.get("url") or "",
            "channel": r.get("channel") or "",
            "description": r.get("text") or "",
            "message_text": r.get("text") or "",
            "only_chat_id": int(chat_id),
            "notify": bool(notify),
            "catchup": not bool(notify),
        }
        data = json.dumps(payload, ensure_ascii=False, default=str).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if 200 <= resp.status < 300:
                    ok += 1
                else:
                    err += 1
        except Exception as e:
            err += 1
            logger.warning(f"backfill score fail {r.get('id')} chat={chat_id}: {e}")
    return {"ok": ok, "err": err, "total": len(rows)}


async def backfill_user_feed(
    chat_id: int,
    *,
    days: int = 7,
    limit: int = 40,
    notify: bool = False,
) -> dict[str, Any]:
    rows = await select_matching_for_user(int(chat_id), days=days, limit=limit)
    if not rows:
        return {"matched": 0, "ok": 0, "err": 0, "total": 0}
    stats = await score_rows_for_user(int(chat_id), rows, notify=notify)
    stats["matched"] = len(rows)
    logger.info(
        f"backfill chat={chat_id}: matched={stats['matched']} scored_ok={stats['ok']} err={stats['err']}"
    )
    return stats
