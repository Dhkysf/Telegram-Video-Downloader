@echo off
REM Runs the Telegram bot and restarts it automatically if it stops.
REM The bot writes its own size-rotated log to bot.log (bot.log.1 .. .5).
REM Console output (crash tracebacks, startup errors) of the latest run goes
REM to bot.console.log; the previous run's copy is kept as bot.console.prev.log.
cd /d "%~dp0"
:loop
if exist bot.console.log move /y bot.console.log bot.console.prev.log >nul
".venv\Scripts\python.exe" bot.py > bot.console.log 2>&1
echo [%date% %time%] Bot stopped (exit code %errorlevel%), restarting in 10 seconds... >> bot.console.log
REM ping is used as a 10-second wait because it also works in a hidden window.
ping -n 11 127.0.0.1 >nul
goto loop
