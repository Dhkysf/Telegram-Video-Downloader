# Creates project-folder, Desktop, and Startup shortcuts for the Telegram Video Bot tray controller.
$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$trayApp = Join-Path $root "tray_app.pyw"
$icoPath = Join-Path $root "assets\app.ico"

if (-not (Test-Path $pythonw)) {
    Write-Error "Missing $pythonw. Create the .venv first."
    exit 1
}
if (-not (Test-Path $trayApp)) {
    Write-Error "Missing $trayApp."
    exit 1
}
if (-not (Test-Path $icoPath)) {
    Write-Error "Missing $icoPath. Run '.venv\Scripts\python.exe tray_app.pyw --selftest' first."
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$specs = @(
    @{ Dir = $root; Args = "`"$trayApp`"" },
    @{ Dir = [Environment]::GetFolderPath("Desktop"); Args = "`"$trayApp`"" },
    @{ Dir = [Environment]::GetFolderPath("Startup"); Args = "`"$trayApp`" --hidden --start" }
)

foreach ($spec in $specs) {
    $lnkPath = Join-Path $spec.Dir "Telegram Video Bot.lnk"
    $shortcut = $shell.CreateShortcut($lnkPath)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = $spec.Args
    $shortcut.WorkingDirectory = $root
    $shortcut.IconLocation = "$icoPath,0"
    $shortcut.Description = "Telegram Video Bot Tray Controller"
    $shortcut.Save()
    Write-Host "Created: $lnkPath"
    Write-Host "  TargetPath:       $($shortcut.TargetPath)"
    Write-Host "  Arguments:        $($shortcut.Arguments)"
    Write-Host "  WorkingDirectory: $($shortcut.WorkingDirectory)"
    Write-Host "  IconLocation:     $($shortcut.IconLocation)"
}

$startupDir = [Environment]::GetFolderPath("Startup")
$oldLinks = Get-ChildItem -Path $startupDir -Filter "*.lnk" -ErrorAction SilentlyContinue | Where-Object {
    try {
        $sc = $shell.CreateShortcut($_.FullName)
        ($sc.TargetPath -like "$root\*.vbs") -or ($sc.Arguments -like "*$root*.vbs*")
    } catch {
        $false
    }
}

foreach ($old in $oldLinks) {
    Remove-Item -LiteralPath $old.FullName -Force
    Write-Host "Removed old Startup shortcut: $($old.FullName)"
}
