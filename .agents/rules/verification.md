---
trigger: model_decision
description: "How to test and verify changes before committing"
---

## How to verify changes

`tests/smoke_test.py` provides offline unit and smoke tests (no live Telegram
API). Before reporting done, run the checks that apply, and say plainly which
ones you ran and which you could not run (e.g. nothing here talks to the real
Telegram API without a token).

1. **Compile and lint:** `python -m compileall -q bot.py tgbot downloaders tests` (works
   on Windows shells too). If available, also
   `python -m pyflakes bot.py tgbot/ downloaders/ tests/`.
   Run `python tests/smoke_test.py` before every commit.
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
   - Patch `tgbot.flows.probe_ytdlp`, `tgbot.flows.probe_with_browser`,
     `tgbot.workers.download_browser_stream`, `tgbot.preview.browser_available`,
     `tgbot.settings.browser_available`, `tgbot.handlers.runtime_info`, plus
     `downloaders.probe.browser_available`.
   - Swap `tgbot.workers.DOWNLOADERS` entries for fakes that write `{job_id}.mp4` into
     `TEMP_DIR`.
   - Drive buttons by calling `tgbot.handlers.handle_choice` with
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

