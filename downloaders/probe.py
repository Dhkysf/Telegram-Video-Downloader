"""Link checking before download: yt-dlp first, headless browser as fallback.

The bot calls :func:`probe_ytdlp` first. When that fails on a generic site,
:func:`should_try_browser` decides whether opening the page in a headless
browser could help, and :func:`probe_with_browser` does it. Both steps are
separate so the bot can tell the user what is happening in between.
"""

from __future__ import annotations

import threading
from typing import Optional

from .browser import browser_available, capture_page
from .common import BLOCKED_ADDRESS_MESSAGE, ErrorCode, ProbeResult
from .streaminfo import analyze_capture

# yt-dlp outcomes where a browser might still find the video.
BROWSER_RETRY_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.UNSUPPORTED, ErrorCode.NOT_FOUND, ErrorCode.UNKNOWN}
)
GENERIC_PLATFORM: str = "generic"


def should_try_browser(platform: str, result: ProbeResult) -> bool:
    """Return True if a failed yt-dlp probe is worth retrying in a browser.

    Only for generic sites (known platforms have dedicated handling), only
    for "nothing found"-type errors, never for blocked local addresses or
    DRM-protected videos (a browser cannot get around either).
    """
    detail = result.detail.lower()
    return (
        platform == GENERIC_PLATFORM
        and result.error in BROWSER_RETRY_CODES
        and BLOCKED_ADDRESS_MESSAGE not in detail
        and "drm" not in detail
        and browser_available()
    )


def probe_with_browser(url: str, cancel_event: Optional[threading.Event] = None) -> ProbeResult:
    """Open ``url`` in a headless browser and list the videos it plays.

    Blocking; run in a worker thread. Never raises.
    """
    capture = capture_page(url, cancel_event)
    return analyze_capture(capture, cancel_event)
