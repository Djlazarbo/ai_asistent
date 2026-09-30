"""Shared vacancy title / keyword filters for pollers and multi-user fan-out."""
from __future__ import annotations

import json
import re

_STOP = {
    "the", "and", "для", "или", "при", "без", "над",
    "менеджер", "manager", "руководитель", "lead",
}


def _norm(s: str) -> str:
    return (s or "").lower().replace("ё", "е").strip()


def _tokens(s: str) -> list[str]:
    raw = re.split(r"[^\w]+", _norm(s), flags=re.UNICODE)
    out: list[str] = []
    keep_short = {"it", "qa", "pm", "po", "em", "ai"}
    for t in raw:
        if not t:
            continue
        if t in keep_short or len(t) >= 3:
            out.append(t)
    return out


def _token_in(tok: str, hay: set[str]) -> bool:
    if tok in hay:
        return True
    if len(tok) >= 5:
        stem = tok[:5]
        return any(h.startswith(stem) or tok.startswith(h[:5]) for h in hay if len(h) >= 5)
    return False


# Ingest-level junk: roles nobody in this bot is searching for.
_JUNK_TITLE = re.compile(
    r"(клининг|клинингов|"
    r"строительн|открой?тие новых филиалов|"
    r"онлайн.?школ|продюсер|"
    r"упаковк|пищев|товарные группы|"
    r"прокат концерт|"
    r"менеджер по продажам|отдел продаж|руководитель отдела продаж|"
    r"аккаунт.?менеджер|account\s*manager|"
    r"рекрутер|подбор персонала|hr\s*bp|"
    r"сетевой инженер|системный администратор|"
    r"backend|frontend|fullstack|разработчик|"
    r"инженер.?конструктор|"
    r"зарплатн\w* проект)",
    re.IGNORECASE,
)

_OFF_PM_TITLE = re.compile(
    r"(таргетолог|маркетолог|бренд.?менеджер|smm\b|контент.?менеджер|"
    r"экономическ\w*\s+безопасн|"
    r"информационн\w*\s+безопасн|"
    r"продаж|маркетинг|бренд)",
    re.IGNORECASE,
)

_IT_PM_TITLE = re.compile(
    r"(technical\s+project\s+manager|"
    r"it\s*[- ]?project\s+manager|"
    r"project\s+manager|"
    r"program\s+manager|"
    r"delivery\s+manager|"
    r"\btpm\b|\bpmo\b|"
    r"руководитель\s+(it|ит)[-\s]*проект|"
    r"менеджер\s+(it|ит)[-\s]*проект|"
    r"технический\s+(менеджер|руководитель|лид)\s*проект|"
    r"технический\s+проектн|"
    r"delivery\s*project)",
    re.IGNORECASE,
)

_NOT_PM_TITLE = re.compile(
    r"(product\s+owner|продакт|product\s+manager|"
    r"team\s*lead|тимлид|tech\s*lead|"
    r"engineering\s+manager|"
    r"scrum\s+master|agile\s+coach|"
    r"cto|технический\s+директор|"
    r"продаж|маркетинг|бренд)",
    re.IGNORECASE,
)

_IT_SIGNAL = re.compile(
    r"(\bit\b|\bит\b|digital|software|saas|tpm|agile|scrum|jira|"
    r"разработ|программн|информационн\w*\s+технолог|цифров|"
    r"внедрен\w*\s+по|автоматизац|delivery|technical\s+project)",
    re.IGNORECASE,
)

_PM_SEARCHER = re.compile(
    r"(project\s*manager|program\s*manager|delivery|tpm|"
    r"руководитель|менеджер\s*(it|ит|проект)|technical\s*project|"
    r"технический\s+менеджер)",
    re.IGNORECASE,
)

_DUTY_OR_GENERIC = re.compile(
    r"(приоритизац|бэклог|backlog|стейкхолдер|stakeholder|"
    r"координация команды|взаимодействие с бизнес|"
    r"преобразование бизнес|оптимизация бизнес|"
    r"управление сроками|управление качеством|"
    r"аккредитован|информационные технологии|"
    r"разработка и внедрение)",
    re.IGNORECASE,
)

_DUTY_PREFIX = (
    "управление ",
    "формирование ",
    "координация ",
    "взаимодействие ",
    "анализ и ",
    "преобразование ",
    "работа со ",
)


def is_junk_title(title: str) -> bool:
    t = _norm(title)
    if not t:
        return False
    return bool(_JUNK_TITLE.search(t))


def is_off_pm_title(title: str) -> bool:
    t = _norm(title)
    if not t:
        return False
    return bool(_OFF_PM_TITLE.search(t) or _NOT_PM_TITLE.search(t))


def is_it_pm_title(title: str) -> bool:
    t = _norm(title)
    if not t or is_junk_title(t):
        return False
    if _NOT_PM_TITLE.search(t) and not _IT_PM_TITLE.search(t):
        return False
    if not _IT_PM_TITLE.search(t):
        return False
    if re.search(r"(it|ит|tpm|delivery|technical|технический)", t):
        return True
    if re.search(r"project\s+manager", t) and not re.search(
        r"(школ|строител|клининг|продаж|маркетинг)", t
    ):
        return True
    return False


def has_it_signal(title: str, text: str = "") -> bool:
    blob = f"{title} {text[:800]}"
    return bool(_IT_SIGNAL.search(_norm(blob)))


def is_search_keyword(word: str) -> bool:
    k = (word or "").strip()
    if len(k) < 4 or len(k) > 60:
        return False
    n = _norm(k)
    if n in {"рукодитель", "аккредитованная", "аккредитованный"}:
        return False
    if n.startswith(_DUTY_PREFIX):
        return False
    if _DUTY_OR_GENERIC.search(n):
        return False
    return True


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
    parts = [p.strip() for p in s.replace("\n", ",").split(",")]
    return [p for p in parts if p]


def user_is_pm_searcher(keywords: list[str]) -> bool:
    blob = " ".join(keywords)
    return bool(_PM_SEARCHER.search(_norm(blob)))


def keyword_hits_title(keyword: str, title: str) -> bool:
    kw = _norm(keyword)
    title_n = _norm(title)
    if not kw or not title_n:
        return False
    if len(kw) >= 5 and kw in title_n:
        return True
    kt = [t for t in _tokens(kw) if t not in _STOP]
    if not kt:
        kt = _tokens(kw)
    hay = set(_tokens(title))
    if not kt or not hay:
        return False
    return all(_token_in(t, hay) for t in kt)


def build_pm_hh_query(keywords: list[str]) -> str:
    """Boolean hh.ru query: NAME:(a OR b) NOT junk — only for PM searchers."""
    parts: list[str] = []
    for k in keywords:
        k = (k or "").strip()
        if not is_search_keyword(k):
            continue
        if re.search(r'[\s/"]', k):
            parts.append(f'NAME:"{k}"')
        else:
            parts.append(f"NAME:{k}")
    if not parts:
        return ""
    q = "(" + " OR ".join(parts) + ")"
    for term in (
        "продажам",
        "маркетинг",
        "строитель",
        "стажёр",
        "стажер",
        "клининг",
        "рекрутер",
    ):
        q += f" NOT {term}"
    return q


def vacancy_matches_user(keywords: list[str], title: str, text: str = "") -> bool:
    """True if this vacancy belongs in this user's board feed."""
    kws = [k for k in keywords if is_search_keyword(k)]
    if not kws:
        return False
    if is_junk_title(title):
        return False
    pm = user_is_pm_searcher(kws)
    if pm:
        if is_off_pm_title(title) and not is_it_pm_title(title):
            return False
        if is_it_pm_title(title):
            return True
        if any(keyword_hits_title(k, title) for k in kws):
            return has_it_signal(title, text)
        return False
    return any(keyword_hits_title(k, title) for k in kws)
