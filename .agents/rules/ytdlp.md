---
trigger: glob
globs: "downloaders/ytdlp.py, downloaders/tiktok.py"
description: "yt-dlp options, format/size limits, remuxing, error mapping, and TikTok fallback"
---

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
  5. no space left / errno 28 / disk full → DISK_FULL
  6. ffmpeg → UNKNOWN
  7. 404/unavailable/not found → NOT_FOUND
  8. 5xx/timeouts → NETWORK

  Live streams return `ErrorCode.LIVE`. Only RATE_LIMITED and 5xx are retried
  (exponential backoff with jitter, max 3 attempts), in both probe and
  download.
- **TikTok fallback** (`tiktok.py`) runs only when yt-dlp fails, and not for
  TOO_LARGE, TIMEOUT, or DISK_FULL. `OSError(ENOSPC)` on write maps to
  `ErrorCode.DISK_FULL`.
  - Endpoints, form fields, and regexes are module constants.
  - An empty 200 body from ssstik is a soft rate limit, reported as
    RATE_LIMITED.

