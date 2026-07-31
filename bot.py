"""
Telegram bot — управление Job Hunter системой.
"""
import asyncio
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

BOT_TOKEN = _config["telegram_bot"]["token"]
OWNER_CHAT_ID = int(_config["telegram_bot"]["chat_id"])

PAGE_SIZE = 5
_MSK = datetime.timezone(datetime.timedelta(hours=3))
_MONTHS_RU_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

MAIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📋 Вакансии"), KeyboardButton("⭐ Избранное")],
        [KeyboardButton("🔍 Искать сейчас"), KeyboardButton("📊 Статистика")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

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

    total = sum(source_stats.values()) if source_stats else 0
    text = f"📈 Анализ рынка — TPM/PM\n\nЗа последние 30 дней бот видел {total} вакансий.\n\n"

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

    src_labels = {"hh": "hh.ru", "habr": "Хабр", "getmatch": "Getmatch", "tg": "Telegram", "other": "Другое"}
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

def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != OWNER_CHAT_ID:
            await update.message.reply_text("⛔ Нет доступа.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ── Main menu helpers ──────────────────────────────────────────────────────────

async def _send_main_menu(bot, chat_id: int):
    stats = await db.get_stats()
    monitoring = await db.is_monitoring_active()
    mon_str = "🟢 Активен" if monitoring else "⏸ Пауза"

    stale = await db.get_stale_applied_vacancies(days=3)
    keyboard_rows = []
    attention_lines = []
    for v in stale[:3]:
        company = v.get("company") or "—"
        try:
            upd = datetime.datetime.fromisoformat(v["updated_at"])
            days_stale = (datetime.datetime.utcnow() - upd).days
        except Exception:
            days_stale = 3
        attention_lines.append(f"• Нет ответа от {company} ({days_stale} дн.)")
        ext = v["external_id"]
        keyboard_rows.append([
            InlineKeyboardButton(f"✉️ Follow-up: {company}", callback_data=f"followup_{ext}")
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


import agents as _agents


async def _settings_text_and_keyboard():
    monitoring = await db.is_monitoring_active()
    mon_btn = (
        InlineKeyboardButton("⏸ Пауза мониторинга", callback_data="s_pause")
        if monitoring
        else InlineKeyboardButton("▶️ Запустить мониторинг", callback_data="s_unpause")
    )
    keyboard = InlineKeyboardMarkup([
        [mon_btn],
        [
            InlineKeyboardButton("🔍 Ключевые слова", callback_data="s_kw"),
            InlineKeyboardButton("📢 Каналы", callback_data="s_ch"),
        ],
        [
            InlineKeyboardButton("📊 Порог оценки", callback_data="s_thr"),
            InlineKeyboardButton("📄 Резюме", callback_data="s_resume"),
        ],
        [InlineKeyboardButton("🤖 Агенты (модели ИИ)", callback_data="s_agents")],
        [InlineKeyboardButton("🏠 Меню", callback_data="menu_main")],
    ])
    mon_str = "🟢 Активен" if monitoring else "⏸ Пауза"
    return f"⚙️ Настройки\n\nМониторинг: {mon_str}", keyboard


async def _show_settings(bot, chat_id: int):
    text, keyboard = await _settings_text_and_keyboard()
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)


async def _show_vacancy_page(bot, chat_id: int, page: int, context: ContextTypes.DEFAULT_TYPE, source: str = "all"):
    db_source = {"all": None, "hh": "hh", "tg": "tg", "habr": "habr", "getmatch": "getmatch"}.get(source)
    try:
        vacancies = await db.get_recent_vacancies(7, source=db_source)
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Ошибка БД: {e}")
        return

    scored = [v for v in vacancies if v.get("score") is not None]
    unscored = [v for v in vacancies if v.get("score") is None]
    scored.sort(key=lambda v: -(v.get("score") or 0))
    display = scored + unscored

    total = len(display)
    source_labels = {"all": "Все", "hh": "hh.ru", "tg": "TG", "habr": "Хабр", "getmatch": "Getmatch"}
    if not total:
        await bot.send_message(chat_id=chat_id, text=f"Нет вакансий за последние 7 дней ({source_labels[source]}).")
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
        company = _html.escape(v.get("company") or "—")
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
            [InlineKeyboardButton("🏠 Меню", callback_data="menu_main")],
        ]),
    )


# ── /start, /menu ──────────────────────────────────────────────────────────────

@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Job Hunter Bot", reply_markup=MAIN_REPLY_KEYBOARD)
    await _send_main_menu(context.bot, update.effective_chat.id)


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
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
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
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
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
    msg = await update.message.reply_text("🤖 AI анализирует резюме...")
    try:
        profile = _clean_profile(await ai.extract_profile(text))
        # Store extracted profile and resume text — await confirmation before saving
        context.user_data["pending_profile"] = profile
        context.user_data["pending_resume_text"] = text
        preview = json.dumps(profile, ensure_ascii=False, indent=2)
        await msg.edit_text(
            f"📋 Извлечённый профиль:\n\n<pre>{preview[:3500]}</pre>\n\n"
            f"Сохранить этот профиль? (текущий будет заменён)",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Сохранить", callback_data="profile_confirm_yes"),
                InlineKeyboardButton("❌ Оставить текущий", callback_data="profile_confirm_no"),
            ]]),
        )
    except Exception as e:
        logger.error(f"Resume processing error: {e}")
        await msg.edit_text(f"❌ Ошибка: {e}")


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
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
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
        await update.message.reply_text("Использование: /addchannel @channelname")
        return
    username = context.args[0]
    if not username.startswith("@"):
        username = "@" + username
    if await db.channel_exists(username):
        await db.set_channel_active(username, 1)
        await update.message.reply_text(f"✅ Канал {username} активирован.")
    else:
        await db.add_channel(username)
        await update.message.reply_text(
            f"✅ Канал {username} добавлен. Telethon подхватит его в течение минуты."
        )


@owner_only
async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /removechannel @channelname")
        return
    username = context.args[0]
    if not username.startswith("@"):
        username = "@" + username
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
    await update.message.reply_text(f"✅ Добавлено: {word}")


@owner_only
async def cmd_removekeyword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /removekeyword Слово")
        return
    word = " ".join(context.args)
    await db.remove_keyword(word)
    await update.message.reply_text(f"🗑 Удалено: {word}")


@owner_only
async def cmd_searchnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Запускаю поиск hh.ru + Хабр + Getmatch...")

    async def progress(text: str):
        try:
            await msg.edit_text(text)
        except Exception:
            pass

    try:
        hh_task = asyncio.create_task(hh_poller.poll_hh(progress_callback=progress))
        habr_task = asyncio.create_task(habr_poller.poll_habr())
        getmatch_task = asyncio.create_task(getmatch_poller.poll_getmatch())
        hh_count, habr_count, getmatch_count = await asyncio.gather(hh_task, habr_task, getmatch_task)
        total = hh_count + habr_count + getmatch_count
        await msg.edit_text(
            f"✅ Поиск завершён\n"
            f"📥 hh.ru: {hh_count}\n"
            f"📥 Хабр: {habr_count}\n"
            f"📥 Getmatch: {getmatch_count}\n"
            f"📥 Итого: {total}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Посмотреть", callback_data="source_all_0")],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu_main")],
            ]),
        )
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")


# ── Monitoring control ─────────────────────────────────────────────────────────

@owner_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.set_monitoring_active(False)
    await update.message.reply_text("⏸ Мониторинг приостановлен.")


@owner_only
async def cmd_unpause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.set_monitoring_active(True)
    await update.message.reply_text("▶️ Мониторинг возобновлён.")


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
            [InlineKeyboardButton("🏠 Меню", callback_data="menu_main")],
        ]),
    )


@owner_only
async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_market_analysis(context.bot, update.effective_chat.id)


# ── Scheduled jobs ────────────────────────────────────────────────────────────

async def morning_digest_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        ystats = await db.get_yesterday_stats()
        if not ystats.get("new"):
            return
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
            "☀️ Доброе утро, Юлий!\n\n"
            f"📊 За вчера:\n"
            f"📥 Новых вакансий: {ystats['new']}\n"
            f"👁 Просмотрел: {ystats['viewed']}\n"
            f"📝 Откликнулся: {ystats['applied']}"
        )
        if top_lines:
            text += "\n\n🔥 Топ вакансии сегодня:\n" + "\n".join(top_lines)
        await context.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Смотреть все", callback_data="source_all_0"),
                InlineKeyboardButton("🏠 Меню", callback_data="menu_main"),
            ]]),
        )
    except Exception as e:
        logger.error(f"Morning digest job error: {e}")


async def followup_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        stale = await db.get_stale_applied_vacancies(days=3)
        for v in stale:
            external_id = v["external_id"]
            company = v.get("company") or "—"
            title = v.get("title") or "—"
            try:
                upd = datetime.datetime.fromisoformat(v["updated_at"])
                days_stale = (datetime.datetime.utcnow() - upd).days
                applied_str = _format_date_ru(upd + datetime.timedelta(hours=3))
            except Exception:
                days_stale = 3
                applied_str = "недавно"
            text = (
                f"⏰ Нет ответа от {company} ({days_stale} дн.)\n\n"
                f"Вакансия: {title}\n"
                f"Откликнулся: {applied_str}\n\n"
                "Написать follow-up?"
            )
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=text,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✉️ Написать follow-up", callback_data=f"followup_{external_id}"),
                    InlineKeyboardButton("❌ Закрыть", callback_data=f"closefollowup_{external_id}"),
                ]]),
            )
    except Exception as e:
        logger.error(f"Follow-up reminder job error: {e}")


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        deleted = await db.cleanup_old_vacancies(days=30)
        if deleted > 0:
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=f"🗑 Автоочистка: удалено {deleted} вакансий старше 30 дней (pending/skipped).",
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
        await _send_one_and_wait(v, profile)
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
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "noop":
        return

    elif data == "menu_main":
        await _send_main_menu(context.bot, chat_id)

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
        msg = await context.bot.send_message(chat_id=chat_id, text="🔍 Запускаю поиск hh.ru + Хабр + Getmatch...")

        async def progress(text: str):
            try:
                await msg.edit_text(text)
            except Exception:
                pass

        try:
            hh_task = asyncio.create_task(hh_poller.poll_hh(progress_callback=progress))
            habr_task = asyncio.create_task(habr_poller.poll_habr())
            getmatch_task = asyncio.create_task(getmatch_poller.poll_getmatch())
            hh_count, habr_count, getmatch_count = await asyncio.gather(hh_task, habr_task, getmatch_task)
            total = hh_count + habr_count + getmatch_count
            await msg.edit_text(
                f"✅ Поиск завершён\n"
                f"📥 hh.ru: {hh_count}\n"
                f"📥 Хабр: {habr_count}\n"
                f"📥 Getmatch: {getmatch_count}\n"
                f"📥 Итого: {total}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Посмотреть", callback_data="source_all_0")],
                    [InlineKeyboardButton("🏠 Меню", callback_data="menu_main")],
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
        text, keyboard = await _settings_text_and_keyboard()
        await query.edit_message_text(text=text, reply_markup=keyboard)

    elif data == "s_pause":
        await db.set_monitoring_active(False)
        text, keyboard = await _settings_text_and_keyboard()
        await query.edit_message_text(text=f"⏸ Мониторинг приостановлен.\n\n{text}", reply_markup=keyboard)

    elif data == "s_unpause":
        await db.set_monitoring_active(True)
        text, keyboard = await _settings_text_and_keyboard()
        await query.edit_message_text(text=f"▶️ Мониторинг возобновлён.\n\n{text}", reply_markup=keyboard)

    elif data == "s_kw":
        kws = await db.get_keywords()
        text = "🔍 Ключевые слова hh.ru:\n" + "\n".join(f"• {k}" for k in kws)
        text += "\n\nДобавить: /addkeyword Слово\nУдалить: /removekeyword Слово"
        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")]]),
        )

    elif data == "s_ch":
        channels = await db.get_all_channels()
        lines = [("🟢" if ch["active"] else "⏸") + " " + ch["username"] for ch in channels]
        text = "📢 Каналы:\n" + "\n".join(lines) if lines else "Каналов нет."
        text += "\n\nДобавить: /addchannel @name\nУдалить: /removechannel @name"
        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Настройки", callback_data="menu_settings")]]),
        )

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
    query = update.callback_query
    await query.answer()

    external_id = query.data[len("detail_"):]
    await query.edit_message_text("⏳ Загружаю вакансию...")

    await db.mark_viewed(external_id)
    vacancy = await db.get_vacancy_by_external(external_id)
    if not vacancy:
        await query.edit_message_text("❌ Вакансия не найдена")
        return

    profile = await db.get_active_profile()
    if not profile:
        await query.edit_message_text("❌ Профиль не загружен. Используй /resume.")
        return

    cached = vacancy.get("analysis")
    analysis = None
    if cached:
        try:
            analysis = json.loads(cached)
            if not isinstance(analysis, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            analysis = None

    if analysis is None:
        try:
            await query.edit_message_text("⏳ Анализирую вакансию (30-60 сек)...")
            analysis = await ai.analyze_vacancy(vacancy["text"] or "", profile)
            await db.update_vacancy_analysis(external_id, analysis)
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка анализа: {e}")
            return

    pros = "\n".join(f"• {p}" for p in analysis.get("pros", [])[:3])
    cons = "\n".join(f"• {c}" for c in analysis.get("cons", [])[:2])
    flags = ", ".join(analysis.get("red_flags", [])) or "нет"
    score = analysis.get("score")
    url = vacancy.get("url", "—")

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{score_emoji(score)} {analysis.get('title', vacancy.get('title', '—'))}\n"
        f"🏢 {analysis.get('company', '—')} | 💰 {analysis.get('salary', '—')}\n"
        f"📍 {analysis.get('format', '—')} | 📊 {score}/10\n\n"
        f"✅ Плюсы:\n{pros}\n\n"
        f"⚠️ Минусы:\n{cons}\n\n"
        f"🚩 Флаги: {flags}\n\n"
        f"📝 {analysis.get('match_summary', '')}\n\n"
        f"🔗 {url}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

    fav_status = await db.is_favorite(external_id)
    fav_button = (
        InlineKeyboardButton("🌟 В избранном ✓", callback_data=f"unfav_{external_id}")
        if fav_status else
        InlineKeyboardButton("⭐ Избранное", callback_data=f"fav_{external_id}")
    )
    keyboard = InlineKeyboardMarkup([
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
        [InlineKeyboardButton("◀️ К списку", callback_data="menu_vacancies")],
    ])

    await query.edit_message_text(text=text, reply_markup=keyboard)


# ── Favorites ──────────────────────────────────────────────────────────────────

async def _show_favorites(bot, chat_id: int, page: int = 0):
    total = await db.count_favorites()
    if total == 0:
        await bot.send_message(
            chat_id=chat_id,
            text="⭐ Избранное пока пусто.\n\nОткрывай вакансии и нажимай ⭐ чтобы сохранять интересные.",
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
    footer_rows.append([InlineKeyboardButton("🏠 Меню", callback_data="menu_main")])
    await bot.send_message(
        chat_id=chat_id,
        text="─────────────────────",
        reply_markup=InlineKeyboardMarkup(footer_rows),
    )


@owner_only
async def cmd_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_favorites(context.bot, update.effective_chat.id, 0)


async def handle_favorites_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await query.edit_message_text("✅ Профиль сохранён.", parse_mode="HTML")
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
        v = await db.get_vacancy_by_external(ext)
        title = v.get("title") or "данную позицию" if v else "данную позицию"
        letter = _followup_text(title)
        await query.message.reply_text(f"✉️ Follow-up письмо:\n\n{letter}")
        return

    if data.startswith("closefollowup_"):
        try:
            await query.edit_message_text("✅ Закрыто")
        except Exception:
            pass
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
        sheets.update_status(external_id, "applied")
        await query.message.reply_text(
            "✅ Отклик зафиксирован! Вакансия перемещена в 'applied'.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Все вакансии", callback_data="menu_vacancies")],
            ]),
        )

    elif action == "skip":
        await db.update_vacancy_status(external_id, "skipped")
        sheets.update_status(external_id, "skipped")
        await query.message.reply_text(
            "⏭ Вакансия пропущена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Все вакансии", callback_data="menu_vacancies")],
            ]),
        )

    elif action == "copy":
        await query.message.reply_text("☝️ Скопируй текст из сообщения выше.")


# ── Universal message router ───────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OWNER_CHAT_ID:
        return
    text = (update.message.text or "").strip()

    if context.user_data.get("awaiting_resume"):
        await handle_text_resume(update, context)
        return
    if context.user_data.get("awaiting_profile_edit"):
        await handle_profile_edit(update, context)
        return

    if context.user_data.get("awaiting_cover_edit"):
        external_id = context.user_data.pop("awaiting_cover_edit")
        await db.save_cover_example(external_id, text)
        await update.message.reply_text("✅ Сохранено как пример твоего стиля.")
        return

    if context.user_data.get("awaiting_interview_notes"):
        info = context.user_data.pop("awaiting_interview_notes")
        await db.save_interview(info.get("company", "—"), info.get("external_id", ""), text, [])
        await update.message.reply_text("✅ Сохранено. Учту это на следующих подготовках.")
        return

    if text == "📋 Вакансии":
        await _show_vacancy_page(context.bot, update.effective_chat.id, 0, context)
    elif text == "⭐ Избранное":
        await _show_favorites(context.bot, update.effective_chat.id, 0)
    elif text == "🔍 Искать сейчас":
        msg = await update.message.reply_text("🔍 Запускаю поиск hh.ru + Хабр + Getmatch...")

        async def progress(t: str):
            try:
                await msg.edit_text(t)
            except Exception:
                pass

        try:
            hh_task = asyncio.create_task(hh_poller.poll_hh(progress_callback=progress))
            habr_task = asyncio.create_task(habr_poller.poll_habr())
            getmatch_task = asyncio.create_task(getmatch_poller.poll_getmatch())
            hh_count, habr_count, getmatch_count = await asyncio.gather(hh_task, habr_task, getmatch_task)
            total = hh_count + habr_count + getmatch_count
            await msg.edit_text(
                f"✅ Поиск завершён\n"
                f"📥 hh.ru: {hh_count}\n"
                f"📥 Хабр: {habr_count}\n"
                f"📥 Getmatch: {getmatch_count}\n"
                f"📥 Итого: {total}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 Посмотреть", callback_data="source_all_0")],
                ]),
            )
        except Exception as e:
            await msg.edit_text(f"❌ Ошибка: {e}")
    elif text == "📊 Статистика":
        await cmd_stats(update, context)
    elif text == "⚙️ Настройки":
        await _show_settings(context.bot, update.effective_chat.id)


# ── Main ───────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await db.init_db()
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("menu", "Главное меню"),
        BotCommand("resume", "Загрузить резюме"),
        BotCommand("myprofile", "Текущий профиль"),
        BotCommand("editprofile", "Редактировать профиль"),
        BotCommand("channels", "Список каналов"),
        BotCommand("addchannel", "Добавить канал"),
        BotCommand("removechannel", "Удалить канал"),
        BotCommand("keywords", "Ключевые слова hh"),
        BotCommand("addkeyword", "Добавить ключевое слово"),
        BotCommand("removekeyword", "Удалить ключевое слово"),
        BotCommand("searchnow", "Поиск hh прямо сейчас"),
        BotCommand("list", "Вакансии (с пагинацией)"),
        BotCommand("listhh", "Вакансии с hh.ru [дней]"),
        BotCommand("listgroups", "Вакансии из каналов [дней]"),
        BotCommand("stats", "Статистика"),
        BotCommand("status", "Обновить статус"),
        BotCommand("threshold", "Порог оценки"),
        BotCommand("pause", "Приостановить мониторинг"),
        BotCommand("unpause", "Возобновить мониторинг"),
        BotCommand("market", "Анализ рынка зарплат"),
        BotCommand("favorites", "Избранные вакансии"),
        BotCommand("fav", "Избранные вакансии"),
        BotCommand("retryscoring", "Повторить оценку pending вакансий"),
        BotCommand("drainpending", "Прогнать все pending вакансии до конца"),
    ])
    logger.info("Bot started")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"PTB error: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ Ошибка: {type(context.error).__name__}: {context.error}",
            )
        except Exception:
            pass


def main():
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=15.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
        httpx_kwargs={"trust_env": False, "http2": False},
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
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

    app.add_handler(CommandHandler("start", cmd_start))
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
    app.add_handler(CommandHandler("market", cmd_market))

    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CommandHandler("fav", cmd_favorites))

    app.add_handler(CallbackQueryHandler(handle_detail, pattern="^detail_"))
    app.add_handler(CallbackQueryHandler(handle_menu, pattern="^(menu_|vac_page_|s_|source_|noop)"))
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


if __name__ == "__main__":
    main()
