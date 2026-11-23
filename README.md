# Telegram Video Downloader Bot

A personal Telegram bot for downloading videos: send a link, the bot inspects the video, displays a preview, and sends the video file back to the chat. The bot runs on a Windows PC using python-telegram-bot (long polling) and yt-dlp, augmented with a headless browser (Microsoft Edge via Playwright) for sites unrecognized by yt-dlp.

## Features

- Supports TikTok, Instagram, X/Twitter, YouTube, and over 1,000 other sites via yt-dlp
- Pre-download preview (title, duration, quality, estimated size, thumbnail) with confirmation buttons
- Headless browser fallback: opens the page, clicks play, captures video streams (HLS/DASH/MP4), and flags potential ad streams
- Dedicated ssstik.io scraper fallback for TikTok
- Automatic resolution step-down (720p → 480p → 360p → 240p) to fit files under 50 MB
- Audio-only results (e.g., SoundCloud) sent as audio messages
- Public-network guard: rejects links resolving to local/LAN addresses
- Per-user cooldown, concurrent download limits, and automatic temporary file cleanup
- Rotating log files (`bot.log`, capped at ~6 MB)

## Installation

Prerequisites:

- **Python 3.10 or newer**
- **FFmpeg** (system binary, not a pip package) for merging video+audio and remuxing to MP4
- **Microsoft Edge** (pre-installed on Windows 10/11) for the browser fallback

Steps (from the project directory, no need to activate the venv):

```cmd
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
winget install Gyan.FFmpeg
```

If FFmpeg is not in PATH, set the `FFMPEG_DIR` environment variable to the directory containing `ffmpeg.exe`. Do not install the pip package named `ffmpeg`.

If neither Edge nor Chrome is installed, run this once:

```cmd
.venv\Scripts\python -m playwright install chromium
```

## Configuration

The bot requires a bot token from [@BotFather](https://t.me/BotFather). Choose one of the following methods:

**1. Environment variable**

```cmd
set TELEGRAM_BOT_TOKEN=123456789:YOUR_TOKEN_HERE
.venv\Scripts\python bot.py
```

**2. `config.py` file** in the same folder as `bot.py`:

```python
TELEGRAM_BOT_TOKEN = "123456789:YOUR_TOKEN_HERE"
```

`config.py` is included in `.gitignore`. Never commit or share your real token. Environment variables take precedence: `config.py` is only read if `TELEGRAM_BOT_TOKEN` is not set in the environment.

Optional settings (environment variables):

| Variable | Default | Description |
|---|---|---|
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Number of probe/download jobs allowed to run concurrently |
| `JOB_TIMEOUT` | `300` | Timeout per download job (seconds) |
| `USER_COOLDOWN_SECONDS` | `10` | Minimum cooldown between links per user (seconds) |
| `ALLOWED_USER_IDS` | empty (allow all) | Comma-separated list of Telegram user IDs; other users are ignored |
| `CONFIRM_BEFORE_DOWNLOAD` | enabled | `0`/`false`/`no`/`off`/`tidak` to download immediately without confirmation buttons |
| `BROWSER_FALLBACK` | enabled | Same values to disable the headless browser fallback |
| `FFMPEG_DIR` | empty | Directory containing `ffmpeg`, prepended to PATH |

Since `config.py` is executed as Python, optional settings can also be defined there:

```python
import os
os.environ.setdefault("ALLOWED_USER_IDS", "111111111,222222222")
TELEGRAM_BOT_TOKEN = "123456789:YOUR_TOKEN_HERE"
```

## Usage

1. Open a chat with the bot on Telegram and send `/start` (or `/help`) to view instructions.
2. Send a video link. The link can be embedded inside a sentence, e.g., `check this out https://vt.tiktok.com/xxxx cool`. The bot uses the first link found in the message.
3. The bot responds with "Memeriksa link…", then presents a preview with buttons:
   - ✅ download this video
   - 🔍 search again using the browser (generic sites only)
   - ❌ cancel
4. For sites other than TikTok/Instagram/X/YouTube, if yt-dlp fails to detect a video, the bot automatically opens the page using the browser and presents a screenshot along with up to 5 stream candidates ("Video N"). Candidates marked with ⚠️ are likely advertisements.
5. Once a button is pressed, the bot downloads the stream and replies with the video (or audio) file.

Notes:

- Buttons can only be pressed by the user who sent the link and expire after 15 minutes.
- A user can only have one active job at a time and must wait through the cooldown period (default 10 seconds) before submitting another link.

## Windows Deployment

Two helper scripts are provided to keep the bot running in the background:

- **`start_bot.bat`**: Runs `.venv\Scripts\python.exe bot.py` in a restart loop. If the bot stops or crashes, it automatically restarts after 10 seconds. Console output (e.g., crash tracebacks) is written to `bot.console.log`; output from the previous run is preserved as `bot.console.prev.log`.
- **`start_bot_hidden.vbs`**: Launches `start_bot.bat` without opening a command prompt window.

The bot's own application logs are saved in `bot.log` and automatically rotate every 1 MB (`bot.log.1` … `bot.log.5`).

To run the bot automatically on user login:

1. Press `Win + R`, type `shell:startup`, and press Enter.
2. Create a shortcut to `start_bot_hidden.vbs` inside the opened folder.

To stop the bot: open Task Manager, then terminate both the `python.exe` process (the bot) **and** the `cmd.exe` process running `start_bot.bat`. If only `python.exe` is killed, the restart loop will launch it again.

> Both scripts must maintain CRLF (Windows) line endings. Stop the bot before making edits to `start_bot.bat`.

## Troubleshooting

**FFmpeg not found**
The log displays `FFmpeg not found: merging video/audio and remuxing to MP4 may fail`. The bot will still run, but videos with separated video and audio tracks (such as YouTube) may fail to merge. Solution: run `winget install Gyan.FFmpeg`, then open a new terminal (or restart the bot) so PATH updates take effect. Alternatively, set `FFMPEG_DIR` to the folder containing `ffmpeg.exe`. Verify with `ffmpeg -version`.

**File exceeds 50 MB**
Telegram bots can only upload files up to 50 MB. The bot automatically attempts lower resolutions (down to 240p). If the file is still too large, the bot replies with "❌ Video terlalu besar (lebih dari 50 MB)". For long videos, this is a platform limitation that cannot be bypassed by the bot.

**Link not supported**
A reply saying "🔍 Link ini tidak didukung…" means the bot found no downloadable video: yt-dlp failed, and (for generic sites) the browser fallback also found no playable streams. Common causes:

- DRM-protected videos (Netflix, Disney+, etc.) or live streams: not supported.
- Private content or pages requiring login. For Instagram, place a Netscape-formatted cookie file named `instagram_cookies.txt` in the project root directory.
- yt-dlp is outdated. Update it via `.venv\Scripts\python -m pip install -U yt-dlp`, then restart the bot.
- Links resolving to local/LAN addresses (`localhost`, `192.168.x.x`, etc.) are intentionally blocked.

**Invalid or missing token**
- `TELEGRAM_BOT_TOKEN is not set…`: The token is missing from both environment variables and `config.py`.
- `Telegram rejected TELEGRAM_BOT_TOKEN; check the token with @BotFather`: The token is invalid or has been revoked. Generate a new token via @BotFather.

In both cases, the bot exits with code 1. When running under `start_bot.bat`, these messages appear in `bot.log`, and the batch script will retry every 10 seconds until the token is corrected.
