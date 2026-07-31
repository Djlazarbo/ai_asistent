"""Bulk cleanup: mark all irrelevant vacancies as filtered across all sources."""
import asyncio
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
import db_postgres as db

_TITLE_WHITELIST = re.compile(
    r"(project\s*manager|program\s*manager|product\s*manager|delivery\s*manager|scrum\s*master"
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
    r"|copywriter|smm\b|контент"
    r"|специалист\s*(внедрени|сопровожден|поддержк|техническ)"
    r"|поддержк[аи]\s*(пользовател|1\s*лини|2\s*лини|клиент)"
    r"|писатель|writer(?!\s*manager)"
    r"|SRE\b|site\s*reliability)",
    re.IGNORECASE,
)

# Habr titles come wrapped: «Actual Title» (City)
_HABR_TITLE_RE = re.compile(r"[«\"\"](.*?)[»\"\"]")


def _clean_title(title: str) -> str:
    m = _HABR_TITLE_RE.search(title)
    return m.group(1).strip() if m else title.strip()


def is_junk(title: str) -> bool:
    if not title:
        return False
    clean = _clean_title(title)
    if _TITLE_WHITELIST.search(clean):
        return False
    if _TITLE_BLACKLIST.search(clean):
        return True
    return False


async def cleanup():
    await db.init_db()
    pool = await db._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT external_id, source, title, status FROM vacancies "
            "WHERE status NOT IN ('applied', 'interview', 'filtered') "
            "AND title IS NOT NULL AND title != ''"
        )

        to_filter = []
        for r in rows:
            if is_junk(r["title"]):
                to_filter.append((r["external_id"], r["source"], r["title"], r["status"]))

        print(f"Проверено: {len(rows)} вакансий")
        print(f"Нерелевантных: {len(to_filter)}")
        print()

        by_source: dict[str, list] = {}
        for eid, src, title, status in to_filter:
            by_source.setdefault(src, []).append((title, status))

        for src, items in sorted(by_source.items()):
            print(f"--- {src.upper()} ({len(items)}) ---")
            for title, status in items:
                print(f"  [{status:8}] {_clean_title(title)[:65]}")

        if not to_filter:
            print("База чистая.")
            return

        print(f"\nОбновляю {len(to_filter)} записей...")
        for eid, src, title, status in to_filter:
            await conn.execute(
                "UPDATE vacancies SET status = 'filtered' WHERE external_id = $1",
                eid,
            )

        print(f"✅ Готово: {len(to_filter)} вакансий → filtered")


asyncio.run(cleanup())
