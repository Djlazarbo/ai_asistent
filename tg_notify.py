"""Send instant 'new vacancy' pushes via Telegram Bot API (HTTP).

Used by score_server's webhook fan-out (`from tg_notify import send_vacancy_push`).
The import existed since 1.2.0 but the module was missing, so any code path
reaching it died on ImportError inside a broad except. Token comes from env
TELEGRAM_BOT_TOKEN or config.yaml telegram_bot.token — never logged.
"""
from __future__ import annotations

import html
import os
from typing import Any, Optional

import yaml

_PUSH_TIMEOUT_SEC = 20


def _bot_token() -> str:
    tok = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        with open("config.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        tok = str(((cfg.get("telegram_bot") or {}).get("token")) or "").strip()
        if tok and "PLACEHOLDER" not in tok:
            return tok
    except Exception:
        pass
    return ""


def format_vacancy_push(
    vacancy: dict,
    score: Optional[int] = None,
    reason: str = "",
) -> str:
    """Build the HTML card text (pure, unit-testable)."""
    v = vacancy if isinstance(vacancy, dict) else {}
    title = html.escape(str(v.get("title") or "Вакансия"))
    url = str(v.get("url") or v.get("link") or "").strip()
    title_part = f'<a href="{url}">{title}</a>' if url else title
    company = html.escape(str(v.get("company") or "—"))
    salary = html.escape(str(v.get("salary") or "—"))
    score_str = f"{int(score)}/10" if score is not None else "?"
    channel = str(v.get("channel") or "").strip()
    src = str(v.get("source") or "").strip().lower()
    badge = "💬 " + html.escape(channel.lstrip("@")) if channel and src in ("telegram", "tg") else ""
    text = (
        f"🆕 Новая вакансия {badge}\n"
        f"<b>{title_part}</b>\n"
        f"🏢 {company} | 💰 {salary}\n"
        f"📊 {score_str}"
    )
    if reason:
        text += "\n\n" + html.escape(str(reason)[:400])
    return text


async def send_vacancy_push(
    chat_id: int,
    vacancy: Any,
    score: Optional[int] = None,
    reason: str = "",
    vacancy_id: Optional[str] = None,
) -> bool:
    """Send one vacancy card. Returns True on success, False otherwise."""
    token = _bot_token()
    if not token:
        return False
    text = format_vacancy_push(vacancy if isinstance(vacancy, dict) else {}, score, reason)
    payload = {
        "chat_id": int(chat_id),
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        import aiohttp

        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=_PUSH_TIMEOUT_SEC)) as s:
            async with s.post(
                f"https://api.telegram.org/bot{token}/sendMessage", json=payload
            ) as r:
                # 403: user blocked the bot; 400: bad chat — not retryable here.
                return r.status == 200
    except Exception:
        return False
