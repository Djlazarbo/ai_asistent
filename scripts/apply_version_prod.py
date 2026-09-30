#!/usr/bin/env python3
"""Install version.py on running prod containers and expose /version + health.version."""
from __future__ import annotations

import subprocess
from pathlib import Path

SRC = Path("/tmp/jh_version")
FILES = ("version.py", "VERSION", "CHANGELOG.md")


def docker_cp(local: Path, container: str, dest: str) -> None:
    subprocess.check_call(["sudo", "docker", "cp", str(local), f"{container}:{dest}"])


def pull(container: str, rel: str) -> Path:
    tmp = Path(f"/tmp/{container}_{Path(rel).name}")
    subprocess.check_call(["sudo", "docker", "cp", f"{container}:/app/{rel}", str(tmp)])
    return tmp


def push(tmp: Path, container: str, rel: str) -> None:
    docker_cp(tmp, container, f"/app/{rel}")


def once(text: str, old: str, new: str, marker: str) -> str:
    if marker in text:
        return text
    if old not in text:
        raise SystemExit(f"missing pattern {marker}: {old[:80]!r}")
    return text.replace(old, new, 1)


def main() -> None:
    for name in FILES:
        docker_cp(SRC / name, "jobhunter_score", f"/app/{name}")
        docker_cp(SRC / name, "jobhunter_bot", f"/app/{name}")
        docker_cp(SRC / name, "jobhunter_poller", f"/app/{name}")
        print("copied", name)

    ss = pull("jobhunter_score", "score_server.py")
    t = ss.read_text(encoding="utf-8")
    t = once(
        t,
        'async def handle_health(request: web.Request) -> web.Response:\n    return web.json_response({"status": "ok"})\n',
        'async def handle_health(request: web.Request) -> web.Response:\n'
        '    from version import get_git_sha, get_version\n\n'
        '    body = {"status": "ok", "version": get_version()}\n'
        '    sha = get_git_sha()\n'
        '    if sha:\n'
        '        body["git"] = sha\n'
        '    return web.json_response(body)\n',
        'get_version()',
    )
    ss.write_text(t, encoding="utf-8")
    push(ss, "jobhunter_score", "score_server.py")
    print("patched score health")

    bot = pull("jobhunter_bot", "bot.py")
    t = bot.read_text(encoding="utf-8")
    if "async def cmd_version" not in t:
        needle = "async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):"
        if needle not in t:
            raise SystemExit("cmd_start not found in prod bot.py")
        snippet = (
            "async def cmd_version(update, context):\n"
            "    from version import version_message\n"
            "    await update.message.reply_text(version_message())\n\n\n"
        )
        t = t.replace(needle, snippet + needle, 1)
    if 'CommandHandler("version"' not in t:
        t = once(
            t,
            'app.add_handler(CommandHandler("start", cmd_start))',
            'app.add_handler(CommandHandler("version", cmd_version))\n'
            '    app.add_handler(CommandHandler("start", cmd_start))',
            'CommandHandler("version", cmd_version)',
        )
    if 'BotCommand("version"' not in t:
        t = once(
            t,
            'BotCommand("start", "Сегодня"),',
            'BotCommand("version", "Версия сборки"),\n        BotCommand("start", "Сегодня"),',
            'BotCommand("version"',
        )
    bot.write_text(t, encoding="utf-8")
    push(bot, "jobhunter_bot", "bot.py")
    print("patched bot /version")

    subprocess.check_call(["sudo", "docker", "restart", "jobhunter_score", "jobhunter_bot"])
    print("restarted score + bot")


if __name__ == "__main__":
    main()
