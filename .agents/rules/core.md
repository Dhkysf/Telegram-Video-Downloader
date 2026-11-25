---
trigger: always_on
description: "Project layout, setup, run commands, and environment configuration"
---

## Layout

```
bot.py                  Entry point: main() and build_application() only.
tray_app.pyw            Windows system-tray controller UI (pystray + tkinter + Pillow).
tgbot_tray/             Tray controller backend (controller.py: supervisor, log rotation, IPC).
tgbot/                  Telegram bot package (one-way imports: texts/links/settings
                        -> state -> telegram_io -> preview/workers -> flows -> handlers).
  __init__.py           Package marker (no imports or side effects).
  texts.py              User-facing Bahasa Indonesia strings + ERROR_MESSAGES.
  settings.py           Settings, load_settings, env helpers, load_token, logging/RedactingFormatter, configure_ffmpeg.
  links.py              URL extraction, detect_platform, site_name, PLATFORM_* maps.
  state.py              BotState, PendingChoice, probe cache, get_state, is_allowed, try_start_job.
  telegram_io.py        tg_call, safe_reply/edit/delete, send_video_file.
  workers.py            run_worker, late cleanup, DOWNLOADERS, make_runner.
  preview.py            Format helpers, build_preview, send_preview, finish_preview, auto_pick.
  flows.py              probe_link, check_link, process_download.
  handlers.py           start/help, ping, health, handle_text, handle_choice, error_handler.
tests/
  smoke_test.py         Offline smoke tests (stdlib + PTB only, no token/network).
  test_tray_controller.py  Offline tray controller and graceful stop tests.
downloaders/            Everything that touches the network for media. Never imports telegram.
  __init__.py           Re-exports the public API (keep __all__ in sync).
  common.py             Shared constants, ErrorCode, DownloadResult, MediaCandidate,
                        ProbeResult, ad heuristics, cookie-file writer, temp helpers,
                        and the public-network guard.
  ytdlp.py              yt-dlp engine: probe_ytdlp(), download_with_ytdlp(),
                        get_youtube/twitter/instagram/tiktok/generic,
                        download_browser_stream(), describe_site().
  tiktok.py             ssstik.io scraper (fallback for TikTok only).
  probe.py              should_try_browser() + probe_with_browser() (orchestration).
  browser.py            Playwright capture: launch, request guard, play-clicking,
                        stream capture, screenshot, cookies.
  streaminfo.py         Describe captured streams (HLS/DASH/MP4 parsing), drop
                        segments/DRM/live, flag ads, rank candidates.
  guardproxy.py         Local HTTP/CONNECT proxy; all browser traffic goes through it.
requirements.txt        PTB ~=22.5, yt-dlp >= recent, requests ~=2.32, playwright ~=1.65, pystray >= 0.19, Pillow >= 10.0.
assets/app.ico          Application icon for tray_app.pyw and shortcuts.
create_shortcuts.ps1    Creates project-folder, Desktop, and Startup shortcuts for tray_app.pyw.
.gitignore              Ignores config.py, .env, instagram_cookies.txt, temp_downloads/, ...
```

Not in git, but present at runtime: `config.py` (token), `instagram_cookies.txt`,
`temp_downloads/`, `bot.log`, `bot.ready`, `.venv/`.

## Setup and run

FFmpeg is a **system binary**, not a pip package. On Windows install it with
`winget install Gyan.FFmpeg`. Or point `FFMPEG_DIR` at the folder that contains
`ffmpeg`.

Windows (the owner's machine; call the venv's Python directly, no activation):
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python bot.py
```
Linux/macOS: the same commands with `.venv/bin/python`.

On Windows, Playwright needs no browser download because it uses Edge. If
neither Edge nor Chrome is installed, run
`python -m playwright install chromium` once. On Linux add `--with-deps`.
The bundled Chromium lacks H.264/AAC, so prefer a real Chrome/Edge.

The token comes from env `TELEGRAM_BOT_TOKEN`, or from `config.py` next to `bot.py`:
`TELEGRAM_BOT_TOKEN = "..."`. Without a token the bot logs an error and exits 1.

### Configuration (environment variables)

- `TELEGRAM_BOT_TOKEN` (required): the bot token.
- `MAX_CONCURRENT_DOWNLOADS` (default `2`): global semaphore, shared by probes
  and downloads.
- `JOB_TIMEOUT` (default `300`): seconds per download job.
- `USER_COOLDOWN_SECONDS` (default `10`): per-user cooldown between links.
- `ALLOWED_USER_IDS` (default unset): comma-separated Telegram user IDs. When
  set, all other users are silently ignored. Set but invalid = deny everyone.
- `CONFIRM_BEFORE_DOWNLOAD` (default on): `0/false/no/off/tidak` turns it off.
  When off, the bot auto-picks the best candidate and refuses if only likely
  ads were found.
- `BROWSER_FALLBACK` (default on): turns the headless browser step on or off.
  It is also off automatically when Playwright is not installed.
- `FFMPEG_DIR` (default unset): prepended to PATH at startup.

Quirk: `config.py` is executed **only when `TELEGRAM_BOT_TOKEN` is not set in
the environment**. The owner sets extra options there with
`import os; os.environ.setdefault("ALLOWED_USER_IDS", "123")`, which works only
because of this. Do not break this path, and do not move settings loading
before `load_token()`.

Fixed timeouts: `PROBE_TIMEOUT=90` and `BROWSER_PROBE_TIMEOUT=180` in `tgbot/flows.py`
(includes waiting for the single browser slot), and `PENDING_TTL_SECONDS=900` in
`tgbot/state.py` (how long buttons stay valid).

