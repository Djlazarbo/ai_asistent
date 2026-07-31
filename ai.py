"""Job Hunter AI calls. All LLM dispatch goes through agents.call_agent().

Public functions keep their old signatures so bot.py and tests don't break.
For new code, prefer agents.call_agent("<role>", prompt) directly.
"""
import json
import re

from loguru import logger

from agents import call_agent, _call_ollama, _call_deepseek, PROVIDERS
import db_postgres as db

MAX_RESUME_CHARS = 5000
MAX_RESUME_EXTRACT_CHARS = 12000
MAX_VACANCY_CHARS = 3000


def _extract_json(text: str) -> dict:
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError(f"No JSON found in response: {text[:200]}")
    return json.loads(match.group())


# ── Backward-compat shims (used by existing tests + a few callers) ─────────────

async def _ollama_generate(model: str, prompt: str, temperature: float = 0.3) -> str:
    return await _call_ollama(prompt, model, temperature)


async def ollama_filter(prompt: str) -> bool:
    """YES/NO vacancy filter — dispatches via is_pm_filter agent."""
    try:
        answer = await call_agent("is_pm_filter", prompt)
        logger.debug(f"Filter answer: {answer[:50]}")
        return "YES" in answer.upper()
    except Exception as e:
        logger.error(f"Filter error: {e}")
        return False


async def ollama_chat(prompt: str, temperature: float = 0.3) -> str:
    """Legacy entry point. New code should call_agent() directly with a role."""
    return await call_agent("vacancy_analyzer", prompt, temperature=temperature)


# ── Public API ─────────────────────────────────────────────────────────────────

async def analyze_vacancy(vacancy_text: str, profile: dict) -> dict:
    from prompts import DEEPSEEK_ANALYZE
    prompt = DEEPSEEK_ANALYZE.format(
        active_profile=json.dumps(profile, ensure_ascii=False, indent=2),
        vacancy_text=vacancy_text[:MAX_VACANCY_CHARS],
    )
    raw = await call_agent("vacancy_analyzer", prompt)
    return _extract_json(raw)


def _profile_to_text(profile: dict) -> str:
    lines = []
    lines.append(f"Роль: {profile.get('target_role', '')}")
    lines.append(f"Опыт: {profile.get('experience_years', '')} лет, уровень: {profile.get('level', '')}")
    lines.append(f"Команда: до {profile.get('team_size', '')}, бюджеты до {profile.get('budget', '')}")
    lines.append(f"Домены: {', '.join(profile.get('domains', []))}")
    lines.append(f"Навыки: {', '.join(profile.get('key_skills', []))}")
    lines.append(f"Стек: {', '.join(profile.get('tech_stack', []))}")
    lines.append(f"Методологии: {', '.join(profile.get('methodologies', []))}")
    lines.append(f"Языки: {', '.join(profile.get('languages', []))}")
    lines.append("")
    lines.append("Ключевые достижения (из ВСЕХ компаний — выбери наиболее релевантное):")
    for ach in profile.get("key_achievements", []):
        lines.append(f"• {ach}")
    lines.append("")
    lines.append(f"Summary: {profile.get('summary', '')}")
    return "\n".join(lines)


async def _get_resume_text() -> str:
    """Возвращает лучший доступный текст резюме. Приоритет: raw_text > active_profile JSON."""
    raw = await db.get_setting("resume_text")
    if raw and len(raw) > 500:
        return raw
    profile_str = await db.get_setting("active_profile")
    if profile_str:
        return profile_str
    return ""


def format_cover_letter(text: str) -> str:
    text = text.strip()
    # Отделить приветствие от основного текста
    text = text.replace("Добрый день!", "Добрый день!\n\n")
    # Убрать тройные переносы если появились
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


async def generate_cover_letter(vacancy_text: str, profile: dict) -> str:
    from prompts import COVER_LETTER_PROMPT
    vt = vacancy_text[:MAX_VACANCY_CHARS]
    resume_text = await _get_resume_text()
    cover_examples = await db.get_cover_examples(limit=3)
    prompt = COVER_LETTER_PROMPT.format(
        resume_text=resume_text[:MAX_RESUME_CHARS],
        vacancy_text=vt,
        cover_examples=cover_examples or "Нет сохранённых примеров.",
    )
    return format_cover_letter(await call_agent("letter_writer", prompt))


async def generate_cover_letter_short(vacancy_text: str, profile: dict) -> str:
    from prompts import COVER_LETTER_SHORT_PROMPT
    vt = vacancy_text[:MAX_VACANCY_CHARS]
    resume_text = await _get_resume_text()
    prompt = COVER_LETTER_SHORT_PROMPT.format(
        resume_text=resume_text[:MAX_RESUME_CHARS],
        vacancy_text=vt,
    )
    return format_cover_letter(await call_agent("letter_writer", prompt))


async def generate_interview_prep(vacancy_text: str, profile: dict) -> str:
    from prompts import DEEPSEEK_INTERVIEW_PREP
    prompt = DEEPSEEK_INTERVIEW_PREP.format(
        active_profile=json.dumps(profile, ensure_ascii=False, indent=2),
        vacancy_text=vacancy_text[:MAX_VACANCY_CHARS],
    )
    return await call_agent("interview_coach", prompt)


async def extract_profile(resume_text: str) -> dict:
    from prompts import DEEPSEEK_EXTRACT_PROFILE
    prompt = DEEPSEEK_EXTRACT_PROFILE.format(
        resume_text=resume_text[:MAX_RESUME_EXTRACT_CHARS]
    )
    raw = await call_agent("profile_extractor", prompt)
    return _extract_json(raw)


async def prepare_interview(vacancy_text: str, resume_text: str, history: list[dict]) -> str:
    from prompts import INTERVIEW_PREP_PROMPT
    history_parts = []
    for h in history[:5]:
        company = h.get("company", "—")
        notes = h.get("notes", "")
        if notes:
            history_parts.append(f"Компания: {company}\n{notes}")
    history_text = "\n\n".join(history_parts) if history_parts else "нет истории"
    prompt = INTERVIEW_PREP_PROMPT.format(
        vacancy_text=vacancy_text[:MAX_VACANCY_CHARS],
        resume_text=resume_text[:MAX_RESUME_CHARS],
        interview_history=history_text,
    )
    return await call_agent("interview_coach", prompt)


async def adapt_resume(vacancy_text: str, resume_text: str) -> str:
    from prompts import RESUME_ADAPT_PROMPT
    prompt = RESUME_ADAPT_PROMPT.format(
        vacancy_text=vacancy_text[:MAX_VACANCY_CHARS],
        resume_text=resume_text[:MAX_RESUME_CHARS],
    )
    return await call_agent("resume_adapter", prompt)
