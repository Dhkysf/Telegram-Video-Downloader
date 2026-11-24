# Telegram Video Downloader Bot

A personal Telegram bot for downloading videos on a Windows PC using `python-telegram-bot` (long polling) and `yt-dlp`. Send a link, inspect the preview, and confirm to receive the video (or audio) back in chat.

> **Note:** This bot is for personal use only; please respect copyright and the rules of each platform.

## 1. What it is

- **Multi-site support via `yt-dlp`**: TikTok (with an `ssstik.io` scraper fallback), Instagram, X/Twitter, YouTube, SoundCloud, and other public HTTP/HTTPS links.
- **Preview and confirmation buttons**: Inspects title, duration, quality, estimated size, and thumbnail before downloading anything.
- **"Audio saja" option**: Download audio-only M4A (or MP3 if already MP3) from supported links (and audio-only sites like SoundCloud are sent as audio).
- **Headless-browser fallback**: Uses Microsoft Edge via Playwright when `yt-dlp` finds nothing on a generic site, capturing video streams and flagging likely ads.
- **5-minute probe cache**: Reuses recent probe results for 5 minutes (`PROBE_CACHE_TTL_SECONDS = 300`) to avoid re-probing the same link.
- **`/ping` and `/health` commands**: Quick liveness check and full runtime status report.
- **Auto `yt-dlp` update on start**: `tray_app.pyw` updates `yt-dlp` at most once per 24 hours before starting the bot.
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

Expected result: `python-telegram-bot`, `yt-dlp`, `requests`, `playwright`, `pystray`, and `Pillow` are installed inside `.venv`.

## 4. Configuration

Create a `config.py` file next to `bot.py` with your bot token from [@BotFather](https://t.me/BotFather):

```python
import os

TELEGRAM_BOT_TOKEN = "123456789:ABCdef..."
os.environ.setdefault("ALLOWED_USER_IDS", "123456789")
```

> **How `config.py` works:** `config.py` is only read when `TELEGRAM_BOT_TOKEN` is **not** set in the environment. Using `os.environ.setdefault(...)` inside `config.py` lets you keep your optional settings in the same file.
>
> **Recommended:** Set `ALLOWED_USER_IDS` to your Telegram user ID so the bot silently ignores everyone else.

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

### Manual run

```powershell
.venv\Scripts\python bot.py
```

Expected result: The bot starts polling Telegram and writes logs to `bot.log` (and echoes them to the console). Press `Ctrl+C` to stop.

### Windows Tray Controller (recommended for daily use)

Double-click `tray_app.pyw` (or the `Telegram Video Bot` shortcut) or run:

```powershell
.venv\Scripts\pythonw.exe tray_app.pyw
```

- **Auto-venv**: If launched with system Python, `tray_app.pyw` re-launches itself using `.venv\Scripts\pythonw.exe`.
- **Status window & tray icon**: Shows `Running` (green), `Restarting` (yellow), or `Stopped` / `Crashed` (red), plus uptime and last exit code. Closing (`X`) or minimizing hides the window to the system tray; double-clicking the tray icon (or launching `tray_app.pyw` again) restores it.
- **CLI flags**: `--hidden` (start in tray only), `--start` (start the bot immediately, combinable with `--hidden`), `--stop` (stop the running bot and exit).
- **Controls**: `Start`, `Stop`, `Restart`, `Open log`, `Open folder`, `"Start bot when app opens"` (saved in `tray_settings.json`), and `Exit` (prompts before stopping a running bot).
- **Graceful stop**: Clicking `Stop` (or running `.venv\Scripts\pythonw.exe tray_app.pyw --stop`) creates `stop.request` and waits up to 15 seconds for `bot.py` to shut down cleanly; if it does not exit in time, the full process tree is force-killed (`taskkill /PID ... /T /F`).
- **Auto-restart & `yt-dlp` updates**: Restarts automatically with backoff (`10s` → `30s` → `60s` → max `5m`, resetting after `10m` of stable running). Never restarts on fatal exit code `1` or after a user `Stop`. Updates `yt-dlp` at most once per 24 hours (non-fatal).
- **Autostart shortcuts**: Run `.\create_shortcuts.ps1` in PowerShell to create `Telegram Video Bot.lnk` in the project folder, on the Desktop, and in Startup (`shell:startup`, with `--hidden --start`). Users upgrading from an older setup should delete any legacy Startup shortcut from `shell:startup` (`create_shortcuts.ps1` removes it automatically).
- **Manual console fallback**: `.venv\Scripts\python.exe bot.py` (run in foreground) and `.venv\Scripts\pythonw.exe tray_app.pyw --stop` (stop).

### Logs

- `bot.log`: Main rotating application log (1 MB per file, up to 5 backups: `bot.log.1` .. `bot.log.5`).
- `tray.log`: Rotating tray controller log (`tray.log.1` .. `tray.log.3`).
- `bot.update.log`: Output from the `yt-dlp` update.
- `bot.console.log` / `bot.console.prev.log`: Console stdout/stderr from `bot.py` (rotated on each start).

## 6. Using the bot

1. Send any supported video link to the bot.
2. The bot probes the link and replies with a preview (thumbnail or browser screenshot) and inline buttons:
   - **Ya, unduh** (or numbered stream buttons when multiple streams are found)
   - **Audio saja** (download audio as M4A, or MP3 if already MP3)
   - **cari pakai browser** (retry with the headless browser if `yt-dlp` picked the wrong thing)
   - **Batal** (cancel)
3. **⚠️ `kemungkinan iklan`**: Flagged on browser-captured streams whose URL, duration, or metadata matches ad heuristics. Avoid picking these unless you know it is the actual video.
4. **Button expiry**: Confirmation buttons expire after **15 minutes** (`PENDING_TTL_SECONDS = 900`).

### Commands

- `/start` or `/help`: Show the welcome message and usage instructions.
- `/ping`: Quick check that the bot is alive (`Pong!`).
- `/health`: Show uptime, active slots, pending choices, probe cache count, FFmpeg status, browser fallback status, Playwright version, `yt-dlp` version, `python-telegram-bot` version, and `temp_downloads/` usage.

## 7. Instagram cookies

Some Instagram posts/reels require login cookies:

1. Install the **Get cookies.txt LOCALLY** browser extension.
2. Log in using a **secondary** Instagram account (never your main account).
3. Export cookies for `instagram.com` and save the file as `instagram_cookies.txt` in the project root (next to `bot.py`).
4. **Never share or commit `instagram_cookies.txt`.**

## 8. Limits

- **50 MB Bot API limit**: Files larger than 50 MB cannot be sent by the bot.
- **DRM**: DRM-protected streams are detected and dropped.
- **Live streams**: Live streams are skipped/rejected.
- **Login-only content**: Content behind logins or paywalls is not supported (except optional Instagram cookies).
- **CAPTCHA sites**: Sites gated by CAPTCHAs or anti-bot challenges are not supported.
- **Ad detection is a heuristic**: `kemungkinan iklan` flags likely ads based on hostnames and stream metadata, not a guarantee.

## 9. Troubleshooting

- **Bot does not start or crashes**: Check `bot.log` and `bot.console.log` for the error.
- **Downloads fail on a site that used to work**: Update `yt-dlp`:

```powershell
.venv\Scripts\python -m pip install -U yt-dlp
```

Expected result: `yt-dlp` updates to the latest version.

- **FFmpeg not found**: Install it with `winget install Gyan.FFmpeg` or set `FFMPEG_DIR` to the folder containing `ffmpeg.exe`.
- **Browser fallback unavailable**: Ensure Microsoft Edge is installed, or run `.venv\Scripts\python -m playwright install chromium` if neither Edge nor Chrome is present.
- **Bot is silent / ignores messages**: Check `ALLOWED_USER_IDS` in `config.py` or your environment variables and verify your Telegram user ID is listed.

## 10. Development

### Project layout

```text
bot.py                  Entry point (main, build_application)
tray_app.pyw            Windows system-tray controller UI (pythonw.exe)
tgbot/                  Telegram bot package (handlers, flows, preview, state, settings, texts, links, workers, telegram_io)
tgbot_tray/             Tray controller backend (process supervisor, log rotation, single-instance IPC)
downloaders/            Media probing & downloading (ytdlp, tiktok, probe, browser, streaminfo, guardproxy, common)
tests/smoke_test.py     Offline bot smoke tests
tests/test_tray_controller.py  Offline tray controller & graceful stop tests
create_shortcuts.ps1    Creates project-folder, Desktop, and Startup shortcuts for tray_app.pyw
assets/app.ico          Application icon for tray_app.pyw and shortcuts
requirements.txt        Python dependencies
```

### Running tests

```powershell
.venv\Scripts\python tests\smoke_test.py
```

Expected result: All smoke tests pass (`OK`).

### Contributing

- Read [AGENTS.md](AGENTS.md) and `.agents/rules/` before making changes (especially if you are an AI coding agent).
- Use **Conventional Commits** (`type(section): description`, e.g. `feat(tiktok): support slideshow download`).
- **Never commit** `config.py`, `instagram_cookies.txt`, `bot.log`, `.env`, `.venv/`, or `temp_downloads/`.
