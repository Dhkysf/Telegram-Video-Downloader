# AGENTS.md — Telegram Video Downloader Bot (v3)

Guidance for AI coding agents working in this repository. Read it fully before
changing code. The rules under "Invariants" are not optional: they protect the
user's home network, the bot token, and Telegram's upload limits.

## Git & Commit

- Once a task is complete and the code has been tested (including `python tests/smoke_test.py`), make a commit.
- Before committing, run `git status` and `git diff` to see what has changed. Review uncommitted changes using the `ponytail-review` skill. Fix all identified bugs and risks, then double-check. Other findings (Speed, Lean) should simply be reported; do not address them without my permission.
- Use `git add <filename>` only for files related to that task. Avoid `git add .`.
- NEVER commit `config.py`, `.env`, cookie files, `bot.log`, `tray.log*`, `tray_settings.json`, `tray.lock`, `tray.ipc`, `stop.request`, `.venv/`, or `temp_downloads/`.
- Commit messages must follow the Conventional Commits format: `type(section): description`
  - type: feat, fix, docs, refactor, perf, test, chore
  - section (optional): module name, e.g., tiktok, ytdlp, browser
  - description in English, all lowercase, no period at the end, maximum 72 characters
  - Example: `feat(tiktok): support slideshow download`
- One commit per type of change. Do not combine new features and bug fixes in a single commit.
- If a commit is rejected by the pre-commit hook, fix the issue and try again. Do not use `--no-verify`.
- DO NOT `git push` unless I ask you to.
- At the end of your response, include a summary of the review results: what was fixed and what was intentionally left as is.


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
- The bot runs on the owner's **Windows home PC**, managed for daily use by
  `tray_app.pyw` (`.venv\Scripts\pythonw.exe`, autostarted via Startup
  shortcut from `create_shortcuts.ps1`; `.venv\Scripts\python.exe bot.py` is
  the manual console fallback). The tray controller rotates `bot.console.log`
  -> `bot.console.prev.log`, logs `yt-dlp` updates to `bot.update.log`, and
  writes its own rotating log to `tray.log` (`bot.py` writes rotating
  application logs to `bot.log`).
- The browser uses the built-in **Microsoft Edge** (`channel="msedge"`),
  headless, with no visible window.
- Single process. No database. In-memory state only, plus temp files.
- User-facing text could be **Bahasa Indonesia**. Code, comments, logs: English.
- Always run Python via `.venv\Scripts\python.exe`; do not just use `python` (that command fails in the Antigravity sandbox). For pip: `.venv\Scripts\python.exe -m pip ...`
- The warning “Failed to find real location of ...pythoncore...python.exe” when running `.venv\Scripts\python.exe` in the sandbox is normal and harmless. Ignore it; do not try to fix it.

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
   - They are only called through `run_worker()` in `tgbot/workers.py`
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


## Scoped Rules Index

- `.agents/rules/core.md` (`always_on`): Project layout, setup/run commands, and environment configuration.
- `.agents/rules/telegram.md` (`glob: bot.py, tgbot/*.py`): Probe/confirmation flow and Telegram API rules.
- `.agents/rules/ytdlp.md` (`glob: downloaders/ytdlp.py, downloaders/tiktok.py`): yt-dlp options, size/format limits, remuxing, error mapping, and TikTok fallback.
- `.agents/rules/browser.md` (`glob: downloaders/browser.py, downloaders/streaminfo.py, downloaders/guardproxy.py, downloaders/probe.py`): Playwright capture, play/stop logic, stream parsing, and ad heuristics.
- `.agents/rules/verification.md` (`model_decision`): How to test and verify changes before committing.
- `.agents/rules/common-tasks.md` (`model_decision`): Recipes for adding a platform, fixing yt-dlp breakage, tuning ads/browser timing, adding config options, and editing startup scripts.
