"""TikTok fallback downloader that scrapes a third-party site (ssstik.io).

Used only when yt-dlp fails for a TikTok URL. Third-party sites change often,
so endpoints, form fields and parsing patterns are module-level constants.
This module never raises to its caller: every outcome is a DownloadResult.
"""

from __future__ import annotations

import html
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from .common import (
    BROWSER_USER_AGENT,
    HTTP_CONNECT_TIMEOUT,
    HTTP_READ_TIMEOUT,
    MAX_FILE_SIZE_BYTES,
    MAX_SOURCE_ATTEMPTS,
    TEMP_DIR,
    BLOCKED_ADDRESS_MESSAGE,
    DownloadResult,
    ErrorCode,
    backoff_delay,
    public_network_only,
)

logger = logging.getLogger(__name__)

# --- Scraper endpoints & form fields ------------------------------------------
SSSTIK_BASE_URL: str = "https://ssstik.io"
SSSTIK_PAGE_URL: str = "https://ssstik.io/en"
SSSTIK_API_URL: str = "https://ssstik.io/abc"
SSSTIK_API_PARAMS: dict[str, str] = {"url": "dl"}
FORM_FIELD_URL: str = "id"
FORM_FIELD_LOCALE: str = "locale"
FORM_FIELD_TOKEN: str = "tt"
FORM_LOCALE_VALUE: str = "en"
HTMX_HEADERS: dict[str, str] = {
    "HX-Request": "true",
    "HX-Target": "target",
    "HX-Trigger": "_gcaptcha_pt",
    "HX-Current-URL": SSSTIK_PAGE_URL,
    "Origin": SSSTIK_BASE_URL,
    "Referer": SSSTIK_PAGE_URL,
}

# --- Parsing patterns ----------------------------------------------------------
TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""s_tt\s*=\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\btt\s*:\s*['"]([^'"]+)['"]"""),
    re.compile(r"""name=["']tt["'][^>]*value=["']([^"']+)["']"""),
)
ANCHOR_TAG_PATTERN = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
HREF_ATTR_PATTERN = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
CLASS_ATTR_PATTERN = re.compile(r"""\bclass\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
DOWNLOAD_LINK_CLASS: str = "without_watermark"
PRIVATE_MARKERS: tuple[str, ...] = ("private", "login")

# --- TikTok short links & download ---------------------------------------------
SHORT_LINK_HOSTS: tuple[str, ...] = ("vm.tiktok.com", "vt.tiktok.com")
DOWNLOAD_CHUNK_SIZE: int = 256 * 1024
REQUEST_TIMEOUT: tuple[float, float] = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)
VIDEO_CONTENT_TYPES: tuple[str, ...] = ("video/", "application/octet-stream", "binary/octet-stream")
OUTPUT_EXTENSION: str = ".mp4"  # ssstik serves MP4; Content-Type is checked first.


class _ScrapeError(Exception):
    """Internal error carrying an ErrorCode; converted to a DownloadResult."""

    def __init__(self, code: ErrorCode, detail: str) -> None:
        """Store the error code and log detail."""
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    """Abort the job if the caller requested cancellation."""
    if cancel_event is not None and cancel_event.is_set():
        raise _ScrapeError(ErrorCode.TIMEOUT, "cancelled by job timeout")


def _sleep(delay: float, cancel_event: Optional[threading.Event]) -> None:
    """Sleep for ``delay`` seconds, waking up early on cancellation."""
    if cancel_event is not None:
        if cancel_event.wait(delay):
            raise _ScrapeError(ErrorCode.TIMEOUT, "cancelled during backoff")
    else:
        time.sleep(delay)


def _request(
    session: requests.Session,
    method: str,
    url: str,
    cancel_event: Optional[threading.Event],
    **kwargs: Any,
) -> requests.Response:
    """Send an HTTP request, retrying 429/5xx and transient network errors.

    Uses exponential backoff with jitter and at most MAX_SOURCE_ATTEMPTS tries.

    Raises:
        _ScrapeError: RATE_LIMITED, NETWORK or NOT_FOUND after giving up.
    """
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_code = ErrorCode.NETWORK
    last_detail = "no attempt"
    for attempt in range(1, MAX_SOURCE_ATTEMPTS + 1):
        _check_cancel(cancel_event)
        try:
            response = session.request(method, url, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if BLOCKED_ADDRESS_MESSAGE in str(exc):
                raise _ScrapeError(ErrorCode.UNSUPPORTED, BLOCKED_ADDRESS_MESSAGE) from exc
            last_code, last_detail = ErrorCode.NETWORK, f"{type(exc).__name__}"
        except requests.RequestException as exc:
            raise _ScrapeError(ErrorCode.UNKNOWN, f"request failed: {type(exc).__name__}") from exc
        else:
            status = response.status_code
            if status == 429 or status >= 500:
                response.close()
                last_code = ErrorCode.RATE_LIMITED if status == 429 else ErrorCode.NETWORK
                last_detail = f"HTTP {status} from {urlparse(url).hostname}"
            elif status == 404:
                response.close()
                raise _ScrapeError(ErrorCode.NOT_FOUND, f"HTTP 404 from {urlparse(url).hostname}")
            elif status >= 400:
                response.close()
                raise _ScrapeError(ErrorCode.UNKNOWN, f"HTTP {status} from {urlparse(url).hostname}")
            else:
                return response
        if attempt < MAX_SOURCE_ATTEMPTS:
            delay = backoff_delay(attempt)
            logger.info("Scraper: %s, retrying in %.1f s (attempt %d/%d)", last_detail, delay, attempt, MAX_SOURCE_ATTEMPTS)
            _sleep(delay, cancel_event)
    raise _ScrapeError(last_code, last_detail)


def _resolve_short_link(session: requests.Session, url: str, cancel_event: Optional[threading.Event]) -> str:
    """Follow redirects of vm./vt.tiktok.com short links to the full video URL."""
    host = (urlparse(url).hostname or "").lower()
    if host not in SHORT_LINK_HOSTS:
        return url
    response = _request(session, "GET", url, cancel_event, allow_redirects=True, stream=True)
    try:
        return response.url or url
    finally:
        response.close()


def _extract_token(page: str) -> str:
    """Return the anti-bot form token from the ssstik home page."""
    for pattern in TOKEN_PATTERNS:
        match = pattern.search(page)
        if match:
            return match.group(1)
    raise _ScrapeError(ErrorCode.UNKNOWN, "scraper token not found (site layout changed?)")


def _extract_download_link(fragment: str) -> str:
    """Return the no-watermark download URL from the ssstik result HTML."""
    for tag in ANCHOR_TAG_PATTERN.findall(fragment):
        class_match = CLASS_ATTR_PATTERN.search(tag)
        href_match = HREF_ATTR_PATTERN.search(tag)
        if not class_match or not href_match:
            continue
        classes = class_match.group(1).split()
        href = html.unescape(href_match.group(1)).strip()
        if DOWNLOAD_LINK_CLASS in classes and href.lower().startswith(("http://", "https://")):
            return href
    lowered = fragment.lower()
    if any(marker in lowered for marker in PRIVATE_MARKERS):
        raise _ScrapeError(ErrorCode.PRIVATE_OR_LOGIN, "scraper reports private video")
    raise _ScrapeError(ErrorCode.UNKNOWN, "scraper download link not found (site layout changed?)")


def _fetch_download_link(
    session: requests.Session, video_url: str, cancel_event: Optional[threading.Event]
) -> str:
    """Load the ssstik page, submit the form and return the download URL.

    ssstik answers bursts of requests with an empty HTTP 200 body (a soft
    rate limit), so an empty body is retried with backoff and a fresh token.
    """
    for attempt in range(1, MAX_SOURCE_ATTEMPTS + 1):
        page = _request(session, "GET", SSSTIK_PAGE_URL, cancel_event)
        token = _extract_token(page.text)
        form = {FORM_FIELD_URL: video_url, FORM_FIELD_LOCALE: FORM_LOCALE_VALUE, FORM_FIELD_TOKEN: token}
        result = _request(
            session, "POST", SSSTIK_API_URL, cancel_event,
            params=SSSTIK_API_PARAMS, data=form, headers=HTMX_HEADERS,
        )
        if result.text.strip():
            return _extract_download_link(result.text)
        if attempt < MAX_SOURCE_ATTEMPTS:
            delay = backoff_delay(attempt)
            logger.info("Scraper: empty response (soft rate limit), retrying in %.1f s", delay)
            _sleep(delay, cancel_event)
    raise _ScrapeError(ErrorCode.RATE_LIMITED, "scraper kept returning empty responses")


def _stream_to_file(
    session: requests.Session, link: str, job_id: str, cancel_event: Optional[threading.Event]
) -> Path:
    """Stream the video to TEMP_DIR, aborting as soon as it exceeds 50 MB."""
    final_path = TEMP_DIR / f"{job_id}{OUTPUT_EXTENSION}"
    part_path = TEMP_DIR / f"{job_id}{OUTPUT_EXTENSION}.part"
    response = _request(session, "GET", link, cancel_event, stream=True, headers={"Referer": SSSTIK_PAGE_URL})
    try:
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and not content_type.startswith(VIDEO_CONTENT_TYPES):
            raise _ScrapeError(ErrorCode.UNKNOWN, f"unexpected content type {content_type!r}")
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_FILE_SIZE_BYTES:
            raise _ScrapeError(ErrorCode.TOO_LARGE, f"Content-Length {declared} exceeds limit")
        written = 0
        with open(part_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                _check_cancel(cancel_event)
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_FILE_SIZE_BYTES:
                    raise _ScrapeError(ErrorCode.TOO_LARGE, "stream exceeded size limit")
                handle.write(chunk)
        if written == 0:
            raise _ScrapeError(ErrorCode.UNKNOWN, "scraper returned an empty file")
        part_path.replace(final_path)
        return final_path
    except requests.RequestException as exc:
        raise _ScrapeError(ErrorCode.NETWORK, f"download interrupted: {type(exc).__name__}") from exc
    finally:
        response.close()


def _delete_quietly(*paths: Path) -> None:
    """Delete files, logging (never raising) on failure."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete %s: %s", path.name, exc)


def scrape_tiktok(url: str, job_id: str, cancel_event: Optional[threading.Event] = None) -> DownloadResult:
    """Download a TikTok video through ssstik.io (blocking; run in a thread).

    Args:
        url: TikTok video URL (short links are resolved first).
        job_id: Unique job prefix for every file written.
        cancel_event: Set by the caller to abort the job.

    Returns:
        A DownloadResult; this function never raises.
    """
    final_path = TEMP_DIR / f"{job_id}{OUTPUT_EXTENSION}"
    part_path = TEMP_DIR / f"{job_id}{OUTPUT_EXTENSION}.part"
    try:
        with public_network_only(), requests.Session() as session:
            session.headers.update(
                {"User-Agent": BROWSER_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
            )
            video_url = _resolve_short_link(session, url, cancel_event)
            link = _fetch_download_link(session, video_url, cancel_event)
            path = _stream_to_file(session, link, job_id, cancel_event)
        return DownloadResult(path, ErrorCode.OK, "ssstik")
    except _ScrapeError as exc:
        _delete_quietly(part_path, final_path)
        return DownloadResult(None, exc.code, f"scraper: {exc.detail}")
    except Exception as exc:  # Parse/IO surprises must never reach the caller.
        logger.warning("TikTok scraper crashed for job %s", job_id, exc_info=True)
        _delete_quietly(part_path, final_path)
        return DownloadResult(None, ErrorCode.UNKNOWN, f"scraper: {type(exc).__name__}")
