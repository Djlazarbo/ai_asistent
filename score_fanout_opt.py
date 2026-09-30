"""Fan-out scoring helpers: only ready users, source-aware recipients."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Iterable, Optional

from loguru import logger

_SEM: asyncio.Semaphore | None = None


def score_semaphore() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        n = int(os.getenv("SCORE_FANOUT_CONCURRENCY", "2"))
        _SEM = asyncio.Semaphore(max(1, min(n, 5)))
    return _SEM


def _parse_keywords(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
    except Exception:
        pass
    # plain comma / newline fallback
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    return [p for p in parts if p]


async def _user_setting(chat_id: int, key: str) -> Optional[str]:
    import db_postgres as db
    import user_data

    return await db._conn_call(user_data.get_user_setting, int(chat_id), key)


async def user_has_profile(chat_id: int) -> bool:
    """True only if user has a real saved profile (not an empty row). Fail closed."""
    try:
        import db_postgres as db
        import user_data

        profile = await db._conn_call(user_data.get_user_profile, int(chat_id))
        if not profile or not isinstance(profile, dict):
            return False
        # any meaningful field
        for k in ("role", "title", "skills", "summary", "experience", "name"):
            v = profile.get(k)
            if isinstance(v, str) and v.strip():
                return True
            if isinstance(v, list) and v:
                return True
        # nested fallback
        blob = json.dumps(profile, ensure_ascii=False)
        return len(blob) > 40
    except Exception as e:
        logger.warning(f"user_has_profile({chat_id}): {e}")
        return False


async def user_has_keywords(chat_id: int) -> bool:
    try:
        raw = await _user_setting(int(chat_id), "hh_keywords")
        return bool(_parse_keywords(raw))
    except Exception as e:
        logger.warning(f"user_has_keywords({chat_id}): {e}")
        return False


async def user_has_any_channel(chat_id: int) -> bool:
    try:
        import db_postgres as db
        import user_data

        ch = await db._conn_call(user_data.list_user_channels, int(chat_id))
        return bool(ch)
    except Exception as e:
        logger.warning(f"user_has_any_channel({chat_id}): {e}")
        return False


def _norm_channel(ch: str | None) -> str:
    s = (ch or "").strip().lower()
    if not s:
        return ""
    if not s.startswith("@") and not s.lstrip("-").isdigit():
        s = "@" + s
    return s


async def user_owns_channel(chat_id: int, channel: str | None) -> bool:
    want = _norm_channel(channel)
    if not want:
        return False
    try:
        import db_postgres as db
        import user_data

        rows = await db._conn_call(user_data.list_user_channels, int(chat_id))
        for r in rows or []:
            uname = _norm_channel(r.get("username") if isinstance(r, dict) else str(r))
            if uname and uname == want:
                # respect active flag when present
                if isinstance(r, dict) and r.get("active") is False:
                    continue
                return True
        return False
    except Exception as e:
        logger.warning(f"user_owns_channel({chat_id},{channel}): {e}")
        return False


async def user_is_search_ready(chat_id: int) -> bool:
    """Resume + (keywords or channels). Otherwise onboarding incomplete → no scoring/pushes."""
    if not await user_has_profile(int(chat_id)):
        return False
    if await user_has_keywords(int(chat_id)):
        return True
    if await user_has_any_channel(int(chat_id)):
        return True
    return False


async def filter_chats_with_profile(chat_ids: Iterable[int]) -> list[int]:
    """Backward-compatible name: now means 'search-ready' users."""
    out: list[int] = []
    skipped = 0
    for cid in chat_ids:
        if await user_is_search_ready(int(cid)):
            out.append(int(cid))
        else:
            skipped += 1
            logger.info(f"score skip chat={cid}: not ready (need resume + keywords/channels)")
    if skipped:
        logger.info(f"score fan-out skipped {skipped} users (not ready)")
    return out


def company_matches_list(company: str | None, names: list[str]) -> bool:
    try:
        import db_postgres as db
        return bool(db.company_name_matches(str(company or ""), names or []))
    except Exception:
        c = (company or "").lower().replace("ё", "е").strip()
        if not c:
            return False
        for n in names or []:
            nn = (n or "").lower().replace("ё", "е").strip()
            if nn and (nn in c or c in nn):
                return True
        return False


async def get_company_lists(chat_id: int) -> tuple[list[str], list[str]]:
    import json as _json

    block: list[str] = []
    want: list[str] = []
    raw_b = await _user_setting(int(chat_id), "companies_block")
    raw_w = await _user_setting(int(chat_id), "companies_want")
    for raw, target in ((raw_b, block), (raw_w, want)):
        if not raw:
            continue
        try:
            data = _json.loads(raw)
            if isinstance(data, list):
                target.extend(str(x).strip() for x in data if str(x).strip())
        except Exception:
            pass
    return block, want


def apply_company_want_boost(score: int | None, *, boosted: bool, amount: int = 1) -> int | None:
    if score is None or not boosted:
        return score
    try:
        return min(10, int(score) + max(0, amount))
    except Exception:
        return score


async def filter_chats_for_vacancy(
    chat_ids: Iterable[int],
    *,
    source: str | None = None,
    channel: str | None = None,
    title: str | None = None,
    text: str | None = None,
    company: str | None = None,
) -> list[int]:
    """
    Ready users only, then source-scoped:
      telegram → only users who added that channel
      hh/habr/getmatch → only users whose keywords match this vacancy
      companies_block → hard skip (no scoring)
    """
    from title_filter import parse_keywords, vacancy_matches_user

    ready = await filter_chats_with_profile(chat_ids)
    src = (source or "").lower().strip()
    out: list[int] = []
    for cid in ready:
        block, _want = await get_company_lists(cid)
        if company_matches_list(company, block):
            logger.info(
                f"score skip chat={cid}: companies_block hit company={(company or '')[:50]!r}"
            )
            continue
        if src in ("telegram", "tg", "channels"):
            if await user_owns_channel(cid, channel):
                out.append(cid)
            else:
                logger.info(f"score skip chat={cid}: tg channel {channel} not theirs")
        else:
            if not await user_has_keywords(cid):
                logger.info(f"score skip chat={cid}: no keywords for board source={src}")
                continue
            raw = await _user_setting(int(cid), "hh_keywords")
            kws = parse_keywords(raw)
            if not vacancy_matches_user(kws, title or "", text or ""):
                logger.info(
                    f"score skip chat={cid}: keywords miss title={(title or '')[:70]!r}"
                )
                continue
            out.append(cid)
    return out


async def map_bounded(
    items: list[Any],
    worker: Callable[[Any], Awaitable[Any]],
) -> list[Any]:
    sem = score_semaphore()

    async def _one(x):
        async with sem:
            return await worker(x)

    return await asyncio.gather(*[_one(x) for x in items], return_exceptions=True)
