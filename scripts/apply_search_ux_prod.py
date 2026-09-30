"""Deploy search UX + follow-up patches into running prod containers.

Requires env (never commit secrets):
  JH_SSH_HOST  e.g. user@host
  JH_SSH_KEY   path to private key (optional; uses ssh default if unset)
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOST = (os.getenv("JH_SSH_HOST") or "").strip()
KEY = (os.getenv("JH_SSH_KEY") or "").strip()
if not HOST:
    raise SystemExit("Set JH_SSH_HOST=user@host (do not hardcode in repo)")

FILES = {
    "bot.py": ["jobhunter_bot"],
    "user_data.py": ["jobhunter_bot", "jobhunter_poller", "jobhunter_score", "jobhunter_telethon"],
    "followup_meta.py": ["jobhunter_bot", "jobhunter_poller", "jobhunter_score"],
    "db_postgres.py": ["jobhunter_bot", "jobhunter_poller", "jobhunter_score", "jobhunter_telethon"],
    "hh_poller.py": ["jobhunter_poller"],
    "title_filter.py": ["jobhunter_poller", "jobhunter_score", "jobhunter_bot"],
    "telethon_monitor.py": ["jobhunter_telethon"],
    "tg_post_parse.py": ["jobhunter_telethon", "jobhunter_score", "jobhunter_bot"],
    "score_server.py": ["jobhunter_score"],
    "score_fanout_opt.py": ["jobhunter_score", "jobhunter_bot"],
}

RESTART = ["jobhunter_bot", "jobhunter_poller", "jobhunter_score", "jobhunter_telethon"]


def sh(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.check_call(cmd)


def _ssh_base() -> list[str]:
    cmd = ["ssh", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
    if KEY:
        cmd[1:1] = ["-i", KEY]
    return cmd


def _scp_base() -> list[str]:
    cmd = ["scp", "-o", "IdentitiesOnly=yes"]
    if KEY:
        cmd[1:1] = ["-i", KEY]
    return cmd


def main() -> None:
    remote_dir = "/tmp/jh_ux_patch"
    sh(_ssh_base() + [HOST, f"mkdir -p {remote_dir}"])
    local_files = [str(ROOT / name) for name in FILES]
    sh(_scp_base() + [*local_files, f"{HOST}:{remote_dir}/"])
    copy_cmds = []
    for name, containers in FILES.items():
        for c in containers:
            copy_cmds.append(f"sudo docker cp {remote_dir}/{name} {c}:/app/{name}")
        copy_cmds.append(f"sudo cp {remote_dir}/{name} /opt/job-hunter/{name}")
    for c in RESTART:
        copy_cmds.append(f"sudo docker restart {c}")
    remote = " && ".join(copy_cmds) + (
        "; sudo docker ps --filter name=jobhunter --format '{{.Names}} {{.Status}}'"
    )
    sh(_ssh_base() + [HOST, remote])
    print("DONE deploy")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
