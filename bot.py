"""
Telegram bot — управление Job Hunter системой.
"""
import asyncio
import os
import datetime
import html
import json
import io
import re
import time

import pdfplumber
import yaml
from loguru import logger
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
from dotenv import load_dotenv

load_dotenv()

import ai
import db_postgres as db
import sheets
import hh_poller
import habr_poller
import getmatch_poller

with open("config.yaml", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or _config["telegram_bot"]["token"]
OWNER_CHAT_ID = int(_config["telegram_bot"]["chat_id"])
MULTI_USER = os.getenv("APP_ENV", "").lower() == "prod" or os.getenv("MULTI_USER", "").lower() in ("1", "true", "yes")
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "aiHANTERj_bot").lstrip("@")

PAGE_SIZE = 5
_MSK = datetime.timezone(datetime.timedelta(hours=3))
_MONTHS_RU_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

# _main_reply_keyboard() resolved via _main_reply_keyboard() at runtime

logger.add(_config["log"]["file"], rotation="10 MB", level=_config["log"]["level"])


def score_emoji(score) -> str:
    if score is None:
        return "⬜"
    score = int(score)
    if score >= 8:
        return "🟢"
    if score >= 6:
        return "🟡"
    return "🔴"


def format_salary(salary) -> str:
    if not salary:
        return "Не указана"
    if isinstance(salary, dict):
        parts = []
        if salary.get("from"):
            parts.append(f"от {salary['from']:,}")
        if salary.get("to"):
            parts.append(f"до {salary['to']:,}")
        currency = {"RUR": "₽", "USD": "$", "EUR": "€"}.get(salary.get("currency", ""), salary.get("currency", ""))
        return f"{' '.join(parts)} {currency}".strip() or "Не указана"
    return str(salary) if salary else "Не указана"


def _format_date_ru(dt: datetime.datetime) -> str:
    return f"{dt.day} {_MONTHS_RU_GEN[dt.month - 1]}"


def _followup_text(title: str) -> str:
    return (
        f"Добрый день! Хотел уточнить статус рассмотрения моей кандидатуры "
        f"на позицию {title or 'данную позицию'}. "
        f"Готов ответить на вопросы — когда вам удобно?"
    )


def _is_followup_reminder_message(query) -> bool:
    """True for push reminder cards; False for main menu / other UIs."""
    try:
        text = (query.message.text or "") if query.message else ""
    except Exception:
        return False
    return text.startswith("⏰") or "Что сделать?" in text


async def _ack_followup_action(query, text: str) -> None:
    """Ack without wiping the main menu when action came from menu buttons."""
    if _is_followup_reminder_message(query):
        try:
            await query.edit_message_text(text)
            return
        except Exception:
            pass
    # From menu / other screens: keep keyboard, just reply
    try:
        await query.message.reply_text(text)
    except Exception:
        pass


async def _stats_funnel_text() -> str:
    s = await db.get_stats_30days()
    total    = s.get("total", 0)
    pending  = s.get("pending", 0)
    filtered = s.get("filtered", 0)
    new_v    = s.get("new", 0)
    skipped  = s.get("skipped", 0)
    viewed   = s.get("viewed", 0)
    applied  = s.get("applied", 0)
    interview = s.get("interview", 0)
    offer    = s.get("offer", 0)

    threshold = await db.get_threshold()

    lines = [f"📊 Воронка за 30 дней  (порог AI: {threshold}/10)\n"]

    # --- intake block ---
    lines.append(f"📥 Поступило всего:    {total}")
    lines.append(f"   ✅ Оценено AI:       {new_v + skipped}  →  подходящих {new_v}, мало {skipped}")
    if pending:
        lines.append(f"   ⏳ В очереди оценки: {pending}  ← не обработано n8n")
    lines.append(f"   🚫 Отфильтровано:    {filtered}  (не IT роль)")

    lines.append("")

    # --- funnel block ---
    BAR_WIDTH = 12
    funnel_stages = [
        ("🎯 Подходящих", new_v),
        ("👁 Просмотрено", viewed),
        ("📨 Откликнулся", applied),
        ("🤝 Интервью   ", interview),
        ("🏆 Оффер      ", offer),
    ]
    max_val = max((v for _, v in funnel_stages), default=1) or 1
    prev = None
    for label, val in funnel_stages:
        filled = round(val / max_val * BAR_WIDTH) if val else 0
        bar = "█" * filled + "·" * (BAR_WIDTH - filled)
        pct_str = f"  ({round(val / prev * 100)}%)" if (prev and prev > 0) else ""
        lines.append(f"{label}  {bar}  {val}{pct_str}")
        if val > 0:
            prev = val

    if applied > 0:
        conv_pct = round(interview / applied * 100) if applied else 0
        marker = "👆 выше рынка" if conv_pct > 15 else ("📉 ниже рынка" if conv_pct < 15 else "= рынок")
        lines.append(f"\n💡 Конверсия отклик→интервью: {conv_pct}%  ({marker}, рынок ~15%)")

    if pending > 10:
        lines.append(f"\n⚠️ {pending} вакансий застряло в очереди — запусти /retryscoring")

    return "\n".join(lines)


def _fmt_money(v: int) -> str:
    return f"{v:,}".replace(",", " ")


async def _send_market_analysis(bot, chat_id: int):
    salary_stats = await db.get_salary_stats(days=30)
    top_companies = await db.get_top_companies(days=30, limit=5)
    source_stats = await db.get_source_stats(days=30)

    role = "вакансии"
    try:
        tok = db.set_request_chat_id(int(chat_id)) if hasattr(db, "set_request_chat_id") else None
        try:
            profile = await db.get_active_profile()
            if isinstance(profile, dict) and (profile.get("target_role") or "").strip():
                role = str(profile.get("target_role")).strip()
        finally:
            if tok is not None:
                db.reset_request_chat_id(tok)
    except Exception:
        pass

    total = sum(source_stats.values()) if source_stats else 0
    text = f"📈 Анализ рынка — {role}\n\nЗа последние 30 дней бот собрал {total} вакансий.\n\n"

    if salary_stats.get("count", 0) > 0:
        text += (
            f"💰 Зарплаты (где указаны, {salary_stats['count']} вак.):\n"
            f"Медиана:    {_fmt_money(salary_stats['median'])} ₽\n"
            f"Минимум:    {_fmt_money(salary_stats['min'])} ₽\n"
            f"Максимум:   {_fmt_money(salary_stats['max'])} ₽\n"
            f"Чаще всего: {_fmt_money(salary_stats['range_from'])} — {_fmt_money(salary_stats['range_to'])} ₽\n\n"
        )
    else:
        text += "💰 Данных о зарплатах недостаточно\n\n"

    if top_companies:
        text += "🏢 Топ компаний по количеству вакансий:\n"
        for i, c in enumerate(top_companies, 1):
            text += f"{i}. {c['company']} — {c['count']}\n"
        text += "\n"

    src_labels = {"hh": "hh.ru", "habr": "Хабр", "getmatch": "Getmatch", "telegram": "Telegram", "tg": "Telegram", "other": "Другое"}
    if source_stats:
        text += "📊 По источникам:\n"
        for src, cnt in sorted(source_stats.items(), key=lambda x: -x[1]):
            text += f"{src_labels.get(src, src)}: {cnt} вакансий\n"

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_main")]]),
    )


async def send_short_card(bot, chat_id: int, vacancy: dict):
    salary = format_salary(vacancy.get("salary"))
    channel = vacancy.get("channel") or vacancy.get("source", "—")
    external_id = vacancy.get("external_id", str(vacancy.get("id", "")))
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 {vacancy.get('title', '—')}\n"
        f"🏢 {vacancy.get('company', '—')}\n"
        f"💰 {salary}\n"
        f"🔗 {channel}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👁 Подробнее", callback_data=f"detail_{external_id}"),
            InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{external_id}"),
        ]
    ])
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


# ── Auth guard ─────────────────────────────────────────────────────────────────

async def _is_allowed(chat_id: int) -> bool:
    if chat_id == OWNER_CHAT_ID:
        return True
    if not MULTI_USER:
        return chat_id == OWNER_CHAT_ID
    try:
        return await db.is_registered_user(chat_id)
    except Exception as e:
        logger.error(f"auth check failed: {e}")
        return False


async def _broadcast(bot, text: str, **kwargs):
    """Send to all active users in multi-user mode, else owner only."""
    if MULTI_USER:
        try:
            ids = await db.list_active_user_chat_ids()
        except Exception:
            ids = [OWNER_CHAT_ID]
        if OWNER_CHAT_ID not in ids:
            ids.append(OWNER_CHAT_ID)
    else:
        ids = [OWNER_CHAT_ID]
    for cid in ids:
        try:
            await bot.send_message(chat_id=cid, text=text, **kwargs)
        except Exception as e:
            logger.warning(f"broadcast to {cid} failed: {e}")


async def _with_chat(chat_id: int, coro_factory):
    token = db.set_request_chat_id(chat_id)
    try:
        return await coro_factory()
    finally:
        db.reset_request_chat_id(token)


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None or not await _is_allowed(chat_id):
            if update.message:
                await update.message.reply_text(
                    "⛔ Нет доступа. Нужна регистрация по invite-ссылке."
                )
            return
        token = db.set_request_chat_id(chat_id)
        try:
            return await func(update, context)
        finally:
            db.reset_request_chat_id(token)
    wrapper.__name__ = func.__name__
    return wrapper


# ── Main menu helpers ──────────────────────────────────────────────────────────


# ── Menu UI v2 / legacy switch ────────────────────────────────────────────────
# Rollback: set JH_MENU_UI=legacy in .env and restart bot
# or /menu_old (stores setting menu_ui=legacy). Backup in /opt/job-hunter/backups/

def _menu_ui_mode() -> str:
    """v2 (default) or legacy."""
    env = (os.getenv("JH_MENU_UI") or "v2").strip().lower()
    if env in ("legacy", "old", "v1"):
        return "legacy"
    return "v2"


async def _menu_ui_mode_for_user() -> str:
    """Per-user override via setting menu_ui; else env."""
    try:
        v = await db.get_setting("menu_ui")
        if v:
            s = str(v).strip().lower()
            if s in ("legacy", "old", "v1"):
                return "legacy"
            if s in ("v2", "new"):
                return "v2"
    except Exception:
        pass
    return _menu_ui_mode()


async def _send_main_menu_legacy(bot, chat_id: int):
    stats = await db.get_stats()
    monitoring = await db.is_monitoring_active()
    mon_str = "🟢 Активен" if monitoring else "⏸ Пауза"

    try:
        fu_days = await db.get_follow_up_days_for_chat(chat_id)
    except Exception:
        fu_days = 3
    stale = await db.get_stale_applied_vacancies(days=fu_days)
    keyboard_rows = []
    attention_lines = []
    for v in stale[:3]:
        company = v.get("company") or "—"
        try:
            upd = datetime.datetime.fromisoformat(v["updated_at"])
            days_stale = (datetime.datetime.utcnow() - upd).days
        except Exception:
            days_stale = fu_days
        attention_lines.append(f"• Нет ответа от {company} ({days_stale} дн.)")
        ext = v["external_id"]
        keyboard_rows.append([
            InlineKeyboardButton(f"✉️ {company}", callback_data=f"followup_{ext}"),
            InlineKeyboardButton("⏳", callback_data=f"fu_snooze_{ext}"),
            InlineKeyboardButton("🔕", callback_data=f"closefollowup_{ext}"),
        ])

    text = "━━━━━━━━━━━━━━━━━━━━━━\n🤖 Job Hunter Bot\n"
    if attention_lines:
        text += "\n⚠️ Требует внимания:\n" + "\n".join(attention_lines) + "\n"
    text += (
        f"\nМониторинг: {mon_str}\n"
        f"📥 Новых: {stats.get('new', 0)}  "
        f"📨 Откликнулся: {stats.get('applied', 0)}  "
        f"🎯 Интервью: {stats.get('interview', 0)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard_rows.extend([
        [
            InlineKeyboardButton("📋 Все вакансии", callback_data="source_all_0"),
            InlineKeyboardButton("⭐ Избранное", callback_data="fav_list_0"),
        ],
        [
            InlineKeyboardButton("🔵 hh.ru", callback_data="source_hh_0"),
            InlineKeyboardButton("🟠 Хабр", callback_data="source_habr_0"),
            InlineKeyboardButton("🟣 GM", callback_data="source_getmatch_0"),
            InlineKeyboardButton("💬 TG", callback_data="source_tg_0"),
        ],
        [InlineKeyboardButton("🔍 Искать сейчас", callback_data="menu_search")],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"),
            InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
        ],
    ])
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard_rows))


async def _settings_text_and_keyboard_legacy():
    monitoring = await db.is_monitoring_active()
    notify_on = await db.is_notify_new_enabled()
    mon_btn = (
        InlineKeyboardButton("⏸ Пауза анализа", callback_data="s_pause")
        if monitoring
        else InlineKeyboardButton("▶️ Запустить анализ", callback_data="s_unpause")
    )
    notify_btn = (
        InlineKeyboardButton("🔕 Выключить уведомления", callback_data="s_notify_off")
        if notify_on
        else InlineKeyboardButton("🔔 Включить уведомления", callback_data="s_notify_on")
    )
    keyboard = InlineKeyboardMarkup([
        [mon_btn],
        [notify_btn],
        [
            InlineKeyboardButton("🔍 Ключевые слова", callback_data="s_kw"),
            InlineKeyboardButton("📢 Каналы", callback_data="s_ch"),
        ],
        [
            InlineKeyboardButton("📊 Порог оценки", callback_data="s_thr"),
            InlineKeyboardButton("📄 Резюме", callback_data="s_resume"),
        ],
        [InlineKeyboardButton("🤖 Агенты (модели ИИ)", callback_data="s_agents")],
        [InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")],
    ])
    mon_str = "🟢 Активен" if monitoring else "⏸ Пауза"
    notify_str = "🔔 Вкл" if notify_on else "🔕 Выкл"
    return (
        "⚙️ Настройки\n\n"
        f"Анализ вакансий: {mon_str}\n"
        f"Уведомления о новых: {notify_str}\n\n"
        "⏸ Пауза анализа — останавливает оценку новых вакансий.\n"
        "🔕 Уведомления — только пуши в Telegram; анализ может продолжаться.\n"
        "Чтобы получать пуши, нужны keywords/каналы и «Включить уведомления».\n\n"
        "↩️ Старое меню (legacy)."
    ), keyboard


async def _readiness_bits() -> tuple[bool, bool, bool, str]:
    """resume_ok, search_ok, notify_on, ready_line."""
    profile = None
    try:
        profile = await db.get_active_profile()
    except Exception:
        profile = None
    resume_ok = bool(profile)
    search_ok = False
    try:
        if "_user_search_ready" in globals():
            search_ok, _ = await _user_search_ready()
        else:
            kws = await db.get_keywords()
            ch = await db.get_active_channels()
            search_ok = bool(kws) or bool(ch)
    except Exception:
        search_ok = False
    notify_on = False
    try:
        notify_on = await db.is_notify_new_enabled()
    except Exception:
        pass
    parts = [
        ("✅" if resume_ok else "❌") + " резюме",
        ("✅" if search_ok else "❌") + " поиск",
        ("🔔" if notify_on else "🔕") + (" пуши" if notify_on else " пуши выкл"),
    ]
    return resume_ok, search_ok, notify_on, " · ".join(parts)


async def _send_main_menu_v2(bot, chat_id: int):
    stats = await db.get_stats()
    resume_ok, search_ok, notify_on, ready_line = await _readiness_bits()

    fav_n = 0
    try:
        fav_n = await db.count_favorites()
    except Exception:
        fav_n = 0

    try:
        fu_days = await db.get_follow_up_days_for_chat(chat_id)
    except Exception:
        fu_days = 3
    stale = []
    try:
        stale = await db.get_stale_applied_vacancies(days=fu_days)
    except Exception:
        stale = []

    keyboard_rows = []
    attention_lines = []
    for v in stale[:2]:
        company = v.get("company") or "—"
        try:
            upd = datetime.datetime.fromisoformat(v["updated_at"])
            days_stale = (datetime.datetime.utcnow() - upd).days
        except Exception:
            days_stale = fu_days
        attention_lines.append(f"• Нет ответа от {company} ({days_stale} дн.)")
        ext = v["external_id"]
        keyboard_rows.append([
            InlineKeyboardButton(f"✉️ {company}", callback_data=f"followup_{ext}"),
            InlineKeyboardButton("⏳", callback_data=f"fu_snooze_{ext}"),
            InlineKeyboardButton("🔕", callback_data=f"closefollowup_{ext}"),
        ])

    new_n = int(stats.get("new", 0) or 0)
    applied_n = int(stats.get("applied", 0) or 0)

    if not resume_ok:
        text = (
            "🤖 Job Hunter\n\n"
            f"Готовность: {ready_line}\n\n"
            "📄 Сначала резюме — без профиля оценка будет «в молоко».\n"
            "Пришлите PDF или текст: /resume"
        )
        keyboard_rows = [[InlineKeyboardButton("📄 Загрузить резюме", callback_data="s_resume")]]
        keyboard_rows.append([InlineKeyboardButton("🔍 Охота", callback_data="menu_search_setup")])
    elif not search_ok:
        text = (
            "🤖 Job Hunter\n\n"
            f"Готовность: {ready_line}\n\n"
            "🔍 Охота ещё не настроена.\n"
            "Добавьте ключевое слово (hh/Хабр/GM) или TG-канал — иначе искать нечего."
        )
        keyboard_rows = [[InlineKeyboardButton("⚙️ Настроить охоту", callback_data="menu_search_setup")]]
        keyboard_rows.append([InlineKeyboardButton("👤 Профиль и пуши", callback_data="menu_profile")])
    else:
        text = (
            "🤖 Job Hunter\n\n"
            f"Готовность: {ready_line}\n\n"
            f"Сейчас:\n"
            f"📥 {new_n} новых · ⭐ {fav_n} в избранном · 📨 откликов: {applied_n}\n"
        )
        if not notify_on:
            text += "\n🔕 Пуши выключены — разбор только из меню.\n"
        if attention_lines:
            text += "\n⚠️ Требует внимания:\n" + "\n".join(attention_lines) + "\n"
        text += "\nЧто сделать:"
        keyboard_rows.append([InlineKeyboardButton("📥 Разбирать новые", callback_data="source_all_0")])
        keyboard_rows.append([
            InlineKeyboardButton("🔍 Искать сейчас", callback_data="menu_search"),
            InlineKeyboardButton("⭐ Избранное", callback_data="fav_list_0"),
        ])
        keyboard_rows.append([InlineKeyboardButton("⚙️ Настроить охоту", callback_data="menu_search_setup")])
        keyboard_rows.append([
            InlineKeyboardButton("👤 Профиль и пуши", callback_data="menu_profile"),
            InlineKeyboardButton("📊 Сводка", callback_data="menu_stats"),
        ])

    await bot.send_message(chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard_rows))


async def _search_setup_text_and_keyboard():
    try:
        kws = await db.get_keywords()
    except Exception:
        kws = []
    if not isinstance(kws, list):
        kws = []
    try:
        ch = await db.get_active_channels()
    except Exception:
        ch = []
    n_kw = len(kws or [])
    n_ch = len(ch or [])
    if n_kw:
        kw_lines = "\n".join(f"• {k}" for k in (kws or [])[:8])
    else:
        kw_lines = "• пока нет — пример: delivery manager"
    if n_ch:
        ch_lines = "\n".join(f"• {c}" for c in (ch or [])[:8])
    else:
        ch_lines = "• пока нет — пример: @projects_jobs"

    text = (
        "🔍 Охота\n\n"
        "Ключевые слова (hh / Хабр / Getmatch):\n"
        f"{kw_lines}\n\n"
        "Telegram-каналы:\n"
        f"{ch_lines}\n\n"
        "Слова — для досок. Каналы — для TG. Нужно хотя бы одно из двух."
    )
    rows = [
        [
            InlineKeyboardButton("✏️ Слова", callback_data="s_kw"),
            InlineKeyboardButton("✏️ Каналы", callback_data="s_ch"),
        ],
    ]
    if n_kw == 0:
        rows.insert(0, [InlineKeyboardButton("➕ Добавить слово", callback_data="s_kw")])
    if n_ch == 0:
        rows.insert(0 if n_kw else 0, [InlineKeyboardButton("➕ Добавить канал", callback_data="s_ch")])
    rows.append([InlineKeyboardButton("🏢 Компании", callback_data="s_companies")])
    rows.append([InlineKeyboardButton("🔎 Искать сейчас", callback_data="menu_search")])
    rows.append([InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")])
    return text, InlineKeyboardMarkup(rows)



async def _companies_hub_text_and_keyboard():
    block = await db.get_companies_block()
    want = await db.get_companies_want()
    text = (
        "🏢 Компании\n\n"
        "Поиск не сужается — фильтр на вашей стороне.\n\n"
        f"🚫 Не показывать: {len(block)}\n"
        f"🎯 Целевые: {len(want)}\n\n"
        "🚫 — скрыть из ленты и пушей.\n"
        "🎯 — показывать выше остальных в ленте и дайджесте."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚫 Не показывать ({len(block)})", callback_data="s_cblock")],
        [InlineKeyboardButton(f"🎯 Целевые ({len(want)})", callback_data="s_cwant")],
        [InlineKeyboardButton("◀️ Охота", callback_data="menu_search_setup")],
    ])
    return text, kb


async def _companies_list_keyboard(kind: str):
    items = await db.get_companies_block() if kind == "block" else await db.get_companies_want()
    title = "🚫 Не показывать" if kind == "block" else "🎯 Целевые"
    hint = (
        "Вакансии этих компаний не попадут в ленту и пуши."
        if kind == "block"
        else "Эти компании поднимаются выше в ленте и в дайджесте пушей."
    )
    if items:
        lines = "\n".join(f"• {n}" for n in items[:30])
    else:
        lines = "• пока пусто"
    text = f"{title}\n\n{hint}\n\n{lines}\n\n➕ Пришлите название сообщением или «➕ Добавить».\n🗑 — удалить из списка."
    rows = []
    prefix = "s_cblk_del_" if kind == "block" else "s_cwant_del_"
    for i, name in enumerate(items[:25]):
        label = name if len(name) <= 40 else name[:37] + "..."
        rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"{prefix}{i}")])
    add_cb = "s_cblock_add" if kind == "block" else "s_cwant_add"
    rows.append([InlineKeyboardButton("➕ Добавить", callback_data=add_cb)])
    rows.append([InlineKeyboardButton("◀️ Компании", callback_data="s_companies")])
    return text, InlineKeyboardMarkup(rows)


async def _profile_text_and_keyboard():
    monitoring = await db.is_monitoring_active()
    notify_on = await db.is_notify_new_enabled()
    profile = None
    try:
        profile = await db.get_active_profile()
    except Exception:
        profile = None
    role = "—"
    if isinstance(profile, dict):
        role = profile.get("target_role") or profile.get("level") or "загружено"
    thr = "7"
    try:
        thr = str(await db.get_setting("score_threshold") or "7")
    except Exception:
        pass
    mon_btn = (
        InlineKeyboardButton("⏸ Пауза анализа", callback_data="s_pause")
        if monitoring
        else InlineKeyboardButton("▶️ Запустить анализ", callback_data="s_unpause")
    )
    notify_btn = (
        InlineKeyboardButton("🔕 Выключить пуши", callback_data="s_notify_off")
        if notify_on
        else InlineKeyboardButton("🔔 Включить пуши", callback_data="s_notify_on")
    )
    ears = "hourly"
    try:
        if hasattr(db, "get_ears_freq"):
            ears = await db.get_ears_freq()
    except Exception:
        ears = "hourly"
    ears_label = {"hourly": "раз в час", "daily": "раз в день", "twice": "2 раза в день"}.get(ears, "раз в час")
    quiet_on = True
    try:
        if hasattr(db, "get_quiet_hours"):
            quiet_on = (await db.get_quiet_hours()) != "off"
    except Exception:
        quiet_on = True
    quiet_label = "23–08 МСК" if quiet_on else "выкл"
    ttl = 7
    try:
        if hasattr(db, "get_feed_ttl_days"):
            ttl = await db.get_feed_ttl_days()
    except Exception:
        ttl = 7
    if profile:
        resume_line = f"✅ {role}"
    else:
        resume_line = "❌ не загружено — /resume"
    text = (
        "👤 Профиль и пуши\n\n"
        f"Резюме: {resume_line}\n"
        f"Пуши: {'🔔 Вкл' if notify_on else '🔕 Выкл'}\n"
        f"Частота пушей: {ears_label}\n"
        f"Тихие часы: {quiet_label}\n"
        f"Хранение: {ttl} дн.\n"
        f"Порог: {thr}/10\n"
        f"Анализ: {'🟢 Активен' if monitoring else '⏸ Пауза'}\n\n"
        "⏸ Пауза — не оценивать новые вакансии.\n"
        "🔕 Пуши — мастер-выключатель уведомлений.\n"
        "Частота — как часто присылать пачку подходящих (не по одной).\n"
        "🗂 Хранение — глубина ленты и автоочистки."
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 Резюме", callback_data="s_resume"),
            InlineKeyboardButton("👁 Смотреть профиль", callback_data="s_myprofile"),
        ],
        [notify_btn],
        [InlineKeyboardButton(f"⏱ Частота: {ears_label}", callback_data="s_ears")],
        [InlineKeyboardButton(f"🌙 Тихие часы: {quiet_label}", callback_data="s_quiet")],
        [InlineKeyboardButton(f"🗂 Хранение: {ttl} дн.", callback_data="s_ttl")],
        [
            InlineKeyboardButton("📊 Порог", callback_data="s_thr"),
            mon_btn,
        ],
        [InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")],
    ])
    return text, keyboard


async def _settings_text_and_keyboard_v2():
    """Settings hub in v2."""
    ttl = 7
    try:
        if hasattr(db, "get_feed_ttl_days"):
            ttl = await db.get_feed_ttl_days()
    except Exception:
        ttl = 7
    text = (
        "⚙️ Настройки\n\n"
        "Выберите раздел:\n\n"
        f"🗂 Хранение вакансий: {ttl} дн."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Охота (слова / каналы)", callback_data="menu_search_setup")],
        [InlineKeyboardButton("👤 Профиль и пуши", callback_data="menu_profile")],
        [InlineKeyboardButton(f"🗂 Хранение: {ttl} дн.", callback_data="s_ttl")],
        [InlineKeyboardButton("📊 Сводка", callback_data="menu_stats")],
        [InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")],
    ])
    return text, keyboard


async def _send_main_menu(bot, chat_id: int):
    mode = await _menu_ui_mode_for_user()
    if mode == "legacy":
        await _send_main_menu_legacy(bot, chat_id)
    else:
        await _send_main_menu_v2(bot, chat_id)


async def _settings_text_and_keyboard():
    mode = await _menu_ui_mode_for_user()
    if mode == "legacy":
        return await _settings_text_and_keyboard_legacy()
    return await _settings_text_and_keyboard_v2()


MAIN_REPLY_KEYBOARD_V2 = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🏠 Сегодня"), KeyboardButton("📥 Лента")],
        [KeyboardButton("🔍 Охота")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

MAIN_REPLY_KEYBOARD_LEGACY = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📋 Вакансии"), KeyboardButton("⭐ Избранное")],
        [KeyboardButton("🔍 Искать сейчас"), KeyboardButton("📊 Статистика")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def _main_reply_keyboard():
    if _menu_ui_mode() == "legacy":
        return MAIN_REPLY_KEYBOARD_LEGACY
    return MAIN_REPLY_KEYBOARD_V2



import agents as _agents



async def _parse_kw_list(raw) -> list:
    import json as _json
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    try:
        v = _json.loads(s)
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
    except Exception:
        pass
    return [p.strip() for p in s.replace("\n", ",").split(",") if p.strip()]


async def _user_search_ready(chat_id: int | None = None) -> tuple[bool, str]:
    """Ready = has keywords OR at least one channel. Returns (ok, hint)."""
    tok = None
    try:
        if chat_id is not None:
            tok = db.set_request_chat_id(int(chat_id))
        try:
            kws = await db.get_keywords()
        except Exception:
            kws = []
        if not isinstance(kws, list):
            kws = await _parse_kw_list(kws)
        try:
            ch = await db.get_active_channels()
        except Exception:
            ch = []
        n_kw = len(kws or [])
        n_ch = len(ch or [])
        if n_kw or n_ch:
            try:
                await db.set_setting("onboarding_done", "true")
            except Exception:
                pass
            return True, f"keywords={n_kw}, channels={n_ch}"
        hint = (
            "Сначала настройте поиск:\n"
            "• /addkeyword ваша_фраза — для hh / Хабр / Getmatch\n"
            "• /addchannel @канал — для Telegram\n"
            "Потом: /notify on и «Искать сейчас»."
        )
        return False, hint
    finally:
        if tok is not None:
            db.reset_request_chat_id(tok)


def _onboarding_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Ключевое слово", callback_data="s_kw")],
        [InlineKeyboardButton("➕ Канал", callback_data="s_ch")],
        [InlineKeyboardButton("🔔 Включить пуши", callback_data="s_notify_on")],
        [InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")],
    ])





async def _show_settings(bot, chat_id: int):
    text, keyboard = await _settings_text_and_keyboard()
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


async def _show_vacancy_page(bot, chat_id: int, page: int, context: ContextTypes.DEFAULT_TYPE, source: str = "all"):
    db_source = {"all": None, "hh": "hh", "tg": "tg", "habr": "habr", "getmatch": "getmatch"}.get(source)
    try:
        feed_days = await db.get_feed_ttl_days() if hasattr(db, "get_feed_ttl_days") else 7
    except Exception:
        feed_days = 7
    try:
        vacancies = await db.get_recent_vacancies(feed_days, source=db_source)
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Ошибка БД: {e}")
        return

    try:
        _block = await db.get_companies_block()
        _want = await db.get_companies_want()
    except Exception:
        _block, _want = [], []
    if _block and hasattr(db, "company_name_matches"):
        vacancies = [v for v in vacancies if not db.company_name_matches(str(v.get("company") or ""), _block)]

    scored = [v for v in vacancies if v.get("score") is not None]
    unscored = [v for v in vacancies if v.get("score") is None]
    def _want_rank(v):
        if _want and hasattr(db, "company_name_matches") and db.company_name_matches(str(v.get("company") or ""), _want):
            return 0
        return 1
    scored.sort(key=lambda v: (_want_rank(v), -(v.get("score") or 0)))
    unscored.sort(key=lambda v: _want_rank(v))
    display = scored + unscored

    total = len(display)
    source_labels = {"all": "Все", "hh": "hh.ru", "tg": "TG", "habr": "Хабр", "getmatch": "Getmatch"}
    if not total:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"📥 Пока пусто за {feed_days} дн. ({source_labels[source]})." + chr(10)
                + "Запустите поиск или подождите автообновление."
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Искать сейчас", callback_data="menu_search")],
                [InlineKeyboardButton("⚙️ Охота", callback_data="menu_search_setup")],
                [InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")],
            ]),
        )
        return

    context.user_data["last_list"] = [v["external_id"] for v in display]

    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    page_items = display[start:end]

    import html as _html
    for i, v in enumerate(page_items, start + 1):
        score = v.get("score")
        emoji = score_emoji(score)
        title = _html.escape(v.get("title") or v.get("text", "")[:50])
        company_raw = v.get("company") or "—"
        company = _html.escape(company_raw)
        try:
            if _want and hasattr(db, "company_name_matches") and db.company_name_matches(str(company_raw), _want):
                company = "🎯 " + company
        except Exception:
            pass
        salary = _html.escape(format_salary(v.get("salary")))
        url = v.get("url") or ""
        external_id = v["external_id"]
        score_str = f"{score}/10" if score is not None else "?"
        title_part = f'<a href="{url}">{title}</a>' if url else title
        status = v.get("status", "new")
        viewed = v.get("viewed", 0)
        status_badge = {
            "new": "🆕" if not viewed else "👁",
            "applied": "📤",
            "skipped": "⏭",
            "interview": "🎤",
            "offer": "🏆",
            "rejected": "❌",
        }.get(status, "🆕")
        card = (
            f"{i}. {status_badge} {emoji} {title_part}\n"
            f"🏢 {company} | 💰 {salary}\n"
            f"📊 {score_str}"
        )
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=card,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👁 Открыть", callback_data=f"detail_{external_id}"),
                    InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{external_id}"),
                ]]),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Failed to send vacancy card {external_id}: {e}")
            await bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось отобразить вакансию #{i}: {e}")

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    # Source switcher row
    src_defs = [("all", "📋 Все"), ("hh", "🔵 hh.ru"), ("habr", "🟠 Хабр"), ("getmatch", "🟣 GM"), ("tg", "💬 TG")]
    source_row = []
    for s, label in src_defs:
        if s == source:
            source_row.append(InlineKeyboardButton(f"·{label.split()[-1]}·", callback_data="noop"))
        else:
            source_row.append(InlineKeyboardButton(label, callback_data=f"source_{s}_0"))

    # Sliding window of up to 5 page numbers centred on current page
    half = 2
    win_start = max(0, min(page - half, total_pages - 5))
    win_end = min(total_pages, win_start + 5)
    page_buttons = []
    for p in range(win_start, win_end):
        if p == page:
            page_buttons.append(InlineKeyboardButton(f"·{p + 1}·", callback_data="noop"))
        else:
            page_buttons.append(InlineKeyboardButton(str(p + 1), callback_data=f"source_{source}_{p}"))

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"source_{source}_{page - 1}"))
    nav_row.extend(page_buttons)
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"source_{source}_{page + 1}"))

    await bot.send_message(
        chat_id=chat_id,
        text=f"Страница {page + 1} из {total_pages} | Всего: {total}",
        reply_markup=InlineKeyboardMarkup([
            source_row,
            nav_row,
            [InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")],
        ]),
    )


# ── /start, /menu ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    chat_id = chat.id
    _tok = db.set_request_chat_id(chat_id)
    try:
        return await _cmd_start_body(update, context, chat, user, chat_id)
    finally:
        db.reset_request_chat_id(_tok)


async def _cmd_start_body(update, context, chat, user, chat_id):
    args = context.args or []
    code = args[0] if args else ""

    if MULTI_USER:
        if await db.is_registered_user(chat_id) or chat_id == OWNER_CHAT_ID:
            if chat_id != OWNER_CHAT_ID:
                try:
                    await db.touch_user(chat_id)
                except Exception:
                    pass
            await update.message.reply_text("👋 Job Hunter — сегодня", reply_markup=_main_reply_keyboard())
            await _send_main_menu(context.bot, chat_id)
            return
        if not code:
            await update.message.reply_text(
                "🔐 Доступ по приглашению.\n"
                "Открой ссылку вида:\n"
                f"https://t.me/{BOT_USERNAME}?start=CODE\n"
                "или отправь: /start CODE"
            )
            return
        status, _ = await db.register_user_with_invite(
            chat_id,
            code,
            username=(user.username if user else None),
            first_name=(user.first_name if user else None),
        )
        messages = {
            "ok": "✅ Регистрация успешна! Добро пожаловать.",
            "already": "👋 Снова привет!",
            "invalid": "❌ Неверный invite-код.",
            "used": "❌ Этот код уже использован.",
            "full": "❌ Мест больше нет (лимит пользователей).",
            "need_code": "🔐 Нужен invite-код.",
            "bad_code": "❌ Неверный invite-код.",
        }
        await update.message.reply_text(messages.get(status, "❌ Ошибка регистрации."))
        if status in ("ok", "already"):
            await update.message.reply_text("👋 Job Hunter — сегодня", reply_markup=_main_reply_keyboard())
            await _send_main_menu(context.bot, chat_id)
        return

    if chat_id != OWNER_CHAT_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text("👋 Job Hunter — сегодня", reply_markup=_main_reply_keyboard())
    await _send_main_menu(context.bot, chat_id)


@owner_only
async def cmd_invites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OWNER_CHAT_ID:
        await update.message.reply_text("Только админ.")
        return
    codes = await db.unused_invites()
    if not codes:
        await update.message.reply_text("Свободных invite-кодов нет.")
        return
    lines = [f"https://t.me/{BOT_USERNAME}?start={c}" for c in codes]
    await update.message.reply_text("🎟 Свободные приглашения:\n\n" + "\n".join(lines))



@owner_only
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_main_menu(context.bot, update.effective_chat.id)


# ── Resume upload ──────────────────────────────────────────────────────────────

@owner_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📎 Пришли PDF или текст резюме. AI извлечёт профиль автоматически."
    )
    context.user_data["awaiting_resume"] = True


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_allowed(update.effective_chat.id):
        return
    _tok = db.set_request_chat_id(update.effective_chat.id)
    try:
        return await _wrapped_handle_document(update, context)
    finally:
        db.reset_request_chat_id(_tok)


async def _wrapped_handle_document(update, context):
    if not context.user_data.get("awaiting_resume"):
        return
    doc = update.message.document
    if not doc.mime_type == "application/pdf":
        await update.message.reply_text("Поддерживается только PDF.")
        return
    await update.message.reply_text("⏳ Читаю PDF...")
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = ""
    with pdfplumber.open(io.BytesIO(bytes(data))) as pdf:
        for page in pdf.pages:
            text += (page.extract_text() or "") + "\n"
    await _process_resume(update, context, text)


async def handle_text_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_allowed(update.effective_chat.id):
        return
    _tok = db.set_request_chat_id(update.effective_chat.id)
    try:
        return await _wrapped_handle_text_resume(update, context)
    finally:
        db.reset_request_chat_id(_tok)


async def _wrapped_handle_text_resume(update, context):
    if not context.user_data.get("awaiting_resume"):
        return
    await _process_resume(update, context, update.message.text)


def _clean_profile(profile: dict) -> dict:
    """Deduplicate arrays and remove methodologies/processes from tech_stack."""
    # Items that belong in methodologies/skills, not tech_stack
    _NOT_TECH = {
        "agile", "scrum", "kanban", "waterfall", "safe", "sre", "sdlc", "sla",
        "ci/cd", "hld", "lld", "hld/lld", "roadmap", "backlog",
        "daily stand-up", "daily standup", "sprint review", "retrospective",
        "sprint planning", "demo", "stand-up",
        "тест-кейсы", "чек-листы", "test-кейсы", "тест кейсы",
        "регрессионное тестирование", "интеграционное тестирование",
        "функциональное тестирование", "agile-ритуалы",
        "управление проектами", "управление командой", "управление бэклогом",
        "управление рисками", "управление стейкхолдерами",
    }
    tech = profile.get("tech_stack", [])
    cleaned_tech = list(dict.fromkeys(
        t for t in tech if t.lower() not in _NOT_TECH
    ))
    profile["tech_stack"] = cleaned_tech

    # Deduplicate other arrays
    for field in ("key_skills", "domains", "key_achievements", "methodologies", "languages"):
        if field in profile and isinstance(profile[field], list):
            profile[field] = list(dict.fromkeys(profile[field]))

    return profile


async def _process_resume(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    context.user_data["awaiting_resume"] = False
    import html as _html
    msg = await update.message.reply_text(
        "AI анализирует резюме...\n"
        "Обычно 15–40 сек. Не отправляйте повторно — дождитесь результата."
    )
    stop_keepalive = asyncio.Event()

    async def _keepalive():
        ticks = 0
        while not stop_keepalive.is_set():
            try:
                await asyncio.wait_for(stop_keepalive.wait(), timeout=20)
                break
            except asyncio.TimeoutError:
                ticks += 1
                try:
                    await msg.edit_text(
                        f"AI анализирует резюме... ещё работаю ({ticks * 20} сек)\n"
                        "Пожалуйста, подождите."
                    )
                except Exception:
                    pass

    ka = asyncio.create_task(_keepalive())
    try:
        logger.info(f"resume extract start chars={len(text or '')} chat={update.effective_chat.id}")
        profile = _clean_profile(await ai.extract_profile(text))
        context.user_data["pending_profile"] = profile
        context.user_data["pending_resume_text"] = text
        preview = _html.escape(json.dumps(profile, ensure_ascii=False, indent=2))
        stop_keepalive.set()
        await ka
        await msg.edit_text(
            f"Извлечённый профиль:\n\n<pre>{preview[:3500]}</pre>\n\n"
            f"Сохранить этот профиль? (текущий будет заменён)",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Сохранить", callback_data="profile_confirm_yes"),
                InlineKeyboardButton("Оставить текущий", callback_data="profile_confirm_no"),
            ]]),
        )
        logger.info("resume extract ok")
    except Exception as e:
        stop_keepalive.set()
        try:
            await ka
        except Exception:
            pass
        logger.exception(f"Resume processing error: {e}")
        err = f"Не удалось проанализировать резюме: {type(e).__name__}: {e}\nПопробуйте ещё раз (/resume)."
        try:
            await msg.edit_text(err[:4000])
        except Exception:
            await update.message.reply_text(err[:4000])



# ── /myprofile ─────────────────────────────────────────────────────────────────

@owner_only
async def cmd_myprofile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db.get_active_profile()
    if not profile:
        await update.message.reply_text("Профиль не загружен. Используй /resume.")
        return
    text = json.dumps(profile, ensure_ascii=False, indent=2)
    await update.message.reply_text(f"<pre>{text[:4000]}</pre>", parse_mode="HTML")


# ── /editprofile ───────────────────────────────────────────────────────────────

@owner_only
async def cmd_editprofile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profile = await db.get_active_profile()
    if not profile:
        await update.message.reply_text("Профиль не загружен.")
        return
    text = json.dumps(profile, ensure_ascii=False, indent=2)
    await update.message.reply_text(
        f"Пришли исправленный JSON профиля:\n\n<pre>{text[:3000]}</pre>",
        parse_mode="HTML",
    )
    context.user_data["awaiting_profile_edit"] = True


async def handle_profile_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_allowed(update.effective_chat.id):
        return
    _tok = db.set_request_chat_id(update.effective_chat.id)
    try:
        return await _wrapped_handle_profile_edit(update, context)
    finally:
        db.reset_request_chat_id(_tok)


async def _wrapped_handle_profile_edit(update, context):
    if not context.user_data.get("awaiting_profile_edit"):
        return
    try:
        new_profile = json.loads(update.message.text)
        await db.save_profile(new_profile)
        context.user_data["awaiting_profile_edit"] = False
        await update.message.reply_text("✅ Профиль обновлён.")
    except json.JSONDecodeError:
        await update.message.reply_text("❌ Невалидный JSON. Попробуй ещё раз.")


# ── Channels ───────────────────────────────────────────────────────────────────

@owner_only
async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channels = await db.get_all_channels()
    if not channels:
        await update.message.reply_text("Каналов нет.")
        return
    lines = []
    for ch in channels:
        status = "🟢" if ch["active"] else "⏸"
        title = ch.get("title") or ""
        lines.append(f"{status} {ch['username']} {title}")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Использование: /addchannel @channelname\n"
            "Или открой 📢 Каналы → ➕ Добавить и пришли @name сообщением."
        )
        return
    username = context.args[0]
    if not username.startswith("@"):
        username = "@" + username
    existed = False
    try:
        existed = await db.channel_exists(username)
    except Exception:
        existed = False
    await db.add_channel(username)
    if existed:
        msg = (
            f"✅ Канал {username} уже был — активирован.\n"
            "Подтяну посты в ленту (фильтр TG). Пуши — при высоком score."
        )
    else:
        msg = (
            f"✅ Канал {username} добавлен.\n"
            "За минуту подтяну последние посты в ленту (фильтр TG). Пуши — при высоком score."
        )
    await update.message.reply_text(msg)
    ready, _meta = await _user_search_ready()
    if ready:
        await update.message.reply_text("✅ Поиск готов. Можно «Искать сейчас» и /notify on.")



@owner_only
async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /removechannel @channelname")
        return
    username = context.args[0]
    if not username.startswith("@"):
        username = "@" + username
    if hasattr(db, "remove_channel"):
        await db.remove_channel(username)
    else:
        await db.set_channel_active(username, 0)
    await update.message.reply_text(f"🗑 Канал {username} удалён из мониторинга.")


@owner_only
async def cmd_pausechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /pausechannel @channelname")
        return
    username = context.args[0]
    if not username.startswith("@"):
        username = "@" + username
    await db.set_channel_active(username, 0)
    await update.message.reply_text(f"⏸ Канал {username} на паузе.")


# ── Keywords ───────────────────────────────────────────────────────────────────

@owner_only
async def cmd_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kws = await db.get_keywords()
    if not kws:
        await update.message.reply_text("Ключевых слов нет.")
        return
    text = "\n".join(f"• {k}" for k in kws)
    await update.message.reply_text(f"🔍 Ключевые слова hh.ru:\n{text}")


@owner_only
async def cmd_addkeyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /addkeyword Слово")
        return
    word = " ".join(context.args)
    await db.add_keyword(word)
    ready, meta = await _user_search_ready()
    extra = ""
    if ready:
        extra = "\n✅ Поиск готов. Можно «Искать сейчас» и /notify on."
    await update.message.reply_text(f"✅ Добавлено: {word}{extra}")


@owner_only
async def cmd_removekeyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /removekeyword Слово")
        return
    word = " ".join(context.args)
    await db.remove_keyword(word)
    await update.message.reply_text(f"🗑 Удалено: {word}")


async def _run_searchnow(msg, progress_callback=None):
    """Light search: never hard-crash on missing Playwright."""
    notes = []

    async def _one(name, coro):
        try:
            return await coro
        except Exception as e:
            err = str(e)
            if "playwright" in err.lower() or "Executable doesn" in err or "BrowserType" in err:
                notes.append(f"{name}: пропущен (нет браузера)")
                logger.warning(f"searchnow skip {name}: {e}")
                return 0
            notes.append(f"{name}: ошибка ({type(e).__name__})")
            logger.error(f"searchnow {name} failed: {e}")
            return 0

    hh_count, habr_count, getmatch_count = await asyncio.gather(
        _one("hh.ru", hh_poller.poll_hh(progress_callback=progress_callback)),
        _one("Хабр", habr_poller.poll_habr()),
        _one("Getmatch", getmatch_poller.poll_getmatch()),
    )
    total = (hh_count or 0) + (habr_count or 0) + (getmatch_count or 0)
    text = (
        f"Поиск завершён\n"
        f"hh.ru: {hh_count}\n"
        f"Хабр: {habr_count}\n"
        f"Getmatch: {getmatch_count}\n"
        f"Итого новых: {total}"
    )
    if notes:
        text += "\n\nПримечание:\n- " + "\n- ".join(notes)
    return text, total


@owner_only
async def cmd_searchnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = await update.effective_message.reply_text("🔍 Запускаю поиск hh.ru + Хабр + Getmatch...")

    async def progress(t: str):
        try:
            await msg.edit_text(f"🔍 {t}")
        except Exception:
            pass

    try:
        text_out, _ok = await _execute_search_now(chat.id if chat else None, progress_callback=progress)
        await msg.edit_text(
            _format_search_result(text_out),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Посмотреть", callback_data="source_all_0")],
                [InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")],
            ]),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


@owner_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.set_monitoring_active(False)
    await update.message.reply_text("⏸ Мониторинг приостановлен.")


@owner_only
async def cmd_unpause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.set_monitoring_active(True)
    await update.message.reply_text("▶️ Мониторинг возобновлён.")


@owner_only
async def cmd_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle push notifications without stopping vacancy analysis."""
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg in ("off", "0", "false", "выкл", "стоп"):
        await db.set_notify_new_enabled(False)
        await update.message.reply_text(
            "🔕 Уведомления выключены. Анализ и оценка вакансий продолжаются."
        )
    elif arg in ("on", "1", "true", "вкл", "start"):
        await db.set_notify_new_enabled(True)
        await update.message.reply_text("🔔 Уведомления включены.")
    else:
        on = await db.is_notify_new_enabled()
        state = "🔔 Вкл" if on else "🔕 Выкл"
        await update.message.reply_text(
            f"Уведомления о новых вакансиях: {state}\n"
            "Команды: /notify on  |  /notify off\n"
            "Анализ вакансий при выключении уведомлений не останавливается."
        )


@owner_only
async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        t = await db.get_threshold()
        await update.message.reply_text(f"Текущий порог: {t}. Использование: /threshold 8")
        return
    try:
        val = int(context.args[0])
        assert 1 <= val <= 10
        await db.set_threshold(val)
        await update.message.reply_text(f"✅ Порог установлен: {val}")
    except (ValueError, AssertionError):
        await update.message.reply_text("Введи число от 1 до 10.")


# ── /v — открыть вакансию по номеру из /list ──────────────────────────────────

@owner_only
async def cmd_vacancy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /v 3  (номер из /list)")
        return
    try:
        n = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Введи номер, например: /v 3")
        return

    last_list = context.user_data.get("last_list", [])
    if not last_list:
        await update.message.reply_text("Сначала открой /list")
        return
    if n < 1 or n > len(last_list):
        await update.message.reply_text(f"Номер должен быть от 1 до {len(last_list)}")
        return

    external_id = last_list[n - 1]
    vacancy = await db.get_vacancy_by_external(external_id)
    if not vacancy:
        await update.message.reply_text("Вакансия не найдена")
        return

    salary = format_salary(vacancy.get("salary"))
    title = vacancy.get("title") or "—"
    company = vacancy.get("company") or "—"
    url = vacancy.get("url") or "—"
    score = vacancy.get("score")
    score_str = f"{score}/10" if score is not None else "не оценено"

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{score_emoji(score)} {title}\n"
        f"🏢 {company}\n"
        f"💰 {salary}\n"
        f"📊 Оценка: {score_str}\n"
        f"🔗 {url}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👁 Подробнее", callback_data=f"detail_{external_id}"),
            InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{external_id}"),
        ],
    ])
    await update.message.reply_text(text, reply_markup=keyboard)


# ── /list и /stats ─────────────────────────────────────────────────────────────

_TG_MARKDOWN_RE = re.compile(r'\[[\*_]{0,2}(.+?)[\*_]{0,2}\]\([^)]*\)')
_BOLD_RE = re.compile(r'[\*_`]')


def _clean_title(raw: str) -> str:
    if not raw:
        return ""
    m = _TG_MARKDOWN_RE.search(raw)
    if m:
        return _BOLD_RE.sub("", m.group(1)).strip()
    return _BOLD_RE.sub("", raw)[:70].strip()


def _is_relevant(v: dict, kw_lower: list[str]) -> bool:
    haystack = ((v.get("title") or "") + " " + (v.get("text") or "")).lower()
    return any(kw in haystack for kw in kw_lower)


@owner_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_vacancy_page(context.bot, update.effective_chat.id, 0, context)


@owner_only
async def cmd_listhh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = int(context.args[0]) if context.args else 7
    vacancies = await db.get_recent_vacancies(days, source="hh")
    if not vacancies:
        await update.message.reply_text(f"Нет вакансий с hh.ru за последние {days} дней.")
        return
    vacancies.sort(key=lambda v: (v.get("score") is None, -(v.get("score") or 0)))
    header = f"🔍 hh.ru — последние {days} дней ({len(vacancies)} шт.):\n"
    lines = [header]
    for i, v in enumerate(vacancies[:30], 1):
        title = v.get("title") or v.get("text", "")[:60]
        company = v.get("company") or ""
        salary = v.get("salary") or ""
        score = v.get("score")
        score_str = f"{score}/10" if score is not None else "?"
        url = v.get("url") or ""
        company_str = f" · {company}" if company else ""
        salary_str = f" · {salary}" if salary else ""
        if url:
            line = f'{i}. <a href="{url}">{title}</a>{company_str}{salary_str} — {score_str}'
        else:
            line = f"{i}. {title}{company_str}{salary_str} — {score_str}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines)[:4096], parse_mode="HTML", disable_web_page_preview=True)


@owner_only
async def cmd_listgroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    days = int(context.args[0]) if context.args else 7
    vacancies = await db.get_recent_vacancies(days, source="channels")
    if not vacancies:
        await update.message.reply_text(f"Нет вакансий из каналов за последние {days} дней.")
        return

    keywords = await db.get_keywords()
    kw_lower = [k.lower() for k in keywords] + [
        "project manager", "product manager", "tpm", " pm ", "руководитель", "программ"
    ]

    relevant = [v for v in vacancies if _is_relevant(v, kw_lower)]
    relevant.sort(key=lambda v: (v.get("score") is None, -(v.get("score") or 0)))
    display = relevant[:20] or vacancies[:20]

    header = f"📢 Каналы — {days} дн. | {len(relevant)} релевантных из {len(vacancies)}:\n"
    chunks = [header]
    for v in display:
        title = _clean_title(v.get("title") or v.get("text", ""))
        score = v.get("score")
        score_str = f"{score}/10" if score is not None else "не оценено"
        channel = v.get("channel", "")
        url = v.get("url", "")
        line = f"\n[{v['status']}] {title}\n{channel} · {score_str}"
        if url:
            line += f"\n{url}"
        chunks.append(line)
    await update.message.reply_text("\n".join(chunks)[:4000])


@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _stats_funnel_text()
    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Рынок", callback_data="menu_market")],
            [InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")],
        ]),
    )


@owner_only
async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_market_analysis(context.bot, update.effective_chat.id)


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def ears_digest_job(context: ContextTypes.DEFAULT_TYPE):
    """Send batched vacancy digests per user ears_freq (hourly/daily/twice)."""
    try:
        import html as _html
        from datetime import datetime, timedelta, timezone

        msk = timezone(timedelta(hours=3))
        now = datetime.now(msk)
        ids = [OWNER_CHAT_ID]
        if MULTI_USER:
            try:
                ids = await db.list_active_user_chat_ids()
            except Exception:
                ids = [OWNER_CHAT_ID]

        for cid in ids:
            async def _one(_cid=cid):
                try:
                    if hasattr(db, "is_notify_new_for_chat"):
                        if not await db.is_notify_new_for_chat(int(_cid)):
                            return
                    elif not await db.is_notify_new_enabled():
                        return
                except Exception:
                    return

                # Quiet hours (MSK): default 23:00–08:00 when quiet_hours=on
                try:
                    qh = "on"
                    if hasattr(db, "get_quiet_hours_for_chat"):
                        qh = await db.get_quiet_hours_for_chat(int(_cid))
                    elif hasattr(db, "get_quiet_hours"):
                        qh = await db.get_quiet_hours()
                    if qh != "off" and (now.hour >= 23 or now.hour < 8):
                        return
                except Exception:
                    if now.hour >= 23 or now.hour < 8:
                        return

                freq = "hourly"
                try:
                    if hasattr(db, "get_ears_freq_for_chat"):
                        freq = await db.get_ears_freq_for_chat(int(_cid))
                    elif hasattr(db, "get_ears_freq"):
                        freq = await db.get_ears_freq()
                except Exception:
                    freq = "hourly"

                meta = {"last_sent_at": None}
                try:
                    if hasattr(db, "get_digest_meta_for_chat"):
                        meta = await db.get_digest_meta_for_chat(int(_cid)) or meta
                except Exception:
                    pass
                last = meta.get("last_sent_at")
                if last is not None and getattr(last, "tzinfo", None) is None:
                    last = last.replace(tzinfo=timezone.utc)
                last_msk = last.astimezone(msk) if last is not None else None

                due = False
                if freq == "hourly":
                    due = last_msk is None or (now - last_msk) >= timedelta(hours=1)
                    window_h = 6
                elif freq == "daily":
                    due = now.hour == 9 and (last_msk is None or last_msk.date() < now.date())
                    window_h = 36
                elif freq == "twice":
                    due = now.hour in (9, 18) and (
                        last_msk is None
                        or last_msk.date() < now.date()
                        or last_msk.hour != now.hour
                    )
                    window_h = 18
                else:
                    due = last_msk is None or (now - last_msk) >= timedelta(hours=1)
                    window_h = 6
                if not due:
                    return

                thr = 7
                try:
                    tok = db.set_request_chat_id(int(_cid))
                    try:
                        thr = int(await db.get_threshold() or 7)
                    finally:
                        db.reset_request_chat_id(tok)
                except Exception:
                    thr = 7

                since = datetime.utcnow() - timedelta(hours=window_h)
                if last is not None:
                    try:
                        since = last.replace(tzinfo=None) if last.tzinfo else last
                    except Exception:
                        pass

                items = []
                try:
                    if hasattr(db, "list_digest_candidates_for_chat"):
                        items = await db.list_digest_candidates_for_chat(
                            int(_cid), since=since, threshold=thr, limit=12
                        )
                except Exception as e:
                    logger.error(f"ears digest list {_cid}: {e}")
                    return
                try:
                    _blk = await db.get_companies_block_for_chat(int(_cid)) if hasattr(db, "get_companies_block_for_chat") else []
                    _wnt = await db.get_companies_want_for_chat(int(_cid)) if hasattr(db, "get_companies_want_for_chat") else []
                    if _blk and hasattr(db, "company_name_matches"):
                        items = [v for v in items if not db.company_name_matches(str(v.get("company") or ""), _blk)]
                    if _wnt and hasattr(db, "company_name_matches"):
                        items.sort(key=lambda v: (0 if db.company_name_matches(str(v.get("company") or ""), _wnt) else 1, -(v.get("score") or 0)))
                except Exception:
                    pass
                if not items:
                    return

                freq_l = {"hourly": "час", "daily": "день", "twice": "утро/вечер"}.get(freq, "период")
                lines = [f"🔔 Пуши ({freq_l}): {len(items)} подходящих\n"]
                for i, v in enumerate(items[:10], 1):
                    score = v.get("score") or 0
                    title = _html.escape(str(v.get("title") or "—"))
                    company = _html.escape(str(v.get("company") or "—"))
                    url = v.get("url") or ""
                    title_part = f'<a href="{url}">{title}</a>' if url else title
                    lines.append(f"{i}. {score}/10 · {title_part} · {company}")
                text = "\n".join(lines)
                await context.bot.send_message(
                    chat_id=_cid,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("📥 Лента", callback_data="source_all_0"),
                            InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main"),
                        ]
                    ]),
                )
                try:
                    if hasattr(db, "touch_digest_meta_for_chat"):
                        await db.touch_digest_meta_for_chat(
                            int(_cid), [str(x.get("external_id") or "") for x in items]
                        )
                except Exception as e:
                    logger.error(f"ears touch digest {_cid}: {e}")

            await _with_chat(cid, _one)
    except Exception as e:
        logger.error(f"ears_digest_job error: {e}")


async def morning_digest_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        ids = [OWNER_CHAT_ID]
        if MULTI_USER:
            try:
                ids = await db.list_active_user_chat_ids()
            except Exception:
                ids = [OWNER_CHAT_ID]
        for cid in ids:
            async def _one(_cid=cid):
                try:
                    if hasattr(db, "is_notify_new_for_chat"):
                        if not await db.is_notify_new_for_chat(int(_cid)):
                            return
                    elif hasattr(db, "is_notify_new_enabled"):
                        if not await db.is_notify_new_enabled():
                            return
                except Exception:
                    pass
                ystats = await db.get_yesterday_stats()
                top = await db.get_top_vacancies_by_score(hours=24, limit=3)
                top_lines = []
                for i, v in enumerate(top, 1):
                    score = v.get("score") or 0
                    title = html.escape(v.get("title") or "—")
                    company = html.escape(v.get("company") or "—")
                    url = v.get("url") or ""
                    title_part = f'<a href="{url}">{title}</a>' if url else title
                    top_lines.append(f"{i}. {score_emoji(score)} {title_part} · {company} · {score}/10")
                text = (
                    "☀️ Доброе утро!\n\n"
                    f"📊 За вчера:\n"
                    f"📥 Новых вакансий: {ystats['new']}\n"
                    f"👁 Просмотрел: {ystats['viewed']}\n"
                    f"📝 Откликнулся: {ystats['applied']}"
                )
                if top_lines:
                    text += "\n\n🔥 Топ вакансии сегодня:\n" + "\n".join(top_lines)
                await context.bot.send_message(
                    chat_id=_cid,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📋 Смотреть все", callback_data="source_all_0"),
                        InlineKeyboardButton("🏠 Меню", callback_data="menu_main"),
                    ]]),
                )
            await _with_chat(cid, _one)
    except Exception as e:
        logger.error(f"Morning digest job error: {e}")


async def followup_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        ids = [OWNER_CHAT_ID]
        if MULTI_USER:
            try:
                ids = await db.list_active_user_chat_ids()
            except Exception:
                ids = [OWNER_CHAT_ID]
        for cid in ids:
            async def _one(chat_id=cid):
                try:
                    days = await db.get_follow_up_days_for_chat(chat_id)
                except Exception:
                    days = 3
                stale = await db.get_stale_applied_vacancies(days=days)
                for v in stale:
                    external_id = str(v["external_id"])
                    company = v.get("company") or "—"
                    title = v.get("title") or "—"
                    try:
                        upd = datetime.datetime.fromisoformat(str(v["updated_at"]).replace("Z", ""))
                        days_stale = (datetime.datetime.utcnow() - upd).days
                        applied_str = _format_date_ru(upd + datetime.timedelta(hours=3))
                    except Exception:
                        days_stale = days
                        applied_str = "недавно"
                    text = (
                        f"⏰ Нет ответа от {company} ({days_stale} дн.)\n\n"
                        f"Вакансия: {title}\n"
                        f"Откликнулся: {applied_str}\n\n"
                        "Что сделать?"
                    )
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        reply_markup=InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton(
                                    "✉️ Follow-up", callback_data=f"followup_{external_id}"
                                ),
                                InlineKeyboardButton(
                                    "⏳ Ещё жду", callback_data=f"fu_snooze_{external_id}"
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "🤝 Ответили", callback_data=f"fu_interview_{external_id}"
                                ),
                                InlineKeyboardButton(
                                    "🗑 Не актуально", callback_data=f"fu_reject_{external_id}"
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "🔕 Закрыть напоминания",
                                    callback_data=f"closefollowup_{external_id}",
                                ),
                            ],
                        ]),
                    )
                    try:
                        await db.followup_mark_reminded(chat_id, external_id)
                    except Exception as e:
                        logger.warning(f"followup_mark_reminded {external_id}: {e}")
            await _with_chat(cid, _one)
    except Exception as e:
        logger.error(f"Follow-up reminder job error: {e}")


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        try:
            ttl = await db.get_feed_ttl_days() if hasattr(db, "get_feed_ttl_days") else 30
        except Exception:
            ttl = 30
        deleted = await db.cleanup_old_vacancies(days=int(ttl))
        if deleted > 0:
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=(
                    f"🗑 Автоочистка: удалено {deleted} записей старше {ttl} дн.\n"
                    f"(user_vacancies + pool; applied/interview/offer/⭐ не трогаем)."
                ),
            )
    except Exception as e:
        logger.error(f"Cleanup job error: {e}")


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /status <external_id> <статус>")
        return
    external_id, status = context.args[0], context.args[1]
    valid = {"new", "applied", "interview", "offer", "skipped", "rejected"}
    if status not in valid:
        await update.message.reply_text(f"Статус должен быть одним из: {', '.join(valid)}")
        return
    await db.update_vacancy_status(external_id, status)
    sheets.update_status(external_id, status)
    await update.message.reply_text(f"✅ Статус обновлён: {external_id} → {status}")


_SCORE_BATCH = 5  # max vacancies per retry run (Ollama mode)
_SCORE_TIMEOUT = 180  # seconds to wait for n8n to change vacancy status


async def _filter_is_deepseek() -> bool:
    """True if is_pm_filter agent is currently configured to use DeepSeek."""
    cfg = await _agents.get_agent_config("is_pm_filter")
    return cfg.get("provider") == "deepseek"


async def _send_one_and_wait(v: dict, profile: dict) -> bool:
    """Send one vacancy to n8n.
    DeepSeek: polls DB every 5s until status leaves 'pending' (fast pipeline).
    Ollama: sends and sleeps 30s (slow local model, fire-and-forget style).
    """
    ext = v["external_id"]
    text = v.get("text", "")
    payload = {
        "source": v.get("source", ""),
        "id": ext,
        "external_id": ext,
        "title": v.get("title", ""),
        "company": v.get("company", ""),
        "salary": v.get("salary", ""),
        "url": v.get("url", ""),
        "description": text,
        "message_text": text,
        "active_profile": profile,
    }
    try:
        await hh_poller._send_to_n8n(payload)
    except Exception as e:
        logger.error(f"retry_scoring send error for {ext}: {e}")
        return False

    if await _filter_is_deepseek():
        # DeepSeek is fast — poll until n8n finishes, then move on immediately
        for _ in range(_SCORE_TIMEOUT // 5):
            await asyncio.sleep(5)
            current = await db.get_vacancy_by_external(ext)
            if current and current.get("status") != "pending":
                return True
        logger.warning(f"retry_scoring timeout waiting for {ext}")
    else:
        # Ollama is slow — just give it 30s and move on (old behaviour)
        await asyncio.sleep(30)

    return True


async def _do_retry_scoring(batch: int = _SCORE_BATCH) -> dict:
    """Send up to `batch` pending vacancies to n8n, one at a time."""
    pending = await db.get_pending_vacancies()
    if not pending:
        return {"total": 0, "sent": 0}
    if batch > 0:
        pending = pending[:batch]
    profile = await db.get_active_profile() or {}
    sent = 0
    for v in pending:
        if await _send_one_and_wait(v, profile):
            sent += 1
    logger.info(f"retry_scoring: sent {sent}/{len(pending)} pending vacancies to n8n")
    return {"total": len(pending), "sent": sent}


@owner_only
async def cmd_retryscoring(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = await db.get_pending_vacancies()
    if not pending:
        await update.message.reply_text("✅ Нет вакансий в очереди — всё обработано.")
        return
    total = len(pending)
    cap = _SCORE_BATCH
    msg = await update.message.reply_text(
        f"🔄 Запускаю оценку: отправляю {min(total, cap)} из {total} вакансий в очереди...\n"
        f"(максимум {cap} за раз, остальные — в следующем цикле)"
    )
    result = await _do_retry_scoring(batch=cap)
    remaining = total - result["sent"]
    await msg.edit_text(
        f"✅ Отправлено в n8n: {result['sent']} вакансий.\n"
        f"{'В очереди ещё: ' + str(remaining) + ' — запусти /drainpending чтобы прогнать все.' if remaining > 0 else 'Очередь очищена.'}\n"
        f"Результат появится через несколько минут — проверь /stats"
    )


_drain_task: asyncio.Task | None = None


@owner_only
async def cmd_drainpending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _drain_task
    if _drain_task and not _drain_task.done():
        await update.message.reply_text("⚠️ Drain уже запущен. Подожди завершения.")
        return

    pending = await db.get_pending_vacancies()
    if not pending:
        await update.message.reply_text("✅ Очередь пуста — нечего обрабатывать.")
        return

    total = len(pending)
    is_ds = await _filter_is_deepseek()
    mode_str = "DeepSeek — следующая сразу после завершения предыдущей" if is_ds else "Ollama — 30 сек между вакансиями"
    msg = await update.message.reply_text(
        f"🚀 Запускаю полный drain: {total} вакансий.\n"
        f"Режим: {mode_str}\n"
        f"Прогресс буду присылать каждые {_SCORE_BATCH} штук."
    )

    async def _drain():
        profile = await db.get_active_profile() or {}
        sent_total = 0
        while True:
            pending = await db.get_pending_vacancies()
            if not pending:
                break
            v = pending[0]
            await _send_one_and_wait(v, profile)
            sent_total += 1
            remaining = len(await db.get_pending_vacancies())
            if sent_total % _SCORE_BATCH == 0:
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"🔄 Обработано: {sent_total}  |  в очереди ещё {remaining}",
                    )
                except Exception:
                    pass

        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"✅ Drain завершён. Всего обработано: {sent_total} вакансий.\nПроверь /stats.",
            )
        except Exception:
            pass

    _drain_task = asyncio.create_task(_drain())


async def retry_scoring_job(context: ContextTypes.DEFAULT_TYPE):
    """Auto job every 30 min.
    DeepSeek: starts a background drain (processes all pending one-by-one until empty).
    Ollama: sends up to _SCORE_BATCH vacancies with 30s gap (original behaviour).
    """
    global _drain_task
    if _drain_task and not _drain_task.done():
        return  # drain already running

    try:
        pending_before = len(await db.get_pending_vacancies())
        if not pending_before:
            return

        if await _filter_is_deepseek():
            # DeepSeek: kick off a silent drain — no per-batch Telegram noise
            async def _silent_drain():
                profile = await db.get_active_profile() or {}
                sent_total = 0
                while True:
                    pending = await db.get_pending_vacancies()
                    if not pending:
                        break
                    await _send_one_and_wait(pending[0], profile)
                    sent_total += 1
                logger.info(f"silent_drain finished: {sent_total} vacancies processed")

            _drain_task = asyncio.create_task(_silent_drain())
        else:
            # Ollama: old behaviour — small batch, report result
            result = await _do_retry_scoring(batch=_SCORE_BATCH)
            pending_after = max(0, pending_before - result["sent"])
            if result["sent"]:
                await context.bot.send_message(
                    chat_id=OWNER_CHAT_ID,
                    text=(
                        f"🔄 Авторетрай: обработано {result['sent']}/{result['total']} "
                        f"(в очереди ещё {pending_after})."
                    ),
                )
    except Exception as e:
        logger.error(f"retry_scoring_job error: {e}")


# ── Menu & navigation callbacks ────────────────────────────────────────────────

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _cid = update.effective_chat.id if update.effective_chat else None
    _tok = db.set_request_chat_id(_cid) if _cid else None
    try:
        return await _handle_menu_body(update, context)
    finally:
        if _tok is not None:
            db.reset_request_chat_id(_tok)


async def _handle_menu_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "noop":
        return

    elif data == "menu_main":
        try:
            # Prefer refreshing in-place when possible
            mode = await _menu_ui_mode_for_user()
            # Build via send helpers by deleting/editing: use send for reliability
            await _send_main_menu(context.bot, chat_id)
        except Exception as e:
            logger.exception(f"menu_main failed: {e}")
            await context.bot.send_message(chat_id=chat_id, text=f"Не удалось открыть меню: {e}")

    elif data == "menu_vacancies":
        await _show_vacancy_page(context.bot, chat_id, 0, context, source="all")

    elif data.startswith("source_"):
        # source_{all|hh|tg}_{page}
        _, src, p = data.split("_", 2)
        await _show_vacancy_page(context.bot, chat_id, int(p), context, source=src)

    elif data.startswith("vac_page_"):
        page = int(data[len("vac_page_"):])
        await _show_vacancy_page(context.bot, chat_id, page, context, source="all")

    elif data == "menu_search":
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🔍 Запускаю поиск hh.ru + Хабр + Getmatch...",
        )

        async def progress(t: str):
            try:
                await msg.edit_text(f"🔍 {t}")
            except Exception:
                pass

        try:
            text_out, _ok = await _execute_search_now(chat_id, progress_callback=progress)
            await msg.edit_text(
                _format_search_result(text_out),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Посмотреть", callback_data="source_all_0")],
                    [InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")],
                ]),
            )
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}")

    elif data == "menu_stats":
        text = await _stats_funnel_text()
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📈 Рынок", callback_data="menu_market")],
                [InlineKeyboardButton("◀️ Назад", callback_data="menu_main")],
            ]),
        )

    elif data == "menu_market":
        await _send_market_analysis(context.bot, chat_id)

    elif data == "menu_settings":
        if await _menu_ui_mode_for_user() == "v2":
            text, keyboard = await _settings_text_and_keyboard_v2()
        else:
            text, keyboard = await _settings_text_and_keyboard()
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)

    elif data == "menu_search_setup":
        text, keyboard = await _search_setup_text_and_keyboard()
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)

    elif data == "s_companies":
        context.user_data.pop("awaiting_company", None)
        text, keyboard = await _companies_hub_text_and_keyboard()
        await query.edit_message_text(text=text, reply_markup=keyboard)
    elif data == "s_cblock":
        context.user_data["awaiting_company"] = "block"
        text, keyboard = await _companies_list_keyboard("block")
        await query.edit_message_text(text=text, reply_markup=keyboard)
    elif data == "s_cwant":
        context.user_data["awaiting_company"] = "want"
        text, keyboard = await _companies_list_keyboard("want")
        await query.edit_message_text(text=text, reply_markup=keyboard)
    elif data == "s_cblock_add":
        context.user_data["awaiting_company"] = "block"
        await query.edit_message_text(
            "🚫 Пришлите название компании для чёрного списка.\n\nПример: Яндекс",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="s_cblock")]]),
        )
    elif data == "s_cwant_add":
        context.user_data["awaiting_company"] = "want"
        await query.edit_message_text(
            "🎯 Пришлите название целевой компании.\n\nПример: Т-Банк",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="s_cwant")]]),
        )
    elif data.startswith("s_cblk_del_"):
        try:
            idx = int(data.split("_")[-1])
            await db.remove_company_block(idx)
        except Exception as e:
            await query.answer(str(e)[:100], show_alert=True)
            return
        text, keyboard = await _companies_list_keyboard("block")
        await query.edit_message_text(text=text, reply_markup=keyboard)
    elif data.startswith("s_cwant_del_"):
        try:
            idx = int(data.split("_")[-1])
            await db.remove_company_want(idx)
        except Exception as e:
            await query.answer(str(e)[:100], show_alert=True)
            return
        text, keyboard = await _companies_list_keyboard("want")
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)

    elif data == "menu_profile":
        text, keyboard = await _profile_text_and_keyboard()
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard)
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)

    elif data == "s_myprofile":
        try:
            profile = await db.get_active_profile()
        except Exception:
            profile = None
        if not profile:
            await query.edit_message_text(
                "Профиль не загружен. Используй /resume.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")]]),
            )
        else:
            import json as _json
            pretty = _json.dumps(profile, ensure_ascii=False, indent=2)[:3500]
            await query.edit_message_text(
                "👤 Профиль:" + chr(10) + f"<pre>{pretty}</pre>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_profile")]]),
            )

    elif data == "s_pause":
        await db.set_monitoring_active(False)
        if await _menu_ui_mode_for_user() == "v2":
            text, keyboard = await _profile_text_and_keyboard()
        else:
            text, keyboard = await _settings_text_and_keyboard()
        await query.edit_message_text(text="⏸ Анализ на паузе:" + chr(10) + chr(10) + text, reply_markup=keyboard)
    elif data == "s_unpause":
        await db.set_monitoring_active(True)
        if await _menu_ui_mode_for_user() == "v2":
            text, keyboard = await _profile_text_and_keyboard()
        else:
            text, keyboard = await _settings_text_and_keyboard()
        await query.edit_message_text(text="▶️ Анализ возобновлён:" + chr(10) + chr(10) + text, reply_markup=keyboard)

    elif data == "s_notify_off":
        await db.set_notify_new_enabled(False)
        if await _menu_ui_mode_for_user() == "v2":
            text, keyboard = await _profile_text_and_keyboard()
        else:
            text, keyboard = await _settings_text_and_keyboard()
        await query.edit_message_text(
            text=f"🔕 Пуши выключены:\n\n{text}",
            reply_markup=keyboard,
        )

    elif data == "s_notify_on":
        await db.set_notify_new_enabled(True)
        if await _menu_ui_mode_for_user() == "v2":
            text, keyboard = await _profile_text_and_keyboard()
        else:
            text, keyboard = await _settings_text_and_keyboard()
        await query.edit_message_text(
            text=f"🔔 Пуши включены:\n\n{text}",
            reply_markup=keyboard,
        )

    elif data == "s_ttl":
        ttl = 7
        try:
            ttl = await db.get_feed_ttl_days()
        except Exception:
            pass
        def _mark(d, label):
            return ("✅ " if int(ttl) == int(d) else "") + label
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(_mark(7, "7 дней"), callback_data="s_ttl_7")],
            [InlineKeyboardButton(_mark(14, "14 дней"), callback_data="s_ttl_14")],
            [InlineKeyboardButton(_mark(30, "30 дней"), callback_data="s_ttl_30")],
            [InlineKeyboardButton("◀️ Профиль", callback_data="menu_profile")],
        ])
        await query.edit_message_text(
            "🗂 Глубина хранения вакансий\n\n"
            "Влияет на ленту и ночную автоочистку:\n"
            "вакансии старше выбранного срока удаляются\n"
            "(pending/skipped/filtered; избранное не трогаем).",
            reply_markup=kb,
        )
    elif data in ("s_ttl_7", "s_ttl_14", "s_ttl_30"):
        days = int(data.split("_")[-1])
        try:
            if hasattr(db, "set_feed_ttl_days"):
                await db.set_feed_ttl_days(days)
            else:
                await db.set_setting("feed_ttl_days", str(days))
        except Exception as e:
            await query.answer(f"Ошибка: {e}", show_alert=True)
            return
        text, keyboard = await _profile_text_and_keyboard()
        await query.edit_message_text(
            text=f"🗂 Хранение: {days} дн.\n\n{text}",
            reply_markup=keyboard,
        )


    elif data == "s_quiet":
        qh = "on"
        try:
            qh = await db.get_quiet_hours()
        except Exception:
            pass
        on = qh != "off"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(("✅ " if on else "") + "Вкл 23:00–08:00 МСК", callback_data="s_quiet_on")],
            [InlineKeyboardButton(("✅ " if not on else "") + "Выкл (круглосуточно)", callback_data="s_quiet_off")],
            [InlineKeyboardButton("◀️ Профиль", callback_data="menu_profile")],
        ])
        await query.edit_message_text(
            "🌙 Тихие часы\n\n"
            "Пока включено — пуши-дайджесты не приходят ночью (23:00–08:00 МСК).\n"
            "Утренний слот (~09:00) и дневные пуши работают как раньше.",
            reply_markup=kb,
        )
    elif data in ("s_quiet_on", "s_quiet_off"):
        enabled = data == "s_quiet_on"
        try:
            if hasattr(db, "set_quiet_hours"):
                await db.set_quiet_hours(enabled)
        except Exception as e:
            await query.answer(str(e)[:100], show_alert=True)
            return
        text, keyboard = await _profile_text_and_keyboard()
        label = "23:00–08:00 МСК" if enabled else "выкл"
        try:
            await query.edit_message_text(
                f"🌙 Тихие часы: {label}\n\n{text}",
                reply_markup=keyboard,
            )
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=f"🌙 Тихие часы: {label}", reply_markup=keyboard)

    elif data == "s_ears":
        ears = "hourly"
        try:
            ears = await db.get_ears_freq()
        except Exception:
            pass
        def _mark(code, label):
            return ("✅ " if ears == code else "") + label
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(_mark("hourly", "Раз в час"), callback_data="s_ears_hourly")],
            [InlineKeyboardButton(_mark("daily", "Раз в день"), callback_data="s_ears_daily")],
            [InlineKeyboardButton(_mark("twice", "2 раза в день"), callback_data="s_ears_twice")],
            [InlineKeyboardButton("◀️ Профиль", callback_data="menu_profile")],
        ])
        await query.edit_message_text(
            "⏱ Частота пушей\n\n"
            "Вакансии копятся и приходят пачкой (не по одной).\n"
            "• Раз в час — свежий дайджест каждый час\n"
            "• Раз в день — утром (~09:00 МСК)\n"
            "• 2 раза в день — утром и вечером (~09:00 и 18:00 МСК)\n\n"
            "Сначала включите «Пуши», потом выберите частоту.",
            reply_markup=kb,
        )
    elif data in ("s_ears_hourly", "s_ears_daily", "s_ears_twice"):
        freq = {"s_ears_hourly": "hourly", "s_ears_daily": "daily", "s_ears_twice": "twice"}[data]
        try:
            if hasattr(db, "set_ears_freq"):
                await db.set_ears_freq(freq)
            else:
                await db.set_setting("ears_freq", freq)
        except Exception as e:
            await query.answer(f"Ошибка: {e}", show_alert=True)
            return
        label = {"hourly": "раз в час", "daily": "раз в день", "twice": "2 раза в день"}[freq]
        text, keyboard = await _profile_text_and_keyboard()
        await query.edit_message_text(
            text=f"⏱ Частота пушей: {label}\n\n{text}",
            reply_markup=keyboard,
        )


    elif data == "s_kw_from_resume":
        try:
            profile = await db.get_active_profile()
        except Exception:
            profile = None
        if not profile:
            await query.answer("Сначала загрузите резюме (/resume)", show_alert=True)
            return
        added = []
        try:
            if hasattr(db, "merge_keywords_from_profile"):
                added = await db.merge_keywords_from_profile(profile) or []
            else:
                await query.answer("Функция недоступна", show_alert=True)
                return
        except Exception as e:
            await query.answer(str(e)[:100], show_alert=True)
            return
        kws = await db.get_keywords()
        if added:
            await query.answer(f"Добавлено: {len(added)}")
        else:
            await query.answer("Новых слов нет — уже всё из резюме")
        if kws:
            text = "🔍 Ваши ключевые слова hh.ru:" + chr(10) + (chr(10)).join([f"• {k}" for k in kws])
        else:
            text = "🔍 Ключевых слов пока нет."
        text += (
            chr(10) + chr(10)
            + "Слова подтягиваются из резюме при сохранении профиля."
            + chr(10) + "Можно добавить свои и удалить лишние."
            + chr(10) + chr(10) + "➕ Добавить:" + chr(10) + "/addkeyword ваша фраза"
            + chr(10) + chr(10) + "➖ Удалить: кнопка ниже или" + chr(10) + "/removekeyword фраза"
        )
        if added:
            text += chr(10) + chr(10) + "📥 Добавлено сейчас: " + ", ".join(added[:10])
        rows = []
        for i, k in enumerate(kws[:30]):
            label = k if len(k) <= 40 else k[:37] + "..."
            rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"kw_del_{i}")])
        rows.append([InlineKeyboardButton("➕ Добавить слово", callback_data="s_kw_add")])
        rows.append([InlineKeyboardButton("📥 Из резюме", callback_data="s_kw_from_resume")])
        rows.append([InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(rows))

    elif data == "s_kw":
        kws = await db.get_keywords()
        if kws:
            text = "🔍 Ваши ключевые слова hh.ru:" + chr(10) + (chr(10)).join([f"• {k}" for k in kws])
        else:
            text = "🔍 Ключевых слов пока нет."
        text += (
            chr(10) + chr(10)
            + "Слова подтягиваются из резюме при сохранении профиля."
            + chr(10) + "Можно добавить свои и удалить лишние."
            + chr(10) + chr(10) + "➕ Добавить:" + chr(10) + "/addkeyword ваша фраза"
            + chr(10) + chr(10) + "➖ Удалить: кнопка ниже или" + chr(10) + "/removekeyword фраза"
        )
        rows = []
        for i, k in enumerate(kws[:30]):
            label = k if len(k) <= 40 else k[:37] + "..."
            rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"kw_del_{i}")])
        rows.append([InlineKeyboardButton("➕ Добавить слово", callback_data="s_kw_add")])
        rows.append([InlineKeyboardButton("📥 Из резюме", callback_data="s_kw_from_resume")])
        rows.append([InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("kw_del_"):
        try:
            idx = int(data.split("_")[-1])
        except ValueError:
            await query.answer("Ошибка")
            return
        kws = await db.get_keywords()
        if 0 <= idx < len(kws):
            removed = kws[idx]
            await db.remove_keyword(removed)
            await query.answer(f"Удалено: {removed}")
        else:
            await query.answer("Уже удалено")
        kws = await db.get_keywords()
        if kws:
            text = "🔍 Ваши ключевые слова hh.ru:" + chr(10) + (chr(10)).join([f"• {k}" for k in kws])
        else:
            text = "🔍 Ключевых слов пока нет."
        text += (
            chr(10) + chr(10) + "➕ Добавить:" + chr(10) + "/addkeyword ваша фраза"
            + chr(10) + chr(10) + "➖ Удалить: кнопка ниже или" + chr(10) + "/removekeyword фраза"
        )
        rows = []
        for i, k in enumerate(kws[:30]):
            label = k if len(k) <= 40 else k[:37] + "..."
            rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"kw_del_{i}")])
        rows.append([InlineKeyboardButton("➕ Добавить слово", callback_data="s_kw_add")])
        rows.append([InlineKeyboardButton("📥 Из резюме", callback_data="s_kw_from_resume")])
        rows.append([InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(rows))


    elif data == "s_ch_add":
        context.user_data["awaiting_channel"] = True
        await query.edit_message_text(
            "📢 Пришлите username канала сообщением.\n"
            "Пример: @vacanciesbest\n\n"
            "Отмена — кнопка 🏠 Сегодня.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ К каналам", callback_data="s_ch")],
            ]),
        )
        return

    elif data == "s_kw_add":
        context.user_data["awaiting_keyword"] = True
        await query.edit_message_text(
            "🔍 Пришлите ключевое слово/фразу сообщением.\n"
            "Пример: Product Manager\n\n"
            "Отмена — кнопка 🏠 Сегодня.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ К словам", callback_data="s_kw")],
            ]),
        )
        return

    elif data == "s_ch":
        channels = await db.get_all_channels()
        lines = [("🟢" if ch["active"] else "⏸") + " " + ch["username"] for ch in channels]
        text = ("📢 Каналы:" + chr(10) + (chr(10)).join(lines)) if lines else "📢 Каналов нет."
        text += chr(10) + chr(10) + "➕ Добавить: кнопка ниже или /addchannel @name"
        text += chr(10) + "➖ Удалить: кнопка 🗑"
        rows = []
        for i, ch in enumerate(channels[:30]):
            uname = ch.get("username") or "?"
            label = uname if len(uname) <= 40 else uname[:37] + "..."
            rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"ch_del_{i}")])
        rows.append([InlineKeyboardButton("➕ Добавить канал", callback_data="s_ch_add")])
        rows.append([InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(rows))

    elif data.startswith("ch_del_"):
        try:
            idx = int(data.split("_")[-1])
        except ValueError:
            await query.answer("Ошибка")
            return
        channels = await db.get_all_channels()
        if 0 <= idx < len(channels):
            uname = channels[idx].get("username") or ""
            try:
                await db.remove_channel(uname)
                await query.answer(f"Удалено: {uname}")
            except Exception as e:
                await query.answer(f"Не удалось: {e}")
        else:
            await query.answer("Уже удалено")
        channels = await db.get_all_channels()
        lines = [("🟢" if ch["active"] else "⏸") + " " + ch["username"] for ch in channels]
        text = ("📢 Каналы:" + chr(10) + (chr(10)).join(lines)) if lines else "📢 Каналов нет."
        text += chr(10) + chr(10) + "➕ Добавить: кнопка ниже или /addchannel @name"
        text += chr(10) + "➖ Удалить: кнопка 🗑"
        rows = []
        for i, ch in enumerate(channels[:30]):
            uname = ch.get("username") or "?"
            label = uname if len(uname) <= 40 else uname[:37] + "..."
            rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"ch_del_{i}")])
        rows.append([InlineKeyboardButton("➕ Добавить канал", callback_data="s_ch_add")])
        rows.append([InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")])
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(rows))

    elif data == "s_thr":
        t = await db.get_threshold()
        await query.edit_message_text(
            text=f"📊 Текущий порог оценки: {t}/10\nИзменить: /threshold 8",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")]]),
        )

    elif data == "s_resume":
        await query.edit_message_text(
            text="📎 Используй /resume для загрузки нового резюме.\n/myprofile — посмотреть текущий профиль.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")]]),
        )

    elif data == "s_agents":
        roles = _agents.agent_roles()
        rows = await _agents.list_agents_effective()
        buttons = []
        for i, info in enumerate(rows):
            label = f"{info.get('label', info['role'])} — {info['provider']}/{info['model']}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"s_a_{i}")])
        buttons.append([InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")])
        await query.edit_message_text(
            text="🤖 Выбор провайдера и модели по агентам.\nТап на агента → выбери провайдера → выбери модель.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("s_a_"):
        idx = int(data[len("s_a_"):])
        roles = _agents.agent_roles()
        if not (0 <= idx < len(roles)):
            return
        role = roles[idx]
        cfg = await _agents.get_agent_config(role)
        providers = _agents.provider_names()
        buttons = []
        for pi, p in enumerate(providers):
            mark = "✅ " if p == cfg["provider"] else ""
            buttons.append([InlineKeyboardButton(f"{mark}{p}", callback_data=f"s_ap_{idx}_{pi}")])
        buttons.append([InlineKeyboardButton("↩️ Сброс к дефолту", callback_data=f"s_ar_{idx}")])
        buttons.append([InlineKeyboardButton("◀️ К агентам", callback_data="s_agents")])
        await query.edit_message_text(
            text=f"{cfg.get('label', role)}\n\nТекущий: {cfg['provider']} / {cfg['model']}\n\nВыбери провайдер:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("s_ap_"):
        _, _, idx_s, pi_s = data.split("_")
        idx, pi = int(idx_s), int(pi_s)
        roles = _agents.agent_roles()
        providers = _agents.provider_names()
        if not (0 <= idx < len(roles) and 0 <= pi < len(providers)):
            return
        role, provider = roles[idx], providers[pi]
        models = _agents.provider_models(provider)
        cfg = await _agents.get_agent_config(role)
        buttons = []
        for mi, m in enumerate(models):
            mark = "✅ " if (cfg["provider"] == provider and cfg["model"] == m) else ""
            buttons.append([InlineKeyboardButton(f"{mark}{m}", callback_data=f"s_am_{idx}_{pi}_{mi}")])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data=f"s_a_{idx}")])
        await query.edit_message_text(
            text=f"{cfg.get('label', role)}\nПровайдер: {provider}\n\nВыбери модель:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("s_am_"):
        _, _, idx_s, pi_s, mi_s = data.split("_")
        idx, pi, mi = int(idx_s), int(pi_s), int(mi_s)
        roles = _agents.agent_roles()
        providers = _agents.provider_names()
        if not (0 <= idx < len(roles) and 0 <= pi < len(providers)):
            return
        role, provider = roles[idx], providers[pi]
        models = _agents.provider_models(provider)
        if not (0 <= mi < len(models)):
            return
        model = models[mi]
        await db.set_agent_override(role, provider, model)
        # back to agent list
        rows = await _agents.list_agents_effective()
        buttons = []
        for i, info in enumerate(rows):
            label = f"{info.get('label', info['role'])} — {info['provider']}/{info['model']}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"s_a_{i}")])
        buttons.append([InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")])
        await query.edit_message_text(
            text=f"✅ {role}: {provider}/{model}\n\n🤖 Все агенты:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("s_ar_"):
        idx = int(data[len("s_ar_"):])
        roles = _agents.agent_roles()
        if not (0 <= idx < len(roles)):
            return
        role = roles[idx]
        await db.set_agent_override(role, None, None)
        rows = await _agents.list_agents_effective()
        buttons = []
        for i, info in enumerate(rows):
            label = f"{info.get('label', info['role'])} — {info['provider']}/{info['model']}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"s_a_{i}")])
        buttons.append([InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")])
        await query.edit_message_text(
            text=f"↩️ {role} сброшен к дефолту\n\n🤖 Все агенты:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


# ── Detail view callback ───────────────────────────────────────────────────────

async def handle_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _cid = update.effective_chat.id if update.effective_chat else None
    _tok = db.set_request_chat_id(_cid) if _cid else None
    try:
        return await _handle_detail_body(update, context)
    finally:
        if _tok is not None:
            db.reset_request_chat_id(_tok)



def _as_str_list(val, limit: int = 5) -> list[str]:
    out: list[str] = []
    if not val:
        return out
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    for item in val:
        if isinstance(item, dict):
            t = str(item.get("point") or item.get("detail") or item.get("text") or "").strip()
        else:
            t = str(item).strip()
        if t:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _format_vacancy_detail_card(analysis: dict, vacancy: dict | None = None) -> str:
    """Format A: verdict scan + overlap/gaps + pros/cons + severity flags."""
    vacancy = vacancy or {}
    score = analysis.get("score")
    try:
        score_i = int(score) if score is not None else None
    except Exception:
        score_i = None
    title = analysis.get("title") or vacancy.get("title") or "—"
    company = analysis.get("company") or vacancy.get("company") or "—"
    salary = analysis.get("salary") or vacancy.get("salary") or "—"
    fmt = analysis.get("format") or "—"
    source = str(vacancy.get("source") or analysis.get("source") or "").strip().lower()
    src_map = {"hh": "hh.ru", "habr": "Хабр", "getmatch": "Getmatch", "telegram": "TG", "tg": "TG"}
    src_label = src_map.get(source, source)
    channel = str(vacancy.get("channel") or "").strip()
    if source in ("telegram", "tg") and channel:
        src_label = f"TG {channel if channel.startswith('@') else '@' + channel}"

    age = ""
    raw_dt = vacancy.get("created_at") or analysis.get("created_at")
    if raw_dt:
        try:
            from datetime import datetime, timezone
            if hasattr(raw_dt, "isoformat"):
                dt = raw_dt if getattr(raw_dt, "tzinfo", None) else raw_dt.replace(tzinfo=timezone.utc)
            else:
                s = str(raw_dt).replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            sec = max(0, int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()))
            if sec < 3600:
                age = f"{max(1, sec // 60)} мин назад"
            elif sec < 86400:
                age = f"{sec // 3600} ч назад"
            else:
                age = f"{sec // 86400} дн назад"
        except Exception:
            age = ""

    verdict = str(analysis.get("verdict") or "").strip().lower()
    one = str(analysis.get("verdict_one_liner") or analysis.get("match_summary") or "").strip()
    vmap = {
        "apply": "🟢 Вердикт: откликаться",
        "maybe": "🟡 Вердикт: подумать",
        "skip": "🔴 Вердикт: скорее skip",
    }
    if verdict in vmap:
        verdict_line = vmap[verdict]
        if one:
            verdict_line += f" — {one}"
    elif one:
        verdict_line = f"🎯 {one}"
    else:
        verdict_line = ""

    overlap = _as_str_list(analysis.get("overlap"), 4)
    gaps = _as_str_list(analysis.get("gaps"), 3)
    pros = _as_str_list(analysis.get("pros"), 3)
    cons = _as_str_list(analysis.get("cons"), 2)

    flag_lines: list[str] = []
    flags = analysis.get("flags")
    if isinstance(flags, list) and flags:
        sev_icon = {"high": "🔴", "med": "🟡", "medium": "🟡", "low": "🟢"}
        for f in flags[:3]:
            if isinstance(f, dict):
                sev = str(f.get("severity") or "med").lower()
                detail = str(f.get("detail") or f.get("point") or "").strip()
                if detail:
                    flag_lines.append(f"{sev_icon.get(sev, '🟡')} {detail}")
            else:
                t = str(f).strip()
                if t:
                    flag_lines.append(f"🟡 {t}")
    else:
        for t in _as_str_list(analysis.get("red_flags"), 3):
            flag_lines.append(f"🟡 {t}")

    head_bits = [f"💰 {salary}", f"{fmt}"]
    if src_label:
        head_bits.append(src_label)
    if age:
        head_bits.append(age)

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"{score_emoji(score_i)} {score_i}/10 · {title}" if score_i is not None else f"· {title}",
        f"🏢 {company}",
        " · ".join(str(x) for x in head_bits if x and str(x) != "—"),
        "",
    ]
    if verdict_line:
        lines.append(verdict_line)
    if overlap:
        lines.append("Совпадает: " + "; ".join(overlap))
    if gaps:
        lines.append("Слабо: " + "; ".join(gaps))
    if overlap or gaps or verdict_line:
        lines.append("")

    lines.append("✅ Почему подходит")
    if pros:
        lines.extend(f"• {p}" for p in pros)
    else:
        lines.append("• —")
    lines.append("")
    lines.append("⚠️ На что смотреть")
    if cons:
        lines.extend(f"• {c}" for c in cons)
    else:
        lines.append("• —")
    lines.append("")
    lines.append("🚩 Риски")
    if flag_lines:
        lines.extend(flag_lines)
    else:
        lines.append("рисков не видно")

    url = str(vacancy.get("url") or analysis.get("url") or "").strip()
    if url:
        lines.append("")
        lines.append(f"🔗 {url}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    return chr(10).join(lines)


async def _handle_detail_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"detail answer failed: {e}")

    external_id = (query.data or "")[len("detail_"):].strip()
    chat_id = query.message.chat_id if query.message else update.effective_chat.id

    async def _reply(text: str, reply_markup=None):
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"detail edit failed ({e}); sending new message")
            await context.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup, disable_web_page_preview=True,
            )

    if not external_id:
        await _reply("Пустой ID вакансии")
        return

    try:
        await _reply("Загружаю вакансию...")
        try:
            await db.mark_viewed(external_id)
        except Exception as e:
            logger.warning(f"mark_viewed failed for {external_id}: {e}")

        vacancy = await db.get_vacancy_by_external(external_id)
        if not vacancy:
            await _reply("Вакансия не найдена. Откройте свежий список: Вакансии")
            return

        profile = await db.get_active_profile()
        if not profile:
            await _reply("Профиль не загружен. Используй /resume.")
            return

        # Only trust analysis tied to THIS user (user_vacancies). Shared/legacy analysis
        # can contain another candidate's match text ("чужое резюме").
        cached = vacancy.get("analysis")
        analysis = None
        personal = bool(vacancy.get("personal_score") is not None or vacancy.get("score") is not None)
        # get_user_vacancy maps personal_score→score; still require that we did not fall back
        # to global vacancies table. Heuristic: pool rows from MU always have source/url from join.
        if cached and personal:
            try:
                analysis = json.loads(cached) if isinstance(cached, str) else cached
                if not isinstance(analysis, dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError, TypeError):
                analysis = None

        if analysis is None:
            try:
                await _reply("Анализирую вакансию (30-60 сек)...")
                analysis = await ai.analyze_vacancy(vacancy.get("text") or vacancy.get("title") or "", profile)
                await db.update_vacancy_analysis(external_id, analysis)
            except Exception as e:
                await _reply(f"Ошибка анализа: {e}")
                return

        # Format A: refresh analysis if old schema (no verdict)


        if not analysis.get("verdict") and not analysis.get("verdict_one_liner"):


            try:


                await _reply("Обновляю сводку (формат A)...")


                analysis = await ai.analyze_vacancy(vacancy.get("text") or vacancy.get("title") or "", profile)


                await db.update_vacancy_analysis(external_id, analysis)


            except Exception as e:


                logger.warning(f"format A reanalyze failed: {e}")


        text = _format_vacancy_detail_card(analysis, vacancy)
        if len(text) > 4000:
            text = text[:3990] + "…"
        url = str(vacancy.get("url") or analysis.get("url") or "").strip()
        fav_status = await db.is_favorite(external_id)
        fav_button = (
            InlineKeyboardButton("🌟 В избранном ✓", callback_data=f"unfav_{external_id}")
            if fav_status else
            InlineKeyboardButton("⭐ Избранное", callback_data=f"fav_{external_id}")
        )
        rows = [
            [
                InlineKeyboardButton("📝 Письмо", callback_data=f"cover_{external_id}"),
                InlineKeyboardButton("❓ Подготовка", callback_data=f"prep_{external_id}"),
            ],
            [
                InlineKeyboardButton("📄 Адаптировать", callback_data=f"adapt_{external_id}"),
                fav_button,
            ],
            [
                InlineKeyboardButton("✅ Откликнулся", callback_data=f"track_{external_id}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{external_id}"),
            ],
        ]
        if url.startswith("http"):
            rows.insert(0, [InlineKeyboardButton("🔗 Открыть ссылку", url=url)])
        rows.append([InlineKeyboardButton("◀️ К списку", callback_data="menu_vacancies")])
        await _reply(text, reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        logger.exception(f"handle_detail failed for {external_id}: {e}")
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"Не удалось открыть вакансию: {type(e).__name__}: {e}")
        except Exception:
            pass



# ── Favorites ──────────────────────────────────────────────────────────────────

async def _show_favorites(bot, chat_id: int, page: int = 0):
    total = await db.count_favorites()
    if total == 0:
        await bot.send_message(
            chat_id=chat_id,
            text="⭐ Пока пусто.\n\nОткройте вакансию и нажмите ⭐ — сохранится здесь.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Меню", callback_data="menu_main"),
            ]]),
        )
        return

    vacancies = await db.get_favorites(limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    await bot.send_message(
        chat_id=chat_id,
        text=f"⭐ Избранное ({total} вакансий) — стр. {page + 1} из {pages}",
    )
    for v in vacancies:
        score = v.get("score")
        salary = format_salary(v.get("salary")) or "не указана"
        ext = v["external_id"]
        line = (
            f"{score_emoji(score)} {score}/10 | {v.get('title', '—')}\n"
            f"🏢 {v.get('company', '—')} | 💰 {salary}"
        )
        await bot.send_message(
            chat_id=chat_id,
            text=line,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👁 Открыть", callback_data=f"detail_{ext}"),
                InlineKeyboardButton("🗑 Убрать", callback_data=f"unfav_{ext}"),
            ]]),
        )

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"fav_list_{page - 1}"))
    for p in range(max(0, page - 2), min(pages, page + 3)):
        label = f"·{p + 1}·" if p == page else str(p + 1)
        nav_buttons.append(InlineKeyboardButton(label, callback_data=f"fav_list_{p}"))
    if (page + 1) * PAGE_SIZE < total:
        nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"fav_list_{page + 1}"))

    footer_rows = []
    if nav_buttons:
        footer_rows.append(nav_buttons)
    footer_rows.append([InlineKeyboardButton("🏠 Сегодня", callback_data="menu_main")])
    await bot.send_message(
        chat_id=chat_id,
        text="─────────────────────",
        reply_markup=InlineKeyboardMarkup(footer_rows),
    )


@owner_only
async def cmd_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_favorites(context.bot, update.effective_chat.id, 0)


async def handle_favorites_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _cid = update.effective_chat.id if update.effective_chat else None
    _tok = db.set_request_chat_id(_cid) if _cid else None
    try:
        return await _handle_favorites_callback_body(update, context)
    finally:
        if _tok is not None:
            db.reset_request_chat_id(_tok)


async def _handle_favorites_callback_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat_id

    if data.startswith("fav_list_"):
        await query.answer()
        page = int(data[len("fav_list_"):])
        await _show_favorites(context.bot, chat_id, page)
        return

    if data.startswith("unfav_confirm_"):
        ext = data[len("unfav_confirm_"):]
        await db.toggle_favorite(ext)
        await query.answer("Убрано из избранного")
        await query.edit_message_text(
            "✅ Убрано из избранного.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⭐ Перейти в избранное", callback_data="fav_list_0"),
            ]]),
        )
        return

    if data.startswith("unfav_cancel_"):
        await query.answer()
        try:
            await query.delete_message()
        except Exception:
            await query.edit_message_text("Отмена.")
        return

    if data.startswith("unfav_"):
        await query.answer()
        ext = data[len("unfav_"):]
        vacancy = await db.get_vacancy_by_external(ext)
        company = (vacancy.get("company") or "—") if vacancy else "—"
        await query.message.reply_text(
            f"Убрать «{company}» из избранного?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, убрать", callback_data=f"unfav_confirm_{ext}"),
                InlineKeyboardButton("❌ Отмена", callback_data=f"unfav_cancel_{ext}"),
            ]]),
        )
        return

    if data.startswith("fav_"):
        ext = data[len("fav_"):]
        is_now_fav = await db.toggle_favorite(ext)
        if is_now_fav:
            await query.answer("⭐ Добавлено в избранное")
        else:
            await query.answer("Убрано из избранного")
        fav_button = (
            InlineKeyboardButton("🌟 В избранном ✓", callback_data=f"unfav_{ext}")
            if is_now_fav else
            InlineKeyboardButton("⭐ Избранное", callback_data=f"fav_{ext}")
        )
        new_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📝 Письмо", callback_data=f"cover_{ext}"),
                InlineKeyboardButton("❓ Подготовка", callback_data=f"prep_{ext}"),
            ],
            [
                InlineKeyboardButton("📄 Адаптировать", callback_data=f"adapt_{ext}"),
                fav_button,
            ],
            [
                InlineKeyboardButton("✅ Откликнулся", callback_data=f"track_{ext}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{ext}"),
            ],
            [InlineKeyboardButton("◀️ К списку", callback_data="menu_vacancies")],
        ])
        try:
            await query.edit_message_reply_markup(reply_markup=new_keyboard)
        except Exception:
            pass


# ── Action callbacks ───────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _cid = update.effective_chat.id if update.effective_chat else None
    _tok = db.set_request_chat_id(_cid) if _cid else None
    try:
        return await _handle_callback_body(update, context)
    finally:
        if _tok is not None:
            db.reset_request_chat_id(_tok)


async def _handle_callback_body(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data:
        return

    # Profile confirmation after /resume
    if data == "profile_confirm_yes":
        profile = context.user_data.pop("pending_profile", None)
        resume_text = context.user_data.pop("pending_resume_text", None)
        if not profile:
            await query.edit_message_text("❌ Профиль не найден, загрузи резюме заново.")
            return
        await db.save_profile(profile)
        if resume_text:
            await db.save_resume_text(resume_text)
        kw_added: list[str] = []
        try:
            if hasattr(db, "merge_keywords_from_profile"):
                kw_added = await db.merge_keywords_from_profile(profile) or []
            else:
                for kw in (profile.get("role"),):
                    if isinstance(kw, str) and kw.strip():
                        await db.add_keyword(kw.strip())
                        kw_added.append(kw.strip())
                for field in ("key_skills", "tech_stack", "domains"):
                    for kw in (profile.get(field) or [])[:8]:
                        s = str(kw).strip()
                        if s:
                            before = await db.get_keywords()
                            await db.add_keyword(s)
                            if not any(b.lower() == s.lower() for b in before):
                                kw_added.append(s)
        except Exception as e:
            logger.warning(f"keywords from resume: {e}")
        # Do not spam until search is configured.
        try:
            await db.set_notify_new_enabled(False)
        except Exception:
            pass
        kw_line = ""
        if kw_added:
            shown = ", ".join(kw_added[:8])
            more = f" (+{len(kw_added) - 8})" if len(kw_added) > 8 else ""
            kw_line = (
                chr(10) + chr(10)
                + "🔍 Из резюме добавлены слова:" + chr(10)
                + shown + more + chr(10)
                + "Можно дописать вручную: /addkeyword фраза"
            )
        await query.edit_message_text(
            "✅ Резюме сохранено — шаг 1/3" + chr(10) + chr(10)
            + "Шаг 2/3 — настройте охоту (слово или канал)." + chr(10)
            + "Шаг 3/3 — включите пуши, когда будете готовы."
            + kw_line,
            reply_markup=_onboarding_keyboard(),
        )
        return

    if data == "profile_confirm_no":
        context.user_data.pop("pending_profile", None)
        context.user_data.pop("pending_resume_text", None)
        await query.edit_message_text("↩️ Оставлен текущий профиль без изменений.")
        return

    # Short cover letter (alternative variant)
    if data.startswith("cover2_"):
        external_id = data[len("cover2_"):]
        vacancy = await db.get_vacancy_by_external(external_id)
        profile = await db.get_active_profile()
        if not profile:
            await query.message.reply_text("⚠️ Профиль не загружен. Используй /resume.")
            return
        await query.message.reply_text("✍️ Генерирую короткий вариант (50-70 слов)...")
        try:
            letter = await ai.generate_cover_letter_short(vacancy["text"] if vacancy else "", profile)
            await query.message.reply_text(
                f"📝 Короткий вариант:\n\n{letter}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🔄 Ещё вариант", callback_data=f"cover2_{external_id}"),
                        InlineKeyboardButton("✏️ Редактировать", callback_data=f"editcover_{external_id}"),
                    ],
                    [
                        InlineKeyboardButton("✅ Откликнулся", callback_data=f"track_{external_id}"),
                        InlineKeyboardButton("◀️ К вакансии", callback_data=f"detail_{external_id}"),
                    ],
                ]),
            )
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка: {e}")
        return

    if data.startswith("followup_"):
        ext = data[len("followup_"):]
        chat_id = update.effective_chat.id
        v = await db.get_vacancy_by_external(ext)
        title = (v.get("title") if v else None) or "данную позицию"
        letter = _followup_text(title)
        try:
            await db.followup_snooze(chat_id, ext, days=7)
        except Exception as e:
            logger.warning(f"followup_snooze after letter: {e}")
        await query.message.reply_text(
            f"✉️ Follow-up письмо (напоминание через 7 дн.):\n\n{letter}"
        )
        if _is_followup_reminder_message(query):
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        return

    if data.startswith("fu_snooze_"):
        ext = data[len("fu_snooze_"):]
        chat_id = update.effective_chat.id
        try:
            await db.followup_snooze(chat_id, ext, days=7)
        except Exception as e:
            logger.warning(f"fu_snooze: {e}")
        await _ack_followup_action(query, "⏳ Ок, напомню через 7 дней.")
        return

    if data.startswith("fu_interview_"):
        ext = data[len("fu_interview_"):]
        await db.update_vacancy_status(ext, "interview")
        try:
            sheets.update_status(ext, "interview")
        except Exception:
            pass
        try:
            await db.followup_dismiss(update.effective_chat.id, ext)
        except Exception:
            pass
        await _ack_followup_action(query, "🤝 Статус: интервью / ответили. Напоминания выкл.")
        return

    if data.startswith("fu_reject_"):
        ext = data[len("fu_reject_"):]
        await db.update_vacancy_status(ext, "rejected")
        try:
            sheets.update_status(ext, "rejected")
        except Exception:
            pass
        try:
            await db.followup_dismiss(update.effective_chat.id, ext)
        except Exception:
            pass
        await _ack_followup_action(query, "🗑 Помечено как неактуально. Напоминания выкл.")
        return

    if data.startswith("closefollowup_"):
        ext = data[len("closefollowup_"):]
        chat_id = update.effective_chat.id
        try:
            await db.followup_dismiss(chat_id, ext)
        except Exception as e:
            logger.warning(f"closefollowup dismiss: {e}")
        await _ack_followup_action(query, "🔕 Напоминания по этой вакансии выключены.")
        return

    if data.startswith("editcover_"):
        ext = data[len("editcover_"):]
        context.user_data["awaiting_cover_edit"] = ext
        await query.message.reply_text(
            "✏️ Отправь отредактированный текст письма.\n"
            "Я сохраню его как твой стиль для будущих писем."
        )
        return

    if data.startswith("intv_done_"):
        ext = data[len("intv_done_"):]
        v = await db.get_vacancy_by_external(ext)
        company = v.get("company") or "—" if v else "—"
        context.user_data["awaiting_interview_notes"] = {"external_id": ext, "company": company}
        await query.message.reply_text(
            f"Как прошло интервью в {company}?\n"
            "Что спрашивали? Напиши кратко."
        )
        return

    if data.startswith("adapt_"):
        ext = data[len("adapt_"):]
        v = await db.get_vacancy_by_external(ext)
        if not v:
            await query.message.reply_text("❌ Вакансия не найдена")
            return
        resume_text_raw = await db.get_setting("resume_text") or ""
        if not resume_text_raw:
            await query.message.reply_text("⚠️ Резюме не загружено. Используй /resume.")
            return
        company = v.get("company") or "—"
        await query.message.reply_text(f"📄 Анализирую резюме для {company}...")
        try:
            result = await ai.adapt_resume(v.get("text") or "", resume_text_raw)
            await query.message.reply_text(
                f"📄 Рекомендации по резюме для {company}\n\n{result}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ К вакансии", callback_data=f"detail_{ext}"),
                ]]),
            )
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка: {e}")
        return

    action, _, external_id = data.partition("_")

    vacancy = await db.get_vacancy_by_external(external_id)
    vacancy_text = vacancy["text"] if vacancy else ""
    profile = await db.get_active_profile()

    if not profile:
        await query.message.reply_text("⚠️ Профиль не загружен. Используй /resume.")
        return

    if action == "cover":
        await query.message.reply_text("✍️ Генерирую письмо...")
        try:
            letter = await ai.generate_cover_letter(vacancy_text, profile)
            await query.message.reply_text(
                f"📝 Сопроводительное письмо:\n\n{letter}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🔄 Другой вариант", callback_data=f"cover2_{external_id}"),
                        InlineKeyboardButton("✏️ Редактировать", callback_data=f"editcover_{external_id}"),
                    ],
                    [
                        InlineKeyboardButton("✅ Откликнулся", callback_data=f"track_{external_id}"),
                        InlineKeyboardButton("◀️ К вакансии", callback_data=f"detail_{external_id}"),
                    ],
                ]),
            )
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка: {e}")

    elif action == "prep":
        await query.message.reply_text("🧠 Готовлю подготовку к интервью...")
        try:
            resume_text_raw = await db.get_setting("resume_text") or ""
            history = await db.get_interview_history(limit=10)
            prep = await ai.prepare_interview(vacancy_text, resume_text_raw, history)
            company = vacancy.get("company") or "—" if vacancy else "—"
            full_text = f"🎯 Подготовка к интервью — {company}\n\n{prep}"
            tg_limit = 4096
            chunks = [full_text[i:i + tg_limit] for i in range(0, len(full_text), tg_limit)]
            for i, chunk in enumerate(chunks):
                if i < len(chunks) - 1:
                    await query.message.reply_text(chunk)
                else:
                    await query.message.reply_text(
                        chunk,
                        reply_markup=InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("✅ Интервью прошло", callback_data=f"intv_done_{external_id}"),
                                InlineKeyboardButton("◀️ Назад", callback_data=f"detail_{external_id}"),
                            ],
                        ]),
                    )
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка: {e}")

    elif action == "track":
        await db.update_vacancy_status(external_id, "applied")
        try:
            sheets.update_status(external_id, "applied")
        except Exception:
            pass
        try:
            await db.followup_clear(update.effective_chat.id, external_id)
        except Exception:
            pass
        await query.message.reply_text(
            "✅ Отклик зафиксирован! Вакансия перемещена в 'applied'.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Все вакансии", callback_data="menu_vacancies")],
            ]),
        )

    elif action == "skip":
        await db.update_vacancy_status(external_id, "skipped")
        try:
            sheets.update_status(external_id, "skipped")
        except Exception:
            pass
        await query.message.reply_text(
            "⏭ Не подходит — скрыли из ленты.\n"
            "Похожие пуши можно ослабить, подняв порог: /threshold",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Все вакансии", callback_data="menu_vacancies")],
            ]),
        )


    elif action == "copy":
        await query.message.reply_text("☝️ Скопируй текст из сообщения выше.")


# ── Universal message router ───────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_allowed(update.effective_chat.id):
        return
    _tok = db.set_request_chat_id(update.effective_chat.id)
    try:
        return await _wrapped_handle_message(update, context)
    finally:
        db.reset_request_chat_id(_tok)


async def _wrapped_handle_message(update, context):
    text = (update.message.text or "").strip()



    if context.user_data.get("awaiting_channel"):
        name = text.strip()
        _menu_labels = {
            "🏠 Сегодня", "Сегодня", "🏠 Меню", "Меню",
            "📋 Вакансии", "Вакансии", "📥 Лента", "Лента",
            "⭐ Избранное", "Избранное", "🔍 Охота", "Охота",
            "🔍 Искать сейчас", "Искать сейчас", "🔍 Поиск", "Поиск",
        }
        if name in _menu_labels:
            context.user_data.pop("awaiting_channel", None)
        elif not name or name.startswith("/"):
            await update.message.reply_text("Пришлите @channel (или 🏠 Сегодня — отмена).")
            return
        else:
            context.user_data.pop("awaiting_channel", None)
            if not name.startswith("@"):
                name = "@" + name
            try:
                await db.add_channel(name)
                await update.message.reply_text(
                    f"✅ Канал {name} добавлен." + chr(10)
                    + "Telethon подхватит его в течение ~1 мин."
                )
                channels = await db.get_all_channels()
                lines = [("🟢" if ch["active"] else "⏸") + " " + ch["username"] for ch in channels]
                text_l = ("📢 Каналы:" + chr(10) + (chr(10)).join(lines)) if lines else "📢 Каналов нет."
                rows = []
                for i, ch in enumerate(channels[:30]):
                    uname = ch.get("username") or "?"
                    label = uname if len(uname) <= 40 else uname[:37] + "..."
                    rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"ch_del_{i}")])
                rows.append([InlineKeyboardButton("➕ Добавить канал", callback_data="s_ch_add")])
                rows.append([InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")])
                await update.message.reply_text(text_l, reply_markup=InlineKeyboardMarkup(rows))
            except Exception as e:
                await update.message.reply_text(f"❌ Не удалось добавить: {e}")
            return

    if context.user_data.get("awaiting_keyword"):
        word = text.strip()
        _menu_labels = {
            "🏠 Сегодня", "Сегодня", "🏠 Меню", "Меню",
            "📋 Вакансии", "Вакансии", "📥 Лента", "Лента",
            "⭐ Избранное", "Избранное", "🔍 Охота", "Охота",
            "🔍 Искать сейчас", "Искать сейчас", "🔍 Поиск", "Поиск",
        }
        if word in _menu_labels:
            context.user_data.pop("awaiting_keyword", None)
        elif not word or word.startswith("/"):
            await update.message.reply_text("Пришлите слово/фразу (или 🏠 Сегодня — отмена).")
            return
        else:
            context.user_data.pop("awaiting_keyword", None)
            try:
                await db.add_keyword(word)
                await update.message.reply_text(f"✅ Добавлено: {word}")
                kws = await db.get_keywords()
                text_k = ("🔍 Ключевые слова:" + chr(10) + (chr(10)).join(f"• {k}" for k in kws)) if kws else "🔍 Ключевых слов пока нет."
                rows = []
                for i, k in enumerate(kws[:30]):
                    label = k if len(k) <= 40 else k[:37] + "..."
                    rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"kw_del_{i}")])
                rows.append([InlineKeyboardButton("➕ Добавить слово", callback_data="s_kw_add")])
                rows.append([InlineKeyboardButton("📥 Из резюме", callback_data="s_kw_from_resume")])
                rows.append([InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")])
                await update.message.reply_text(text_k, reply_markup=InlineKeyboardMarkup(rows))
            except Exception as e:
                await update.message.reply_text(f"❌ Не удалось добавить: {e}")
            return

    if context.user_data.get("awaiting_company"):
        kind = context.user_data.get("awaiting_company")
        name = text.strip()
        _menu_labels = {
            "🏠 Сегодня", "Сегодня", "🏠 Меню", "Меню",
            "📋 Вакансии", "Вакансии", "📥 Лента", "Лента",
            "⭐ Избранное", "Избранное", "🔍 Охота", "Охота",
            "🔍 Искать сейчас", "Искать сейчас", "🔍 Поиск", "Поиск",
        }
        if name in _menu_labels:
            context.user_data.pop("awaiting_company", None)
            # fall through to normal menu handlers below
        elif not name or name.startswith("/"):
            await update.message.reply_text("Пришлите название компании текстом (или 🏠 Сегодня — отмена).")
            return
        else:
            context.user_data.pop("awaiting_company", None)
            try:
                if kind == "block":
                    items = await db.add_company_block(name)
                    label = "🚫 Не показывать"
                else:
                    items = await db.add_company_want(name)
                    label = "🎯 Целевые"
                text_l, keyboard = await _companies_list_keyboard(kind)
                await update.message.reply_text(
                    f"✅ {name} → {label} ({len(items)})\n\n{text_l}",
                    reply_markup=keyboard,
                )
            except Exception as e:
                await update.message.reply_text(f"❌ {e}")
            return
    if context.user_data.get("awaiting_resume"):
        await handle_text_resume(update, context)
        return
    if context.user_data.get("awaiting_profile_edit"):
        await handle_profile_edit(update, context)
        return

    if context.user_data.get("awaiting_cover_edit"):
        external_id = context.user_data.pop("awaiting_cover_edit")
        await db.save_cover_example(external_id, text, chat_id=update.effective_chat.id)
        await update.message.reply_text("✅ Сохранено как пример твоего стиля.")
        return

    if context.user_data.get("awaiting_interview_notes"):
        info = context.user_data.pop("awaiting_interview_notes")
        await db.save_interview(info.get("company", "—"), info.get("external_id", ""), text, [], chat_id=update.effective_chat.id)
        await update.message.reply_text("✅ Сохранено. Учту это на следующих подготовках.")
        return

    if text in ("🏠 Сегодня", "Сегодня", "🏠 Меню", "Меню"):
        await update.message.reply_text("🏠", reply_markup=_main_reply_keyboard())
        await _send_main_menu(context.bot, update.effective_chat.id)
        return
    if text in ("📋 Вакансии", "Вакансии", "📥 Лента", "Лента"):
        await _show_vacancy_page(context.bot, update.effective_chat.id, 0, context, source="all")
        return
    if text in ("⭐ Избранное", "Избранное"):
        await _show_favorites(context.bot, update.effective_chat.id, 0)
        return
    if text in ("🔍 Охота", "Охота"):
        text_s, keyboard = await _search_setup_text_and_keyboard()
        await update.message.reply_text(text_s, reply_markup=keyboard)
        return
    if text in ("🔍 Искать сейчас", "Искать сейчас", "🔍 Поиск", "Поиск"):
        msg = await update.message.reply_text("🔍 Запускаю поиск hh.ru + Хабр + Getmatch...")

        async def progress(t: str):
            try:
                await msg.edit_text(f"🔍 {t}")
            except Exception:
                pass

        try:
            text_out, _ok = await _execute_search_now(update.effective_chat.id, progress_callback=progress)
            await msg.edit_text(
                _format_search_result(text_out),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Посмотреть", callback_data="source_all_0")],
                ]),
            )
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}")
    elif text in ("📊 Статистика", "Статистика", "📊 Сводка"):
        await cmd_stats(update, context)
        return
    elif text in ("⚙️ Настройки", "Настройки"):
        text_s, keyboard = await _settings_text_and_keyboard()
        await update.message.reply_text(text_s, reply_markup=keyboard)
        return


# ── Main ───────────────────────────────────────────────────────────────────────



_tg_pending_streak = 0


async def telegram_liveness_job(context=None):
    """Fresh-proxy probe: if getUpdates backlog stuck — exit for watchdog restart."""
    global _tg_pending_streak, _tg_neterr_streak
    import json
    import os
    import time
    from pathlib import Path

    import httpx

    proxy = (os.getenv("TELEGRAM_PROXY") or "").strip() or None
    path = Path(os.getenv("BOT_HEARTBEAT_PATH") or "/app/heartbeat.json")
    pending = None
    tg_ok = False
    err = None
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=15.0, trust_env=False) as client:
            r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo")
            data = r.json()
            tg_ok = bool(data.get("ok"))
            pending = (data.get("result") or {}).get("pending_update_count")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.warning(f"telegram_liveness probe failed: {err}")

    if tg_ok and isinstance(pending, int) and pending > 0:
        _tg_pending_streak += 1
    else:
        if tg_ok:
            _tg_pending_streak = 0
            _tg_neterr_streak = 0

    try:
        payload = {
            "ts": time.time(),
            "pid": os.getpid(),
            "tg_ok": tg_ok,
            "pending": pending,
            "pending_streak": _tg_pending_streak,
        }
        if err:
            payload["probe_err"] = err[:160]
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        logger.warning(f"liveness heartbeat write failed: {e}")

    limit = int(os.getenv("BOT_PENDING_STREAK_EXIT") or "3")
    if _tg_pending_streak >= limit:
        logger.error(
            f"Telegram polling stuck pending={pending} streak={_tg_pending_streak} — exit for watchdog"
        )
        os._exit(75)


async def _jh_write_heartbeat(context=None):
    """Touch heartbeat.json so watchdog knows the process is alive."""
    import json, time, os
    from pathlib import Path
    from loguru import logger as _lg
    path = Path(os.getenv("BOT_HEARTBEAT_PATH") or "/app/heartbeat.json")
    try:
        path.write_text(json.dumps({"ts": time.time(), "pid": os.getpid()}), encoding="utf-8")
        _lg.info("HEARTBEAT ok")
    except Exception as e:
        _lg.warning(f"HEARTBEAT write failed: {e}")



async def cmd_menu_old(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rollback to legacy main menu UI for this user."""
    try:
        await db.set_setting("menu_ui", "legacy")
    except Exception:
        pass
    await update.message.reply_text(
        "↩️ Включено старое меню (legacy) для вас." + chr(10)
        + "Глобально: JH_MENU_UI=legacy в .env + restart." + chr(10)
        + "Бэкап: /opt/job-hunter/backups/",
        reply_markup=MAIN_REPLY_KEYBOARD_LEGACY,
    )
    await _send_main_menu_legacy(context.bot, update.effective_chat.id)



async def cmd_menu_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await db.set_setting("menu_ui", "v2")
    except Exception:
        pass
    await update.message.reply_text("✅ Новое меню (v2).", reply_markup=MAIN_REPLY_KEYBOARD_V2)
    await _send_main_menu_v2(context.bot, update.effective_chat.id)



async def cmd_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_vacancy_page(context.bot, update.effective_chat.id, 0, context, source="all")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 Как пользоваться" + chr(10) + chr(10)
        + "1) Резюме — /resume" + chr(10)
        + "2) Охота — слова и/или TG-каналы" + chr(10)
        + "3) Пуши — в «Профиль и пуши»" + chr(10)
        + "4) «Искать сейчас» или ждать автопоиск" + chr(10)
        + "5) «Разбирать новые» / Лента" + chr(10) + chr(10)
        + "Снизу: Сегодня · Лента · Охота" + chr(10)
        + "Откат меню: /menu_old"
    )


async def post_init(app: Application):
    try:
        _jh_register_open_handler(app)
    except Exception as _e:
        from loguru import logger as _lg
        _lg.warning(f'open handler register: {_e}')
    await db.init_db()
    try:
        await _jh_write_heartbeat()
        if app.job_queue:
            app.job_queue.run_repeating(_jh_write_heartbeat, interval=300, first=60)
            app.job_queue.run_repeating(telegram_liveness_job, interval=120, first=90)
    except Exception as _hb_e:
        from loguru import logger as _lg
        _lg.warning(f'heartbeat schedule: {_hb_e}')
    await app.bot.set_my_commands([
        BotCommand("start", "Сегодня"),
        BotCommand("feed", "Лента"),
        BotCommand("searchnow", "Искать сейчас"),
        BotCommand("keywords", "Ключевые слова"),
        BotCommand("channels", "Каналы"),
        BotCommand("notify", "Пуши on/off"),
        BotCommand("help", "Как пользоваться"),
        BotCommand("menu_old", "Старое меню"),
        BotCommand("menu_new", "Новое меню"),
    ])
    logger.info("Bot started")


_tg_neterr_streak = 0


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    global _tg_neterr_streak
    err = context.error
    name = type(err).__name__ if err is not None else "Unknown"
    msg = str(err or "")
    transient = name in {
        "NetworkError",
        "TimedOut",
        "ConnectError",
        "ReadError",
        "RemoteProtocolError",
        "PoolTimeout",
        "httpx.ConnectError",
        "httpx.ReadError",
        "httpx.RemoteProtocolError",
        "httpx.PoolTimeout",
    } or any(
        x in msg
        for x in (
            "ConnectError",
            "Pool timeout",
            "All connections in the connection pool",
            "Server disconnected",
            "SSL",
            "Timed out",
            "Bad Gateway",
        )
    )
    if transient:
        _tg_neterr_streak += 1
        logger.error(f"PTB transient error ({_tg_neterr_streak}): {err}")
        limit = int(__import__("os").getenv("BOT_NETERR_STREAK_EXIT") or "12")
        if _tg_neterr_streak >= limit:
            logger.error(
                f"Too many Telegram network errors ({_tg_neterr_streak}) — exit for watchdog restart"
            )
            __import__("os")._exit(76)
        return
    _tg_neterr_streak = 0
    logger.error(f"PTB error: {err}", exc_info=err)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ Ошибка: {name}: {err}",
            )
        except Exception:
            pass


def main():
    proxy = os.getenv("TELEGRAM_PROXY", "").strip() or None
    httpx_kwargs = {"trust_env": False, "http2": False}
    if proxy:
        httpx_kwargs["proxy"] = proxy
        logger.info(f"Telegram HTTPX proxy enabled")
    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=20.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=15.0,
        httpx_kwargs=httpx_kwargs,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
        .post_init(post_init)
        .build()
    )
    app.add_error_handler(error_handler)

    app.job_queue.run_daily(
        morning_digest_job,
        time=datetime.time(9, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=3))),
    )
    app.job_queue.run_daily(
        followup_reminder_job,
        time=datetime.time(10, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=3))),
    )
    app.job_queue.run_daily(
        cleanup_job,
        time=datetime.time(3, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=3))),
    )
    app.job_queue.run_repeating(
        retry_scoring_job,
        interval=30 * 60,  # every 30 minutes
        first=60,          # first run 60s after bot start
    )

    app.job_queue.run_repeating(
        ears_digest_job,
        interval=15 * 60,
        first=90,
    )

    app.add_handler(CommandHandler("menu_old", cmd_menu_old))
    app.add_handler(CommandHandler("menu_new", cmd_menu_new))
    app.add_handler(CommandHandler("feed", cmd_feed))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("invites", cmd_invites))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("myprofile", cmd_myprofile))
    app.add_handler(CommandHandler("editprofile", cmd_editprofile))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("addchannel", cmd_addchannel))
    app.add_handler(CommandHandler("removechannel", cmd_removechannel))
    app.add_handler(CommandHandler("pausechannel", cmd_pausechannel))
    app.add_handler(CommandHandler("keywords", cmd_keywords))
    app.add_handler(CommandHandler("addkeyword", cmd_addkeyword))
    app.add_handler(CommandHandler("removekeyword", cmd_removekeyword))
    app.add_handler(CommandHandler("searchnow", cmd_searchnow))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("v", cmd_vacancy))
    app.add_handler(CommandHandler("vacancy", cmd_vacancy))
    app.add_handler(CommandHandler("listhh", cmd_listhh))
    app.add_handler(CommandHandler("listgroups", cmd_listgroups))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("retryscoring", cmd_retryscoring))
    app.add_handler(CommandHandler("drainpending", cmd_drainpending))
    app.add_handler(CommandHandler("threshold", cmd_threshold))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("unpause", cmd_unpause))
    app.add_handler(CommandHandler("notify", cmd_notify))
    app.add_handler(CommandHandler("market", cmd_market))

    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CommandHandler("fav", cmd_favorites))

    app.add_handler(CallbackQueryHandler(handle_detail, pattern="^detail_"))
    app.add_handler(CallbackQueryHandler(handle_menu, pattern="^(menu_|vac_page_|s_|source_|noop|kw_|ch_)"))
    app.add_handler(CallbackQueryHandler(handle_favorites_callback, pattern="^(fav_|unfav_)"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    retry = 0
    while True:
        try:
            app.run_polling(
                drop_pending_updates=(retry == 0),
                timeout=20,
            )
            break  # нормальное завершение
        except Exception as e:
            retry += 1
            wait = min(30 * retry, 300)
            logger.error(f"Polling упал (попытка {retry}): {e}. Переподключение через {wait}с...")
            time.sleep(wait)

# --- open vacancy MU-safe ---
async def _open_vacancy_mu(update, context, vac_id: str):
    """Resolve vacancy for current chat and show link/details. Always answer callback."""
    q = update.callback_query
    chat = update.effective_chat
    try:
        if q:
            await q.answer()
    except Exception:
        pass
    if not chat:
        return
    import db_postgres as db
    vac = None
    try:
        if hasattr(db, "get_user_vacancy"):
            vac = await db.get_user_vacancy(chat.id, vac_id)
        if not vac and hasattr(db, "get_vacancy_pool"):
            vac = await db.get_vacancy_pool(vac_id)
        if not vac and hasattr(db, "get_vacancy"):
            vac = await db.get_vacancy(vac_id)
        if not vac:
            # raw SQL fallback
            pool = await db._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT uv.*, vp.title, vp.company, vp.url, vp.link "
                    "FROM user_vacancies uv "
                    "LEFT JOIN vacancies_pool vp ON vp.id = uv.vacancy_id "
                    "WHERE uv.chat_id=$1 AND (uv.id::text=$2 OR uv.vacancy_id::text=$2) "
                    "LIMIT 1",
                    int(chat.id), str(vac_id),
                )
                if not row:
                    row = await conn.fetchrow(
                        "SELECT * FROM vacancies_pool WHERE id::text=$1 OR external_id=$1 LIMIT 1",
                        str(vac_id),
                    )
                if row:
                    vac = dict(row)
    except Exception as e:
        from loguru import logger
        logger.error(f"open vacancy: {e}")
    if not vac:
        msg = "Вакансия не найдена (возможно, устарела)."
        if q:
            try:
                await q.edit_message_text(msg)
            except Exception:
                if update.effective_message:
                    await update.effective_message.reply_text(msg)
        return
    url = (vac.get("url") or vac.get("link") or "").strip()
    title = vac.get("title") or "Вакансия"
    company = vac.get("company") or ""
    text = f"<b>{title}</b>\n{company}\n{url}".strip()
    if q:
        try:
            await q.edit_message_text(text, parse_mode="HTML", disable_web_page_preview=False)
            return
        except Exception:
            pass
    if update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="HTML")

# --- resume extract UX ---
async def _extract_resume_with_ux(update, context, pdf_text: str):
    """Run profile extraction with clear progress + always a final message."""
    import os
    from loguru import logger
    msg = update.effective_message
    status = await msg.reply_text("🤖 AI анализирует резюме... (обычно 15–40 сек, дождитесь результата) (обычно 15–40 сек)")
    # Prefer fast chat model for extraction
    old_model = os.environ.get("DEEPSEEK_PROFILE_MODEL")
    os.environ.setdefault("DEEPSEEK_PROFILE_MODEL", "deepseek-chat")
    os.environ.setdefault("PROFILE_EXTRACTOR_MODEL", "deepseek-chat")
    try:
        # Call existing extractor if present
        profile = None
        try:
            import agents  # noqa
        except Exception:
            pass
        # Prefer project-specific function names
        for mod_name, fn_name in [
            ("profile_extractor", "extract_profile"),
            ("agents.profile_extractor", "extract_profile"),
            ("resume_parser", "extract_profile"),
        ]:
            try:
                import importlib
                mod = importlib.import_module(mod_name)
                fn = getattr(mod, fn_name, None)
                if fn:
                    import asyncio as _aio
                    profile = await fn(pdf_text) if _aio.iscoroutinefunction(fn) else fn(pdf_text)
                    break
            except Exception:
                continue
        if profile is None:
            # fallback: deepseek via existing deepseek client if any
            try:
                from deepseek_client import chat_completion
                import asyncio
                prompt = "Извлеки профиль кандидата из резюме как JSON: role, skills, experience, summary.\\n\\n" + pdf_text[:12000]
                profile = await chat_completion(prompt, model="deepseek-chat")
            except Exception as e:
                logger.error(f"resume extract fallback: {e}")
                await status.edit_text(f"❌ Не удалось проанализировать резюме: {e}\\nПопробуйте ещё раз или пришлите текстом.")
                return None
        # Show preview
        preview = str(profile)[:3500]
        await status.edit_text(
            "✅ Анализ готов. Проверьте профиль и нажмите «Сохранить», если всё верно:\\n\\n" + preview
        )
        return profile
    except Exception as e:
        logger.error(f"resume UX extract: {e}")
        try:
            await status.edit_text(f"❌ Ошибка анализа резюме: {e}\\nПопробуйте ещё раз.")
        except Exception:
            await msg.reply_text(f"❌ Ошибка анализа резюме: {e}")
        return None
    finally:
        if old_model is not None:
            os.environ["DEEPSEEK_PROFILE_MODEL"] = old_model

async def _open_vacancy_bridge(update, context):
    data = (update.callback_query.data if update.callback_query else "") or ""
    vac_id = data.split("_", 1)[-1] if "_" in data else data
    await _open_vacancy_mu(update, context, vac_id)


def _jh_register_open_handler(app):
    try:
        from telegram.ext import CallbackQueryHandler
        app.add_handler(CallbackQueryHandler(_open_vacancy_bridge, pattern=r"^open_"), group=-1)
    except Exception as e:
        from loguru import logger
        logger.warning(f"register open handler: {e}")

# --- light searchnow (no Playwright) ---

def _format_search_result(text_out: str) -> str:
    """Normalize poller/light notes to PC-like search summary."""
    t = (text_out or "").strip()
    if not t:
        return "❌ Поиск не вернул результат"
    low = t.lower()
    if t.startswith("⏳") or t.startswith("✅") or t.startswith("❌"):
        return t
    if (
        "уже выполняется" in low
        or "подождите" in low
        or "не чаще" in low
        or "раз в час" in low
        or "rate limit" in low
        or "в очереди" in low
    ):
        return t if t.startswith("⏳") else ("⏳ " + t)
    hh = habr = gm = total = None
    for line in t.splitlines():
        s = line.strip()
        sl = s.lower()
        try:
            if sl.startswith("hh.ru"):
                hh = int(s.split(":")[-1].strip().split()[0])
            elif sl.startswith("хабр") or sl.startswith("habr"):
                habr = int(s.split(":")[-1].strip().split()[0])
            elif sl.startswith("getmatch"):
                gm = int(s.split(":")[-1].strip().split()[0])
            elif "итого" in sl:
                total = int(s.split(":")[-1].strip().split()[0])
        except Exception:
            pass
    if hh is not None or habr is not None or gm is not None:
        hh = int(hh or 0); habr = int(habr or 0); gm = int(gm or 0)
        if total is None: total = hh + habr + gm
        return (
            "✅ Поиск завершён" + chr(10)
            + f"📥 hh.ru: {hh}" + chr(10)
            + f"📥 Хабр: {habr}" + chr(10)
            + f"📥 Getmatch: {gm}" + chr(10)
            + f"📥 Итого: {total}"
        )
    if t.startswith("Поиск завершён"):
        return "✅ " + t
    return t


async def _execute_search_now(chat_id: int | None, progress_callback=None) -> tuple[str, bool]:
    """Prod search via Playwright poller only; never Chromium in slim bot."""
    from loguru import logger as _lg

    ready, hint = await _user_search_ready(chat_id)
    if not ready:
        return hint, False

    ok, note = await _search_now_via_poller(chat_id, progress_callback=progress_callback)
    if ok:
        return note, True

    low = (note or "").lower()
    if "уже выполняется" in low or "already running" in low or "подождите" in low:
        return note, True

    _lg.warning("poller search failed: %s", note)
    if progress_callback:
        try:
            await progress_callback("⚠️ Poller недоступен, запасной лёгкий поиск…")
        except Exception:
            pass
    try:
        _n, light_note = await _search_now_light(int(chat_id or 0))
        msg = (
            "Playwright-poller: "
            + str(note)
            + chr(10) + chr(10)
            + "Запасной лёгкий поиск (без HH Playwright): "
            + str(light_note)
        )
        return msg, True
    except Exception as e:
        msg = "Поиск недоступен." + chr(10) + "Poller: " + str(note) + chr(10) + "Light: " + str(e)
        return msg, False



async def _search_now_via_poller(chat_id: int | None = None, progress_callback=None) -> tuple[bool, str]:
    """Ask Playwright poller to run per-user search (queued); stream /progress while waiting."""
    import os
    import asyncio
    import httpx

    if chat_id is None:
        return False, "Нет chat_id — поиск только по вашим ключевым словам."

    base = (os.getenv("POLLER_URL") or "http://poller:5680").rstrip("/")
    url = base + "/searchnow"
    progress_url = base + "/progress"
    try:
        async with httpx.AsyncClient(timeout=1800.0) as client:
            try:
                h = await client.get(base + "/health")
                if h.status_code == 200 and h.json().get("playwright") is False:
                    return False, "Playwright poller up, but browser not ready"
                if h.status_code == 200:
                    hj = h.json()
                    qn = int(hj.get("queue_len") or 0)
                    if hj.get("busy") and progress_callback and qn:
                        try:
                            pos = int(hj.get("position") or qn) + 1
                            eta = int(hj.get("eta_min") or (pos * 10))
                            await progress_callback(
                                f"⏳ Вы ~{pos}-й в очереди, ~{eta} мин. Только ваши keywords."
                            )
                        except Exception:
                            pass
            except Exception:
                return False, "Playwright poller недоступен"

            post_task = asyncio.create_task(
                client.post(url, json={"chat_id": int(chat_id)})
            )
            last_text = ""
            while not post_task.done():
                try:
                    st = await client.get(f"{progress_url}?chat_id={int(chat_id)}", timeout=5.0)
                    if st.status_code == 200:
                        data = st.json()
                        txt = (data.get("text") or "").strip()
                        pos = data.get("position")
                        eta = data.get("eta_min")
                        if pos and (data.get("chat_id") is None or int(data.get("chat_id") or 0) != int(chat_id)):
                            txt = f"⏳ Вы {pos}-й в очереди, ~{eta or int(pos)*10} мин. Только ваши keywords."
                        if txt and txt != last_text and progress_callback:
                            last_text = txt
                            try:
                                await progress_callback(txt)
                            except Exception:
                                pass
                except Exception:
                    if progress_callback:
                        try:
                            await progress_callback("🔍 Идёт поиск, пожалуйста подождите…")
                        except Exception:
                            pass
                try:
                    await asyncio.wait_for(asyncio.shield(post_task), timeout=2.5)
                except asyncio.TimeoutError:
                    pass

            r = post_task.result()
            if r.status_code == 429:
                try:
                    data = r.json()
                except Exception:
                    data = {}
                msg = (data.get("text") or data.get("error") or "").strip()
                if not msg:
                    msg = (
                        "⏳ Поиск можно запускать не чаще 1 раза в час "
                        "(личный лимит для вашего аккаунта)."
                    )
                return True, msg
            if r.status_code == 409:
                # Prefer live progress text if search already running
                prog = ""
                try:
                    st = await client.get(progress_url, timeout=5.0)
                    if st.status_code == 200:
                        prog = (st.json().get("text") or "").strip()
                except Exception:
                    pass
                if prog:
                    return False, f"Поиск в очереди/выполняется: {prog}"
                return False, "Поиск ещё в очереди на сервере. Подождите — для каждого пользователя свой прогон."
            if r.status_code != 200:
                return False, f"Poller error HTTP {r.status_code}: {r.text[:200]}"
            data = r.json()
            if not data.get("ok"):
                return False, data.get("error") or "poller failed"
            return True, data.get("text") or f"Найдено новых: {data.get('total', 0)}"
    except Exception as e:
        return False, f"poller call failed: {e}"



async def _search_now_light(chat_id: int) -> tuple[int, str]:
    """Per-user on-demand search via HH API / Habr RSS. Returns (count, note)."""
    from light_hh import collect_light, parse_keywords
    import db_postgres as db

    keywords = []
    try:
        if hasattr(db, "get_setting"):
            raw = await db.get_setting("hh_keywords", chat_id=chat_id)
            keywords = parse_keywords(raw if isinstance(raw, str) else str(raw or ""))
        elif hasattr(db, "get_user_setting"):
            raw = await db.get_user_setting(chat_id, "hh_keywords")
            keywords = parse_keywords(str(raw or ""))
    except Exception:
        keywords = []

    if not keywords:
        return 0, (
            "У вас не заданы ключевые слова поиска.\n"
            "Добавьте свои keywords в настройках — у каждого пользователя свой список."
        )

    vacs = await collect_light(keywords, include_habr=True)
    saved = 0
    for v in vacs:
        try:
            if hasattr(db, "upsert_vacancy_pool"):
                await db.upsert_vacancy_pool(v)
                saved += 1
            elif hasattr(db, "add_vacancy"):
                await db.add_vacancy(v)
                saved += 1
        except Exception:
            pass

    # Trigger scoring webhook if available
    try:
        import os, httpx
        score_url = os.getenv("SCORE_WEBHOOK_URL", "http://score:8080/vacancy")
        async with httpx.AsyncClient(timeout=30.0) as client:
            for v in vacs[:30]:
                try:
                    await client.post(score_url, json=v)
                except Exception:
                    break
    except Exception:
        pass

    note = (
        f"Найдено {len(vacs)} (HH/Habr, без браузера). "
        f"В пул: {saved}. Источники Playwright пропущены (нет Chromium в bot-образе)."
    )
    return len(vacs), note

if __name__ == "__main__":
    main()
