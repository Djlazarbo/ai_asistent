#!/usr/bin/env python3
"""Apply search-quality pack on prod VM. Run: sudo python3 /tmp/apply_search_quality_prod.py"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path("/opt/job-hunter")
SRC = Path("/tmp/jh_search_quality")


def docker_cp(local: Path, container: str, dest: str) -> None:
    subprocess.check_call(["sudo", "docker", "cp", str(local), f"{container}:{dest}"])


def pull(container: str, rel: str) -> Path:
    tmp = Path(f"/tmp/{container}_{Path(rel).name}")
    subprocess.check_call(["sudo", "docker", "cp", f"{container}:/app/{rel}", str(tmp)])
    return tmp


def push(tmp: Path, container: str, rel: str) -> None:
    docker_cp(tmp, container, f"/app/{rel}")


def once_replace(text: str, old: str, new: str, marker: str, required: bool = True) -> str:
    if marker in text:
        return text
    if old not in text:
        msg = f"pattern not found (marker={marker!r}): {old[:90]!r}"
        if required:
            raise SystemExit(msg)
        print("WARN", msg)
        return text
    return text.replace(old, new, 1)


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"missing {SRC}")

    for name in ("title_filter.py", "score_fanout_opt.py"):
        data = (SRC / name).read_bytes()
        (ROOT / name).write_bytes(data)
        print(f"wrote {ROOT / name}")

    for c in ("jobhunter_poller", "jobhunter_score", "jobhunter_bot"):
        docker_cp(ROOT / "title_filter.py", c, "/app/title_filter.py")
        print(f"copied title_filter.py -> {c}")
    docker_cp(ROOT / "score_fanout_opt.py", "jobhunter_score", "/app/score_fanout_opt.py")

    # hh_poller
    hh = pull("jobhunter_poller", "hh_poller.py")
    t = hh.read_text(encoding="utf-8")
    t = once_replace(
        t,
        "import db_postgres as db\n",
        "import db_postgres as db\nfrom title_filter import is_junk_title, is_search_keyword\n",
        "from title_filter import is_junk_title",
    )
    t = once_replace(
        t,
        "    keywords = await db.get_keywords()\n    if not keywords:",
        "    keywords = await db.get_keywords()\n"
        "    keywords = [k for k in keywords if is_search_keyword(k)]\n"
        "    if not keywords:",
        "is_search_keyword(k)",
    )
    t = once_replace(
        t,
        "                    if await db.vacancy_exists(vid):\n                        continue\n",
        "                    if await db.vacancy_exists(vid):\n                        continue\n\n"
        "                    if is_junk_title(item.get(\"title\") or \"\"):\n"
        "                        logger.info(f\"HH title junk skipped: {item['title']!r}\")\n"
        "                        continue\n",
        "HH title junk skipped",
    )
    hh.write_text(t, encoding="utf-8")
    push(hh, "jobhunter_poller", "hh_poller.py")
    print("patched poller hh_poller.py")

    # habr
    habr = pull("jobhunter_poller", "habr_poller.py")
    t = habr.read_text(encoding="utf-8")
    t = once_replace(
        t,
        "import db_postgres as db\n",
        "import db_postgres as db\nfrom title_filter import is_junk_title, is_search_keyword\n",
        "from title_filter import is_junk_title",
    )
    t = once_replace(
        t,
        "    clean = _clean_habr_title(title)\n    if _TITLE_WHITELIST.search(clean):\n",
        "    clean = _clean_habr_title(title)\n"
        "    if is_junk_title(clean):\n"
        "        return False\n"
        "    if _TITLE_WHITELIST.search(clean):\n",
        "is_junk_title(clean)",
    )
    t = once_replace(
        t,
        "    keywords = await db.get_keywords()\n    if not keywords:",
        "    keywords = await db.get_keywords()\n"
        "    keywords = [k for k in keywords if is_search_keyword(k)]\n"
        "    if not keywords:",
        "is_search_keyword(k)",
    )
    habr.write_text(t, encoding="utf-8")
    push(habr, "jobhunter_poller", "habr_poller.py")
    print("patched poller habr_poller.py")

    # getmatch
    gm = pull("jobhunter_poller", "getmatch_poller.py")
    t = gm.read_text(encoding="utf-8")
    t = once_replace(
        t,
        "import db_postgres as db\n",
        "import db_postgres as db\nfrom title_filter import is_junk_title, is_search_keyword\n",
        "from title_filter import is_junk_title",
    )
    t = once_replace(
        t,
        "def _is_relevant_title(title: str) -> bool:\n    if _TITLE_WHITELIST.search(title):\n",
        "def _is_relevant_title(title: str) -> bool:\n"
        "    if is_junk_title(title):\n"
        "        return False\n"
        "    if _TITLE_WHITELIST.search(title):\n",
        "is_junk_title(title)",
    )
    t = once_replace(
        t,
        "    keywords = await db.get_keywords()\n    if not keywords:",
        "    keywords = await db.get_keywords()\n"
        "    keywords = [k for k in keywords if is_search_keyword(k)]\n"
        "    if not keywords:",
        "is_search_keyword(k)",
    )
    gm.write_text(t, encoding="utf-8")
    push(gm, "jobhunter_poller", "getmatch_poller.py")
    print("patched poller getmatch_poller.py")

    # score_server
    ss = pull("jobhunter_score", "score_server.py")
    t = ss.read_text(encoding="utf-8")
    t = once_replace(
        t,
        "            chat_ids = await _sfo.filter_chats_for_vacancy(\n"
        "                chat_ids, source=source, channel=channel\n"
        "            )",
        "            chat_ids = await _sfo.filter_chats_for_vacancy(\n"
        "                chat_ids, source=source, channel=channel,\n"
        "                title=title, text=text,\n"
        "            )",
        "title=title, text=text",
    )
    t = once_replace(
        t,
        "or a similar IT leadership/PM role.",
        "or руководитель IT-проектов. Not Product Owner, Team Lead, sales, marketing, "
        "construction, cleaning, or generic non-IT project manager.",
        "generic non-IT project manager",
        required=False,
    )
    t = once_replace(
        t,
        '''    if not is_pm:
        for cid in chat_ids:
            if not await db.is_monitoring_for_chat(cid):
                continue
            await db.save_vacancy_score_for_user(
                cid, external_id, None, title, company, salary_str, "{}", "filtered"
            )
            results.append({"chat_id": cid, "status": "filtered"})
        return web.json_response({"ok": True, "is_pm": False, "users": results})
''',
        '''    if not is_pm:
        logging.info(
            f"webhook: LLM is_pm=NO {external_id} title={title[:70]!r} — fan-out by keywords"
        )
''',
        "fan-out by keywords",
    )
    ss.write_text(t, encoding="utf-8")
    push(ss, "jobhunter_score", "score_server.py")
    print("patched score_server.py")

    ud_old = "        for k in kws:\n            lk = k.lower()\n            if lk not in seen:\n"
    ud_new = (
        "        from title_filter import is_search_keyword\n"
        "        for k in kws:\n"
        "            if not is_search_keyword(str(k)):\n"
        "                continue\n"
        "            lk = k.lower()\n            if lk not in seen:\n"
    )
    for c in ("jobhunter_poller", "jobhunter_score", "jobhunter_bot"):
        ud = pull(c, "user_data.py")
        t = ud.read_text(encoding="utf-8")
        t = once_replace(t, ud_old, ud_new, "if not is_search_keyword(str(k))")
        ud.write_text(t, encoding="utf-8")
        push(ud, c, "user_data.py")
        print(f"patched {c} user_data.py")

    sql = r"""
UPDATE user_settings
SET value = $json$["IT project manager", "Руководитель IT проектов", "Руководитель проектов", "IT Project Manager / Delivery Manager"]$json$,
    updated_at = NOW()
WHERE chat_id = 276228826 AND key = 'hh_keywords';

UPDATE user_settings
SET value = '[]',
    updated_at = NOW()
WHERE chat_id = 456399761 AND key = 'hh_keywords';
"""
    subprocess.run(
        [
            "sudo", "docker", "exec", "-i", "jobhunter_postgres",
            "psql", "-U", "jobhunter", "-d", "jobhunter", "-v", "ON_ERROR_STOP=1",
        ],
        input=sql,
        text=True,
        check=True,
    )
    print("cleaned garbage keywords")

    subprocess.check_call(["sudo", "docker", "restart", "jobhunter_score", "jobhunter_poller"])
    print("restarted score + poller")
    return 0


if __name__ == "__main__":
    sys.exit(main())
