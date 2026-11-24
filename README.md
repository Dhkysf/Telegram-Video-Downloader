# Telegram Video Downloader Bot

A personal Telegram bot for downloading videos on a Windows PC using `python-telegram-bot` (long polling) and `yt-dlp`. Send a link, inspect the preview, and confirm to receive the video (or audio) back in chat.

## 1. What it is

- **Multi-site support via `yt-dlp`**: TikTok (with an `ssstik.io` scraper fallback), Instagram, X/Twitter, YouTube, SoundCloud, and other public HTTP/HTTPS links.
- **Preview and confirmation buttons**: Inspects title, duration, quality, estimated size, and thumbnail before downloading anything.
- **"Audio saja" option**: Extracts audio as **M4A** (or keeps **MP3** / **M4A** as-is when already in that format) and sends it with `send_audio`.
- **Headless-browser fallback**: Uses Microsoft Edge via Playwright when `yt-dlp` finds nothing on a generic site, capturing video streams and flagging likely ads.
- **5-minute probe cache**: Reuses recent probe results for 5 minutes (`PROBE_CACHE_TTL_SECONDS = 300`) to avoid re-probing the same link.
- **`/ping` and `/health` commands**: Quick liveness check and full runtime status report.
- **Tray app with auto-restart and daily `yt-dlp` update**: `tray_app.pyw` supervises `bot.py`, restarts it with backoff on unexpected exits, and updates `yt-dlp` at most once per 24 hours.
- **Local-network guard**: Blocks connections and browser traffic to loopback, private LAN, link-local, and CGNAT IP ranges.
- **50 MB limit**: Enforces Telegram's 50 MB Bot API upload limit with automatic resolution step-down (`720p` → `480p` → `360p` → `240p`).

## 2. Requirements

- **Windows 10 or 11**
- **Python 3.10+**
- **FFmpeg** (system binary, **not** the `ffmpeg` pip package) for merging video/audio and remuxing to MP4. Install with `winget`:

```powershell
winget install Gyan.FFmpeg
```

Expected result: `winget` installs FFmpeg and adds `ffmpeg.exe` to your `PATH` (open a new PowerShell window afterward).

- **Microsoft Edge** (pre-installed on Windows 10/11), used by the headless-browser fallback.

## 3. Install

Run these steps in PowerShell from the project folder (no venv activation needed):

1. Create the virtual environment:

```powershell
python -m venv .venv
```

Expected result: A `.venv` folder is created in the project directory.

2. Install Python dependencies:

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
```

Expected result: `python-telegram-bot`, `yt-dlp`, `requests`, `pycryptodomex`, `playwright`, `pystray`, and `Pillow` are installed inside `.venv`.

3. Create shortcuts (project folder, Desktop, and Windows Startup):

```powershell
.\create_shortcuts.ps1
```

Expected result: Creates `Telegram Video Bot.lnk` in the project folder, on your Desktop, and in your Windows Startup folder (the Startup shortcut passes `--hidden --start`).

If PowerShell blocks running `.ps1` scripts, run it with:

```powershell
powershell -ExecutionPolicy Bypass -File .\create_shortcuts.ps1
```

Expected result: Same as above, without changing your system execution policy. Re-run `create_shortcuts.ps1` if you ever move the project folder.

## 4. Configuration

Create a `config.py` file next to `bot.py` with your bot token from [@BotFather](https://t.me/BotFather):

```python
import os

TELEGRAM_BOT_TOKEN = "123456789:ABCdef..."
os.environ.setdefault("ALLOWED_USER_IDS", "123456789")
```

> **How `config.py` works:** `config.py` is only read when `TELEGRAM_BOT_TOKEN` is **not** set in the environment. Using `os.environ.setdefault(...)` inside `config.py` lets you keep your optional settings in the same file.
>
> **Strongly recommended:** Set `ALLOWED_USER_IDS` to your Telegram user ID so the bot silently ignores everyone else (especially important if you share or fork a public repo).

### Environment Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | *(required)* | Telegram bot token from `@BotFather`. |
| `ALLOWED_USER_IDS` | *(unset — everyone allowed)* | Comma-separated Telegram user IDs allowed to use the bot. Invalid values deny everyone. |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Maximum concurrent probes/downloads across all users. |
| `JOB_TIMEOUT` | `300` | Seconds before a download job times out. |
| `USER_COOLDOWN_SECONDS` | `10` | Per-user cooldown in seconds between links. |
| `CONFIRM_BEFORE_DOWNLOAD` | `true` | Show preview + confirmation buttons before downloading (`0`/`false`/`no`/`off`/`tidak` disables it). |
| `BROWSER_FALLBACK` | `true` | Enable headless-browser fallback when `yt-dlp` finds nothing (`0`/`false`/`no`/`off`/`tidak` disables it). |
| `FFMPEG_DIR` | *(unset)* | Directory containing `ffmpeg.exe`, prepended to `PATH` at startup. |

## 5. Running

### Tray app (recommended for daily use)

Double-click `Telegram Video Bot.lnk` (or `tray_app.pyw`), or run:

```powershell
.venv\Scripts\pythonw.exe tray_app.pyw
```

Expected result: The tray controller opens (or re-launches itself inside `.venv` if started with system Python) and places an icon in the Windows system tray.

- **Icon colours**: **Green** = `Running`, **Yellow** = `Restarting`, **Red** = `Stopped` or `Crashed`.
- **Window & tray**: Minimizing or closing (`X`) hides the window to the system tray. Double-click the tray icon (or select **Show**) to restore it.
- **Tray menu items**: `Show`, `Start`, `Stop`, `Restart`, `Open log`, `Open folder`, `Start bot when app opens` (saved in `tray_settings.json`), and `Exit`.
- **Autostart at login**: `create_shortcuts.ps1` puts a shortcut in Windows Startup with `--hidden --start` so the bot starts in the background when you sign in.
- **Auto-restart**: If `bot.py` exits unexpectedly, the controller restarts it with backoff (`10s`, `30s`, `60s`, `300s`). It does **not** restart after exit code `1` (fatal config/token error) or after you click **Stop**.
- **How Stop works**: Writes `stop.request` for a graceful shutdown, then falls back to `taskkill /PID ... /T /F` (process tree kill) if the bot does not exit in time.
- **Single instance**: `tray.lock` / `tray.ipc` ensures only one tray app runs (a second launch sends `SHOW` to the existing window), and `bot.lock` ensures only one `bot.py` runs.

### CLI flags for `tray_app.pyw`

- `--hidden`: Start minimized to the system tray.
- `--start`: Automatically start `bot.py` on launch.
- `--stop`: Stop the running bot (via tray IPC or standalone `stop.request`) and exit.
- `--selftest`: Verify tray imports, icon generation, and `assets/app.ico`, then exit.

### Manual console fallback

```powershell
.venv\Scripts\python.exe bot.py
```

Expected result: The bot starts polling Telegram and writes logs to `bot.log` (and echoes them to the console). Press `Ctrl+C` to stop.

## 6. Logs and files

| File | Purpose | Safe to delete? |
| --- | --- | --- |
| `bot.log`, `bot.log.1`..`bot.log.5` | Rotating application log from `bot.py` (max ~6 MB total). | Yes (when stopped) |
| `bot.console.log`, `bot.console.prev.log` | stdout/stderr captured by `tray_app.pyw` (rotated on start). | Yes (when stopped) |
| `bot.update.log` | Output from the daily `yt-dlp` update check. | Yes |
| `tray.log` | Size-rotated log for `tray_app.pyw`. | Yes (when stopped) |
| `tray_settings.json` | Stores the `Start bot when app opens` preference. | Yes (resets preference) |
| `bot.pid`, `bot.lock` | Tracks the running `bot.py` PID and prevents duplicate bots. | Only if stale after a crash |
| `tray.lock`, `tray.ipc` | Prevents duplicate tray instances and handles `SHOW` IPC. | Only if stale after a crash |
| `stop.request` | Temporary signal file created during graceful stop. | Yes (when stopped) |

## 7. Using the bot

1. Send a supported link to the bot in Telegram.
2. The bot probes the link (without downloading) and replies with a preview (title, duration, quality, estimated size, and thumbnail or page screenshot).
3. Choose an inline button:
   - **Ya, unduh** (or **Stream N**): Download and send the video.
   - **🎵 Audio saja**: Download audio-only (M4A, or MP3 if already MP3).
   - **🌐 Cari pakai browser**: Re-probe the page using headless Microsoft Edge.
   - **❌ Batal**: Cancel the request.
4. Streams flagged with `⚠️ kemungkinan iklan` are likely advertisements; auto-pick (when `CONFIRM_BEFORE_DOWNLOAD=false`) never selects them.
5. Confirmation buttons expire after **15 minutes** (`PENDING_TTL_SECONDS = 900`).

### Commands

- `/start`, `/help`: Show usage instructions.
- `/ping`: Quick liveness check.
- `/health`: Shows uptime, active/pending jobs, probe cache count, `ffmpeg` status, browser fallback & Playwright version, confirmation mode, `temp_downloads/` usage, and free disk space.

## 8. Instagram cookies

Some Instagram posts require a logged-in session:

1. Use a **secondary** Instagram account (never your main account).
2. Export cookies using the **Get cookies.txt LOCALLY** browser extension.
3. Save the file as `instagram_cookies.txt` next to `bot.py`.

Never share `instagram_cookies.txt` or commit it to git.

## 9. Limits

- **50 MB upload limit**: Telegram Bot API rejects files over 50 MB.
- **DRM, live streams, login-only content, and CAPTCHAs**: Not supported; DRM streams are intentionally dropped.
- **Ad detection is a heuristic**: Streams marked `⚠️ kemungkinan iklan` are best-effort guesses based on URL/host/duration patterns.

## 10. Troubleshooting

- **Tray window does not appear**: Check the Windows system tray overflow (`^` arrow) for the icon, or run `.venv\Scripts\python.exe tray_app.pyw` in PowerShell to see any startup error.
- **Tray icon is red**: Check `bot.log` and `bot.console.log` (or click **Open log**). Exit code `1` usually means a missing/invalid `TELEGRAM_BOT_TOKEN`.
- **`Conflict` error in `bot.log`**: Another instance of `bot.py` is already polling with the same token. Stop the other instance (or run `.venv\Scripts\python.exe tray_app.pyw --stop`).
- **FFmpeg / Edge / Playwright missing**: Install FFmpeg with `winget install Gyan.FFmpeg` (or set `FFMPEG_DIR`), ensure Microsoft Edge is installed, and verify `.venv\Scripts\python -m pip install -r requirements.txt`.
- **Bot is silent**: Check `ALLOWED_USER_IDS` in `config.py` or your environment—if set, only listed user IDs receive replies.
- **Update `yt-dlp` manually**: Run `.venv\Scripts\python -m pip install -U yt-dlp` (with the bot stopped).

## 11. Upgrading from the old `.bat`/`.vbs` setup

`start_bot.bat`, `stop_bot.bat`, and `start_bot_hidden.vbs` have been removed. Delete any old Startup shortcut pointing to them, then run `.\create_shortcuts.ps1` (which also cleans up old `.vbs` Startup shortcuts automatically).

## 12. Development

```
bot.py                  Entry point
tray_app.pyw            Windows system-tray controller UI
tgbot_tray/             Tray controller backend (supervisor, log rotation, IPC)
tgbot/                  Telegram bot package (handlers, flows, preview, state, settings)
downloaders/            yt-dlp, TikTok scraper, Playwright browser, GuardProxy
tests/                  Offline unit and smoke tests
create_shortcuts.ps1    Creates project, Desktop, and Startup shortcuts
```

Run all offline checks:

```powershell
.venv\Scripts\python.exe tests/smoke_test.py
.venv\Scripts\python.exe tests/test_tray_controller.py
.venv\Scripts\python.exe tray_app.pyw --selftest
```

Expected result: All tests pass and `--selftest` exits `0`.

- AI agents: read `AGENTS.md` and `.agents/rules/` before editing.
- Commits follow Conventional Commits (`type(section): description`).
- Never commit `config.py`, `instagram_cookies.txt`, logs, or `*.lnk`.

## 13. Disclaimer

For personal use only. Respect copyright and the terms of service of each platform. Do not use this tool to circumvent DRM.
