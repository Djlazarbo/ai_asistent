"""
Getmatch.ru poller — runs daily at 09:00 alongside hh and habr pollers.
Uses requests+BeautifulSoup with Playwright fallback if cards are not found.
"""
import asyncio
import concurrent.futures
import functools
import hashlib
import os
import re
from urllib.parse import quote

import json as _json

import requests
import yaml
from bs4 import BeautifulSoup
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

import db_postgres as db
from title_filter import is_junk_title, is_search_keyword

_http_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="n8n-gm")

with open("config.yaml", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

import os

N8N_WEBHOOK = os.getenv("N8N_WEBHOOK_URL") or _config["n8n"]["webhook_url"]
GM_BASE = "https://getmatch.ru"
LOCATIONS = ["moscow", "remote"]
MAX_PAGES = 3
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}



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
    r"|technical\s*writer|технический\s*писатель"
    r"|data\s*(scientist|engineer|analyst)|ML\s*engineer|ML-инженер"
    r"|quant(?!\w)|квант|трейдер|trader"
    r"|администратор(?!\s*(проект|продукт))"
    r"|сетевой|network(?!\s*(manager|director))"
    r"|системный\s*администратор|sysadmin"
    r"|продажам?\s*(косметолог|оборудовани|B2C)|менеджер\s*по\s*продажам"
    r"|copywriter|smm\b|контент)",
    re.IGNORECASE,
)


def _is_relevant_title(title: str) -> bool:
    if is_junk_title(title):
        return False
    if _TITLE_WHITELIST.search(title):
        return True
    if _TITLE_BLACKLIST.search(title):
        return False
    return True  # unknown title → let AI decide via n8n filter


def _vacancy_id(url: str) -> str:
    # Strip query params so moscow/remote versions of same vacancy dedup correctly
    clean = re.sub(r"\?.*$", "", url.rstrip("/"))
    return hashlib.md5(clean.encode()).hexdigest()


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


# ── Next.js __NEXT_DATA__ extractor ───────────────────────────────────────────

def _extract_from_next_data(html: str) -> list[dict]:
    """Extract vacancies from Next.js embedded JSON (works for SSR/ISR pages)."""
    soup = BeautifulSoup(html, "lxml")
    tag = soup.select_one("script#__NEXT_DATA__")
    if not tag or not tag.string:
        return []
    try:
        data = _json.loads(tag.string)
    except Exception:
        return []

    page_props = data.get("props", {}).get("pageProps", {})

    raw_list: list | None = None
    for key in ("vacancies", "offers", "items", "data", "results", "jobs", "list"):
        val = page_props.get(key)
        if isinstance(val, list) and val:
            raw_list = val
            break

    if raw_list is None:
        for val in page_props.values():
            if isinstance(val, dict):
                for key in ("vacancies", "offers", "items", "data", "results", "jobs"):
                    inner = val.get(key)
                    if isinstance(inner, list) and inner:
                        raw_list = inner
                        break
            if raw_list:
                break

    if not raw_list:
        return []

    items = []
    for v in raw_list:
        if not isinstance(v, dict):
            continue

        title = (v.get("title") or v.get("name") or v.get("position") or "").strip()
        if not title:
            continue

        href = v.get("url") or v.get("link") or v.get("href") or ""
        if not href:
            slug = v.get("slug") or str(v.get("id", ""))
            if slug:
                href = f"/vacancies/{slug}"
        if href.startswith("/"):
            href = GM_BASE + href
        if not href or not re.search(r"/vacancies/[^?/]+", href):
            continue

        company = ""
        comp = v.get("company") or v.get("employer") or {}
        if isinstance(comp, dict):
            company = (comp.get("name") or comp.get("title") or "").strip()
        elif isinstance(comp, str):
            company = comp.strip()

        salary = ""
        sal = v.get("salary") or {}
        if isinstance(sal, dict):
            parts = []
            lo = sal.get("from") or sal.get("min") or sal.get("gross_from")
            hi = sal.get("to") or sal.get("max") or sal.get("gross_to")
            if lo:
                parts.append(f"от {lo:,}" if isinstance(lo, int) else f"от {lo}")
            if hi:
                parts.append(f"до {hi:,}" if isinstance(hi, int) else f"до {hi}")
            currency = sal.get("currency", "₽")
            salary = f"{' '.join(parts)} {currency}".strip()
        elif isinstance(sal, str):
            salary = sal

        items.append({"title": title, "url": href, "company": company, "salary": salary})

    return items


# ── BeautifulSoup scraper ──────────────────────────────────────────────────────

def _parse_card(card) -> dict | None:
    """Extract vacancy fields from a BeautifulSoup card element."""
    title_el = (
        card.select_one("a[class*='title']")
        or card.select_one("h2 > a")
        or card.select_one("h3 > a")
        or card.select_one("a[href*='/vacancies/']")
    )
    if not title_el:
        return None

    title = title_el.get_text(strip=True)
    href = title_el.get("href", "")
    if href.startswith("/"):
        href = GM_BASE + href
    if not href or not re.search(r"/vacancies/[^?/]+", href):
        return None

    company_el = (
        card.select_one("a[class*='company']")
        or card.select_one("span[class*='company']")
        or card.select_one("[class*='employer']")
        or card.select_one("a[href*='/companies/']")
    )
    company = company_el.get_text(strip=True) if company_el else ""

    salary_el = card.select_one("[class*='salary']") or card.select_one("[class*='Salary']")
    salary = salary_el.get_text(strip=True) if salary_el else ""

    return {"title": title, "url": href, "company": company, "salary": salary}


def _scrape_page_bs_sync(keyword: str, location: str, page: int) -> list[dict]:
    url = f"{GM_BASE}/vacancies?q={quote(keyword)}&l={location}&p={page}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            logger.debug(f"Getmatch HTTP {resp.status_code} for {url}")
            return []

        # Next.js SSR pages embed all data in __NEXT_DATA__ — try it first
        items = _extract_from_next_data(resp.text)
        if items:
            logger.info(f"Getmatch __NEXT_DATA__ found {len(items)} cards for '{keyword}' {location} p{page}")
            return items

        # Diagnose why __NEXT_DATA__ failed (logged once per first page)
        if page == 1:
            soup_diag = BeautifulSoup(resp.text, "lxml")
            tag = soup_diag.select_one("script#__NEXT_DATA__")
            if tag and tag.string:
                try:
                    diag_data = _json.loads(tag.string)
                    page_props = diag_data.get("props", {}).get("pageProps", {})
                    logger.warning(
                        f"Getmatch __NEXT_DATA__ present but no vacancies found. "
                        f"pageProps keys: {list(page_props.keys())[:20]}"
                    )
                except Exception as e:
                    logger.warning(f"Getmatch __NEXT_DATA__ parse error: {e}")
            else:
                page_title = soup_diag.title.string if soup_diag.title else "?"
                logger.warning(
                    f"Getmatch no __NEXT_DATA__ on page. Title: '{page_title}'. "
                    f"HTML len: {len(resp.text)}. URL: {url}"
                )

        # Fall back to CSS-selector scraping
        soup = BeautifulSoup(resp.text, "lxml")
        cards = (
            soup.select("div[class*='vacancy-card']")
            or soup.select("article[class*='vacancy']")
            or soup.select("div[class*='VacancyCard']")
            or soup.select("li[class*='vacancy']")
            or soup.select("[data-qa*='vacancy']")
        )

        items = []
        for card in cards:
            parsed = _parse_card(card)
            if parsed:
                items.append(parsed)
        return items
    except Exception as e:
        logger.error(f"Getmatch BS scrape error '{keyword}' {location} p{page}: {e}")
        return []


async def _scrape_page_bs(keyword: str, location: str, page: int) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scrape_page_bs_sync, keyword, location, page)


# ── Playwright fallback ────────────────────────────────────────────────────────

async def _scrape_page_playwright(keyword: str, location: str, page: int) -> list[dict]:
    from playwright.async_api import async_playwright
    url = f"{GM_BASE}/vacancies?q={quote(keyword)}&l={location}&p={page}"
    items: list[dict] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            pg = await browser.new_page()
            await pg.set_extra_http_headers({"Accept-Language": "ru-RU,ru;q=0.9"})
            try:
                await pg.goto(url, wait_until="networkidle", timeout=30_000)
                await pg.wait_for_timeout(2000)
                html = await pg.content()
            finally:
                await pg.close()
                await browser.close()

        soup = BeautifulSoup(html, "lxml")
        # After JS render, try the same selectors plus broader ones
        cards = (
            soup.select("div[class*='vacancy-card']")
            or soup.select("article[class*='vacancy']")
            or soup.select("div[class*='VacancyCard']")
            or soup.select("li[class*='vacancy']")
        )
        seen: set[str] = set()
        if cards:
            for card in cards:
                parsed = _parse_card(card)
                if parsed and parsed["url"] not in seen:
                    seen.add(parsed["url"])
                    items.append(parsed)
        else:
            # Last resort: collect all vacancy links
            for a in soup.select("a[href*='/vacancies/']"):
                href = a.get("href", "")
                if not re.search(r"/vacancies/[^?/]+", href):
                    continue
                if href.startswith("/"):
                    href = GM_BASE + href
                if href in seen:
                    continue
                seen.add(href)
                title = a.get_text(strip=True)
                if title:
                    items.append({"title": title, "url": href, "company": "", "salary": ""})
    except Exception as e:
        logger.error(f"Getmatch Playwright fallback error '{keyword}' {location} p{page}: {e}")
    return items


# ── Detail page ────────────────────────────────────────────────────────────────

def _fetch_detail_sync(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        desc_el = (
            soup.select_one("[class*='description']")
            or soup.select_one("[class*='Description']")
            or soup.select_one("section[class*='vacancy']")
            or soup.select_one("div[class*='job-content']")
            or soup.select_one("main")
        )
        if desc_el:
            return re.sub(r"\s+", " ", desc_el.get_text(separator=" ", strip=True))
        return ""
    except Exception as e:
        logger.warning(f"Getmatch detail fetch error {url}: {e}")
        return ""


async def _fetch_detail(url: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_detail_sync, url)


# ── Keyword scraper (both locations, pagination) ───────────────────────────────

async def _scrape_keyword(keyword: str, location: str) -> list[dict]:
    all_items: list[dict] = []
    seen_urls: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        items = await _scrape_page_bs(keyword, location, page)

        if not items and page == 1:
            logger.debug(f"Getmatch BS found nothing for '{keyword}' {location}, trying Playwright")
            items = await _scrape_page_playwright(keyword, location, page)

        if not items:
            break

        added = 0
        for item in items:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                all_items.append(item)
                added += 1

        if added == 0:
            break

        await asyncio.sleep(2)

    return all_items


# ── Main poll function ─────────────────────────────────────────────────────────

async def poll_getmatch(progress_callback=None) -> int:
    """Main polling function — returns number of new vacancies."""
    await db.init_db()
    keywords = await db.get_keywords()
    keywords = [k for k in keywords if is_search_keyword(k)]
    if not keywords:
        logger.info("No keywords configured, skipping getmatch poll")
        return 0

    seen_ids: set[str] = set()
    new_count = 0
    total_kw = len(keywords)
    profile = await db.get_active_profile() or {}

    for idx, keyword in enumerate(keywords, 1):
        if progress_callback:
            await progress_callback(
                f"🔍 [{idx}/{total_kw}] Getmatch: {keyword}...\n"
                f"📥 Новых найдено: {new_count}"
            )

        kw_new = 0
        for location in LOCATIONS:
            items = await _scrape_keyword(keyword, location)

            for item in items:
                vacancy_id = _vacancy_id(item["url"])

                if vacancy_id in seen_ids:
                    continue
                seen_ids.add(vacancy_id)

                if await db.vacancy_exists(vacancy_id):
                    continue

                if not _is_relevant_title(item["title"]):
                    logger.debug(f"Getmatch title filtered out: {item['title']!r}")
                    continue

                description = await _fetch_detail(item["url"])
                await asyncio.sleep(1)

                full_text = (
                    f"{item['title']}\n{item['company']}\n{item['salary']}\n\n{description}"
                ).strip()

                payload = {
                    "source": "getmatch",
                    "id": vacancy_id,
                    "external_id": vacancy_id,
                    "title": item["title"],
                    "company": item["company"],
                    "salary": item["salary"],
                    "url": item["url"],
                    "description": full_text,
                    "active_profile": profile,
                }

                await db.insert_vacancy({
                    "source": "getmatch",
                    "external_id": vacancy_id,
                    "channel": "getmatch",
                    "text": full_text,
                    "title": item["title"],
                    "company": item["company"],
                    "salary": item["salary"],
                    "url": item["url"],
                    "status": "pending",
                })

                await _send_to_n8n(payload)
                new_count += 1
                kw_new += 1
                logger.info(f"Sent getmatch vacancy {vacancy_id}: {item['title']}")
                await asyncio.sleep(0.5)

            await asyncio.sleep(3)

        logger.info(f"Getmatch keyword '{keyword}': {kw_new} new")

    logger.info(f"Getmatch poll done. New vacancies sent: {new_count}")
    return new_count


async def run_daemon():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    await db.init_db()
    scheduler = AsyncIOScheduler()
    poll_time = _config["hh"].get("poll_time", "09:00")
    hour, minute = map(int, poll_time.split(":"))
    scheduler.add_job(poll_getmatch, "cron", hour=hour, minute=minute)
    scheduler.start()
    logger.info(f"Getmatch poller daemon started. Next run at {poll_time} daily.")
    await poll_getmatch()
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
        asyncio.run(poll_getmatch())
