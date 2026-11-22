#!/usr/bin/env python3
"""Daily morning digest to owner Telegram."""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path("/opt/job-hunter")


def _tg(text: str) -> None:
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8", errors="ignore")
    tok = re.search(r"token:\s*([^\s#]+)", cfg).group(1).strip()
    chat = re.search(r"chat_id:\s*([0-9-]+)", cfg).group(1).strip()
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3500]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def _psql(sql: str) -> str:
    return subprocess.check_output(
        ["docker", "exec", "-i", "jobhunter_postgres", "psql", "-U", "jobhunter", "-d", "jobhunter", "-tAc", sql],
        text=True,
        timeout=30,
    ).strip()


def main() -> None:
    # GROUP BY must reference the plain column: "GROUP BY 1" points at the whole
    # concat expression which contains count(*) → "aggregate functions are not
    # allowed in GROUP BY". This query was broken since install (digest never sent).
    pool = _psql(
        "SELECT coalesce(source,'?')||'='||count(*) FROM vacancies_pool "
        "WHERE created_at > NOW() - INTERVAL '1 day' GROUP BY source ORDER BY count(*) DESC"
    )
    uv = _psql(
        "SELECT coalesce(status,'?')||'='||count(*) FROM user_vacancies "
        "WHERE updated_at > NOW() - INTERVAL '1 day' GROUP BY status ORDER BY count(*) DESC"
    )
    users = _psql("SELECT count(*) FROM users")
    disk = subprocess.check_output(["df", "-h", "/"], text=True).strip().splitlines()[-1]
    lines = [
        "📊 Job Hunter — сутки",
        f"Users: {users}",
        "Pool +1d:",
        pool or "(пусто)",
        "User vacancies +1d:",
        uv or "(пусто)",
        f"Disk: {disk}",
    ]
    try:
        ph = subprocess.check_output(
            [
                "docker",
                "exec",
                "jobhunter_poller",
                "python",
                "-c",
                "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:5680/health', timeout=10).read().decode())",
            ],
            text=True,
            timeout=20,
        )
        h = json.loads(ph.strip())
        lines.append(f"Poller: playwright={h.get('playwright')} busy={h.get('busy')} getmatch={h.get('getmatch')}")
    except Exception as e:
        lines.append(f"Poller health: {e}")
    _tg("\n".join(lines))
    print("digest sent", flush=True)


if __name__ == "__main__":
    main()
