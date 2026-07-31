"""
Habr Career RSS poller — runs daily at 09:00 alongside hh poller, or on demand via /searchnow.
"""
import asyncio
import concurrent.futures
import functools
import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import quote

import feedparser
import requests
import yaml
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

import db_postgres as db

_http_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="n8n-habr")

with open("config.yaml", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

HABR_RSS = "https://career.habr.com/vacancies/rss?q={keyword}&type=all"
N8N_WEBHOOK = _config["n8n"]["webhook_url"]

# ── Title pre-filter (same logic as getmatch_poller) ──────────────────────────

_TITLE_WHITELIST = re.compile(
    r"(project\s*manager|program\s*manager|delivery\s*manager|scrum\s*master"
    r"|product\s*owner|agile\s*coach|engineering\s*manager|менеджер\s*(проекта|проектов|IT)"
    r"|руководитель\s*(проект|IT|цифров|разработ|продукт)"
    r"|директор\s*(проект|IT|продукт)"
    r"|начальник\s*(проект|отдел|управлени)"
    r"|тимлид|team\s*lead|tech\s*lead|технический\s*директор|CTO"
    r"|TPM|PMO|RTE|chief\s*of\s*staff"
    r"|технический\s*(менеджер|руководитель|лид)"
    r"|менеджер\s*(цифров|платформ|сервис))",
    re.IGNORECASE,
)

_TITLE_BLACKLIST = re.compile(
    r"(frontend|backend|fullstack|full.stack|QA|тестировани|разработчик|developer"
    r"|engineer(?!\s*(manager|менеджер))|инженер(?!\s*(менеджер|manager))"
    r"|аналитик(?!\s*(проект|данн))|analyst(?!\s*(project|data\s*PM))"
    r"|DBA|devops|дизайнер|designer|копирайт|автор|редактор"
    r"|маркетолог|бухгалтер|юрист|HR(?!\s*director)|рекрутер|recruiter"
    r"|talent\s*acquisition|подбор\s*персонала|специалист\s*по\s*подбору"
    r"|компенсаци|льгот|C&B\b"
    r"|technical\s*writer|technical\s*writer|технический\s*писатель"
    r"|data\s*(scientist|engineer|analyst)|ML\s*engineer|ML-инженер"
    r"|quant(?!\w)|квант|трейдер|trader"
    r"|администратор(?!\s*(проект|продукт))"
    r"|сетевой|network(?!\s*(manager|director))"
    r"|системный\s*администратор|sysadmin"
    r"|продажам?\s*(косметолог|оборудовани|B2C)|менеджер\s*по\s*продажам"
    r"|copywriter|smm\b|контент)",
    re.IGNORECASE,
)


def _clean_habr_title(title: str) -> str:
    """Strip Habr RSS wrapper: 'Требуется «Actual Title» (City)' → 'Actual Title'."""
    m = re.search(r'[«""](.+?)[»""]', title)
    return m.group(1).strip() if m else title.strip()


def _is_relevant_title(title: str) -> bool:
    clean = _clean_habr_title(title)
    if _TITLE_WHITELIST.search(clean):
        return True
    if _TITLE_BLACKLIST.search(clean):
        return False
    return True  # unknown title → let AI decide via n8n filter



class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


def _strip_html(html: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


def _vacancy_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def _post_sync(url: str, payload: dict) -> int:
    resp = requests.post(url, json=payload, timeout=10, proxies={})
    return resp.status_code


async def _send_to_n8n(payload: dict):
    loop = asyncio.get_event_loop()
    try:
        status = await loop.run_in_executor(
            _http_pool, functools.partial(_post_sync, N8N_WEBHOOK, payload)
        )
        if status not in (200, 201):
            logger.warning(f"n8n returned {status} for {payload.get('id')}")
    except Exception as e:
        logger.error(f"Failed to send to n8n: {e}")


def _fetch_description_sync(url: str) -> str:
    try:
        resp = requests.get(url, timeout=15, headers={"Accept-Language": "ru-RU,ru;q=0.9"})
        html = resp.text
        # Try data-qa="vacancy-description" block first
        m = re.search(r'data-qa=["\']vacancy-description["\'][^>]*>(.*?)</div>', html, re.DOTALL)
        if not m:
            m = re.search(r'class=["\']vacancy-description["\'][^>]*>(.*?)</div>', html, re.DOTALL)
        if m:
            return _strip_html(m.group(1))
        return ""
    except Exception as e:
        logger.warning(f"Habr description fetch error for {url}: {e}")
        return ""


async def _fetch_description(url: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_description_sync, url)


def _parse_rss_sync(keyword: str) -> list[dict]:
    url = HABR_RSS.format(keyword=quote(keyword))
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if not link:
                continue
            summary = entry.get("summary", "") or entry.get("description", "")
            description = _strip_html(summary) if summary else ""
            company = entry.get("author", "") or entry.get("dc_creator", "")
            items.append({
                "title": title,
                "link": link,
                "description": description,
                "company": company,
            })
        return items
    except Exception as e:
        logger.error(f"Habr RSS parse error for '{keyword}': {e}")
        return []


async def _parse_rss(keyword: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _parse_rss_sync, keyword)


async def poll_habr(progress_callback=None) -> int:
    """Main polling function — returns number of new vacancies."""
    await db.init_db()
    keywords = await db.get_keywords()
    if not keywords:
        logger.info("No keywords configured, skipping habr poll")
        return 0

    seen_ids: set[str] = set()
    new_count = 0
    total_kw = len(keywords)
    profile = await db.get_active_profile() or {}

    for idx, keyword in enumerate(keywords, 1):
        if progress_callback:
            await progress_callback(
                f"🔍 [{idx}/{total_kw}] Хабр: {keyword}...\n"
                f"📥 Новых найдено: {new_count}"
            )

        items = await _parse_rss(keyword)
        kw_new = 0

        for item in items:
            url = item["link"]
            vacancy_id = _vacancy_id(url)

            if vacancy_id in seen_ids:
                continue
            seen_ids.add(vacancy_id)

            if await db.vacancy_exists(vacancy_id):
                continue

            if not _is_relevant_title(item["title"]):
                logger.debug(f"Habr title filtered out: {item['title']!r}")
                continue

            description = item["description"]
            if not description:
                description = await _fetch_description(url)
                await asyncio.sleep(1)

            full_text = f"{item['title']}\n{item['company']}\n\n{description}"

            payload = {
                "source": "habr",
                "id": vacancy_id,
                "external_id": vacancy_id,
                "title": item["title"],
                "company": item["company"],
                "salary": "",
                "url": url,
                "description": full_text,
                "active_profile": profile,
            }

            await db.insert_vacancy({
                "source": "habr",
                "external_id": vacancy_id,
                "channel": "habr",
                "text": full_text,
                "title": item["title"],
                "company": item["company"],
                "salary": "",
                "url": url,
                "status": "pending",
            })

            await _send_to_n8n(payload)
            new_count += 1
            kw_new += 1
            logger.info(f"Sent habr vacancy {vacancy_id}: {item['title']}")
            await asyncio.sleep(0.5)

        logger.info(f"Habr keyword '{keyword}': {kw_new} new")

    logger.info(f"Habr poll done. New vacancies sent: {new_count}")
    return new_count


async def run_daemon():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    await db.init_db()
    scheduler = AsyncIOScheduler()
    poll_time = _config["hh"].get("poll_time", "09:00")
    hour, minute = map(int, poll_time.split(":"))
    scheduler.add_job(poll_habr, "cron", hour=hour, minute=minute)
    scheduler.start()
    logger.info(f"Habr poller daemon started. Next run at {poll_time} daily.")
    await poll_habr()
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    import sys
    if "--daemon" in sys.argv:
        asyncio.run(run_daemon())
    else:
        asyncio.run(poll_habr())
