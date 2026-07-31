"""
Минимальный HTTP-сервер на порту 5679.
n8n вызывает POST /update_score после Deepseek-анализа,
чтобы сохранить score/title/company/salary обратно в DB.
POST /filter — YES/NO фильтр через агента is_pm_filter (модель переключается в боте).
"""
import json
import logging
from dotenv import load_dotenv

load_dotenv()

from aiohttp import web
import db_postgres as db
from agents import call_agent

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
    analysis_fields = {k: data[k] for k in ("score", "title", "company", "salary", "format", "pros", "cons", "red_flags", "match_summary") if k in data}
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
        # If not PM — save filtered status immediately so the bot stops waiting
        if not is_pm and external_id != "?":
            await db.save_vacancy_score(external_id, None, "", "", "", "{}", "filtered")
        return web.json_response({"is_pm": is_pm, "answer": answer.strip()})
    except Exception as e:
        logging.error(f"filter error for {external_id}: {e}")
        return web.json_response({"is_pm": False, "error": str(e)}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def on_startup(app: web.Application):
    await db.init_db()


app = web.Application()
app.on_startup.append(on_startup)
app.router.add_post("/update_score", handle_update_score)
app.router.add_post("/filter", handle_filter)
app.router.add_get("/health", handle_health)

if __name__ == "__main__":
    logging.info("Score server starting on port 5679")
    web.run_app(app, host="0.0.0.0", port=5679)
