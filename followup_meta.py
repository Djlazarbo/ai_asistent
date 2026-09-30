"""Follow-up reminder state (dismiss / snooze) stored in user_settings.followup_meta."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

FOLLOWUP_META_KEY = "followup_meta"
FOLLOWUP_SNOOZE_DAYS = 7
DEFAULT_FOLLOW_UP_DAYS = 3


def parse_followup_meta(raw: Optional[str]) -> dict[str, dict]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[str(k)] = v
    return out


def dump_followup_meta(meta: dict[str, dict]) -> str:
    return json.dumps(meta, ensure_ascii=False)


def _parse_iso(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None) if ts.tzinfo else ts
    s = str(ts).strip().replace("Z", "")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def should_send_followup_reminder(
    entry: Optional[dict],
    *,
    now: Optional[datetime] = None,
) -> bool:
    """False if dismissed, snoozed, or already reminded today."""
    now = now or datetime.utcnow()
    if not entry:
        return True
    if entry.get("dismissed"):
        return False
    snooze_until = _parse_iso(entry.get("snooze_until"))
    if snooze_until and snooze_until > now:
        return False
    last = _parse_iso(entry.get("last_reminded_at"))
    if last and last.date() == now.date():
        return False
    return True


def mark_dismissed(meta: dict[str, dict], vacancy_id: str) -> dict[str, dict]:
    vid = str(vacancy_id)
    entry = dict(meta.get(vid) or {})
    entry["dismissed"] = True
    entry.pop("snooze_until", None)
    meta[vid] = entry
    return meta


def mark_snoozed(
    meta: dict[str, dict],
    vacancy_id: str,
    *,
    days: int = FOLLOWUP_SNOOZE_DAYS,
    now: Optional[datetime] = None,
) -> dict[str, dict]:
    now = now or datetime.utcnow()
    vid = str(vacancy_id)
    entry = dict(meta.get(vid) or {})
    entry["dismissed"] = False
    entry["snooze_until"] = (now + timedelta(days=days)).isoformat()
    meta[vid] = entry
    return meta


def mark_reminded(
    meta: dict[str, dict],
    vacancy_id: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, dict]:
    now = now or datetime.utcnow()
    vid = str(vacancy_id)
    entry = dict(meta.get(vid) or {})
    entry["last_reminded_at"] = now.isoformat()
    meta[vid] = entry
    return meta


def parse_follow_up_days(raw: Optional[str], default: int = DEFAULT_FOLLOW_UP_DAYS) -> int:
    try:
        d = int(str(raw or default).strip())
    except Exception:
        return default
    return d if 1 <= d <= 30 else default
