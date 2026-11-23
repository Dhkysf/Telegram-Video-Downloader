@echo off
REM Runs the Telegram bot and restarts it automatically if it stops.
REM The bot writes its own size-rotated log to bot.log (bot.log.1 .. .5).
REM Console output (crash tracebacks, startup errors) of the latest run goes
REM to bot.console.log; the previous run's copy is kept as bot.console.prev.log.
cd /d "%~dp0"
REM Update yt-dlp once per start (sites change often). Failure is non-fatal:
REM the bot still starts with the installed version. Output goes to bot.update.log.
echo [%date% %time%] Updating yt-dlp... > bot.update.log
"%~dp0.venv\Scripts\python.exe" -m pip install -U --timeout 30 --disable-pip-version-check yt-dlp >> bot.update.log 2>&1
if errorlevel 1 echo [%date% %time%] yt-dlp update failed (exit code %errorlevel%), continuing with installed version. >> bot.update.log
:loop
if exist bot.console.log move /y bot.console.log bot.console.prev.log >nul
"%~dp0.venv\Scripts\python.exe" "%~dp0bot.py" > bot.console.log 2>&1
set "EXIT_CODE=%errorlevel%"
if "%EXIT_CODE%"=="1" (
    echo [%date% %time%] Bot exited with fatal code 1; not restarting. See bot.log / bot.console.log. >> bot.console.log
    exit /b 1
)
echo [%date% %time%] Bot stopped (exit code %EXIT_CODE%), restarting in 10 seconds... >> bot.console.log
REM ping is used as a 10-second wait because it also works in a hidden window.
ping -n 11 127.0.0.1 >nul
goto loop
