"""
Retry all pending vacancies — sends them back to n8n for scoring.
Run once after score_server was down and vacancies accumulated.
"""
import asyncio
import sys
from dotenv import load_dotenv

load_dotenv()

import db_postgres as db
import hh_poller


async def main():
    await db.init_db()
    pending = await db.get_pending_vacancies()
    total = len(pending)
    if not total:
        print("No pending vacancies — nothing to retry.")
        return

    print(f"Retrying {total} pending vacancies... (estimated {total} seconds)")
    by_source = {}
    for v in pending:
        src = v.get("source", "?")
        by_source[src] = by_source.get(src, 0) + 1
    for src, cnt in sorted(by_source.items()):
        print(f"  {src}: {cnt}")
    print()

    done = 0
    for v in pending:
        profile = await db.get_active_profile() or {}
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
        await hh_poller._send_to_n8n(payload)
        done += 1
        if done % 50 == 0:
            print(f"  {done}/{total} sent...")
        await asyncio.sleep(3)

    print(f"Done. {done} vacancies sent to n8n.")
    print("Scores will appear in the bot as n8n+Ollama processes them (check logs).")


if __name__ == "__main__":
    asyncio.run(main())
