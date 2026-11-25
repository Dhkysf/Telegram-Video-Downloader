---
trigger: glob
globs: "bot.py, tgbot/*.py"
description: "Probe/confirmation flow and Telegram API rules for bot.py and tgbot"
---

## Probe and confirmation flow (tgbot/)

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
  1. Hold `state.semaphore` ONLY around `probe_ytdlp`.
  2. If `should_try_browser(platform, result)`, release `state.semaphore`,
     signal any single-flight `on_browser` event, edit the status to
     `BROWSER_TEXT` (swallowing any `TelegramError` so one user's Telegram
     failure cannot abort a shared probe), and run `probe_with_browser`
     outside `state.semaphore` (it already has `_browser_slots`,
     `MAX_BROWSERS=1`). `force_browser=True` also runs without
     `state.semaphore`. This applies only to generic sites, only for
     UNSUPPORTED/NOT_FOUND/UNKNOWN, never for blocked-address or DRM errors,
     and only if Playwright is installed.
  3. If both fail, report the more informative error.
- **Probe cache & single-flight:** `check_link` caches successful `ProbeResult`
  (`error == OK` and non-empty `candidates`) in `state.probe_cache` for
  `PROBE_CACHE_TTL_SECONDS=300` (max `PROBE_CACHE_MAX_ENTRIES=50`, oldest
  evicted). Key is `(normalized URL, platform, mode)` where `mode` is `"auto"`
  or `"browser"` (lowercase scheme+host, `#fragment` dropped, path/query kept).
  Concurrent probes for the same key share `(asyncio.Future, asyncio.Event)`
  in `state.in_flight_probes` (single-flight; no `await` between check and
  set, cleaned up in `finally`). Setting the event wakes waiters so they also
  edit their own status to `BROWSER_TEXT` (best-effort). Errors are never cached.
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
  - `callback_data` is `dl:<token>:<index>`, `au:<token>:<index>`, `br:<token>`, or `cx:<token>`.
    It must stay under 64 bytes.
  - `PendingChoice` entries live in `state.pending`. They are one-shot and
    owner-only (compare `update.effective_user.id`), expire after
    `PENDING_TTL_SECONDS`, and are purged whenever a new preview is stored.
  - `handle_choice` re-applies the active-job check-and-set.
  - `finish_preview` removes the keyboard and appends a note.
  - Always answer the callback query (`_answer`), even on errors.
- **Commands:** `/start`, `/help`, `/ping`, `/health` (registered in
  `build_application()` and `post_init` via `set_my_commands`).
- **Graceful stop:** `post_init` starts `watch_stop_request(application.stop_running)`
  to poll `stop.request` every 1 s; `post_shutdown` cancels it, and `main()`
  clears any stale `stop.request` at startup.
- **Download runner:** `make_runner()` returns `DOWNLOADERS[platform]` for
  yt-dlp candidates, or `download_browser_stream(candidate, probe, ...)` for
  browser candidates.

## Telegram rules (tgbot/)

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
- **Error handler** logs `NetworkError`/`TimedOut` (other than `BadRequest`) as a
  1-line `WARNING`, logs other errors with `exc_info=context.error`, and sends a
  generic apology.
- **User-facing strings** live in the `*_TEXT`/`*_NOTE`/`*_QUESTION`
  constants and `ERROR_MESSAGES`: Indonesian, friendly, no paths, tokens, or
  raw errors. New `ErrorCode` values need an `ERROR_MESSAGES` entry.

