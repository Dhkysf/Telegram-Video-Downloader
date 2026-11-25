"""Shared constants, result types and temp-file helpers for all downloaders.

Every constant shared between modules is defined exactly once here. Importing
this module has no side effects besides defining constants: directory creation
and cleanup are triggered explicitly from ``bot.py`` at startup.
"""

from __future__ import annotations

import importlib.metadata
import ipaddress
import logging
import random
import re
import shutil
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse
from uuid import uuid4

logger = logging.getLogger(__name__)

# --- Paths (absolute, independent of the current working directory) ---------
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
TEMP_DIR: Path = PROJECT_ROOT / "temp_downloads"
INSTAGRAM_COOKIES_FILE: Path = PROJECT_ROOT / "instagram_cookies.txt"

# --- Size limits ---------------------------------------------------------------
MAX_FILE_SIZE_MB: int = 50  # Telegram Bot API upload limit for bots.
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024
MIN_FREE_DISK_BYTES: int = 3 * MAX_FILE_SIZE_BYTES
DEFAULT_MAX_HEIGHT: int = 720  # Default video resolution cap (pixels).

# --- Network -------------------------------------------------------------------
HTTP_CONNECT_TIMEOUT: float = 10.0
HTTP_READ_TIMEOUT: float = 30.0
MAX_SOURCE_ATTEMPTS: int = 3  # Attempts against a source platform (429/5xx).
BACKOFF_BASE_SECONDS: float = 2.0  # Gives delays of ~2 s, 4 s, 8 s (+ jitter).
BROWSER_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class ErrorCode(Enum):
    """Outcome of a download job."""

    OK = "ok"
    TOO_LARGE = "too_large"
    NOT_FOUND = "not_found"
    PRIVATE_OR_LOGIN = "private_or_login"
    UNSUPPORTED = "unsupported"
    RATE_LIMITED = "rate_limited"
    NETWORK = "network"
    TIMEOUT = "timeout"
    LIVE = "live"
    DISK_FULL = "disk_full"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DownloadResult:
    """Result returned by every downloader (never a bare ``None``).

    Attributes:
        path: Absolute path of the downloaded file, or ``None`` on failure.
        error: Outcome classification; ``ErrorCode.OK`` on success.
        detail: Technical detail for server logs only (never shown to users).
    """

    path: Optional[Path]
    error: ErrorCode
    detail: str = ""
    audio_only: bool = False  # True when the site only offers audio (e.g. SoundCloud).

    @property
    def ok(self) -> bool:
        """Return True when the download succeeded and a file path exists."""
        return self.error is ErrorCode.OK and self.path is not None


# =============================================================================
# Network guard: downloads may only connect to public internet addresses
# =============================================================================
# Hostnames that always point to the local machine or the local network.
LOCAL_HOSTNAMES: frozenset[str] = frozenset({"localhost", "localhost.localdomain"})
LOCAL_HOST_SUFFIXES: tuple[str, ...] = (".localhost", ".local", ".lan", ".internal", ".home.arpa")
# RFC 6052 / RFC 8215 NAT64 prefixes can embed private/loopback IPv4 inside
# IPv6 addresses that Python's ipaddress marks as globally routable.
NAT64_NETWORKS: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)
BLOCKED_ADDRESS_MESSAGE: str = "connection to a non-public address was blocked"

_guard_state = threading.local()
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex


class BlockedAddressError(ConnectionRefusedError):
    """Raised when a guarded download tries to reach a non-public address."""


def is_public_ip(value: str) -> bool:
    """Return True if ``value`` is a public (globally routable) IP address.

    Loopback, private LAN, link-local (incl. cloud metadata 169.254.x.x),
    carrier-grade NAT, multicast and reserved ranges are all non-public.
    """
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if any(address in net for net in NAT64_NETWORKS):
            return False
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def is_blocked_host(host: str) -> bool:
    """Return True for hosts that are obviously local (checked without DNS).

    Hostnames that resolve to private addresses are caught later by the
    connection guard, which checks the real IP of every connection.
    """
    host = host.lower().rstrip(".").strip("[]")
    if not host:
        return True
    if host in LOCAL_HOSTNAMES or host.endswith(LOCAL_HOST_SUFFIXES):
        return True
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False  # A normal hostname; resolved and checked at connect time.
    return not is_public_ip(host)


def is_network_guard_active() -> bool:
    """Return True if the current thread is inside ``public_network_only()``."""
    return bool(getattr(_guard_state, "active", False))


def _check_destination(sock: socket.socket, address: Any) -> None:
    """Raise BlockedAddressError if a guarded thread connects to a non-public IP."""
    if not is_network_guard_active():
        return
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return
    host = address[0] if isinstance(address, tuple) and address else ""
    if not is_public_ip(str(host)):
        raise BlockedAddressError(BLOCKED_ADDRESS_MESSAGE)


def _guarded_connect(self: socket.socket, address: Any) -> None:
    _check_destination(self, address)
    return _original_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    _check_destination(self, address)
    return _original_connect_ex(self, address)


def install_network_guard() -> None:
    """Install the connection guard (call once at startup, from bot.py).

    The guard only acts inside :func:`public_network_only` blocks, i.e. in
    download worker threads. It checks the real IP address of every
    connection, so it also stops redirects and DNS tricks that point to the
    local network. Telegram traffic and other threads are never affected.
    """
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    from .ytdlp import install_ytdlp_guard

    install_ytdlp_guard()


@contextmanager
def public_network_only() -> Iterator[None]:
    """Allow only public-internet connections from the current thread."""
    previous = getattr(_guard_state, "active", False)
    _guard_state.active = True
    try:
        yield
    finally:
        _guard_state.active = previous


@dataclass(frozen=True)
class MediaCandidate:
    """One video the bot found for a link, shown to the user for confirmation.

    Attributes:
        url: What to download (the page URL for yt-dlp, the stream URL for
            browser captures).
        title: Video or page title.
        duration: Length in seconds, if known.
        height: Vertical resolution in pixels, if known.
        size_bytes: Estimated download size, if known.
        thumbnail: Thumbnail URL (yt-dlp results only).
        uploader: Channel/account name, if known.
        audio_only: The media has no video stream.
        likely_ad: Heuristics say this is probably an advertisement.
        stream_host: Host serving the stream (browser captures only).
        stream_kind: "HLS", "DASH", "MP4", "WEBM" (browser captures only).
        via_browser: Found by the headless browser instead of yt-dlp.
        info_unavailable: Details could not be read, but a download may work.
    """

    url: str
    title: str = ""
    duration: Optional[float] = None
    height: Optional[int] = None
    size_bytes: Optional[int] = None
    thumbnail: str = ""
    uploader: str = ""
    audio_only: bool = False
    likely_ad: bool = False
    stream_host: str = ""
    stream_kind: str = ""
    via_browser: bool = False
    info_unavailable: bool = False


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of checking a link before downloading (never a bare None).

    Attributes:
        error: ``ErrorCode.OK`` when at least one candidate was found.
        candidates: Videos found, best guess first (ads sorted last).
        detail: Technical detail for logs only.
        via_browser: Candidates come from the headless browser.
        screenshot: JPEG screenshot of the page (browser only).
        page_title: Title of the page (browser only).
        page_url: Final page URL after redirects (browser only), used as Referer.
        user_agent: Browser User-Agent to reuse when downloading the stream.
        cookies: Browser cookies to reuse when downloading the stream.
        can_try_browser: The user may ask for a browser search instead.
    """

    error: ErrorCode
    candidates: tuple[MediaCandidate, ...] = ()
    detail: str = ""
    via_browser: bool = False
    screenshot: Optional[bytes] = None
    page_title: str = ""
    page_url: str = ""
    user_agent: str = ""
    cookies: tuple[dict[str, Any], ...] = ()
    can_try_browser: bool = False


# --- Advertisement heuristics ----------------------------------------------------
# Hosts that serve video ads (ad servers and their video CDNs).
AD_HOST_SUFFIXES: tuple[str, ...] = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com", "2mdn.net",
    "imasdk.googleapis.com", "adnxs.com", "adsrvr.org", "springserve.com",
    "spotxchange.com", "spotx.tv", "teads.tv", "innovid.com", "serving-sys.com",
    "moatads.com", "pubmatic.com", "rubiconproject.com", "aniview.com", "vidazoo.com",
    "smartadserver.com", "adform.net", "criteo.com", "fwmrm.net", "freewheel.tv",
    "tremorhub.com", "unrulymedia.com", "yieldmo.com", "sharethrough.com",
    "outbrain.com", "taboola.com", "adsafeprotected.com", "flashtalking.com",
)
AD_PATH_PATTERN = re.compile(
    r"(^|[/_.\-=])(ads?|adserver|advert\w*|preroll|pre-roll|midroll|vast|vpaid|commercials?)([/_.\-?&=]|$)",
    re.IGNORECASE,
)


def is_ad_host(host: str) -> bool:
    """Return True if ``host`` belongs to a known video-ad network."""
    host = host.lower().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in AD_HOST_SUFFIXES)


def looks_like_ad_url(url: str, frame_url: str = "") -> bool:
    """Return True if a stream URL (or the frame that loaded it) looks like an ad."""
    parsed = urlparse(url)
    if is_ad_host(parsed.hostname or "") or AD_PATH_PATTERN.search(parsed.path):
        return True
    return bool(frame_url) and is_ad_host(urlparse(frame_url).hostname or "")


def write_cookie_file(cookies: tuple[dict[str, Any], ...], path: Path) -> Optional[Path]:
    """Write browser cookies to a Netscape cookie file for yt-dlp.

    Args:
        cookies: Cookie dicts as returned by Playwright ``context.cookies()``.
        path: Destination (inside TEMP_DIR, job-prefixed so cleanup removes it).

    Returns:
        ``path`` on success, None if there were no cookies or writing failed.
    """
    if not cookies:
        return None
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = str(cookie.get("domain", ""))
        name = str(cookie.get("name", ""))
        if not domain or not name or any(ch in name + str(cookie.get("value", "")) for ch in "\t\n\r"):
            continue
        expires = cookie.get("expires", -1)
        expiry = int(expires) if isinstance(expires, (int, float)) and expires > 0 else 0
        lines.append(
            "\t".join(
                [
                    domain,
                    "TRUE" if domain.startswith(".") else "FALSE",
                    str(cookie.get("path") or "/"),
                    "TRUE" if cookie.get("secure") else "FALSE",
                    str(expiry),
                    name,
                    str(cookie.get("value", "")),
                ]
            )
        )
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write browser cookie file: %s", exc)
        return None
    return path


def new_job_id(platform: str) -> str:
    """Return a unique job ID used as the filename prefix for a job.

    Args:
        platform: Platform key, e.g. ``"tiktok"``.

    Returns:
        A string of the form ``"{platform}_{uuid4 hex}"``.
    """
    return f"{platform}_{uuid4().hex}"


def backoff_delay(attempt: int) -> float:
    """Return an exponential backoff delay with jitter.

    Args:
        attempt: 1-based number of the attempt that just failed.

    Returns:
        Seconds to wait: 2, 4, 8, ... plus up to 1 s of random jitter.
    """
    return BACKOFF_BASE_SECONDS * (2 ** (max(attempt, 1) - 1)) + random.uniform(0.0, 1.0)


def _remove_path(path: Path) -> bool:
    """Delete a file or directory, logging (never raising) on failure."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.warning("Could not delete temp path %s: %s", path.name, exc)
        return False


def cleanup_job(job_id: str) -> int:
    """Delete every entry in TEMP_DIR whose name starts with ``job_id``.

    Covers the final file and leftovers such as ``.part``, ``.ytdl`` and the
    separate video/audio streams. Failures are logged, never raised.

    Args:
        job_id: The job prefix returned by :func:`new_job_id`.

    Returns:
        Number of entries removed.
    """
    if not job_id:
        return 0
    try:
        entries = list(TEMP_DIR.iterdir())
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.warning("Could not list temp dir for cleanup: %s", exc)
        return 0
    return sum(1 for entry in entries if entry.name.startswith(job_id) and _remove_path(entry))


def clear_temp_dir() -> int:
    """Delete everything inside TEMP_DIR (leftovers from a crash or shutdown).

    Returns:
        Number of entries removed. Failures are logged, never raised.
    """
    try:
        entries = list(TEMP_DIR.iterdir())
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.warning("Could not list temp dir: %s", exc)
        return 0
    return sum(1 for entry in entries if _remove_path(entry))


def prepare_temp_dir() -> int:
    """Create TEMP_DIR if needed and remove leftovers from previous runs.

    Returns:
        Number of leftover entries removed.

    Raises:
        OSError: If the directory cannot be created.
    """
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return clear_temp_dir()


def free_disk_bytes(path: Path = TEMP_DIR) -> Optional[int]:
    """Return free disk space in bytes for ``path``, or None if unknown. Never raises."""
    try:
        target = path if path.exists() else path.parent
        if not target.exists():
            target = PROJECT_ROOT
        return shutil.disk_usage(target).free
    except OSError:
        return None


def runtime_info() -> dict[str, Any]:
    """Collect versions, tool availability, temp stats and free disk space.

    Never raises and never returns file paths or secrets; missing values
    become ``"?"`` or ``None``.
    """
    yt_dlp_version = "?"
    try:
        import yt_dlp.version  # noqa: PLC0415

        yt_dlp_version = str(yt_dlp.version.__version__)
    except Exception:
        try:
            yt_dlp_version = importlib.metadata.version("yt-dlp")
        except Exception:
            pass

    browser_ok = False
    playwright_version: Optional[str] = None
    try:
        import playwright  # noqa: PLC0415

        browser_ok = True
        playwright_version = getattr(playwright, "__version__", None) or importlib.metadata.version("playwright")
    except Exception:
        pass

    ffmpeg_ok: Optional[bool]
    try:
        ffmpeg_ok = shutil.which("ffmpeg") is not None
    except Exception:
        ffmpeg_ok = None

    temp_files: Optional[int] = None
    temp_bytes: Optional[int] = None
    try:
        if TEMP_DIR.exists():
            count = 0
            total = 0
            for entry in TEMP_DIR.rglob("*"):
                try:
                    if entry.is_file():
                        count += 1
                        total += entry.stat().st_size
                except OSError:
                    pass
            temp_files = count
            temp_bytes = total
        else:
            temp_files = 0
            temp_bytes = 0
    except OSError:
        pass

    disk_free_bytes = free_disk_bytes(TEMP_DIR)

    return {
        "yt_dlp_version": yt_dlp_version,
        "browser_available": browser_ok,
        "playwright_version": playwright_version,
        "ffmpeg_available": ffmpeg_ok,
        "temp_files": temp_files,
        "temp_bytes": temp_bytes,
        "disk_free_bytes": disk_free_bytes,
    }
