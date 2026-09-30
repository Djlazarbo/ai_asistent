"""
HTTP API on port 5679.

Prod (Docker, no n8n): pollers POST /webhook/vacancy — filter + DeepSeek analyze + DB.
Stage/n8n: also supports POST /filter and POST /update_score.
Multi-user prod: vacancies_pool + per-user scoring into user_vacancies.
"""
import json
import logging
from dotenv import load_dotenv

load_dotenv()

from aiohttp import web
import db_postgres as db
from agents import call_agent
import ai
import user_data
try:
    import score_fanout_opt as _sfo
except Exception:
    _sfo = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


# --- resume-post filter (candidate seeking work, not employer vacancy) ---
_RESUME_SEEK_PATTERNS = (
    "ищу работу",
    "ищу новую работу",
    "ищу проект",
    "ищу проекты",
    "ищу позицию",
    "ищу роль",
    "ищу вакансию",
    "в поиске работы",
    "в поиске новой работы",
    "открыт к предложениям",
    "открыта к предложениям",
    "рассмотрю предложения",
    "рассматриваю предложения",
    "предложите вакансии",
    "предложите проект",
    "мое резюме",
    "моё резюме",
    "резюме ниже",
    "резюме:",
    "#резюме",
    "#resume",
    "#opentowork",
    "opentowork",
    "open to work",
    "looking for a job",
    "looking for opportunities",
    "looking for a role",
    "seeking opportunities",
    "cv:",
    "ищу удаленк",
    "ищу удалёнк",
    "ищу remote",
)

_RESUME_SEEK_STRONG = (
    "ищу работу",
    "в поиске работы",
    "в поиске новой работы",
    "open to work",
    "looking for a job",
    "#резюме",
    "#resume",
    "#opentowork",
    "opentowork",
    "мое резюме",
    "моё резюме",
    "предложите вакансии",
)

_VACANCY_HIRE_PATTERNS = (
    "ищем",
    "требуется",
    "требуются",
    "нужен",
    "нужна",
    "нужны",
    "вакансия",
    "открыта позиция",
    "открыта вакансия",
    "в команду нужен",
    "в команду нужна",
    "we are hiring",
    "we're hiring",
    "hiring for",
    "job opening",
    "обязанности:",
    "требования:",
    "условия:",
    "зарплатная вилка",
    "вилка:",
)


def looks_like_resume_post(text: str) -> bool:
    """True if text looks like a candidate resume / seeking-work post, not a job opening."""
    t = (text or "").lower().replace("ё", "е")
    if not t.strip():
        return False
    seek = sum(1 for p in _RESUME_SEEK_PATTERNS if p.replace("ё", "е") in t)
    hire = sum(1 for p in _VACANCY_HIRE_PATTERNS if p.replace("ё", "е") in t)
    strong = any(p.replace("ё", "е") in t for p in _RESUME_SEEK_STRONG)
    if strong and hire == 0:
        return True
    if seek >= 2 and hire <= 1:
        return True
    if seek >= 1 and hire == 0:
        # Avoid false positives on short employer blurbs: require seek cue length
        return True
    return False


_FILTER_PROMPT = (
    "You are a vacancy filter. Reply ONLY with YES or NO.\n\n"
    "Reply YES only if the text is an EMPLOYER job opening — a company/recruiter is HIRING "
    "for IT Project Manager, Technical Project Manager, Delivery Manager, TPM, Product Manager, "
    "or руководитель IT-проектов. Not Product Owner, Team Lead, sales, marketing, construction, cleaning, or generic non-IT project manager.\n\n"
    "Reply NO if the text is:\n"
    "- a candidate resume or self-pitch (person looking for a job/project)\n"
    "- phrases like: ищу работу, в поиске, open to work, look at my CV/resume\n"
    "- spam, ads, or unrelated roles\n\n"
    "Text: {text}\n\nAnswer (YES or NO only):"
)


async def handle_update_score(request: web.Request) -> web.Response:
    data = await request.json()
    external_id = data.get("external_id", "")
    logging.info(
        f"update_score: external_id={external_id} score={data.get('score')} "
        f"title={data.get('title', '')[:40]}"
    )
    if not external_id:
        return web.json_response({"ok": False, "error": "no external_id"}, status=400)

    score = data.get("score")
    title = data.get("title", "")
    company = data.get("company", "")
    salary = data.get("salary", "")
    analysis_fields = {
        k: data[k]
        for k in (
            "score", "title", "company", "salary", "format",
            "pros", "cons", "red_flags", "match_summary",
        )
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
    prompt = _FILTER_PROMPT.format(text=text)
    try:
        answer = await call_agent("is_pm_filter", prompt)
        is_pm = "YES" in answer.upper()
        logging.info(f"filter: {external_id} -> {'YES' if is_pm else 'NO'} (raw: {answer[:30]})")
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

    already = await db.vacancy_exists(external_id)
    payload = {
        "source": source,
        "external_id": external_id,
        "channel": channel,
        "text": text,
        "title": title,
        "company": company,
        "salary": salary_str,
        "url": url,
        "status": "pending",
    }
    # Remap to canonical pool id after dedup (url / title+company).
    _pool_id = await db.insert_vacancy(payload)
    if _pool_id is not None and str(_pool_id) not in ("", "1", "None"):
        if str(_pool_id) != str(external_id):
            logging.info(f"webhook remap {external_id} -> {_pool_id}")
            external_id = str(_pool_id)
            payload["external_id"] = external_id

    if already and not user_data.multi_user_on():
        return web.json_response({"ok": True, "skipped": "exists"})

    combined = f"{title} {text}".strip()
    if looks_like_resume_post(combined):
        logging.info(f"webhook: resume/seeking-work filtered {external_id}")
        try:
            await db.save_vacancy_score(
                external_id, None, title, company, salary_str, "{}", "filtered"
            )
        except Exception:
            pass
        return web.json_response({"ok": True, "is_pm": False, "status": "filtered", "reason": "resume_post"})

    prompt = _FILTER_PROMPT.format(text=combined[:1000])
    try:
        answer = await call_agent("is_pm_filter", prompt)
        is_pm = "YES" in answer.upper()
    except Exception as e:
        logging.error(f"webhook filter error {external_id}: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)

    if not user_data.multi_user_on():
        if not is_pm:
            await db.save_vacancy_score(
                external_id, None, title, company, salary_str, "{}", "filtered"
            )
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
        return web.json_response(
            {"ok": True, "is_pm": True, "score": score, "status": new_status}
        )

    # Multi-user fan-out (only ready users; source-scoped)
    chat_ids = await db.list_active_users_for_scoring()
    if _sfo is not None:
        if hasattr(_sfo, "filter_chats_for_vacancy"):
            chat_ids = await _sfo.filter_chats_for_vacancy(
                chat_ids, source=source, channel=channel,
                title=title, text=text, company=company,
            )
        else:
            chat_ids = await _sfo.filter_chats_with_profile(chat_ids)
    # Catchup / historical ingest: score into DB but do not push Telegram cards.
    skip_notify = bool(data.get("catchup") or data.get("notify") is False)
    # Drop very old cards from notify (still may score if fresh enough for DB).
    try:
        from datetime import datetime, timedelta
        max_age_h = int(__import__("os").getenv("NOTIFY_MAX_AGE_HOURS", "48"))
        date_raw = (data.get("date") or "").strip()
        if date_raw:
            dt = datetime.fromisoformat(date_raw.replace("Z", ""))
            if datetime.utcnow() - dt.replace(tzinfo=None) > timedelta(hours=max_age_h):
                skip_notify = True
    except Exception:
        pass
    results = []
    if not is_pm:
        logging.info(
            f"webhook: LLM is_pm=NO {external_id} title={title[:70]!r} — fan-out by keywords"
        )

    score_llm = 0
    score_skip_cached = 0
    for cid in chat_ids:
        if not await db.is_monitoring_for_chat(cid):
            continue
        # Skip DeepSeek if this chat already has a personal score (MU: user_vacancies).
        try:
            pool = await db._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT personal_score, status FROM user_vacancies "
                    "WHERE chat_id=$1 AND vacancy_id=$2",
                    int(cid),
                    str(external_id),
                )
            if (
                row is not None
                and row["personal_score"] is not None
                and (row["status"] or "") != "pending"
            ):
                results.append(
                    {
                        "chat_id": cid,
                        "status": "exists",
                        "score": int(row["personal_score"]),
                    }
                )
                score_skip_cached += 1
                continue
        except Exception as e:
            logging.warning(f"webhook cache-check {external_id} user={cid}: {e}")

        profile = await db.get_profile_for_chat(cid) or {}
        try:
            analysis = await ai.analyze_vacancy(text or title, profile)
            score_llm += 1
        except Exception as e:
            logging.error(f"webhook analyze error {external_id} user={cid}: {e}")
            await db.save_vacancy_score_for_user(
                cid, external_id, None, title, company, salary_str, "{}", "pending"
            )
            results.append({"chat_id": cid, "status": "pending", "error": str(e)})
            continue

        score = analysis.get("score")
        # companies_want: +1 boost (cap 10); also allow notify at threshold-1
        want_hit = False
        try:
            if _sfo is not None and hasattr(_sfo, "get_company_lists"):
                _block, want = await _sfo.get_company_lists(cid)
                co = analysis.get("company") or company
                if hasattr(_sfo, "company_matches_list"):
                    want_hit = bool(_sfo.company_matches_list(co, want))
                if want_hit and hasattr(_sfo, "apply_company_want_boost"):
                    score = _sfo.apply_company_want_boost(score, boosted=True, amount=1)
                    analysis = dict(analysis)
                    analysis["score"] = score
                    analysis["company_want_boost"] = True
        except Exception as e:
            logging.warning(f"companies_want boost {external_id} user={cid}: {e}")

        threshold = await db.get_threshold_for_chat(cid)
        effective_threshold = max(1, int(threshold) - (1 if want_hit else 0))
        new_status = (
            "new"
            if (score is not None and int(score) >= effective_threshold)
            else "skipped"
        )
        await db.save_vacancy_score_for_user(
            cid,
            external_id,
            score,
            analysis.get("title") or title,
            analysis.get("company") or company,
            analysis.get("salary") or salary_str,
            json.dumps(analysis, ensure_ascii=False),
            new_status,
        )
        results.append({"chat_id": cid, "score": score, "status": new_status})
        logging.info(f"webhook: {external_id} user={cid} score={score} status={new_status}")
        if new_status == "new" and not skip_notify:
            vac_card = {
                    "title": analysis.get("title") or title,
                    "company": analysis.get("company") or company,
                    "url": data.get("url") or data.get("link") or "",
                    "link": data.get("link") or data.get("url") or "",
                    "source": data.get("source") or analysis.get("source") or "",
                    "channel": data.get("channel") or "",
                    "created_at": data.get("created_at") or data.get("published_at"),
                }
            pushed = await _jh_notify_if_new(
                cid,
                vac_card,
                "new",
                score=score,
                reason=str(analysis.get("reason") or analysis.get("summary") or ""),
                vacancy_id=external_id,
            )
            if vac_card.get("_skipped_archived"):
                try:
                    await db.update_vacancy_status_for_chat(cid, external_id, "skipped_archived")
                except Exception:
                    try:
                        tok = db.set_request_chat_id(int(cid))
                        try:
                            await db.update_vacancy_status(external_id, "skipped_archived")
                        finally:
                            db.reset_request_chat_id(tok)
                    except Exception:
                        pass

    logging.info(
        f"webhook fanout {external_id} score_llm={score_llm} score_skip_cached={score_skip_cached}"
    )
    return web.json_response({"ok": True, "is_pm": True, "users": results})


async def handle_health(request: web.Request) -> web.Response:
    from version import get_git_sha, get_version

    body = {"status": "ok", "version": get_version()}
    sha = get_git_sha()
    if sha:
        body["git"] = sha
    return web.json_response(body)


async def on_startup(app: web.Application):
    await db.init_db()


app = web.Application()
app.on_startup.append(on_startup)
app.router.add_post("/update_score", handle_update_score)
app.router.add_post("/filter", handle_filter)
app.router.add_post("/webhook/vacancy", handle_vacancy_webhook)
app.router.add_get("/health", handle_health)

# --- jobhunter notify-on-new / fanout opt ---
try:
    import score_fanout_opt as _sfo
except Exception:
    _sfo = None


async def _jh_notify_if_new(chat_id, vacancy, status, score=None, reason="", vacancy_id=None):
    try:
        if str(status).lower() != "new":
            return
        # Push toggle is independent of analysis/scoring (monitoring_active).
        try:
            if hasattr(db, "is_notify_new_for_chat"):
                if not await db.is_notify_new_for_chat(int(chat_id)):
                    return
            elif hasattr(db, "is_notify_new_enabled"):
                # fallback if request chat context is set
                if not await db.is_notify_new_enabled():
                    return
        except Exception:
            pass
        # Ears digest modes: no per-vacancy instant push (hourly/daily/twice).
        try:
            freq = "hourly"
            if hasattr(db, "get_ears_freq_for_chat"):
                freq = await db.get_ears_freq_for_chat(int(chat_id))
            elif hasattr(db, "get_ears_freq"):
                freq = await db.get_ears_freq()
            if str(freq).lower() in ("hourly", "daily", "twice", "1h", "1d", "2d"):
                return False
        except Exception:
            pass
        # Company blocklist
        try:
            block = []
            if hasattr(db, "get_companies_block_for_chat"):
                block = await db.get_companies_block_for_chat(int(chat_id))
            vac0 = vacancy if isinstance(vacancy, dict) else {}
            cname = vac0.get("company") or vac0.get("employer") or ""
            if block and hasattr(db, "company_name_matches") and db.company_name_matches(str(cname), block):
                return False
        except Exception:
            pass
        from tg_notify import send_vacancy_push
        vac = vacancy if isinstance(vacancy, dict) else {"title": str(vacancy)}
        ok = await send_vacancy_push(
            int(chat_id),
            vac,
            score=score,
            reason=reason or "",
            vacancy_id=vacancy_id,
        )
        if isinstance(vacancy, dict) and vac.get("_skipped_archived"):
            vacancy["_skipped_archived"] = True
        return ok
    except Exception as e:
        import logging
        logging.error(f"notify-on-new failed: {e}")


if __name__ == "__main__":
    logging.info("Score server starting on port 5679")
    web.run_app(app, host="0.0.0.0", port=5679)

