"""yt-dlp download engine: the primary downloader for every platform.

All functions here are blocking and must be run from a worker thread
(``asyncio.to_thread``). They never raise: every outcome is a DownloadResult.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError

from .common import (
    DEFAULT_MAX_HEIGHT,
    INSTAGRAM_COOKIES_FILE,
    MAX_FILE_SIZE_BYTES,
    MAX_SOURCE_ATTEMPTS,
    TEMP_DIR,
    BLOCKED_ADDRESS_MESSAGE,
    DownloadResult,
    ErrorCode,
    MediaCandidate,
    ProbeResult,
    backoff_delay,
    cleanup_job,
    new_job_id,
    public_network_only,
    write_cookie_file,
)
from .tiktok import scrape_tiktok

logger = logging.getLogger(__name__)

# Budget reserved for the audio stream when video and audio are merged, so the
# sum of both streams stays under the 50 MB limit.
AUDIO_SIZE_BUDGET_BYTES: int = 6 * 1024 * 1024
# Resolution caps tried in order when the previous attempt was too large.
HEIGHT_STEPS: tuple[int, ...] = (1080, 720, 480, 360, 240)
SOCKET_TIMEOUT_SECONDS: int = 30
YTDLP_RETRIES: int = 3
# Leftover/intermediate suffixes that are never a final output file.
INTERMEDIATE_SUFFIXES: frozenset[str] = frozenset({".part", ".ytdl", ".temp", ".tmp"})
# Matches separate stream files such as "<job>.f137.mp4" (pre-merge).
FORMAT_STREAM_PATTERN = re.compile(r"\.f[0-9a-zA-Z_-]+\.[0-9a-zA-Z]+$")
COOKIE_COPY_SUFFIX: str = ".cookies.txt"

# Error message classification (case-insensitive). "rate" is matched only as
# "rate limit"/"rate-limited" because a bare substring also matches words like
# "generate" (assumption: safest interpretation of the spec).
RATE_LIMIT_PATTERN = re.compile(r"http error 429|too many requests|\brate[- ]?limit", re.IGNORECASE)
LOGIN_PATTERN = re.compile(
    r"login|log in|private|sign in|authentication|cookies", re.IGNORECASE
)
UNSUPPORTED_PATTERN = re.compile(r"unsupported url", re.IGNORECASE)
NOT_FOUND_PATTERN = re.compile(
    r"http error 404|\b404\b|not available|unavailable|not found|does not exist|no video", re.IGNORECASE
)
SERVER_ERROR_PATTERN = re.compile(r"http error 5\d\d", re.IGNORECASE)
NETWORK_PATTERN = re.compile(
    r"timed out|timeout|connection|network is unreachable|name resolution|getaddrinfo|ssl",
    re.IGNORECASE,
)
FORMAT_UNAVAILABLE_PATTERN = re.compile(r"requested format is not available", re.IGNORECASE)
FFMPEG_PATTERN = re.compile(r"ffmpeg|ffprobe|postprocessing", re.IGNORECASE)
BLOCKED_PATTERN = re.compile(re.escape(BLOCKED_ADDRESS_MESSAGE), re.IGNORECASE)
DRM_PATTERN = re.compile(r"\bdrm\b", re.IGNORECASE)
# Remux only video containers to MP4; audio files (mp3, m4a, opus) stay as they are.
REMUX_MAPPING: str = "webm>mp4/mkv>mp4/mov>mp4/flv>mp4/avi>mp4/m4v>mp4/3gp>mp4/ts>mp4"
AUDIO_EXTENSIONS: frozenset[str] = frozenset({"mp3", "m4a", "aac", "opus", "ogg", "oga", "wav", "flac"})
# Audio-only mode: FFmpegExtractAudio keeps MP3/M4A as they are and turns anything
# else into M4A (AAC is copied without re-encoding). Telegram plays both inline.
AUDIO_CODEC_MAPPING: str = "mp3>mp3/m4a>m4a/m4a"
# Audio-only mode falls back to a muxed file ("b") when a site has no separate
# audio stream; this cap keeps that download small. Audio formats have no height.
AUDIO_MODE_MAX_HEIGHT: int = 480
GENERIC_EXTRACTOR_KEY: str = "Generic"
MAX_FILESIZE_MARKER: str = "max-filesize"
BROWSER_COOKIE_SUFFIX: str = ".browser-cookies.txt"
# yt-dlp probe errors that still allow a TikTok download through the scraper.
TIKTOK_SCRAPER_RETRY_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.UNKNOWN, ErrorCode.NOT_FOUND, ErrorCode.NETWORK, ErrorCode.UNSUPPORTED}
)


class _TooLargeDuringDownload(DownloadCancelled):
    """Raised from the progress hook once a download passes the size limit.

    Needed for streams (HLS/DASH, direct links) whose size is unknown up front,
    where ``max_filesize`` cannot stop the download early.
    """


@dataclass(frozen=True)
class _Attempt:
    """Internal result of one yt-dlp run."""

    result: DownloadResult
    retryable: bool = False  # Transient source error (429/5xx): back off and retry.
    try_lower: bool = False  # Too large: retry with a lower resolution cap.


class _YtdlpLogger:
    """Collects yt-dlp output without printing it or leaking it to users."""

    def __init__(self) -> None:
        """Initialise the logger state."""
        self.max_filesize_hit: bool = False
        self.last_error: str = ""

    def _inspect(self, msg: str) -> None:
        if MAX_FILESIZE_MARKER in msg:
            self.max_filesize_hit = True

    def debug(self, msg: str) -> None:
        """Receive yt-dlp debug/screen output."""
        self._inspect(msg)

    def info(self, msg: str) -> None:
        """Receive yt-dlp info output."""
        self._inspect(msg)

    def warning(self, msg: str) -> None:
        """Receive yt-dlp warnings (logged at DEBUG level)."""
        self._inspect(msg)
        logger.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        """Receive yt-dlp errors (kept for classification)."""
        self._inspect(msg)
        self.last_error = msg


def ffmpeg_available() -> bool:
    """Return True if an ``ffmpeg`` binary is reachable on PATH."""
    return shutil.which("ffmpeg") is not None


def classify_error(message: str) -> ErrorCode:
    """Map a yt-dlp error message to an ErrorCode.

    Args:
        message: Text of a DownloadError/ExtractorError.

    Returns:
        The matching ErrorCode (UNKNOWN if nothing matches).
    """
    if BLOCKED_PATTERN.search(message) or DRM_PATTERN.search(message):
        return ErrorCode.UNSUPPORTED  # Local-network target, or DRM-protected video.
    if RATE_LIMIT_PATTERN.search(message):
        return ErrorCode.RATE_LIMITED
    if LOGIN_PATTERN.search(message):
        return ErrorCode.PRIVATE_OR_LOGIN
    if UNSUPPORTED_PATTERN.search(message):
        return ErrorCode.UNSUPPORTED
    if FFMPEG_PATTERN.search(message):
        return ErrorCode.UNKNOWN  # e.g. "ffmpeg not found" must not become NOT_FOUND.
    if NOT_FOUND_PATTERN.search(message):
        return ErrorCode.NOT_FOUND
    if SERVER_ERROR_PATTERN.search(message) or NETWORK_PATTERN.search(message):
        return ErrorCode.NETWORK
    return ErrorCode.UNKNOWN


def _size_filter(limit: int) -> str:
    """Return a yt-dlp filter that keeps formats under ``limit`` bytes.

    The ``?`` keeps formats whose size is unknown; ``max_filesize`` still
    enforces the limit during the actual download.
    """
    return f"[filesize<?{limit}][filesize_approx<?{limit}]"


def build_format_selector(can_merge: bool, last_resort: bool) -> str:
    """Build a format selector preferring MP4 video + M4A audio under 50 MB.

    The resolution cap is applied through ``format_sort`` (see
    :func:`build_format_sort`), not through a height filter, so portrait
    videos (e.g. 576x1024 TikToks) count as 576p instead of being rejected.

    Args:
        can_merge: Whether FFmpeg is available to merge separate streams.
        last_resort: Append a final "worst quality" fallback.

    Returns:
        A yt-dlp format selection string.
    """
    audio_budget = min(AUDIO_SIZE_BUDGET_BYTES, MAX_FILE_SIZE_BYTES // 4)
    total = _size_filter(MAX_FILE_SIZE_BYTES)
    video = _size_filter(MAX_FILE_SIZE_BYTES - audio_budget)
    audio = _size_filter(audio_budget)
    choices: list[str] = []
    if can_merge:
        choices.append(f"bv*[ext=mp4]{video}+ba[ext=m4a]{audio}")
    choices.append(f"b[ext=mp4]{total}")
    if can_merge:
        choices.append(f"bv*{video}+ba{audio}")
    choices.append(f"b{total}")
    if last_resort:
        choices.append("wv*+wa/w" if can_merge else "w")
    return "/".join(choices)


def build_audio_format_selector(can_extract: bool, last_resort: bool) -> str:
    """Build the format selector for audio-only downloads ("ba/b").

    ``ba`` is the best audio-only stream. ``b`` (best file with both video and
    audio) is the fallback for sites without a separate audio stream; FFmpeg
    then extracts its audio track, so it is only used when FFmpeg exists.

    Args:
        can_extract: Whether FFmpeg is available to extract audio from video.
        last_resort: Append a final "worst quality" fallback.

    Returns:
        A yt-dlp format selection string.
    """
    total = _size_filter(MAX_FILE_SIZE_BYTES)
    choices: list[str] = [f"ba{total}"]
    if can_extract:
        choices.append(f"b{total}")
    if last_resort:
        choices.append("wa/w" if can_extract else "wa")
    return "/".join(choices)


def build_format_sort(max_height: int) -> list[str]:
    """Return a yt-dlp ``format_sort`` list for a resolution cap.

    ``res`` is the smallest video dimension: ``res:720`` prefers the best
    format at or below 720p. ``vcodec:h264`` prefers H.264, which every
    Telegram client can play inline.
    """
    return [f"res:{max_height}", "vcodec:h264", "ext:mp4:m4a"]


def _height_caps(max_height: int) -> list[int]:
    """Return the resolution caps to try, highest first, starting at ``max_height``."""
    caps = [h for h in HEIGHT_STEPS if h < max_height]
    return [max_height, *caps]


def _prepare_cookiefile(job_id: str) -> Optional[Path]:
    """Copy the Instagram cookie file into a per-job temp file.

    yt-dlp writes cookies back on exit; a per-job copy keeps the original
    file intact and avoids concurrent jobs writing the same file. The copy is
    removed by the normal job cleanup (same job-ID prefix).
    """
    if not INSTAGRAM_COOKIES_FILE.is_file():
        logger.warning(
            "instagram_cookies.txt not found; Instagram downloads may fail for login-only content"
        )
        return None
    target = TEMP_DIR / f"{job_id}{COOKIE_COPY_SUFFIX}"
    try:
        shutil.copyfile(INSTAGRAM_COOKIES_FILE, target)
    except OSError as exc:
        logger.warning("Could not copy Instagram cookie file: %s", exc)
        return None
    return target


def _build_options(
    job_id: str,
    max_height: int,
    last_resort: bool,
    ydl_logger: _YtdlpLogger,
    cancel_event: Optional[threading.Event],
    extra_options: Optional[dict[str, Any]],
    audio_only: bool = False,
) -> dict[str, Any]:
    """Return the yt-dlp option dictionary for one attempt.

    ``extra_options`` (cookiefile, http_headers, ...) are merged in last.
    ``audio_only`` selects audio formats and extracts the audio with FFmpeg.
    """
    can_merge = ffmpeg_available()

    def _progress_hook(status: dict[str, Any]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("job cancelled")
        downloaded = status.get("downloaded_bytes") or 0
        expected = status.get("total_bytes") or 0
        if downloaded > MAX_FILE_SIZE_BYTES or expected > MAX_FILE_SIZE_BYTES:
            raise _TooLargeDuringDownload("download exceeded the size limit")

    if audio_only:
        format_selector = build_audio_format_selector(can_merge, last_resort)
    else:
        format_selector = build_format_selector(can_merge, last_resort)
    options: dict[str, Any] = {
        "outtmpl": str(TEMP_DIR / f"{job_id}.%(ext)s"),
        "format": format_selector,
        "format_sort": build_format_sort(max_height),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlist_items": "1",  # Multi-video posts: only the first video.
        "max_filesize": MAX_FILE_SIZE_BYTES,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": SOCKET_TIMEOUT_SECONDS,
        "retries": YTDLP_RETRIES,
        "fragment_retries": YTDLP_RETRIES,
        "logger": ydl_logger,
        "progress_hooks": [_progress_hook],
    }
    if can_merge and audio_only:
        # Keep MP3/M4A as they are; extract (or copy AAC) to M4A otherwise.
        options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": AUDIO_CODEC_MAPPING}]
    elif can_merge:
        # Remux (no re-encode) to MP4 when the final container is something else.
        options["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": REMUX_MAPPING}]
    if extra_options:
        options.update(extra_options)
    return options


def _first_entry(info: dict[str, Any]) -> dict[str, Any]:
    """Return the first downloaded entry if yt-dlp returned a playlist."""
    if info.get("_type") == "playlist":
        for entry in info.get("entries") or []:
            if entry:
                return entry
    return info


def _is_final_file(path: Path, job_id: str) -> bool:
    """Return True if ``path`` looks like a finished output file of ``job_id``."""
    name = path.name
    return (
        path.is_file()
        and name.startswith(f"{job_id}.")
        and path.suffix.lower() not in INTERMEDIATE_SUFFIXES
        and not name.endswith((COOKIE_COPY_SUFFIX, BROWSER_COOKIE_SUFFIX))
        and not FORMAT_STREAM_PATTERN.search(name[len(job_id):])
    )


def resolve_output_path(info: dict[str, Any], job_id: str) -> Optional[Path]:
    """Find the real output file of a job.

    Reads ``info["requested_downloads"][0]["filepath"]`` first and falls back
    to globbing TEMP_DIR for ``{job_id}.*``.
    """
    entry = _first_entry(info)
    downloads = entry.get("requested_downloads") or []
    if downloads:
        filepath = downloads[0].get("filepath")
        if filepath and Path(filepath).is_file():
            return Path(filepath)
    candidates = [p for p in TEMP_DIR.glob(f"{job_id}.*") if _is_final_file(p, job_id)]
    if not candidates:
        return None
    # Prefer MP4, then the largest file (a finished merge beats a stray stream).
    candidates.sort(key=lambda p: (p.suffix.lower() == ".mp4", p.stat().st_size), reverse=True)
    return candidates[0]


def _estimated_size(info: dict[str, Any]) -> int:
    """Return the size yt-dlp expected to download, in bytes (0 if unknown)."""
    entry = _first_entry(info)
    formats = entry.get("requested_formats") or [entry]
    total = 0
    for fmt in formats:
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        try:
            total += int(size)
        except (TypeError, ValueError):
            continue
    return total


def _is_audio_only(info: dict[str, Any], path: Path) -> bool:
    """Return True if the downloaded file has no video stream."""
    entry = _first_entry(info)
    downloads = entry.get("requested_downloads") or [entry]
    vcodec = downloads[0].get("vcodec") or entry.get("vcodec")
    if vcodec:
        return vcodec == "none"
    return path.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS


def describe_site(url: str) -> str:
    """Return a readable site name for ``url`` without any network access.

    Uses the first matching yt-dlp extractor (e.g. "Facebook", "Vimeo");
    falls back to the hostname when only the generic extractor would match.
    """
    try:
        for extractor in gen_extractor_classes():
            if extractor.ie_key() != GENERIC_EXTRACTOR_KEY and extractor.suitable(url):
                return extractor.ie_key()
    except Exception:  # A broken extractor regex must not break the bot.
        logger.debug("Extractor lookup failed for %s", url, exc_info=True)
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.") or "link"


def _single_attempt(
    url: str,
    job_id: str,
    max_height: int,
    last_resort: bool,
    cancel_event: Optional[threading.Event],
    extra_options: Optional[dict[str, Any]],
    audio_only: bool = False,
) -> _Attempt:
    """Run yt-dlp once and classify the outcome."""
    ydl_logger = _YtdlpLogger()
    options = _build_options(
        job_id, max_height, last_resort, ydl_logger, cancel_event, extra_options, audio_only
    )
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except _TooLargeDuringDownload:
        return _Attempt(
            DownloadResult(None, ErrorCode.TOO_LARGE, "aborted at size limit"), try_lower=True
        )
    except DownloadCancelled:
        return _Attempt(DownloadResult(None, ErrorCode.TIMEOUT, "cancelled by job timeout"))
    except (DownloadError, ExtractorError) as exc:
        message = str(exc)
        if FORMAT_UNAVAILABLE_PATTERN.search(message) and not last_resort:
            return _Attempt(
                DownloadResult(None, ErrorCode.TOO_LARGE, "no format under size limit"),
                try_lower=True,
            )
        code = classify_error(message)
        retryable = code is ErrorCode.RATE_LIMITED or bool(SERVER_ERROR_PATTERN.search(message))
        return _Attempt(DownloadResult(None, code, message[:500]), retryable=retryable)
    except Exception as exc:  # yt-dlp can raise arbitrary errors on site changes.
        logger.warning("Unexpected yt-dlp failure for job %s", job_id, exc_info=True)
        return _Attempt(DownloadResult(None, ErrorCode.UNKNOWN, f"{type(exc).__name__}: {exc}"[:500]))

    if not info:
        return _Attempt(DownloadResult(None, ErrorCode.NOT_FOUND, "yt-dlp returned no info"))

    path = resolve_output_path(info, job_id)
    if path is not None:
        # After audio extraction the format info still describes the source
        # (which may have had video), so the file extension decides.
        extracted_audio = audio_only and path.suffix.lower().lstrip(".") in AUDIO_EXTENSIONS
        return _Attempt(
            DownloadResult(
                path,
                ErrorCode.OK,
                "yt-dlp",
                audio_only=extracted_audio or _is_audio_only(info, path),
            )
        )
    # No file produced: yt-dlp silently skips downloads that exceed max_filesize.
    if ydl_logger.max_filesize_hit or _estimated_size(info) > MAX_FILE_SIZE_BYTES:
        return _Attempt(
            DownloadResult(None, ErrorCode.TOO_LARGE, "skipped by max_filesize"), try_lower=True
        )
    return _Attempt(
        DownloadResult(None, ErrorCode.UNKNOWN, ydl_logger.last_error[:500] or "no output file")
    )


def _attempt_with_backoff(
    url: str,
    job_id: str,
    max_height: int,
    last_resort: bool,
    cancel_event: Optional[threading.Event],
    extra_options: Optional[dict[str, Any]],
    audio_only: bool = False,
) -> _Attempt:
    """Run yt-dlp, retrying 429/5xx errors with exponential backoff + jitter."""
    attempt_result = _Attempt(DownloadResult(None, ErrorCode.UNKNOWN, "not started"))
    for attempt in range(1, MAX_SOURCE_ATTEMPTS + 1):
        if cancel_event is not None and cancel_event.is_set():
            return _Attempt(DownloadResult(None, ErrorCode.TIMEOUT, "cancelled before attempt"))
        attempt_result = _single_attempt(
            url, job_id, max_height, last_resort, cancel_event, extra_options, audio_only
        )
        if not attempt_result.retryable or attempt == MAX_SOURCE_ATTEMPTS:
            return attempt_result
        delay = backoff_delay(attempt)
        logger.info(
            "Job %s: source error %s, retrying in %.1f s (attempt %d/%d)",
            job_id, attempt_result.result.error.name, delay, attempt, MAX_SOURCE_ATTEMPTS,
        )
        if cancel_event is not None:
            if cancel_event.wait(delay):
                return _Attempt(DownloadResult(None, ErrorCode.TIMEOUT, "cancelled during backoff"))
        else:
            time.sleep(delay)
    return attempt_result


def download_with_ytdlp(
    url: str,
    job_id: str,
    platform: str,
    cancel_event: Optional[threading.Event] = None,
    max_height: int = DEFAULT_MAX_HEIGHT,
    extra_options: Optional[dict[str, Any]] = None,
    audio_only: bool = False,
) -> DownloadResult:
    """Download a video with yt-dlp into TEMP_DIR using the job-ID prefix.

    Tries progressively lower resolutions when the result would exceed 50 MB.

    Args:
        url: Video URL.
        job_id: Unique job prefix for every file written.
        platform: Platform key (``"instagram"`` enables the cookie file).
        cancel_event: Set by the caller to abort the job (e.g. on timeout).
        max_height: Highest resolution to try first.
        extra_options: Additional yt-dlp options (headers, cookie file, ...).
        audio_only: Download only the audio track (sent as an audio file).

    Returns:
        A DownloadResult; this function never raises.
    """
    if audio_only:
        max_height = min(max_height, AUDIO_MODE_MAX_HEIGHT)
    try:
        with public_network_only():
            return _download_capped(
                url, job_id, platform, cancel_event, max_height, extra_options, audio_only
            )
    except Exception as exc:  # Defensive: the caller must never see an exception.
        logger.warning("download_with_ytdlp crashed for job %s", job_id, exc_info=True)
        return DownloadResult(None, ErrorCode.UNKNOWN, f"{type(exc).__name__}: {exc}"[:500])


def _download_capped(
    url: str,
    job_id: str,
    platform: str,
    cancel_event: Optional[threading.Event],
    max_height: int,
    extra_options: Optional[dict[str, Any]],
    audio_only: bool = False,
) -> DownloadResult:
    """Try each resolution cap until the video fits under the size limit."""
    options = dict(extra_options or {})
    if platform == "instagram" and "cookiefile" not in options:
        cookiefile = _prepare_cookiefile(job_id)
        if cookiefile is not None:
            options["cookiefile"] = str(cookiefile)
    caps = _height_caps(max_height)
    attempt = _Attempt(DownloadResult(None, ErrorCode.UNKNOWN, "not started"))
    for index, cap in enumerate(caps):
        last_resort = index == len(caps) - 1
        attempt = _attempt_with_backoff(url, job_id, cap, last_resort, cancel_event, options, audio_only)
        if attempt.try_lower and not last_resort:
            logger.info("Job %s: too large at <=%dp, trying a lower resolution", job_id, cap)
            _remove_partial_files(job_id)
            continue
        return attempt.result
    return attempt.result


def _remove_partial_files(job_id: str) -> None:
    """Delete media leftovers of a job but keep its cookie files."""
    for path in TEMP_DIR.glob(f"{job_id}.*"):
        if path.name.endswith((COOKIE_COPY_SUFFIX, BROWSER_COOKIE_SUFFIX)):
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete partial file %s: %s", path.name, exc)


def get_youtube(url: str, job_id: str, cancel_event: Optional[threading.Event] = None) -> DownloadResult:
    """Download a YouTube video (blocking; run in a worker thread)."""
    return download_with_ytdlp(url, job_id, "youtube", cancel_event)


def get_twitter(url: str, job_id: str, cancel_event: Optional[threading.Event] = None) -> DownloadResult:
    """Download an X/Twitter video (blocking; run in a worker thread)."""
    return download_with_ytdlp(url, job_id, "twitter", cancel_event)


def get_instagram(url: str, job_id: str, cancel_event: Optional[threading.Event] = None) -> DownloadResult:
    """Download an Instagram video, using instagram_cookies.txt if present."""
    return download_with_ytdlp(url, job_id, "instagram", cancel_event)


def get_generic(url: str, job_id: str, cancel_event: Optional[threading.Event] = None) -> DownloadResult:
    """Download from any other site yt-dlp supports (or its generic extractor)."""
    return download_with_ytdlp(url, job_id, "generic", cancel_event)


def get_tiktok(url: str, job_id: str, cancel_event: Optional[threading.Event] = None) -> DownloadResult:
    """Download a TikTok video with yt-dlp, falling back to the scraper.

    The scraper is only used when yt-dlp fails. It is skipped for TOO_LARGE
    (the scraper serves the same file) and TIMEOUT (the job was cancelled).
    """
    result = download_with_ytdlp(url, job_id, "tiktok", cancel_event)
    if result.error in (ErrorCode.OK, ErrorCode.TOO_LARGE, ErrorCode.TIMEOUT):
        return result
    if cancel_event is not None and cancel_event.is_set():
        return DownloadResult(None, ErrorCode.TIMEOUT, "cancelled before fallback")
    logger.info("Job %s: yt-dlp failed for TikTok (%s), trying scraper fallback", job_id, result.error.name)
    cleanup_job(job_id)
    with public_network_only():
        fallback = scrape_tiktok(url, job_id, cancel_event)
    if fallback.ok:
        return fallback
    # Report the more informative of the two errors.
    if fallback.error is ErrorCode.UNKNOWN:
        return DownloadResult(None, result.error, f"yt-dlp: {result.detail} | scraper: {fallback.detail}")
    return fallback


def download_audio(
    url: str, job_id: str, platform: str, cancel_event: Optional[threading.Event] = None
) -> DownloadResult:
    """Download only the audio of a link as M4A/MP3 (blocking; run in a worker thread).

    Works for every platform yt-dlp supports (Instagram uses its cookie file).
    There is no TikTok scraper fallback here: the scraper only serves video.
    """
    return download_with_ytdlp(url, job_id, platform, cancel_event, audio_only=True)


# =============================================================================
# Probe (check a link before downloading) and browser-stream downloads
# =============================================================================
def _candidate_from_info(entry: dict[str, Any], url: str) -> MediaCandidate:
    """Build a MediaCandidate from a yt-dlp info dict (no download done)."""
    formats = entry.get("requested_formats") or [entry]
    heights: list[int] = []
    for fmt in formats:
        height, width = fmt.get("height"), fmt.get("width")
        if isinstance(height, (int, float)) and height > 0:
            # Quality = shortest side, so a 720x1280 portrait video counts as 720p.
            heights.append(int(min(height, width) if isinstance(width, (int, float)) and width > 0 else height))
    vcodecs = [f.get("vcodec") for f in formats if f.get("vcodec")]
    audio_only = bool(vcodecs) and all(codec == "none" for codec in vcodecs)
    duration = entry.get("duration")
    duration = float(duration) if isinstance(duration, (int, float)) and duration > 0 else None
    size = _estimated_size(entry)
    if not size and duration:
        bitrate = entry.get("tbr") or sum(float(f.get("tbr") or 0) for f in formats)
        size = int(float(bitrate) * duration * 125) if bitrate else 0  # kbit/s -> bytes
    thumbnail = str(entry.get("thumbnail") or "")
    return MediaCandidate(
        url=url,
        title=str(entry.get("title") or "").strip(),
        duration=duration,
        height=max(heights) if heights and not audio_only else None,
        size_bytes=size or None,
        thumbnail=thumbnail if thumbnail.startswith(("http://", "https://")) else "",
        uploader=str(entry.get("uploader") or entry.get("channel") or "").strip(),
        audio_only=audio_only,
    )


def _probe_once(
    url: str, probe_id: str, platform: str, cancel_event: Optional[threading.Event]
) -> tuple[ProbeResult, bool]:
    """Extract video info without downloading. Returns (result, retryable)."""
    extra: dict[str, Any] = {}
    if platform == "instagram":
        cookiefile = _prepare_cookiefile(probe_id)
        if cookiefile is not None:
            extra["cookiefile"] = str(cookiefile)
    options = _build_options(probe_id, DEFAULT_MAX_HEIGHT, True, _YtdlpLogger(), cancel_event, extra)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadCancelled:
        return ProbeResult(ErrorCode.TIMEOUT, detail="cancelled"), False
    except (DownloadError, ExtractorError) as exc:
        message = str(exc)
        code = classify_error(message)
        retryable = code is ErrorCode.RATE_LIMITED or bool(SERVER_ERROR_PATTERN.search(message))
        return ProbeResult(code, detail=message[:500]), retryable
    except Exception as exc:  # yt-dlp can raise arbitrary errors on site changes.
        logger.warning("Unexpected yt-dlp probe failure for %s", probe_id, exc_info=True)
        return ProbeResult(ErrorCode.UNKNOWN, detail=f"{type(exc).__name__}: {exc}"[:500]), False

    if not info or (info.get("_type") == "playlist" and not any(info.get("entries") or [])):
        return ProbeResult(ErrorCode.NOT_FOUND, detail="yt-dlp found no video"), False
    entry = _first_entry(info)
    if entry.get("is_live") or entry.get("live_status") in ("is_live", "is_upcoming"):
        return ProbeResult(ErrorCode.LIVE, detail="live stream"), False
    candidate = _candidate_from_info(entry, url)
    return (
        ProbeResult(
            ErrorCode.OK,
            (candidate,),
            detail=f"yt-dlp:{entry.get('extractor_key', '')}",
            can_try_browser=platform == "generic",
        ),
        False,
    )


def probe_ytdlp(url: str, platform: str, cancel_event: Optional[threading.Event] = None) -> ProbeResult:
    """Check a link with yt-dlp (title, duration, size) without downloading.

    Blocking; run in a worker thread. Never raises. For TikTok, a failed probe
    still returns a candidate (marked ``info_unavailable``) because the
    scraper fallback may download it anyway.
    """
    probe_id = new_job_id(f"probe-{platform}")
    result = ProbeResult(ErrorCode.UNKNOWN, detail="not started")
    try:
        with public_network_only():
            for attempt in range(1, MAX_SOURCE_ATTEMPTS + 1):
                if cancel_event is not None and cancel_event.is_set():
                    result = ProbeResult(ErrorCode.TIMEOUT, detail="cancelled")
                    break
                result, retryable = _probe_once(url, probe_id, platform, cancel_event)
                if not retryable or attempt == MAX_SOURCE_ATTEMPTS:
                    break
                delay = backoff_delay(attempt)
                logger.info("Probe %s: source error, retrying in %.1f s", probe_id, delay)
                if cancel_event is not None and cancel_event.wait(delay):
                    result = ProbeResult(ErrorCode.TIMEOUT, detail="cancelled during backoff")
                    break
                if cancel_event is None:
                    time.sleep(delay)
    except Exception as exc:  # Defensive: the caller must never see an exception.
        logger.warning("probe_ytdlp crashed for %s", probe_id, exc_info=True)
        result = ProbeResult(ErrorCode.UNKNOWN, detail=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        cleanup_job(probe_id)
    if platform == "tiktok" and result.error in TIKTOK_SCRAPER_RETRY_CODES:
        return ProbeResult(
            ErrorCode.OK,
            (MediaCandidate(url=url, info_unavailable=True),),
            detail=f"yt-dlp probe failed, scraper may work: {result.detail}"[:500],
        )
    return result


def download_browser_stream(
    candidate: MediaCandidate,
    probe: ProbeResult,
    job_id: str,
    cancel_event: Optional[threading.Event] = None,
    audio_only: bool = False,
) -> DownloadResult:
    """Download a stream captured by the headless browser.

    Reuses the browser's User-Agent, page URL (as Referer) and cookies,
    because many video CDNs refuse requests without them. ``audio_only``
    keeps only the audio track (extracted locally with FFmpeg).
    """
    headers = {"Referer": probe.page_url or candidate.url}
    if probe.user_agent:
        headers["User-Agent"] = probe.user_agent
    extra: dict[str, Any] = {"http_headers": headers, "force_generic_extractor": True}
    cookie_path = write_cookie_file(probe.cookies, TEMP_DIR / f"{job_id}{BROWSER_COOKIE_SUFFIX}")
    if cookie_path is not None:
        extra["cookiefile"] = str(cookie_path)
    return download_with_ytdlp(
        candidate.url, job_id, "browser", cancel_event, extra_options=extra, audio_only=audio_only
    )
