@echo off
REM Stops the Telegram bot: first the start_bot.bat restart loop, then bot.py.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$dir = (Get-Location).Path;" ^
  "$loops = Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | Where-Object { $_.CommandLine -like '*start_bot.bat*' };" ^
  "$bots = Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*bot.py*' -and ($_.ExecutablePath -like ($dir + '\*') -or $_.CommandLine -like ('*' + $dir + '*')) };" ^
  "$n = 0; foreach ($p in @($loops) + @($bots)) { if ($p) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; $n++ } };" ^
  "if ($n -gt 0) { Write-Host ('Bot stopped (' + $n + ' process(es) ended).') } else { Write-Host 'Bot is not running.' }"
timeout /t 3 >nul