"""Telegram vacancy post heuristics: title extraction and digest detection."""
from __future__ import annotations

import re

_DIGEST_RE = re.compile(
    r"(подборка\s+вакансий|вакансии\s+недели|вакансии\s+дня|"
    r"дайджест|свежие\s+вакансии|топ\s+вакансий|"
    r"vacancies\s+of\s+the\s+week|job\s+digest)",
    re.IGNORECASE,
)

_LINK_RE = re.compile(r"https?://|t\.me/", re.IGNORECASE)

_TITLE_NOISE = re.compile(
    r"^(вакансия|ищем|требуется|нужен|нужна|открыта\s+позиция)[:\s\-—]*",
    re.IGNORECASE,
)


def is_digest_post(text: str) -> bool:
    """True for multi-job digests / weekly compilations — skip before scoring."""
    t = (text or "").strip()
    if not t:
        return False
    if _DIGEST_RE.search(t):
        return True
    links = len(_LINK_RE.findall(t))
    if links >= 4 and len(t) > 400:
        return True
    # many bullet roles without a single clear vacancy body
    bullets = len(re.findall(r"(?m)^\s*[-•*]\s+\S+", t))
    if bullets >= 5 and links >= 2:
        return True
    return False


def extract_tg_title(text: str, max_len: int = 120) -> str:
    """Best-effort title from first meaningful line of a TG post."""
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    # Prefer **bold** or line after emoji role markers
    m = re.search(r"\*\*(.+?)\*\*", raw)
    if m:
        cand = m.group(1).strip()
        if 3 <= len(cand) <= max_len:
            return _TITLE_NOISE.sub("", cand).strip() or cand

    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        # skip pure links / hashtags / short noise
        if line.startswith("http") or line.startswith("t.me/"):
            continue
        if line.startswith("#") and " " not in line:
            continue
        if re.fullmatch(r"[\W_]+", line):
            continue
        # strip leading emoji/symbols
        line = re.sub(r"^[\W_\d]+", "", line, count=1).strip() if line[:1].isdigit() else line
        line = re.sub(
            r"^[^\wа-яА-ЯёЁ]+",
            "",
            line,
            flags=re.UNICODE,
        ).strip()
        line = _TITLE_NOISE.sub("", line).strip()
        if len(line) < 3:
            continue
        if len(line) > max_len:
            line = line[: max_len - 1].rstrip() + "…"
        return line
    return ""
