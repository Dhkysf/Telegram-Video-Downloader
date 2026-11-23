"""Headless-browser fallback: open a page like a visitor and capture video streams.

Used only when yt-dlp cannot find a video on a generic page. Playwright drives
a real browser without a window (Microsoft Edge on Windows, else Chrome, else
Playwright's bundled Chromium), clicks "play", and records every response that
looks like a video stream (HLS .m3u8, DASH .mpd, MP4/WebM files).

Playwright is optional: when it is not installed this module reports itself
unavailable and the bot simply skips the browser step. All functions are
blocking (Playwright sync API) and must run in a worker thread. They never
raise to the caller.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

from .common import ErrorCode, install_network_guard, is_blocked_host, looks_like_ad_url
from .guardproxy import GuardProxy

logger = logging.getLogger(__name__)

# --- Browser launch --------------------------------------------------------------
# Tried in order. "msedge" exists on every Windows 10/11 PC; None = bundled Chromium.
BROWSER_CHANNELS: tuple[Optional[str], ...] = ("msedge", "chrome", None)
BROWSER_ARGS: tuple[str, ...] = (
    "--mute-audio",
    "--autoplay-policy=no-user-gesture-required",
    # All traffic must go through the guard proxy: no QUIC/UDP side channels.
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
)
VIEWPORT: dict[str, int] = {"width": 1280, "height": 720}
BROWSER_LOCALE: str = "id-ID"
MAX_BROWSERS: int = 1  # Each browser needs ~300-500 MB RAM; run one at a time.
BROWSER_SLOT_WAIT_SECONDS: float = 120.0

# --- Capture timing (seconds) ----------------------------------------------------------
PAGE_LOAD_TIMEOUT_MS: int = 30_000
MIN_CAPTURE_SECONDS: float = 8.0  # Always watch the page at least this long.
SETTLE_SECONDS: float = 6.0  # Stop once nothing new appeared for this long...
MAX_CAPTURE_SECONDS: float = 45.0  # ...or at the latest after this (pre-roll ads).
PLAY_INTERVAL_SECONDS: float = 3.0
POLL_MS: int = 500
MAX_CAPTURED_STREAMS: int = 25
SCREENSHOT_QUALITY: int = 60

# --- Stream recognition ---------------------------------------------------------------
STREAM_CONTENT_TYPES: tuple[str, ...] = (
    "video/",
    "audio/mpegurl",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
)
STREAM_EXTENSIONS: tuple[str, ...] = (".m3u8", ".mpd", ".mp4", ".m4v", ".webm", ".mov")
# HLS/DASH media segments: never a whole video on their own.
SEGMENT_PATTERN = re.compile(
    r"\.(ts|m4s|m4a|aac|vtt|webvtt)$|/(seg|segment|chunk|frag|fragment)[-_]?\d+[^/]*$",
    re.IGNORECASE,
)
SEGMENT_CONTENT_TYPES: tuple[str, ...] = ("video/mp2t", "video/iso.segment")

# Clicks "play" (only when nothing is playing, to avoid toggling pause) and
# "skip ad" buttons, mutes everything, and reports <video> elements.
PLAY_SCRIPT: str = """
() => {
  const videos = Array.from(document.querySelectorAll('video'));
  const playing = videos.some(v => !v.paused && !v.ended && v.readyState > 2);
  for (const v of videos) {
    try { v.muted = true; if (v.paused) { const p = v.play(); if (p && p.catch) p.catch(() => {}); } } catch (e) {}
  }
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 4 && r.height > 4; };
  const click = sel => {
    for (const el of document.querySelectorAll(sel)) {
      if (visible(el)) { try { el.click(); } catch (e) {} return true; }
    }
    return false;
  };
  for (const sel of ['.videoAdUiSkipButton', '.ytp-ad-skip-button', '.ytp-skip-ad-button',
                     '[class*="skip" i][class*="ad" i]', 'button[aria-label*="skip" i]']) { click(sel); }
  if (!playing) {
    for (const sel of ['.vjs-big-play-button', '.jw-display-icon-display', '.plyr__control--overlaid',
                       '.ytp-large-play-button', '.fp-play', '[class*="big-play" i]',
                       '[class*="play-button" i]', '[class*="playButton" i]',
                       'button[aria-label*="play" i]', '[title*="play" i]']) {
      if (click(sel)) break;
    }
  }
  return videos.map(v => ({
    src: v.currentSrc || v.src || '',
    duration: Number.isFinite(v.duration) ? v.duration : null,
    height: v.videoHeight || null,
  }));
}
"""


@dataclass
class CapturedStream:
    """A network response (or <video> source) that looks like a video stream."""

    url: str
    content_type: str = ""
    frame_url: str = ""
    duration_hint: Optional[float] = None  # From a <video> element, if known.
    height_hint: Optional[int] = None


@dataclass
class BrowserCapture:
    """Everything the browser saw on the page."""

    error: ErrorCode
    detail: str = ""
    streams: list[CapturedStream] = field(default_factory=list)
    screenshot: Optional[bytes] = None
    title: str = ""
    final_url: str = ""
    user_agent: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)


_browser_slots = threading.BoundedSemaphore(MAX_BROWSERS)


def browser_available() -> bool:
    """Return True if Playwright is installed."""
    return importlib.util.find_spec("playwright") is not None


def _url_allowed(url: str) -> bool:
    """Quick first check of a request URL (obviously local hosts are refused).

    This only catches the obvious cases early. The real protection is the
    GuardProxy, which checks the actual IP of every connection the browser
    makes, including redirects that request routing cannot see.
    """
    parsed = urlparse(url)
    if parsed.scheme in ("data", "blob", "about"):
        return True
    if parsed.scheme not in ("http", "https", "ws", "wss"):
        return False
    return not is_blocked_host((parsed.hostname or "").lower())


def _looks_like_stream(url: str, content_type: str) -> bool:
    """Return True for responses that may be a whole video (not a segment)."""
    if not url.lower().startswith(("http://", "https://")):
        return False
    path = urlparse(url).path.lower()
    content_type = content_type.lower()
    if content_type.startswith(SEGMENT_CONTENT_TYPES) or SEGMENT_PATTERN.search(path):
        return False
    if content_type.startswith(STREAM_CONTENT_TYPES):
        return True
    return path.endswith(STREAM_EXTENSIONS) and not content_type.startswith(("text/html", "image/"))


def _launch(playwright: Any, proxy_url: str) -> Any:
    """Launch the first available browser channel (Edge, Chrome, Chromium).

    Playwright adds ``<-loopback>`` to Chromium's proxy bypass rules, so even
    localhost requests go through the guard proxy (and are refused there).
    """
    last_error: Optional[Exception] = None
    for channel in BROWSER_CHANNELS:
        try:
            kwargs: dict[str, Any] = {
                "headless": True,
                "args": list(BROWSER_ARGS),
                "proxy": {"server": proxy_url},
            }
            if channel:
                kwargs["channel"] = channel
            browser = playwright.chromium.launch(**kwargs)
            logger.info("Headless browser started (%s)", channel or "bundled chromium")
            return browser
        except Exception as exc:  # Channel not installed: try the next one.
            last_error = exc
            logger.debug("Browser channel %s unavailable: %s", channel, exc)
    raise RuntimeError(f"no usable browser: {last_error}")


def _normal_user_agent(browser: Any) -> str:
    """Return the browser's User-Agent without the "Headless" marker."""
    try:
        page = browser.new_page()
        try:
            user_agent = str(page.evaluate("navigator.userAgent"))
        finally:
            page.close()
    except Exception:  # Fall back to the browser default.
        return ""
    return user_agent.replace("HeadlessChrome", "Chrome")


def capture_page(url: str, cancel_event: Optional[threading.Event] = None) -> BrowserCapture:
    """Open ``url`` in a headless browser and record video streams it loads.

    Args:
        url: Public page URL (already checked by the caller).
        cancel_event: Set by the caller to abort (job timeout).

    Returns:
        A BrowserCapture; this function never raises.
    """
    if not browser_available():
        return BrowserCapture(ErrorCode.UNSUPPORTED, "playwright not installed")
    if not _url_allowed(url):
        return BrowserCapture(ErrorCode.UNSUPPORTED, "blocked non-public page")
    waited = 0.0
    while not _browser_slots.acquire(timeout=1.0):
        waited += 1.0
        if (cancel_event is not None and cancel_event.is_set()) or waited > BROWSER_SLOT_WAIT_SECONDS:
            return BrowserCapture(ErrorCode.TIMEOUT, "no free browser slot")
    try:
        return _capture(url, cancel_event)
    except Exception as exc:  # Playwright errors must never reach the caller.
        logger.warning("Browser capture failed for %s", urlparse(url).hostname, exc_info=True)
        return BrowserCapture(ErrorCode.UNKNOWN, f"browser: {type(exc).__name__}: {exc}"[:500])
    finally:
        _browser_slots.release()


def _capture(url: str, cancel_event: Optional[threading.Event]) -> BrowserCapture:
    """Run one browser session (see :func:`capture_page`)."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    install_network_guard()  # Idempotent; the proxy relies on it.
    streams: dict[str, CapturedStream] = {}
    last_new = [time.monotonic()]

    def add_stream(stream: CapturedStream) -> None:
        parsed = urlparse(stream.url)
        key = f"{parsed.netloc}{parsed.path}"  # Same file with other tokens = duplicate.
        if key in streams:
            existing = streams[key]
            existing.duration_hint = existing.duration_hint or stream.duration_hint
            existing.height_hint = existing.height_hint or stream.height_hint
            return
        if len(streams) < MAX_CAPTURED_STREAMS:
            streams[key] = stream
            last_new[0] = time.monotonic()

    def on_response(response: Any) -> None:
        try:
            if response.status not in (200, 206):
                return
            content_type = response.headers.get("content-type", "")
            if not _looks_like_stream(response.url, content_type):
                return
            try:
                frame_url = response.frame.url
            except Exception:  # Detached frame.
                frame_url = ""
            add_stream(CapturedStream(response.url, content_type, frame_url))
        except Exception:  # Never break the page because of the listener.
            logger.debug("Response listener error", exc_info=True)

    def route_guard(route: Any) -> None:
        try:
            if _url_allowed(route.request.url):
                route.fallback()
            else:
                logger.info("Browser blocked a request to a non-public host")
                route.abort("blockedbyclient")
        except PlaywrightError:
            pass  # Page closed while routing.

    def websocket_guard(ws: Any) -> None:
        if _url_allowed(ws.url):
            ws.connect_to_server()
        else:
            ws.close()

    with GuardProxy() as proxy, sync_playwright() as playwright:
        browser = _launch(playwright, proxy.url)
        try:
            context = browser.new_context(
                user_agent=_normal_user_agent(browser) or None,
                viewport=VIEWPORT,
                locale=BROWSER_LOCALE,
                service_workers="block",  # Service workers would bypass the request guard.
                accept_downloads=False,
            )
            context.route("**/*", route_guard)
            if hasattr(context, "route_web_socket"):
                context.route_web_socket(re.compile(".*"), websocket_guard)
            context.on("page", lambda popup: popup.close() if popup.opener() else None)
            page = context.new_page()
            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            except PlaywrightError as exc:
                if proxy.blocked:
                    return BrowserCapture(ErrorCode.UNSUPPORTED, "page redirected to a blocked non-public address")
                if not streams and "Timeout" not in str(exc):
                    first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                    return BrowserCapture(ErrorCode.NETWORK, f"page load failed: {first_line}"[:300])

            started = time.monotonic()
            next_play = started
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return BrowserCapture(ErrorCode.TIMEOUT, "cancelled")
                now = time.monotonic()
                if now >= next_play:
                    _play_and_collect(page, add_stream)
                    next_play = now + PLAY_INTERVAL_SECONDS
                elapsed = now - started
                quiet_for = now - last_new[0]
                if elapsed >= MAX_CAPTURE_SECONDS:
                    break
                # Keep watching while only ads were seen: the real video usually
                # starts loading after a pre-roll ad ends.
                has_main = any(not looks_like_ad_url(s.url, s.frame_url) for s in streams.values())
                if has_main and elapsed >= MIN_CAPTURE_SECONDS and quiet_for >= SETTLE_SECONDS:
                    break
                page.wait_for_timeout(POLL_MS)

            capture = BrowserCapture(
                ErrorCode.OK if streams else ErrorCode.NOT_FOUND,
                f"{len(streams)} stream(s) captured",
                list(streams.values()),
            )
            try:
                capture.screenshot = page.screenshot(type="jpeg", quality=SCREENSHOT_QUALITY)
            except PlaywrightError:
                logger.debug("Screenshot failed", exc_info=True)
            capture.title = _safe(page.title)
            capture.final_url = page.url
            capture.user_agent = _safe(lambda: str(page.evaluate("navigator.userAgent")))
            capture.cookies = list(context.cookies())
            return capture
        finally:
            browser.close()


def _safe(func: Any) -> str:
    """Call ``func`` and return its string result, or "" on any error."""
    try:
        return str(func() or "")
    except Exception:
        return ""


def _play_and_collect(page: Any, add_stream: Any) -> None:
    """Run PLAY_SCRIPT in every frame and record direct <video> sources."""
    for frame in list(page.frames):
        try:
            videos = frame.evaluate(PLAY_SCRIPT)
        except Exception:  # Cross-origin/detached frames may refuse; ignore.
            continue
        for video in videos or []:
            src = str(video.get("src") or "")
            if src.startswith(("http://", "https://")) and _looks_like_stream(src, ""):
                duration = video.get("duration")
                add_stream(
                    CapturedStream(
                        src,
                        frame_url=frame.url,
                        duration_hint=float(duration) if duration else None,
                        height_hint=video.get("height") or None,
                    )
                )
