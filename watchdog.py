"""
Watchdog для bot.py — перезапускает бота если он завис или упал.
Критерии зависания: лог не обновлялся более 5 минут.
"""
import subprocess
import sys
import time
import os
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("D:/Job/job_hunter.log")
WATCHDOG_LOG = Path("D:/Job/watchdog.log")
CHECK_INTERVAL = 60        # проверять каждые 60 сек
MAX_SILENCE = 15 * 60      # если лог молчит 15 минут — перезапуск
RESTART_DELAY = 5          # пауза перед перезапуском


def wlog(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | WATCHDOG | {msg}"
    print(line, flush=True)
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_age_seconds() -> float:
    """Сколько секунд прошло с последней записи в лог."""
    try:
        return time.time() - LOG_FILE.stat().st_mtime
    except FileNotFoundError:
        return float("inf")


def start_bot() -> subprocess.Popen:
    wlog("Запускаю bot.py...")
    proc = subprocess.Popen(
        [sys.executable, "bot.py"],
        cwd="D:/Job",
    )
    wlog(f"bot.py запущен, PID={proc.pid}")
    return proc


def main():
    wlog("Watchdog запущен")
    proc = start_bot()
    # даём боту 30 сек на старт
    time.sleep(30)

    while True:
        time.sleep(CHECK_INTERVAL)

        # 1. Бот упал — перезапуск
        if proc.poll() is not None:
            exit_code = proc.returncode
            wlog(f"bot.py завершился с кодом {exit_code}. Перезапускаю через {RESTART_DELAY}с...")
            time.sleep(RESTART_DELAY)
            proc = start_bot()
            time.sleep(30)
            continue

        # 2. Бот завис (лог молчит > MAX_SILENCE)
        age = log_age_seconds()
        if age > MAX_SILENCE:
            wlog(f"Лог молчит {int(age)}с (>{MAX_SILENCE}с) — бот завис. Убиваю PID={proc.pid}...")
            proc.kill()
            proc.wait()
            time.sleep(RESTART_DELAY)
            proc = start_bot()
            time.sleep(30)
        else:
            wlog(f"OK — bot.py жив, лог обновлялся {int(age)}с назад")


if __name__ == "__main__":
    main()
