---
trigger: glob
globs: "downloaders/browser.py, downloaders/streaminfo.py, downloaders/guardproxy.py, downloaders/probe.py"
description: "Playwright browser capture, play/stop logic, stream parsing, and ad heuristics"
---

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

