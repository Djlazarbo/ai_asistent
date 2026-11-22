"""Lightweight on-demand search fallback (no Playwright / no Chromium in bot image).

Uses Habr RSS only. HH HTML needs Playwright → left to the poller service.
"""
from __future__ import annotations

import json
import re
from typing import Any


def parse_keywords(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    try:
        val = json.loads(s)
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
    except Exception:
        pass
    return [p.strip() for p in s.replace("\n", ",").split(",") if p.strip()]


async def collect_light(keywords: list[str], include_habr: bool = True) -> list[dict[str, Any]]:
    """Return vacancy dicts suitable for upsert_vacancy_pool / insert."""
    out: list[dict[str, Any]] = []
    if not keywords:
        return out
    if include_habr:
        try:
            from habr_poller import _parse_rss, _vacancy_id, _is_relevant_title
            from title_filter import is_search_keyword, is_junk_title

            for kw in keywords:
                if not is_search_keyword(kw):
                    continue
                items = await _parse_rss(kw)
                for item in items:
                    title = item.get("title") or ""
                    if is_junk_title(title) or not _is_relevant_title(title):
                        continue
                    url = item.get("link") or ""
                    vid = _vacancy_id(url)
                    out.append(
                        {
                            "source": "habr",
                            "external_id": vid,
                            "id": vid,
                            "title": title,
                            "company": item.get("company") or "",
                            "url": url,
                            "text": item.get("description") or title,
                            "channel": "habr",
                        }
                    )
        except Exception:
            pass
    # de-dupe by id
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for v in out:
        vid = str(v.get("external_id") or v.get("id") or "")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        uniq.append(v)
    return uniq
