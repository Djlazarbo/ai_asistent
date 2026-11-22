#!/usr/bin/env python3
"""Host-side health monitor for Job Hunter. Cron every 2 min. Alerts via Telegram bot."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.getenv("JH_ROOT") or "/opt/job-hunter")
STATE = Path(os.getenv("JH_MONITOR_STATE") or "/var/lib/job-hunter/monitor_state.json")
COOLDOWN_SEC = int(os.getenv("JH_ALERT_COOLDOWN_SEC") or str(30 * 60))


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"alerts": {}}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _bot_token_chat() -> tuple[str, str]:
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8", errors="ignore")
    tok = re.search(r"token:\s*([^\s#]+)", cfg)
    chat = re.search(r"chat_id:\s*([0-9-]+)", cfg)
    return (tok.group(1).strip() if tok else ""), (chat.group(1).strip() if chat else "")


def _tg(text: str) -> None:
    token, chat = _bot_token_chat()
    if not token or not chat:
        print("no telegram creds", flush=True)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3500]}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception as e:
        print("tg alert failed", e, flush=True)


def _alert(st: dict, key: str, text: str) -> None:
    now = time.time()
    last = float((st.get("alerts") or {}).get(key) or 0)
    if now - last < COOLDOWN_SEC:
        print(f"suppress {key}", flush=True)
        return
    st.setdefault("alerts", {})[key] = now
    _save_state(st)
    print(f"ALERT {key}: {text}", flush=True)
    _tg(f"⚠️ Job Hunter\n{text}")


def _ok(st: dict, key: str) -> None:
    if key in (st.get("alerts") or {}):
        st["alerts"].pop(key, None)
        _save_state(st)


def _http_json(url: str, timeout: float = 15) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"_error": str(e)}


def _docker_status(name: str) -> tuple[str, int]:
    try:
        out = subprocess.check_output(
            ["docker", "inspect", "-f", "{{.State.Status}} {{.RestartCount}}", name],
            text=True,
            timeout=15,
        ).strip()
        parts = out.split()
        return parts[0], int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return "missing", 0


def main() -> None:
    st = _load_state()

    for c in (
        "jobhunter_bot",
        "jobhunter_poller",
        "jobhunter_score",
        "jobhunter_postgres",
        "jobhunter_telethon",
    ):
        status, restarts = _docker_status(c)
        key = f"container_{c}"
        if status != "running":
            _alert(st, key, f"Контейнер {c} = {status}")
        else:
            _ok(st, key)
            if c == "jobhunter_telethon" and restarts >= 5:
                _alert(st, "telethon_restarts", f"Telethon RestartCount={restarts}")
            else:
                _ok(st, "telethon_restarts")

    score = _http_json("http://127.0.0.1:5679/health")
    if score.get("status") != "ok":
        _alert(st, "score_health", f"Score /health: {score}")
    else:
        _ok(st, "score_health")

    try:
        out = subprocess.check_output(
            [
                "docker",
                "exec",
                "jobhunter_poller",
                "python",
                "-c",
                "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:5680/health', timeout=20).read().decode())",
            ],
            text=True,
            timeout=40,
        )
        ph = json.loads(out.strip())
        st["poller_health_fails"] = 0
    except Exception as e:
        ph = {"_error": str(e)}
        # During Playwright search docker exec often flakes under CPU/RAM pressure.
        # Only alert after 3 consecutive failures while container is still Up.
        fails = int(st.get("poller_health_fails") or 0) + 1
        st["poller_health_fails"] = fails
        _save_state(st)
        poller_up = _docker_status("jobhunter_poller")[0] == "running"
        if poller_up and fails < 3:
            ph = {"ok": True, "playwright": True, "busy": True, "_soft_skip": True}
        else:
            ph = {"_error": str(e), "fails": fails}

    if ph.get("_error") or not ph.get("ok"):
        _alert(st, "poller_health", f"Poller /health: {ph}")
    else:
        if not ph.get("_soft_skip"):
            _ok(st, "poller_health")
        if ph.get("playwright") is False:
            _alert(st, "poller_pw", "Poller: Playwright=false")
        else:
            _ok(st, "poller_pw")
        if int(ph.get("queue_len") or 0) >= 5:
            _alert(st, "poller_queue", f"Poller queue_len={ph.get('queue_len')}")
        else:
            _ok(st, "poller_queue")
        if ph.get("busy"):
            busy_since = st.get("busy_since") or time.time()
            st["busy_since"] = busy_since
            _save_state(st)
            if time.time() - float(busy_since) > 45 * 60:
                _alert(st, "poller_busy", f"Poller busy >45 мин chat={ph.get('busy_chat')}")
        else:
            st.pop("busy_since", None)
            _save_state(st)
            _ok(st, "poller_busy")

    usage = shutil.disk_usage("/")
    pct = 100.0 * usage.used / usage.total
    if pct >= 85:
        _alert(st, "disk", f"Диск / заполнен на {pct:.0f}%")
    else:
        _ok(st, "disk")

    try:
        mem = Path("/proc/meminfo").read_text(encoding="utf-8")

        def _kb(name: str) -> int:
            m = re.search(rf"^{name}:\s+(\d+)", mem, re.M)
            return int(m.group(1)) if m else 0

        avail_mb = _kb("MemAvailable") / 1024
        if avail_mb < 250:
            _alert(st, "ram", f"Мало RAM: MemAvailable={avail_mb:.0f} MB")
        else:
            _ok(st, "ram")
    except Exception:
        pass

    try:
        logs = subprocess.check_output(
            ["docker", "logs", "--tail", "30", "jobhunter_telethon"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        if "Please enter your phone" in logs:
            _alert(st, "telethon_auth", "Telethon просит телефон — session битая")
        else:
            _ok(st, "telethon_auth")
    except Exception:
        pass

    print("monitor done", flush=True)


if __name__ == "__main__":
    main()
