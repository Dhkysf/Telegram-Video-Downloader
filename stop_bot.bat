@echo off
REM Stops the Telegram bot: terminates the start_bot.bat restart loop and bot.py process trees.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$dir = (Get-Location).Path.TrimEnd('\');" ^
  "$all = @(Get-CimInstance Win32_Process);" ^
  "$loops = @($all | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*start_bot.bat*' });" ^
  "$bots = @($all | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -like '*bot.py*' -and (($_.ExecutablePath -and $_.ExecutablePath.StartsWith($dir + '\', [StringComparison]::OrdinalIgnoreCase)) -or ($_.CommandLine -and $_.CommandLine.IndexOf($dir, [StringComparison]::OrdinalIgnoreCase) -ge 0)) });" ^
  "$seen = [System.Collections.Generic.HashSet[int]]::new();" ^
  "$queue = [System.Collections.Generic.Queue[int]]::new();" ^
  "foreach ($p in $loops + $bots) { if ($p -and $seen.Add([int]$p.ProcessId)) { $queue.Enqueue([int]$p.ProcessId) } };" ^
  "$targets = [System.Collections.Generic.List[int]]::new();" ^
  "while ($queue.Count -gt 0) { $cur = $queue.Dequeue(); $targets.Add($cur); foreach ($c in $all) { if ($c.ParentProcessId -eq $cur -and $seen.Add([int]$c.ProcessId)) { $queue.Enqueue([int]$c.ProcessId) } } };" ^
  "$n = 0; foreach ($id in $targets) { try { Stop-Process -Id $id -Force -ErrorAction Stop; $n++ } catch {} };" ^
  "if ($n -gt 0) { Write-Host ('Bot stopped (' + $n + ' process(es) ended).') } else { Write-Host 'Bot is not running.' }"
timeout /t 3 >nul 2>nul
