"""
hh.ru poller — runs daily at 09:00 or on demand via /searchnow bot command.
Uses Playwright to scrape hh.ru (api.hh.ru requires OAuth since 2025-12-15).
"""
import asyncio
import concurrent.futures
import functools
import re
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
import yaml
from loguru import logger
from playwright.async_api import async_playwright, Browser, Page
from dotenv import load_dotenv

load_dotenv()

import db_postgres as db

_http_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="n8n-hh")

with open("config.yaml", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

HH_BASE = "https://hh.ru"
N8N_WEBHOOK = _config["n8n"]["webhook_url"]
AREA_MOSCOW = _config["hh"]["area_moscow"]



def _post_sync(url: str, payload: dict) -> int:
    resp = requests.post(url, json=payload, timeout=90, proxies={})
    return resp.status_code


async def _send_to_n8n(payload: dict, _retries: int = 3):
    loop = asyncio.get_event_loop()
    vid = payload.get("id", "?")
    for attempt in range(1, _retries + 1):
        try:
            status = await loop.run_in_executor(
                _http_pool, functools.partial(_post_sync, N8N_WEBHOOK, payload)
            )
            if status in (200, 201):
                return
            logger.warning(f"n8n returned {status} for {vid} (attempt {attempt})")
        except Exception as e:
            logger.warning(f"n8n send error for {vid} (attempt {attempt}): {e}")
        if attempt < _retries:
            await asyncio.sleep(5 * attempt)
    logger.error(f"Failed to send to n8n after {_retries} attempts: {vid}")


async def _new_page(browser: Browser) -> Page:
    page = await browser.new_page()
    await page.set_extra_http_headers({"Accept-Language": "ru-RU,ru;q=0.9"})
    return page


async def _scrape_vacancies(browser: Browser, keyword: str, extra_params: str, date_from: str, max_pages: int = 3) -> list[dict]:
    items = []
    for page_num in range(max_pages):
        url = (
            f"{HH_BASE}/search/vacancy"
            f"?text={quote(keyword)}"
            f"&search_field=name"
            f"&order_by=publication_time"
            f"&date_from={date_from}"
            f"&per_page=50"
            f"&page={page_num}"
            f"{extra_params}"
        )
        page = await _new_page(browser)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_selector("[data-qa='vacancy-serp__vacancy']", timeout=10_000)
            except Exception:
                if page_num == 0:
                    logger.debug(f"No vacancies found for '{keyword}' with params {extra_params}")
                break  # No more pages

            cards = await page.query_selector_all("[data-qa='vacancy-serp__vacancy']")
            if not cards:
                break  # No more pages

            page_items = 0
            for card in cards:
                try:
                    title_el = await card.query_selector("a[data-qa='serp-item__title']")
                    if not title_el:
                        continue
                    title = (await title_el.inner_text()).strip()
                    href = await title_el.get_attribute("href") or ""
                    match = re.search(r"/vacancy/(\d+)", href)
                    if not match:
                        continue
                    vid = match.group(1)

                    company_el = await card.query_selector("[data-qa='vacancy-serp__vacancy-employer']")
                    company = (await company_el.inner_text()).strip() if company_el else ""

                    salary_el = await card.query_selector("[data-qa='vacancy-serp__vacancy-compensation']")
                    salary = (await salary_el.inner_text()).strip() if salary_el else ""

                    items.append({
                        "id": vid,
                        "title": title,
                        "company": company,
                        "salary": salary,
                        "url": f"{HH_BASE}/vacancy/{vid}",
                    })
                    page_items += 1
                except Exception as e:
                    logger.debug(f"Card parse error: {e}")

            logger.debug(f"'{keyword}' page {page_num}: {page_items} cards")
            # If less than 50 results — this is the last page
            if len(cards) < 50:
                break
        except Exception as e:
            logger.error(f"Search scrape error for '{keyword}' page {page_num}: {e}")
            break
        finally:
            await page.close()
        await asyncio.sleep(1)  # polite delay between pages
    return items


async def _fetch_full_description(browser: Browser, vacancy_url: str) -> str:
    page = await _new_page(browser)
    try:
        await page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30_000)
        desc_el = await page.query_selector("[data-qa='vacancy-description']")
        if desc_el:
            text = await desc_el.inner_text()
            return re.sub(r"\s+", " ", text).strip()
        return ""
    except Exception as e:
        logger.warning(f"Description fetch error for {vacancy_url}: {e}")
        return ""
    finally:
        await page.close()


_RETRY_BATCH = 10  # max vacancies per retry run to avoid Ollama overload


async def _retry_pending():
    pending = await db.get_pending_vacancies()
    if not pending:
        return
    batch = pending[:_RETRY_BATCH]
    logger.info(f"Retrying {len(batch)}/{len(pending)} pending vacancies")
    profile = await db.get_active_profile() or {}
    for v in batch:
        payload = {
            "source": v["source"],
            "id": v["external_id"],
            "external_id": v["external_id"],
            "title": v.get("title", ""),
            "company": v.get("company", ""),
            "salary": v.get("salary", ""),
            "url": v.get("url", ""),
            "description": v.get("text", ""),
            "active_profile": profile,
        }
        await _send_to_n8n(payload)
        await asyncio.sleep(30)  # let Ollama finish before next vacancy


async def poll_hh(progress_callback=None):
    await db.init_db()
    keywords = await db.get_keywords()
    if not keywords:
        logger.info("No keywords configured, skipping hh poll")
        return 0

    last_poll_str = await db.get_setting("last_hh_poll")
    if last_poll_str:
        last_poll_dt = datetime.fromisoformat(last_poll_str)
        days_since = (datetime.utcnow() - last_poll_dt).days + 1
        days_back = min(days_since, 3)
    else:
        days_back = 1
    date_from = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    logger.info(f"hh poll: ищу за последние {days_back} дн. (с {date_from})")
    seen_ids: set[str] = set()
    new_count = 0
    total_kw = len(keywords)

    search_configs = [
        f"&area={AREA_MOSCOW}&experience=between1And3",
        f"&area={AREA_MOSCOW}&experience=between3And6",
        f"&schedule=remote&experience=between1And3",
        f"&schedule=remote&experience=between3And6",
    ]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for idx, keyword in enumerate(keywords, 1):
                if progress_callback:
                    await progress_callback(
                        f"🔍 [{idx}/{total_kw}] Ищу: {keyword}...\n"
                        f"📥 Новых найдено: {new_count}"
                    )

                all_items: list[dict] = []
                for extra in search_configs:
                    items = await _scrape_vacancies(browser, keyword, extra, date_from)
                    all_items.extend(items)
                    await asyncio.sleep(2)

                kw_new = 0
                for item in all_items:
                    vid = item["id"]
                    if vid in seen_ids:
                        continue
                    seen_ids.add(vid)

                    if await db.vacancy_exists(vid):
                        continue

                    description = await _fetch_full_description(browser, item["url"])
                    await asyncio.sleep(1)

                    profile = await db.get_active_profile() or {}
                    payload = {
                        "source": "hh",
                        "id": vid,
                        "external_id": vid,
                        "title": item["title"],
                        "company": item["company"],
                        "salary": item["salary"],
                        "url": item["url"],
                        "description": f"{item['title']}\n{item['company']}\n{item['salary']}\n\n{description}",
                        "active_profile": profile,
                    }

                    await db.insert_vacancy({
                        "source": "hh",
                        "external_id": vid,
                        "channel": "hh.ru",
                        "text": payload["description"],
                        "title": item["title"],
                        "company": item["company"],
                        "salary": item["salary"],
                        "url": item["url"],
                        "status": "pending",
                    })

                    await _send_to_n8n(payload)
                    new_count += 1
                    kw_new += 1
                    logger.info(f"Sent hh vacancy {vid}: {item['title']}")

                logger.info(f"Keyword '{keyword}': {kw_new} new")
        finally:
            await browser.close()

    pending = await db.get_pending_vacancies()
    if pending:
        logger.info(f"Запускаю retry для {len(pending)} pending вакансий в фоне")
        asyncio.create_task(_retry_pending())

    await db.set_setting("last_hh_poll", datetime.utcnow().isoformat())
    logger.info(f"hh poll done. New vacancies sent: {new_count}")
    return new_count


async def _scheduled_run():
    from habr_poller import poll_habr
    from getmatch_poller import poll_getmatch
    logger.info("Scheduled poll starting (hh + habr + getmatch)")
    hh_count = await poll_hh()
    habr_count = await poll_habr()
    getmatch_count = await poll_getmatch()
    logger.info(f"Total new: hh={hh_count}, habr={habr_count}, getmatch={getmatch_count}")


async def run_daemon():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    await db.init_db()
    scheduler = AsyncIOScheduler()
    poll_time = _config["hh"].get("poll_time", "09:00")
    hour, minute = map(int, poll_time.split(":"))
    scheduler.add_job(_scheduled_run, "cron", hour=hour, minute=minute)
    scheduler.start()
    logger.info(f"hh poller daemon started. Next run at {poll_time} daily.")
    await _scheduled_run()
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
        asyncio.run(poll_hh())
