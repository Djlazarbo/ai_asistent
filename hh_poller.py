"""
hh.ru poller — runs daily at 09:00 or on demand via /searchnow bot command.
Uses Playwright to scrape hh.ru (api.hh.ru requires OAuth since 2025-12-15).
"""
import asyncio
import concurrent.futures
import functools
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
from loguru import logger
from playwright.async_api import async_playwright, Browser, Page
from dotenv import load_dotenv

load_dotenv()

import db_postgres as db

def _playwright_ready() -> bool:
    """True if Chromium is available. Safe to call from asyncio loop."""
    try:
        def _check() -> bool:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                path = pw.chromium.executable_path
                return bool(path) and Path(path).exists()

        import asyncio
        try:
            asyncio.get_running_loop()
            in_async = True
        except RuntimeError:
            in_async = False

        if in_async:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return bool(pool.submit(_check).result(timeout=60))
        return _check()
    except Exception as e:
        logger.warning(f"Playwright unavailable for hh: {e}")
        return False


_http_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="n8n-hh")

with open("config.yaml", encoding="utf-8") as f:
    _config = yaml.safe_load(f)

HH_BASE = "https://hh.ru"
N8N_WEBHOOK = os.getenv("N8N_WEBHOOK_URL") or _config["n8n"]["webhook_url"]
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


async def _salary_extra_params() -> str:
    """Optional &salary=&only_with_salary=true from per-user salary_from setting."""
    try:
        raw = await db.get_setting("salary_from")
        if raw is None:
            return ""
        n = int(str(raw).strip().replace(" ", "").replace("\u00a0", ""))
        if n >= 10000:
            return f"&salary={n}&only_with_salary=true"
    except Exception:
        pass
    return ""


async def _scrape_vacancies(browser: Browser, keyword: str, extra_params: str, date_from: str, max_pages: int = 3) -> list[dict]:
    items = []
    # Boolean queries (NAME:/OR/NOT) must search all fields via text=; keep name for simple keywords.
    use_name_field = "NAME:" not in keyword and " OR " not in keyword
    for page_num in range(max_pages):
        url = (
            f"{HH_BASE}/search/vacancy"
            f"?text={quote(keyword)}"
            f"{'&search_field=name' if use_name_field else ''}"
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
    # prod multi-user: keywords via union when no chat context
    await db.init_db()

    if not _playwright_ready():
        logger.warning("hh.ru пропущен: браузер Playwright недоступен на этом сервере")
        if progress_callback:
            try:
                await progress_callback("hh.ru пропущен (нет Playwright)")
            except Exception:
                pass
        return 0
    keywords = await db.get_keywords()
    from title_filter import (
        build_pm_hh_query,
        is_junk_title,
        is_search_keyword,
        user_is_pm_searcher,
    )

    keywords = [k for k in keywords if is_search_keyword(k)]
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

    # Moscow (office/any) + remote (any region) + hybrid Moscow.
    search_configs = [
        f"&area={AREA_MOSCOW}&experience=between1And3",
        f"&area={AREA_MOSCOW}&experience=between3And6",
        f"&area={AREA_MOSCOW}&experience=moreThan6",
        f"&schedule=remote&experience=between1And3",
        f"&schedule=remote&experience=between3And6",
        f"&schedule=remote&experience=moreThan6",
        f"&area={AREA_MOSCOW}&work_format=HYBRID&experience=between3And6",
        f"&area={AREA_MOSCOW}&work_format=HYBRID&experience=moreThan6",
    ]
    salary_extra = await _salary_extra_params()
    if salary_extra:
        search_configs = [c + salary_extra for c in search_configs]
        logger.info(f"hh poll: salary filter applied {salary_extra}")

    if user_is_pm_searcher(keywords):
        q = build_pm_hh_query(keywords)
        queries = [q] if q else list(keywords)
        logger.info(f"hh poll: PM boolean query ({len(keywords)} keywords → 1 request)")
    else:
        queries = list(keywords)

    total_kw = len(queries)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for idx, keyword in enumerate(queries, 1):
                if progress_callback:
                    if user_is_pm_searcher(keywords) and total_kw == 1:
                        status = (
                            f"hh.ru: поиск по вашим словам ({len(keywords)})…\n"
                            f"Новых пока: {new_count}"
                        )
                    else:
                        short = keyword if len(keyword) < 40 else keyword[:37] + "…"
                        status = (
                            f"hh.ru: {idx}/{total_kw} — «{short}»\n"
                            f"Новых пока: {new_count}"
                        )
                    await progress_callback(status)

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

                    if is_junk_title(item.get("title") or ""):
                        logger.info(f"HH title junk skipped: {item['title']!r}")
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

                logger.info(f"Keyword '{keyword[:60]}': {kw_new} new")
        finally:
            await browser.close()

    pending = await db.get_pending_vacancies()
    if pending:
        logger.info(f"Запускаю retry для {len(pending)} pending вакансий в фоне")
        asyncio.create_task(_retry_pending())

    await db.set_setting("last_hh_poll", datetime.utcnow().isoformat())
    logger.info(f"hh poll done. New vacancies sent: {new_count}")
    return new_count


def _env_flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name) or default).strip().lower() in ("1", "true", "yes", "on")


async def _scheduled_run():
    from habr_poller import poll_habr
    from getmatch_poller import poll_getmatch

    logger.info("Scheduled poll starting (hh + habr + getmatch)")
    hh_count = await poll_hh()
    habr_count = await poll_habr()
    if _env_flag("GETMATCH_ENABLED", "0"):
        getmatch_count = await poll_getmatch()
    else:
        getmatch_count = 0
        logger.info("Getmatch skipped (GETMATCH_ENABLED=0)")
    logger.info(f"Total new: hh={hh_count}, habr={habr_count}, getmatch={getmatch_count}")


# ── On-demand HTTP API for bot /searchnow (POLLER_URL=http://poller:5680) ───
POLLER_HTTP_PORT = int(os.getenv("POLLER_HTTP_PORT") or "5680")
_SEARCH_COOLDOWN_SEC = int(os.getenv("SEARCH_COOLDOWN_SEC") or str(60 * 60))

_progress: dict[int, dict] = {}
_queue: asyncio.Queue | None = None
_busy_chat: int | None = None
_queued_chats: set[int] = set()
_last_search_ts: dict[int, float] = {}
_worker_task: asyncio.Task | None = None
_active_tasks: dict[int, asyncio.Task] = {}
_cancel_requested: set[int] = set()
_pw_cached: bool | None = None


def _set_progress(chat_id: int, text: str, **extra) -> None:
    payload = {"text": text, "chat_id": int(chat_id)}
    payload.update(extra)
    _progress[int(chat_id)] = payload


def _check_cancelled(chat_id: int) -> None:
    if int(chat_id) in _cancel_requested:
        raise asyncio.CancelledError()


async def _run_user_search(chat_id: int) -> dict:
    """Run hh+habr(+optional getmatch) in the request chat context."""
    from habr_poller import poll_habr
    from getmatch_poller import poll_getmatch

    token = db.set_request_chat_id(int(chat_id))
    try:
        async def progress_callback(t: str):
            _check_cancelled(chat_id)
            _set_progress(chat_id, t)

        _check_cancelled(chat_id)
        _set_progress(chat_id, "🔍 Запускаю поиск hh.ru…")
        hh_count = await poll_hh(progress_callback=progress_callback)
        _check_cancelled(chat_id)
        _set_progress(chat_id, f"🔍 Хабр… (hh новых: {hh_count})")
        habr_count = await poll_habr(progress_callback=progress_callback)
        _check_cancelled(chat_id)
        if _env_flag("GETMATCH_ENABLED", "0"):
            _set_progress(chat_id, f"🔍 Getmatch… (hh={hh_count}, habr={habr_count})")
            getmatch_count = await poll_getmatch(progress_callback=progress_callback)
        else:
            getmatch_count = 0
        total = int(hh_count or 0) + int(habr_count or 0) + int(getmatch_count or 0)
        # Multi-user: pool often already has owner-scraped cards → 0 "new".
        # Attach matching recent pool vacancies to this user's feed.
        backfilled = 0
        try:
            _set_progress(chat_id, f"📥 Подбираю подходящие из базы… (свежих: {total})")
            from user_feed_backfill import backfill_user_feed

            bf = await backfill_user_feed(int(chat_id), days=7, limit=40, notify=False)
            backfilled = int(bf.get("ok") or 0)
        except Exception as e:
            logger.warning(f"backfill after search chat={chat_id}: {e}")
        text = (
            f"Поиск завершён\n"
            f"hh.ru: {hh_count}\n"
            f"Хабр: {habr_count}\n"
            f"Getmatch: {getmatch_count}\n"
            f"Итого новых: {total}"
        )
        if backfilled:
            text += f"\nИз базы к вашей ленте: +{backfilled}"
        _set_progress(chat_id, text)
        return {
            "ok": True,
            "text": text,
            "total": total + backfilled,
            "hh": hh_count,
            "habr": habr_count,
            "getmatch": getmatch_count,
            "backfilled": backfilled,
        }
    finally:
        db.reset_request_chat_id(token)


async def _search_worker() -> None:
    global _busy_chat
    assert _queue is not None
    while True:
        chat_id, fut = await _queue.get()
        chat_id = int(chat_id)
        _queued_chats.discard(chat_id)
        if chat_id in _cancel_requested:
            _cancel_requested.discard(chat_id)
            _set_progress(chat_id, "⏹ Поиск отменён")
            if not fut.done():
                fut.set_result({"ok": True, "cancelled": True, "text": "⏹ Поиск отменён"})
            _queue.task_done()
            continue
        _busy_chat = chat_id
        task = asyncio.create_task(_run_user_search(chat_id))
        _active_tasks[chat_id] = task
        try:
            _set_progress(chat_id, "🔍 Идёт поиск, пожалуйста подождите…")
            result = await task
            if not result.get("cancelled"):
                _last_search_ts[chat_id] = datetime.utcnow().timestamp()
            if not fut.done():
                fut.set_result(result)
        except asyncio.CancelledError:
            _set_progress(chat_id, "⏹ Поиск отменён")
            if not fut.done():
                fut.set_result({"ok": True, "cancelled": True, "text": "⏹ Поиск отменён"})
        except Exception as e:
            logger.exception(f"searchnow failed chat={chat_id}: {e}")
            err = {"ok": False, "error": str(e)}
            _set_progress(chat_id, f"❌ Ошибка поиска: {e}")
            if not fut.done():
                fut.set_result(err)
        finally:
            _active_tasks.pop(chat_id, None)
            _cancel_requested.discard(chat_id)
            _busy_chat = None
            _queue.task_done()


def _queue_position(chat_id: int) -> int | None:
    if _busy_chat == int(chat_id):
        return 0
    if int(chat_id) not in _queued_chats:
        return None
    return max(1, len(_queued_chats))


async def _http_health(request):
    from aiohttp import web

    global _pw_cached
    qlen = _queue.qsize() if _queue is not None else 0
    if _pw_cached is None and _busy_chat is None:
        try:
            _pw_cached = await asyncio.get_running_loop().run_in_executor(
                None, _playwright_ready
            )
        except Exception:
            _pw_cached = False
    elif _pw_cached is None:
        _pw_cached = True
    return web.json_response(
        {
            "ok": True,
            "playwright": bool(_pw_cached),
            "busy": _busy_chat is not None,
            "busy_chat": _busy_chat,
            "queue_len": qlen,
            "position": qlen,
            "eta_min": max(1, (qlen + (1 if _busy_chat else 0)) * 10),
            "getmatch": _env_flag("GETMATCH_ENABLED", "0"),
        }
    )


async def _http_progress(request):
    from aiohttp import web

    try:
        chat_id = int(request.query.get("chat_id") or "0")
    except ValueError:
        chat_id = 0
    data = dict(_progress.get(chat_id) or {})
    pos = _queue_position(chat_id)
    if pos is not None:
        data.setdefault("position", pos + (1 if _busy_chat and _busy_chat != chat_id else 0))
        data.setdefault("eta_min", int(data["position"]) * 10)
    if not data.get("text"):
        if _busy_chat == chat_id:
            data["text"] = "🔍 Идёт поиск, пожалуйста подождите…"
        elif pos is not None:
            data["text"] = f"⏳ Вы в очереди (~{data.get('eta_min', 10)} мин)"
        else:
            data["text"] = ""
    data["chat_id"] = chat_id or data.get("chat_id")
    return web.json_response(data)


async def _http_cancel(request):
    from aiohttp import web

    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        chat_id = int(body.get("chat_id") or request.query.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if not chat_id:
        return web.json_response({"ok": False, "error": "chat_id required"}, status=400)

    _cancel_requested.add(chat_id)
    _queued_chats.discard(chat_id)
    task = _active_tasks.get(chat_id)
    if task and not task.done():
        task.cancel()
    _set_progress(chat_id, "⏹ Отмена…")
    return web.json_response({"ok": True, "text": "⏹ Поиск отменён (или будет пропущен в очереди)"})


async def _http_searchnow(request):
    from aiohttp import web

    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        chat_id = int(body.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    if not chat_id:
        return web.json_response({"ok": False, "error": "chat_id required"}, status=400)

    _cancel_requested.discard(chat_id)
    now = datetime.utcnow().timestamp()
    last = _last_search_ts.get(chat_id) or 0
    if now - last < _SEARCH_COOLDOWN_SEC and _busy_chat != chat_id and chat_id not in _queued_chats:
        left = int(_SEARCH_COOLDOWN_SEC - (now - last))
        return web.json_response(
            {
                "ok": False,
                "error": "rate_limited",
                "text": (
                    "⏳ Поиск можно запускать не чаще 1 раза в час "
                    f"(личный лимит; ещё ~{max(1, left // 60)} мин)."
                ),
            },
            status=429,
        )

    if _busy_chat == chat_id or chat_id in _queued_chats:
        prog = (_progress.get(chat_id) or {}).get("text") or ""
        return web.json_response(
            {"ok": False, "error": "busy", "text": prog},
            status=409,
        )

    assert _queue is not None
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _queued_chats.add(chat_id)
    qlen = _queue.qsize() + (1 if _busy_chat else 0)
    _set_progress(
        chat_id,
        f"⏳ Вы ~{qlen + 1}-й в очереди, ~{(qlen + 1) * 10} мин. Только ваши keywords.",
        position=qlen + 1,
        eta_min=(qlen + 1) * 10,
    )
    await _queue.put((chat_id, fut))
    result = await fut
    if result.get("cancelled"):
        return web.json_response(result, status=200)
    status = 200 if result.get("ok") else 500
    return web.json_response(result, status=status)


async def run_daemon():
    from aiohttp import web
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    global _queue, _worker_task

    await db.init_db()
    _queue = asyncio.Queue()
    _worker_task = asyncio.create_task(_search_worker())

    app = web.Application()
    app.router.add_get("/health", _http_health)
    app.router.add_get("/progress", _http_progress)
    app.router.add_post("/searchnow", _http_searchnow)
    app.router.add_post("/cancel", _http_cancel)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", POLLER_HTTP_PORT)
    await site.start()
    logger.info(f"poller HTTP on :{POLLER_HTTP_PORT} (/health /searchnow /progress /cancel)")

    scheduler = AsyncIOScheduler()
    poll_time = _config["hh"].get("poll_time", "09:00")
    hour, minute = map(int, poll_time.split(":"))
    scheduler.add_job(_scheduled_run, "cron", hour=hour, minute=minute)
    scheduler.start()
    logger.info(
        f"hh poller daemon started. Next cron at {poll_time} daily "
        f"(no scrape on restart; GETMATCH_ENABLED={_env_flag('GETMATCH_ENABLED', '0')})"
    )
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    import sys
    if "--daemon" in sys.argv:
        asyncio.run(run_daemon())
    else:
        asyncio.run(poll_hh())
