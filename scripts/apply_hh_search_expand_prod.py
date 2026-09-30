"""Apply HH search expansion + admin keyword cleanup on prod."""
from __future__ import annotations

import pathlib
import subprocess
import sys


def sh(cmd: list[str]) -> str:
    print("+", " ".join(cmd))
    out = subprocess.check_output(cmd, text=True)
    return out


def once_replace(text: str, old: str, new: str, marker: str) -> str:
    if marker in text and old not in text:
        print(f"already patched ({marker})")
        return text
    if old not in text:
        raise SystemExit(f"pattern not found for {marker!r}")
    return text.replace(old, new, 1)


def patch_hh_poller() -> None:
    raw = sh(["sudo", "docker", "exec", "jobhunter_poller", "cat", "/app/hh_poller.py"])
    old = (
        "    search_configs = [\n"
        '        f"&area={AREA_MOSCOW}&experience=between1And3",\n'
        '        f"&area={AREA_MOSCOW}&experience=between3And6",\n'
        '        f"&schedule=remote&experience=between1And3",\n'
        '        f"&schedule=remote&experience=between3And6",\n'
        "    ]"
    )
    new = (
        "    # Moscow (office/any) + remote (any region) + hybrid Moscow.\n"
        "    # moreThan6 catches senior TPM (e.g. 6+ yrs); remote covers non-Moscow remote/hybrid offers.\n"
        "    search_configs = [\n"
        '        f"&area={AREA_MOSCOW}&experience=between1And3",\n'
        '        f"&area={AREA_MOSCOW}&experience=between3And6",\n'
        '        f"&area={AREA_MOSCOW}&experience=moreThan6",\n'
        '        f"&schedule=remote&experience=between1And3",\n'
        '        f"&schedule=remote&experience=between3And6",\n'
        '        f"&schedule=remote&experience=moreThan6",\n'
        '        f"&area={AREA_MOSCOW}&work_format=HYBRID&experience=between3And6",\n'
        '        f"&area={AREA_MOSCOW}&work_format=HYBRID&experience=moreThan6",\n'
        "    ]"
    )
    patched = once_replace(raw, old, new, "experience=moreThan6")
    path = pathlib.Path("/tmp/hh_poller_search_expanded.py")
    path.write_text(patched, encoding="utf-8")
    sh(["sudo", "docker", "cp", str(path), "jobhunter_poller:/app/hh_poller.py"])
    print("patched jobhunter_poller:/app/hh_poller.py")


def clean_admin_keywords() -> None:
    sql = r"""
UPDATE user_settings
SET value = $json$["IT project manager", "технический менеджер проектов", "руководитель IT проектов", "менеджер IT проектов", "delivery manager", "technical program manager", "Technical Project Manager", "Product manager"]$json$,
    updated_at = NOW()
WHERE chat_id = 242107017 AND key = 'hh_keywords';

SELECT chat_id, left(value, 400) FROM user_settings WHERE chat_id = 242107017 AND key = 'hh_keywords';
"""
    path = pathlib.Path("/tmp/clean_admin_kw.sql")
    path.write_text(sql, encoding="utf-8")
    sh(["sudo", "docker", "cp", str(path), "jobhunter_postgres:/tmp/clean_admin_kw.sql"])
    out = sh(
        [
            "sudo",
            "docker",
            "exec",
            "jobhunter_postgres",
            "psql",
            "-U",
            "jobhunter",
            "-d",
            "jobhunter",
            "-f",
            "/tmp/clean_admin_kw.sql",
        ]
    )
    print(out)


def verify() -> None:
    out = sh(
        [
            "sudo",
            "docker",
            "exec",
            "jobhunter_poller",
            "grep",
            "-n",
            "moreThan6\\|work_format=HYBRID",
            "/app/hh_poller.py",
        ]
    )
    print(out)


def main() -> None:
    patch_hh_poller()
    clean_admin_keywords()
    verify()
    sh(["sudo", "docker", "restart", "jobhunter_poller"])
    print("restarted jobhunter_poller")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(e, file=sys.stderr)
        sys.exit(e.returncode)
