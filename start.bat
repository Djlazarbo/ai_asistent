@echo off
cd /d D:\Job

echo [1/4] Запускаю score_server...
start "score_server" cmd /k "python score_server.py"

timeout /t 2 /nobreak >nul

echo [2/4] Запускаю bot (через watchdog)...
start "bot_watchdog" cmd /k "python watchdog.py"

timeout /t 2 /nobreak >nul

echo [3/4] Запускаю hh_poller daemon (включает Хабр Карьера)...
start "hh_poller" cmd /k "python hh_poller.py --daemon"

timeout /t 2 /nobreak >nul

echo [4/4] Запускаю telethon_monitor...
start "telethon" cmd /k "python telethon_monitor.py"

echo.
echo Готово! n8n уже запущен в Docker.
echo Все компоненты запущены. Можно закрыть это окно.
pause
