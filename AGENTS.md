# AGENTS.md — Telegram Video Downloader Bot (v3)

Guidance for AI coding agents working in this repository. Read it fully before
changing code. The rules under "Invariants" are not optional: they protect the
user's home network, the bot token, and Telegram's upload limits.

## What this project is

A personal Telegram bot (python-telegram-bot v22, long polling, asyncio). A user
sends a message containing a link, and the bot handles it in three steps.

1. **Probe** (no download). yt-dlp `extract_info(download=False)` reads the
   title, duration, quality, estimated size, and thumbnail.
   - If yt-dlp finds nothing on a generic site, a **headless browser**
     (Playwright) opens the page.
   - The browser clicks play, records the video streams the page loads
     (HLS/DASH/MP4), describes them, and flags likely advertisements.
2. **Confirm.** The bot replies with a preview: a thumbnail, or a page
   screenshot when the browser was used. Inline buttons let the user pick:
   download / choose stream N / "not this one, search with browser" / cancel.
   Nothing is downloaded before the user confirms. Confirmation can be turned
   off.
3. **Download and send.** yt-dlp downloads the chosen item. Browser-captured
   streams reuse the browser's UA, Referer, and cookies. The bot sends the
   file back as a reply (max 50 MB), then deletes every temporary file.

Platforms and runtime:

- Known platforms get dedicated handling: TikTok (yt-dlp, then an ssstik.io
  scraper fallback), Instagram (optional cookie file), X/Twitter, YouTube.
  Any other public http(s) link is `generic`.
- Audio-only results (e.g. SoundCloud) are sent with `send_audio`.
- The bot runs on the owner's **Windows home PC**, started hidden at logon by
  `start_bot_hidden.vbs`, which runs `start_bot.bat`. That script restarts the
  bot in a loop and logs to `bot.log`.
- The browser uses the built-in **Microsoft Edge** (`channel="msedge"`),
  headless, with no visible window.
- Single process. No database. In-memory state only, plus temp files.
- User-facing text is **Bahasa Indonesia**. Code, comments, logs: English.

## Layout

```
bot.py                  Entry point. Startup, handlers, Telegram I/O, preview/confirm UI.
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
requirements.txt        PTB ~=22.5, yt-dlp >= recent, requests ~=2.32, playwright ~=1.65.
start_bot.bat           Windows restart loop (CRLF line endings!).
start_bot_hidden.vbs    Runs start_bot.bat with no window (CRLF line endings!).
.gitignore              Ignores config.py, .env, instagram_cookies.txt, temp_downloads/, ...
```

Not in git, but present at runtime: `config.py` (token), `instagram_cookies.txt`,
`temp_downloads/`, `bot.log`, `.venv/`.

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

Fixed timeouts in `bot.py`: `PROBE_TIMEOUT=90`, `BROWSER_PROBE_TIMEOUT=180`
(includes waiting for the single browser slot), and `PENDING_TTL_SECONDS=900`
(how long buttons stay valid).

## Architecture invariants (do not break)

1. **Separation.** `bot.py` holds no download, probe, or parsing logic.
   `downloaders/` never imports `telegram`.
2. **Single source of constants.** `TEMP_DIR`, size limits, timeouts, the
   User-Agent, and ad-host lists are defined once in `downloaders/common.py`.
   Never duplicate them.
3. **No import-time side effects** besides defining constants. The
   module-level `threading.BoundedSemaphore` in `browser.py` is the one
   accepted exception. Directory creation, temp cleanup, and
   `install_network_guard()` run from `main()` in `bot.py`.
   `browser.py` calls `install_network_guard()` again, which is idempotent.
4. **Startup order in `main()`:** logging, then token (`redact_token_in_logs`),
   then FFmpeg, then `install_network_guard()`, then temp dir
   (`prepare_temp_dir`), then build the Application, then
   `run_polling(drop_pending_updates=True)`.
5. **Blocking workers never raise.**
   - Downloaders `fn(url, job_id, cancel_event) -> DownloadResult` and probes
     (`probe_ytdlp`, `probe_with_browser`) `-> ProbeResult` always return an
     explicit result. `detail` is for logs only.
   - They are only called through `run_worker()` in `bot.py`
     (`asyncio.to_thread` inside `asyncio.wait_for(asyncio.shield(...))`).
   - Never call yt-dlp, `requests`, or Playwright on the event loop.
6. **Cooperative cancellation.** `to_thread` cannot be killed. Long loops,
   including the browser capture loop and stream analysis, check
   `cancel_event`. Backoff uses `cancel_event.wait(delay)`. yt-dlp is stopped
   by the progress hook raising `DownloadCancelled`.
7. **Job files.** Every file a job writes lives in `TEMP_DIR` and starts with
   the job ID (`f"{platform}_{uuid4().hex}"`).
   - Probes use a `probe-<platform>_...` prefix and clean up in `finally`.
   - Browser cookie files are `{job_id}.browser-cookies.txt`.
   - `process_download` always calls `cleanup_job(job_id)` in `finally`.
   - A worker that outlives its timeout gets a late-cleanup done-callback.
8. **Public-network guard (security-critical).** The bot runs on a home LAN.
   - **Socket guard:** `install_network_guard()` patches
     `socket.socket.connect`/`connect_ex`. Inside `public_network_only()`
     (thread-local), any connection to a non-global IP raises
     `BlockedAddressError`. This covers loopback, RFC1918 private ranges,
     link-local/cloud metadata, CGNAT, and multicast. It checks the real IP,
     so redirects and DNS tricks like `127.0.0.1.nip.io` are blocked.
   - All Python network code in `downloaders/` (yt-dlp, probe, scraper,
     stream analysis) runs inside `public_network_only()`.
   - **Browser:** Chromium is a separate process, so the socket guard cannot
     see its traffic. **Playwright request routing does not see redirects.**
     That is why every browser session runs behind `GuardProxy`:
     `launch(proxy={"server": proxy.url})`.
     - The proxy opens every upstream connection under the socket guard.
     - Playwright auto-adds `<-loopback>` to the bypass list, so localhost
       also goes through the proxy and is refused.
     - `--disable-quic` and `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`
       stop UDP side channels.
     - Service workers are blocked (`service_workers="block"`) because they
       bypass routing.
     - The route guard (`_url_allowed`) is only a fast early filter. Never
       remove the proxy, and never launch a browser without it.
   - Never spawn external programs that open remote URLs themselves (ffprobe
     or ffmpeg on URLs, curl, another browser) unless their traffic is
     guarded too. Stream metadata is parsed in Python for exactly this reason.
   - Telegram traffic runs on the event loop thread, so it is unaffected.
9. **Host matching** uses only `host == domain or host.endswith("." + domain)`
   on `urlparse(url).hostname`. Substring matching is forbidden, and
   `x.com@evil.com` resolves to host `evil.com`. `detect_platform()` rejects
   obviously local hosts early (`is_blocked_host`).
10. **Secrets.** Never log or print the bot token, cookie contents, or raw
    exceptions to users.
    - The `httpx` logger stays at WARNING.
    - `RedactingFormatter` masks the token. This is needed because PTB's
      `InvalidToken` text contains it, and `run_polling` is wrapped to exit 1
      cleanly on `InvalidToken`.
    - Browser cookies live only in memory (`ProbeResult.cookies`, for at most
      the button TTL) and in job-prefixed temp files.
    - Never commit or create `config.py`, `.env`, or `instagram_cookies.txt`
      with real values. Never ask the owner to paste a token into chat.
11. **No download without consent.** When confirmation is on, nothing is
    downloaded until the user presses a button. With confirmation off,
    `auto_pick()` must never pick a candidate flagged `likely_ad`.

## Probe and confirmation flow (bot.py)

- `handle_text`:
  1. `extract_first_url` + `detect_platform`.
  2. `try_start_job` (cooldown + one active job; no `await` between check and
     set).
  3. `check_link`, which shows the "Memeriksa..." status, then runs
     `probe_link`.
  4. Then either `send_preview`, or `process_download` directly when
     confirmation is off.
  5. Release the user in `finally`.
- `probe_link`:
  1. `probe_ytdlp`.
  2. If `should_try_browser(platform, result)`, edit the status to
     `BROWSER_TEXT` and run `probe_with_browser`. This applies only to
     generic sites, only for UNSUPPORTED/NOT_FOUND/UNKNOWN, never for
     blocked-address or DRM errors, and only if Playwright is installed.
  3. If both fail, report the more informative error.
- **Probe cache & single-flight:** `check_link` caches successful `ProbeResult`
  (`error == OK` and non-empty `candidates`) in `state.probe_cache` for
  `PROBE_CACHE_TTL_SECONDS=300` (max `PROBE_CACHE_MAX_ENTRIES=50`, oldest
  evicted). Key is `(normalized URL, platform, mode)` where `mode` is `"auto"`
  or `"browser"` (lowercase scheme+host, `#fragment` dropped, path/query kept).
  Concurrent probes for the same key share an in-flight `asyncio.Future` in
  `state.in_flight_probes` (single-flight). Errors are never cached.
- For TikTok, a failed yt-dlp probe still returns one candidate marked
  `info_unavailable`, because the scraper may download it anyway.
- **Preview** (`build_preview`):
  - yt-dlp results show title, site, uploader, duration, quality (shortest
    side, so portrait 720x1280 = 720p), and estimated size, with buttons
    ✅ / 🔍 browser (generic only) / ❌.
  - Browser results show a page screenshot plus up to 5 numbered candidates
    (⚠️ = likely ad), with buttons "Video N (m:ss)" / ❌.
  - Captions are capped at 1024 chars. If the text is longer, or Telegram
    rejects the thumbnail (`BadRequest`), fall back to a text message.
  - Escape every dynamic value with `html.escape`, and use `parse_mode=HTML`.
- **Buttons:**
  - `callback_data` is `dl:<token>:<index>`, `br:<token>`, or `cx:<token>`.
    It must stay under 64 bytes.
  - `PendingChoice` entries live in `state.pending`. They are one-shot and
    owner-only (compare `update.effective_user.id`), expire after
    `PENDING_TTL_SECONDS`, and are purged whenever a new preview is stored.
  - `handle_choice` re-applies the active-job check-and-set.
  - `finish_preview` removes the keyboard and appends a note.
  - Always answer the callback query (`_answer`), even on errors.
- **Commands:** `/start`, `/help`, `/ping`, `/health` (registered in
  `build_application()` and `post_init` via `set_my_commands`).
- **Download runner:** `make_runner()` returns `DOWNLOADERS[platform]` for
  yt-dlp candidates, or `download_browser_stream(candidate, probe, ...)` for
  browser candidates.

## yt-dlp rules (downloaders/ytdlp.py)

- **Option names are exact:** `outtmpl` = `TEMP_DIR / f"{job_id}.%(ext)s"`,
  `merge_output_format="mp4"`, `noplaylist=True`, `playlist_items="1"`,
  `max_filesize`, `socket_timeout=30`, `retries=3`, `fragment_retries=3`,
  `quiet`, `no_warnings`. Extra options (cookiefile, http_headers,
  force_generic_extractor) go through `extra_options`. Do not invent options.
  Check the yt-dlp source or docs first.
- **Resolution cap** goes through `format_sort` (`res:N`, `vcodec:h264`,
  `ext:mp4:m4a`), never `[height<=N]`. A height filter rejects portrait
  videos.
- **Size filters** keep unknown sizes: `[filesize<?N][filesize_approx<?N]`.
  The audio budget is clamped (`min(6 MiB, limit // 4)`).
- **Fitting under 50 MB:** caps are tried 720 → 480 → 360 → 240.
  - A `max_filesize` skip is **silent**. It is detected via the logger's
    `max-filesize` marker and the estimated size, and returns `TOO_LARGE`.
  - Streams of unknown size (HLS/DASH, direct links) are stopped by the
    progress hook raising `_TooLargeDuringDownload` (a `DownloadCancelled`
    subclass). It must be caught **before** `DownloadCancelled` and maps to
    TOO_LARGE with a lower-resolution retry.
- **Output path:** read from `info["requested_downloads"][0]["filepath"]`.
  Fall back to globbing `{job_id}.*`, excluding `.part/.ytdl/.temp`,
  `.fNNN.` stream files, and cookie files.
- **Remux** with `FFmpegVideoRemuxer` and `preferedformat=REMUX_MAPPING`
  (`webm>mp4/mkv>mp4/...`). Do not use plain `"mp4"`: that would wrap
  audio-only files. Never append `.mp4` to a filename.
- **Instagram cookies:** copied to a per-job `{job_id}.cookies.txt` (yt-dlp
  writes cookies back).
- **Browser streams** are downloaded with `force_generic_extractor=True`,
  `http_headers={"Referer": page_url, "User-Agent": browser UA}`, and the
  browser cookies written as a Netscape file (`write_cookie_file`).
- **Error mapping** is `classify_error()`. Order matters:
  1. blocked/DRM → UNSUPPORTED
  2. 429/rate limit → RATE_LIMITED
  3. login/private/sign in/cookies → PRIVATE_OR_LOGIN
  4. unsupported url → UNSUPPORTED
  5. ffmpeg → UNKNOWN
  6. 404/unavailable/not found → NOT_FOUND
  7. 5xx/timeouts → NETWORK

  Live streams return `ErrorCode.LIVE`. Only RATE_LIMITED and 5xx are retried
  (exponential backoff with jitter, max 3 attempts), in both probe and
  download.
- **TikTok fallback** (`tiktok.py`) runs only when yt-dlp fails, and not for
  TOO_LARGE or TIMEOUT.
  - Endpoints, form fields, and regexes are module constants.
  - An empty 200 body from ssstik is a soft rate limit, reported as
    RATE_LIMITED.

## Browser and stream rules (browser.py, streaminfo.py)

- **Launch:** Playwright **sync** API, only inside a worker thread (no running
  event loop in that thread).
  - Channels are tried in order: `msedge` → `chrome` → bundled Chromium.
  - Headless only. "HeadlessChrome" is removed from the User-Agent.
  - One browser at a time (`MAX_BROWSERS=1`, about 300–500 MB RAM each).
  - The browser is always closed in `finally`.
- **Capture:**
  - `page.on("response")` keeps responses whose content type or extension
    looks like a whole video: `.m3u8`, `.mpd`, `.mp4/.webm/.mov/.m4v`,
    `video/*`, mpegurl, dash+xml.
  - Segments (`.ts`, `.m4s`, `video/mp2t`, `seg-N`, ...) are dropped.
    Duplicates are deduped by host+path.
  - Popups are closed.
- **Play logic** (`PLAY_SCRIPT`, run in every frame every 3 s):
  - Mute everything.
  - Call `video.play()`.
  - Click "skip ad" buttons.
  - Click a play button **only if nothing is already playing**, because
    clicking again would pause.
- **Stop rule:**
  - Wait at least 8 s.
  - Then stop once a non-ad-looking stream exists and nothing new has
    appeared for 6 s.
  - Stop at the latest after 45 s, so the bot keeps waiting through a
    pre-roll ad.
- **Analysis without ffprobe:**
  - HLS: sum `#EXTINF` for duration; read `RESOLUTION`. `SAMPLE-AES`,
    Widevine, PlayReady, or FairPlay key formats mean DRM (dropped). A
    playlist without `#EXT-X-ENDLIST`/VOD is live (dropped). Variants of a
    captured master are not listed twice.
  - DASH: read `mediaPresentationDuration` and `height` attributes.
    `<ContentProtection>` means DRM, and `type="dynamic"` means live.
  - MP4: Range-fetch the first 256 KB, and the last 512 KB if needed, then
    parse `mvhd` (duration) and `tkhd` (dimensions). `moof` without `mvhd`
    is a fragment (dropped).
  - The whole analysis has a 30 s budget and runs under the network guard,
    with the browser's UA, Referer, and cookies.
- **Ad heuristic:** `likely_ad` is set when either of these holds:
  - The stream or its frame host is in `AD_HOST_SUFFIXES` (`2mdn.net`,
    `doubleclick.net`, ...), or the path matches `AD_PATH_PATTERN`.
  - The stream is ≤ 60 s next to another stream that is ≥ 90 s and ≥ 3×
    longer.

  It is a heuristic, which is why the user confirms. Ranking puts non-ads
  first, then non-audio, then the longest, then the highest quality, and
  keeps at most 5.

## Telegram rules (bot.py)

- `concurrent_updates(True)` is on. Every check-and-set on `state` (cooldown,
  active users, pending) must stay free of `await` between check and set.
- **Retry wrapper `tg_call()`, in this order:**
  1. `RetryAfter`: sleep `retry_after` (int or timedelta), max 2 retries.
  2. `(BadRequest, Forbidden)`: re-raise, never retry. `BadRequest` subclasses
     `NetworkError`, so this order matters.
  3. `(TimedOut, NetworkError)`: backoff, max 2 retries.
- **Uploads** reopen the file inside the retried callable. Use
  `write_timeout=120` and `read_timeout=60`. Use
  `reply_parameters=ReplyParameters(..., allow_sending_without_reply=True)`.
  Link previews are disabled with `LinkPreviewOptions(is_disabled=True)`, not
  `disable_web_page_preview`.
- **BadRequest on edit/delete** of status or preview messages is ignored with
  a DEBUG log. `Forbidden` (user blocked the bot) is logged at INFO and the
  flow stops.
- **Error handler** logs with `exc_info=context.error` and sends a generic
  apology.
- **User-facing strings** live in the `*_TEXT`/`*_NOTE`/`*_QUESTION`
  constants and `ERROR_MESSAGES`: Indonesian, friendly, no paths, tokens, or
  raw errors. New `ErrorCode` values need an `ERROR_MESSAGES` entry.

## How to verify changes

There is no test suite in the repo. Before reporting done, run the checks that
apply, and say plainly which ones you ran and which you could not run (e.g.
nothing here talks to the real Telegram API without a token).

1. **Compile and lint:** `python -m compileall -q bot.py downloaders` (works
   on Windows shells too). If available, also
   `python -m pyflakes bot.py downloaders/`.
2. **Pure logic.** Test `extract_first_url` and `detect_platform` with real PTB
   `Message`/`MessageEntity` objects (`parse_entities` needs them).

   Must hold:
   - A link embedded in a sentence is found.
   - A scheme-less entity (`youtu.be/x`) works.
   - `TEXT_LINK` works.
   - `x.com@evil.com` → generic, never twitter.
   - `localhost`, `127.0.0.1`, `192.168.x.x`, `[::1]`, `169.254.169.254`,
     `*.local`, `*.lan` → None.

   Also check that `build_preview`/`candidate_line` output is valid HTML,
   with titles containing `<`, `&` escaped.
3. **Handler flow without a token.**
   - Patch `telegram.Message.reply_text` and `reply_photo` at class level
     with async fakes that return objects with
     `edit_text`/`edit_caption`/`delete`.
   - Patch `bot.probe_ytdlp`, `bot.probe_with_browser`,
     `bot.download_browser_stream`, and `bot.browser_available`, plus
     `downloaders.probe.browser_available`.
   - Swap `bot.DOWNLOADERS` entries for fakes that write `{job_id}.mp4` into
     `TEMP_DIR`.
   - Drive buttons by calling `bot.handle_choice` with
     `SimpleNamespace(callback_query=FakeQuery(data), effective_user=User(...))`.

   Cover these cases, and check that `TEMP_DIR` and `active_users` are empty
   afterwards:
   - confirm → download
   - "bukan ini" → browser preview → choose N
   - cancel
   - a different user pressing (refused)
   - reused or expired token (refused, keyboard dropped)
   - yt-dlp UNSUPPORTED → automatic browser fallback
   - DRM/blocked → no browser
   - thumbnail rejected → text preview
   - TikTok `info_unavailable`
   - confirmation off → auto download, and ads-only → refused
   - probe cache: TTL hit/expiry, error not cached, `"br"` vs `"auto"`,
     concurrent single-flight, max entries eviction
   - `/ping` and `/health`: allow-list enforcement, non-blocking under held
     semaphore (< 1 s), job counters (`1` / `1`), missing Playwright fallback,
     valid HTML, no secret/path/user-ID leaks
   - plus the v2 basics: concurrency, cooldown, active job, >50 MB, timeout,
     RetryAfter/TimedOut on upload
4. **Live smoke tests** (network): call `install_network_guard()` first.
   - `probe_ytdlp` + downloads for a few sites.
   - `probe_with_browser` on known pages:
     - `https://hlsjs.video-dev.org/demo/`: one HLS candidate, ~10:35, 1080p.
       Downloading it must step down to 360p and stay under 50 MB.
     - `https://www.w3schools.com/html/html5_video.asp`: a direct MP4,
       ~10 s.
     - `https://googleads.github.io/googleads-ima-html5/vsi/`: only the ad
       loads, so it must be flagged `likely_ad`.
   - Datacenter IPs often fail on YouTube, Instagram, Vimeo, and Bilibili.
     That is the environment, not a bug.
5. **Guard tests (mandatory after touching network code):**
   - `get_generic()` and `probe_with_browser()` on
     `https://postman-echo.com/redirect-to?url=http%3A%2F%2F127.0.0.1%3A<port>%2F`
     must not reach the local service: UNSUPPORTED, and the proxy logs
     "blocked". httpbin.org answers 403, so do not use it.
   - Inside a proxied browser on a public page, `fetch()` to `127.0.0.1`,
     `[::1]`, and `127.0.0.1.nip.io` must be refused by the proxy
     (`GuardProxy.blocked` increases).
6. **Token leak test:** run
   `TELEGRAM_BOT_TOKEN=123456:FAKEtoken... python bot.py` and grep the output
   for the fake token. The count must be 0, and the exit code must be 1.

## Common tasks

- **Add a dedicated platform:** add its domains to `PLATFORM_DOMAINS`, a
  display name to `PLATFORM_NAMES`, a `get_<name>()` in `ytdlp.py` (exported
  in `__init__.py`), and an entry in `DOWNLOADERS`. Keep exact host matching.
  Decide whether its probe failures should still offer a download (like
  TikTok).
- **yt-dlp breaks for a site:** first `pip install -U yt-dlp` and bump the
  minimum version in `requirements.txt`. Only write custom extraction if
  updating does not help.
- **Tune ad detection:** edit `AD_HOST_SUFFIXES`/`AD_PATH_PATTERN` in
  `common.py`, or the `SHORT_AD_*` constants in `streaminfo.py`. Re-run the
  IMA sample page test.
- **Tune browser timing:** change the constants at the top of `browser.py`.
  Keep `BROWSER_PROBE_TIMEOUT` in `bot.py` comfortably above
  `MAX_CAPTURE_SECONDS` + analysis budget + slot wait.
- **New config option:** read it in `load_settings()` with a safe default,
  add it to `Settings`, log it (never secrets), and document it in this file.
- **Editing `start_bot.bat` / `start_bot_hidden.vbs`:** keep CRLF line
  endings, keep `cd /d "%~dp0"`, and keep the restart loop. Wait with
  `ping -n 11 127.0.0.1 >nul`, not `timeout`, because `timeout` fails in a
  hidden window.

## Do not

- Add Docker, databases, webhooks, web frameworks, scheduled jobs, or the
  `ffmpeg` pip package unless the owner explicitly asks.
- Raise the 50 MB limit. It is the Telegram Bot API upload limit.
- Run the browser headed, without `GuardProxy`, with service workers enabled,
  or more than one at a time.
- Attempt to bypass DRM, paywalls, captchas, or logins, or ship code that does.
  DRM streams are detected and dropped on purpose.
- Remove the confirmation step's owner check or TTL, or let auto-pick choose
  a likely ad.
- Touch `config.py`, `instagram_cookies.txt`, or `bot.log` contents, or paste
  them into chat or commits.
- Leave placeholders or TODOs. Deliver complete, runnable files with type
  hints and docstrings (PEP 8, Python 3.10+, cross-platform paths via
  `pathlib` from `PROJECT_ROOT`).

## Code Navigation

- To understand the code structure, find functions, or trace the flow between files, use the `codegraph_explore` tool first before reading the files one by one.
- Before modifying a function, check who calls it using CodeGraph to ensure your changes don’t break other parts of the code.
- Read a file directly only if you need its full contents to edit it.

## Git & Commit

- Once a task is complete and the code has been tested, make a commit.
- Before committing, run `git status` and `git diff` to see what has changed.
- Use `git add <filename>` only for files related to that task. Avoid `git add .`.
- NEVER commit `config.py`, `.env`, cookie files, `bot.log`, `.venv/`, or `temp_downloads/`.
- Commit messages must follow the Conventional Commits format: `type(section): description`
  - type: feat, fix, docs, refactor, perf, test, chore
  - section (optional): module name, e.g., tiktok, ytdlp, browser
  - description in English, all lowercase, no period at the end, maximum 72 characters
  - Example: `feat(tiktok): support slideshow download`
- One commit per type of change. Do not combine new features and bug fixes in a single commit.
- If a commit is rejected by the pre-commit hook, fix the issue and try again. Do not use `--no-verify`.
- DO NOT `git push` unless I ask you to.

Translated with DeepL.com (free version)