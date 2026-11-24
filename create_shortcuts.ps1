# Creates Desktop and Startup shortcuts for the Telegram Video Bot tray controller.
$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$trayApp = Join-Path $root "tray_app.pyw"

if (-not (Test-Path $pythonw)) {
    Write-Error "Missing $pythonw. Create the .venv first."
    exit 1
}
if (-not (Test-Path $trayApp)) {
    Write-Error "Missing $trayApp."
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    [Environment]::GetFolderPath("Desktop"),
    [Environment]::GetFolderPath("Startup")
)

foreach ($dir in $targets) {
    $lnkPath = Join-Path $dir "Telegram Video Bot.lnk"
    $shortcut = $shell.CreateShortcut($lnkPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = "`"$trayApp`""
    $shortcut.WorkingDirectory = $root
    $shortcut.Description = "Telegram Video Bot Tray Controller"
    $shortcut.Save()
    Write-Host "Created: $lnkPath"
}

$startupDir = [Environment]::GetFolderPath("Startup")
$oldLinks = Get-ChildItem -Path $startupDir -Filter "*.lnk" -ErrorAction SilentlyContinue | Where-Object {
    try {
        $sc = $shell.CreateShortcut($_.FullName)
        $sc.TargetPath -like "*start_bot_hidden.vbs*" -or $sc.Arguments -like "*start_bot_hidden.vbs*"
    } catch {
        $false
    }
}

Write-Host ""
Write-Host "Reminder: delete any old start_bot_hidden.vbs shortcut from shell:startup so it does not conflict with the tray app."
foreach ($old in $oldLinks) {
    Write-Host "  Found old Startup shortcut: $($old.FullName)"
}
