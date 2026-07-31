"""
HTTP API on port 5679.

Prod (Docker, no n8n): pollers POST /webhook/vacancy — filter + DeepSeek analyze + DB.
Stage/n8n: also supports POST /filter and POST /update_score.
"""
import json
import logging
from dotenv import load_dotenv

load_dotenv()

from aiohttp import web
import db_postgres as db
from agents import call_agent
import ai

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


async def handle_update_score(request: web.Request) -> web.Response:
    data = await request.json()
    external_id = data.get("external_id", "")
    logging.info(f"update_score: external_id={external_id} score={data.get('score')} title={data.get('title','')[:40]}")
    if not external_id:
        return web.json_response({"ok": False, "error": "no external_id"}, status=400)

    score = data.get("score")
    title = data.get("title", "")
    company = data.get("company", "")
    salary = data.get("salary", "")
    analysis_fields = {
        k: data[k]
        for k in ("score", "title", "company", "salary", "format", "pros", "cons", "red_flags", "match_summary")
        if k in data
    }
    analysis = json.dumps(analysis_fields, ensure_ascii=False)

    threshold = await db.get_threshold()
    new_status = "new" if (score is not None and int(score) >= threshold) else "skipped"

    await db.save_vacancy_score(external_id, score, title, company, salary, analysis, new_status)
    return web.json_response({"ok": True})


async def handle_filter(request: web.Request) -> web.Response:
    data = await request.json()
    text = (data.get("message_text") or data.get("description") or "")[:1000]
    external_id = data.get("external_id", "?")
    prompt = (
        "Ты фильтр вакансий. Ответь ТОЛЬКО одним словом: YES или NO.\n\n"
        "Определи: является ли следующий текст вакансией на позицию IT Project Manager, "
        "Technical Project Manager, Руководитель проектов, TPM, Product Manager, "
        "Технический руководитель или схожую роль в IT?\n\n"
        f"Текст: {text}\n\nОтвет (только YES или NO):"
    )
    try:
        answer = await call_agent("is_pm_filter", prompt)
        is_pm = "YES" in answer.upper()
        logging.info(f"filter: {external_id} → {'YES' if is_pm else 'NO'} (raw: {answer[:30]})")
        if not is_pm and external_id != "?":
            await db.save_vacancy_score(external_id, None, "", "", "", "{}", "filtered")
        return web.json_response({"is_pm": is_pm, "answer": answer.strip()})
    except Exception as e:
        logging.error(f"filter error for {external_id}: {e}")
        return web.json_response({"is_pm": False, "error": str(e)}, status=500)


async def handle_vacancy_webhook(request: web.Request) -> web.Response:
    """Drop-in replacement for n8n POST /webhook/vacancy (prod Docker stack)."""
    data = await request.json()
    external_id = str(data.get("external_id") or data.get("id") or "")
    text = data.get("description") or data.get("message_text") or data.get("text") or ""
    title = data.get("title") or ""
    company = data.get("company") or ""
    salary = data.get("salary") or ""
    source = data.get("source") or "unknown"
    url = data.get("url") or ""
    channel = data.get("channel") or source
    salary_str = salary if isinstance(salary, str) else json.dumps(salary, ensure_ascii=False)

    if not external_id:
        return web.json_response({"ok": False, "error": "no external_id"}, status=400)

    if await db.vacancy_exists(external_id):
        return web.json_response({"ok": True, "skipped": "exists"})

    await db.insert_vacancy({
        "source": source,
        "external_id": external_id,
        "channel": channel,
        "text": text,
        "title": title,
        "company": company,
        "salary": salary_str,
        "url": url,
        "status": "pending",
    })

    filter_prompt = (
        "Ты фильтр вакансий. Ответь ТОЛЬКО одним словом: YES или NO.\n\n"
        "Определи: является ли следующий текст вакансией на позицию IT Project Manager, "
        "Technical Project Manager, Руководитель проектов, TPM, Product Manager, "
        "Технический руководитель или схожую роль в IT?\n\n"
        f"Текст: {(title + ' ' + text)[:1000]}\n\nОтвет (только YES или NO):"
    )
    try:
        answer = await call_agent("is_pm_filter", filter_prompt)
        is_pm = "YES" in answer.upper()
    except Exception as e:
        logging.error(f"webhook filter error {external_id}: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    if not is_pm:
        await db.save_vacancy_score(external_id, None, title, company, salary_str, "{}", "filtered")
        return web.json_response({"ok": True, "is_pm": False, "status": "filtered"})

    profile = data.get("active_profile") or await db.get_active_profile() or {}
    try:
        analysis = await ai.analyze_vacancy(text or title, profile)
    except Exception as e:
        logging.error(f"webhook analyze error {external_id}: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    score = analysis.get("score")
    threshold = await db.get_threshold()
    new_status = "new" if (score is not None and int(score) >= threshold) else "skipped"
    await db.save_vacancy_score(
        external_id,
        score,
        analysis.get("title") or title,
        analysis.get("company") or company,
        analysis.get("salary") or salary_str,
        json.dumps(analysis, ensure_ascii=False),
        new_status,
    )
    logging.info(f"webhook: {external_id} score={score} status={new_status}")
    return web.json_response({"ok": True, "is_pm": True, "score": score, "status": new_status})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def on_startup(app: web.Application):
    await db.init_db()


app = web.Application()
app.on_startup.append(on_startup)
app.router.add_post("/update_score", handle_update_score)
app.router.add_post("/filter", handle_filter)
app.router.add_post("/webhook/vacancy", handle_vacancy_webhook)
app.router.add_get("/health", handle_health)

if __name__ == "__main__":
    logging.info("Score server starting on port 5679")
    web.run_app(app, host="0.0.0.0", port=5679)
