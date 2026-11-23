#!/usr/bin/env python3
"""Telegram multi-platform video downloader bot (entry point).

Responsibilities: startup lifecycle, Telegram handlers and Telegram I/O only.
All download logic lives in the ``downloaders`` package.
"""

from __future__ import annotations

import asyncio
import html
import importlib.util
import logging
import os
import re
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, TypeVar
from urllib.parse import urlparse
from uuid import uuid4

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    MessageEntity,
    ReplyParameters,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from downloaders import (
    MAX_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_MB,
    PROJECT_ROOT,
    DownloadResult,
    ErrorCode,
    MediaCandidate,
    ProbeResult,
    backoff_delay,
    browser_available,
    cleanup_job,
    clear_temp_dir,
    describe_site,
    download_browser_stream,
    get_generic,
    get_instagram,
    get_tiktok,
    get_twitter,
    get_youtube,
    install_network_guard,
    is_blocked_host,
    new_job_id,
    prepare_temp_dir,
    probe_with_browser,
    probe_ytdlp,
    should_try_browser,
)

logger = logging.getLogger("video_bot")

T = TypeVar("T")
Downloader = Callable[[str, str, Optional[threading.Event]], DownloadResult]
JobRunner = Callable[[str, threading.Event], DownloadResult]  # (job_id, cancel_event)

# --- Defaults (overridable with environment variables) ------------------------
DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
DEFAULT_JOB_TIMEOUT = 300
DEFAULT_USER_COOLDOWN_SECONDS = 10
CANCEL_GRACE_SECONDS = 15.0  # Time a cancelled worker gets to stop before cleanup.
PROBE_TIMEOUT = 90.0  # Checking a link with yt-dlp.
BROWSER_PROBE_TIMEOUT = 180.0  # Includes waiting for the single browser slot.
PENDING_TTL_SECONDS = 15 * 60  # How long confirmation buttons stay valid.
MAX_TITLE_CHARS = 200
CAPTION_LIMIT = 1024  # Telegram limit for photo captions.
CONFIG_FILE: Path = PROJECT_ROOT / "config.py"
FALSE_VALUES = frozenset({"0", "false", "no", "off", "tidak"})
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)

# --- Telegram retry policy ------------------------------------------------------
TG_MAX_RETRY_AFTER = 2
TG_MAX_NETWORK_RETRIES = 2
UPLOAD_READ_TIMEOUT = 60.0
UPLOAD_WRITE_TIMEOUT = 120.0
UPLOAD_CONNECT_TIMEOUT = 30.0
UPLOAD_POOL_TIMEOUT = 30.0

# --- Platforms -----------------------------------------------------------------
# Known platforms get dedicated handling (TikTok scraper fallback, Instagram
# cookies). Every other public link goes to yt-dlp as "generic".
GENERIC_PLATFORM = "generic"
PLATFORM_DOMAINS: dict[str, tuple[str, ...]] = {
    "tiktok": ("tiktok.com",),
    "twitter": ("twitter.com", "x.com", "t.co"),
    "instagram": ("instagram.com",),
    "youtube": ("youtube.com", "youtu.be"),
}
PLATFORM_NAMES: dict[str, str] = {
    "tiktok": "TikTok",
    "twitter": "X/Twitter",
    "instagram": "Instagram",
    "youtube": "YouTube",
}
DOWNLOADERS: dict[str, Downloader] = {
    "tiktok": get_tiktok,
    "twitter": get_twitter,
    "instagram": get_instagram,
    "youtube": get_youtube,
    GENERIC_PLATFORM: get_generic,
}

URL_REGEX = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
URL_TRAILING_CHARS = ".,;:!?)]}>\"'»”’"
SCHEME_REGEX = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# --- User-facing texts (Bahasa Indonesia) -------------------------------------
WELCOME_TEXT = (
    "👋 <b>Halo!</b> Kirim link video, nanti aku unduhkan dan kirim balik ke sini.\n\n"
    "<b>Bisa dari banyak situs</b>, misalnya TikTok, Instagram, X/Twitter, YouTube, "
    "Facebook, Reddit, Vimeo, Dailymotion, Twitch, Bilibili, sampai link langsung "
    "ke file video (.mp4). Totalnya lebih dari 1.000 situs.\n\n"
    f"📦 Batas ukuran video: <b>{MAX_FILE_SIZE_MB} MB</b> (batas upload bot Telegram).\n"
    "🔒 Video berbayar/ber-DRM (Netflix, Disney+, dll.) tidak bisa diunduh.\n"
    "💡 Link boleh diselipkan di dalam kalimat.\n"
    "✅ Sebelum mengunduh, aku tunjukkan dulu videonya supaya kamu bisa memastikan "
    "itu video yang benar (bukan iklan)."
)
INVALID_LINK_TEXT = (
    "🤔 Aku tidak menemukan link yang bisa diproses.\n"
    "Kirim link video yang diawali http:// atau https:// ya."
)
ACTIVE_JOB_TEXT = "⏳ Video kamu sebelumnya masih diproses. Tunggu sampai selesai dulu ya."
COOLDOWN_TEXT = "🕐 Pelan-pelan ya, tunggu {seconds} detik lagi sebelum kirim link baru."
STATUS_TEXT = "⏳ Mengunduh video dari {platform}..."
CHECKING_TEXT = "🔎 Memeriksa link dari {site}..."
BROWSER_TEXT = (
    "🌐 Videonya belum ketemu dengan cara biasa. Sekarang aku buka halamannya pakai "
    "browser, tunggu sebentar ya (bisa sampai 1 menit)..."
)
BROWSER_FORCED_TEXT = "🌐 Oke, aku buka halamannya pakai browser. Tunggu sebentar ya (bisa sampai 1 menit)..."
CONFIRM_QUESTION = "Betul ini video yang mau kamu unduh?"
CHOOSE_QUESTION = "Pilih video yang mau diunduh:"
ONLY_ADS_TEXT = (
    "⚠️ Aku cuma menemukan video yang kelihatannya iklan, jadi tidak aku unduh. "
    "Nyalakan konfirmasi (CONFIRM_BEFORE_DOWNLOAD) kalau mau memilih sendiri."
)
EXPIRED_TEXT = "⌛ Pilihan ini sudah kedaluwarsa. Kirim ulang link-nya ya."
NOT_YOURS_TEXT = "🙅 Tombol ini hanya untuk orang yang mengirim link-nya."
CANCELLED_NOTE = "❌ Dibatalkan."
CHOSEN_NOTE = "⬇️ Dipilih: {label}. Sedang diunduh..."
BROWSER_NOTE = "🔍 Mencari ulang pakai browser..."
SUCCESS_CAPTION = "✅ Video {platform} berhasil diunduh ({size:.2f} MB)"
SUCCESS_AUDIO_CAPTION = "✅ Audio {platform} berhasil diunduh ({size:.2f} MB)"
GENERIC_APOLOGY = "⚠️ Maaf, terjadi kesalahan. Silakan coba lagi nanti."
ERROR_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.TOO_LARGE: (
        f"❌ Video terlalu besar (lebih dari {MAX_FILE_SIZE_MB} MB). "
        f"Bot Telegram hanya bisa mengirim file maksimal {MAX_FILE_SIZE_MB} MB."
    ),
    ErrorCode.PRIVATE_OR_LOGIN: "🔒 Konten ini privat atau butuh login, jadi tidak bisa diunduh.",
    ErrorCode.NOT_FOUND: "🔍 Video tidak ditemukan di link itu.",
    ErrorCode.UNSUPPORTED: (
        "🔍 Link ini tidak didukung atau tidak berisi video yang bisa diunduh "
        "(misalnya video ber-DRM seperti Netflix)."
    ),
    ErrorCode.RATE_LIMITED: "🚦 Platform sedang membatasi permintaan. Coba lagi beberapa menit lagi ya.",
    ErrorCode.NETWORK: "📡 Ada masalah koneksi atau proses terlalu lama. Silakan coba lagi.",
    ErrorCode.TIMEOUT: "📡 Ada masalah koneksi atau proses terlalu lama. Silakan coba lagi.",
    ErrorCode.LIVE: "🔴 Ini siaran langsung (live), jadi belum bisa diunduh. Coba lagi setelah siarannya selesai.",
    ErrorCode.UNKNOWN: "⚠️ Maaf, video gagal diproses. Silakan coba lagi nanti.",
}


@dataclass(frozen=True)
class Settings:
    """Runtime settings read from the environment at startup."""

    max_concurrent_downloads: int
    job_timeout: float
    user_cooldown_seconds: float
    allowed_user_ids: Optional[frozenset[int]]  # None means everyone is allowed.
    confirm_before_download: bool = True
    browser_fallback: bool = True


@dataclass
class PendingChoice:
    """A probe result waiting for the user to press a button (memory only)."""

    user_id: int
    message: Message  # The user's original message (download replies go here).
    url: str
    platform: str
    site: str
    probe: ProbeResult
    created: float
    preview: Optional[Message] = None
    preview_html: str = ""
    preview_is_photo: bool = False


@dataclass
class BotState:
    """In-memory state shared by all handlers (no persistence)."""

    settings: Settings
    semaphore: asyncio.Semaphore
    last_request: dict[int, float] = field(default_factory=dict)
    active_users: set[int] = field(default_factory=set)
    pending: dict[str, PendingChoice] = field(default_factory=dict)


# =============================================================================
# Startup helpers
# =============================================================================
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_FILE: Path = PROJECT_ROOT / "bot.log"
LOG_MAX_BYTES = 1024 * 1024  # Rotate at 1 MB...
LOG_BACKUP_COUNT = 5  # ...keeping bot.log.1 .. bot.log.5 (max ~6 MB total).


class RedactingFormatter(logging.Formatter):
    """Formatter that masks the bot token anywhere in a log line or traceback."""

    def __init__(self, fmt: str, secret: str) -> None:
        """Create the formatter for ``fmt`` that hides ``secret``."""
        super().__init__(fmt)
        self._secret = secret

    def format(self, record: logging.LogRecord) -> str:
        """Format the record, replacing the secret with a placeholder."""
        return super().format(record).replace(self._secret, "[REDACTED_TOKEN]")


def configure_logging() -> None:
    """Configure root logging and silence request logs that contain the token.

    Logs always go to a size-rotated ``bot.log``. They are also echoed to the
    console when running in a terminal; when stderr is redirected (as by
    start_bot.bat), the echo is skipped so the redirect target stays small.
    """
    handlers: list[logging.Handler] = []
    try:
        handlers.append(
            RotatingFileHandler(
                LOG_FILE,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    except OSError as exc:  # Unwritable folder: keep running with console logs.
        print(f"Could not open {LOG_FILE.name}: {exc}", file=sys.stderr)
    stderr_is_terminal = bool(getattr(sys.stderr, "isatty", None) and sys.stderr.isatty())
    if stderr_is_terminal or not handlers:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def redact_token_in_logs(token: str) -> None:
    """Mask the token in all log output (PTB includes it in some error messages)."""
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter(LOG_FORMAT, token))


def load_token() -> Optional[str]:
    """Return TELEGRAM_BOT_TOKEN from the environment or the optional config.py."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token
    if not CONFIG_FILE.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("bot_local_config", CONFIG_FILE)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # A broken config.py must not crash with a traceback.
        logger.error("config.py could not be loaded: %s", type(exc).__name__)
        return None
    value = getattr(module, "TELEGRAM_BOT_TOKEN", "")
    return value.strip() if isinstance(value, str) and value.strip() else None


def configure_ffmpeg() -> bool:
    """Prepend FFMPEG_DIR to PATH (if set) and check that FFmpeg exists.

    Returns:
        True if an ``ffmpeg`` binary was found.
    """
    ffmpeg_dir = os.environ.get("FFMPEG_DIR", "").strip()
    if ffmpeg_dir:
        resolved = str(Path(ffmpeg_dir).expanduser().resolve())
        os.environ["PATH"] = resolved + os.pathsep + os.environ.get("PATH", "")
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        logger.warning("FFmpeg not found: merging video/audio and remuxing to MP4 may fail")
        return False
    logger.info("FFmpeg found: %s", ffmpeg_path)
    return True


def _env_number(name: str, default: int) -> int:
    """Read a positive integer environment variable, falling back to ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using default %d", name, raw, default)
        return default
    if value < 1:
        logger.warning("%s must be >= 1; using default %d", name, default)
        return default
    return value


def _env_flag(name: str, default: bool) -> bool:
    """Read an on/off environment variable ("0", "false", "no", "off" = off)."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in FALSE_VALUES


def _parse_allowed_users(raw: str) -> Optional[frozenset[int]]:
    """Parse ALLOWED_USER_IDS (comma-separated). Empty/unset means everyone."""
    if not raw.strip():
        return None
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("Ignoring invalid entry in ALLOWED_USER_IDS: %r", part)
    if not ids:
        # Assumption: a set-but-invalid allow-list denies everyone (safest option).
        logger.error("ALLOWED_USER_IDS is set but has no valid IDs: every user will be ignored")
    return frozenset(ids)


def load_settings() -> Settings:
    """Build Settings from environment variables."""
    settings = Settings(
        max_concurrent_downloads=_env_number("MAX_CONCURRENT_DOWNLOADS", DEFAULT_MAX_CONCURRENT_DOWNLOADS),
        job_timeout=float(_env_number("JOB_TIMEOUT", DEFAULT_JOB_TIMEOUT)),
        user_cooldown_seconds=float(_env_number("USER_COOLDOWN_SECONDS", DEFAULT_USER_COOLDOWN_SECONDS)),
        allowed_user_ids=_parse_allowed_users(os.environ.get("ALLOWED_USER_IDS", "")),
        confirm_before_download=_env_flag("CONFIRM_BEFORE_DOWNLOAD", True),
        browser_fallback=_env_flag("BROWSER_FALLBACK", True),
    )
    browser_state = "off"
    if settings.browser_fallback:
        browser_state = "on" if browser_available() else "unavailable (pip install playwright)"
    logger.info(
        "Settings: max_concurrent=%d job_timeout=%.0fs cooldown=%.0fs allow_list=%s confirm=%s browser=%s",
        settings.max_concurrent_downloads,
        settings.job_timeout,
        settings.user_cooldown_seconds,
        "off" if settings.allowed_user_ids is None else f"{len(settings.allowed_user_ids)} user(s)",
        "on" if settings.confirm_before_download else "off",
        browser_state,
    )
    return settings


# =============================================================================
# URL extraction & platform detection
# =============================================================================
def _normalize_url(candidate: str) -> Optional[str]:
    """Return ``candidate`` as an http(s) URL, or None if it is not one."""
    candidate = candidate.strip()
    if not candidate:
        return None
    if not SCHEME_REGEX.match(candidate):
        # Assumption: Telegram URL entities like "youtu.be/abc" have no scheme; use https.
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    return candidate


def extract_first_url(message: Message) -> Optional[str]:
    """Return the first http(s) URL in a message (entities first, then regex)."""
    entities = message.parse_entities([MessageEntity.URL, MessageEntity.TEXT_LINK])
    for entity, text in sorted(entities.items(), key=lambda item: item[0].offset):
        raw = entity.url if entity.type == MessageEntity.TEXT_LINK else text
        url = _normalize_url(raw or "")
        if url:
            return url
    for match in URL_REGEX.finditer(message.text or ""):
        url = _normalize_url(match.group(0).rstrip(URL_TRAILING_CHARS))
        if url:
            return url
    return None


def detect_platform(url: str) -> Optional[str]:
    """Return the platform key for ``url``.

    Known platforms are matched by exact host or proper subdomain only
    (``host == domain`` or ``host.endswith("." + domain)``; never substring
    matching). Any other public host becomes ``"generic"``. Local/private
    hosts (localhost, 192.168.x.x, ...) return None and are rejected.
    """
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not host or is_blocked_host(host):
        return None
    for platform, domains in PLATFORM_DOMAINS.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return platform
    return GENERIC_PLATFORM


# =============================================================================
# Telegram I/O helpers
# =============================================================================
def _retry_after_seconds(exc: RetryAfter) -> float:
    """Return RetryAfter delay in seconds (PTB may expose int or timedelta)."""
    value = exc.retry_after
    if isinstance(value, timedelta):
        return max(value.total_seconds(), 1.0)
    return max(float(value), 1.0)


async def tg_call(action: Callable[[], Awaitable[T]], description: str) -> T:
    """Run a Telegram API call with RetryAfter and network retry handling.

    RetryAfter: sleep ``retry_after`` and retry (max 2 times). TimedOut /
    NetworkError: retry with backoff (max 2 times). BadRequest and Forbidden
    are never retried and are re-raised to the caller.
    """
    retry_after_count = 0
    network_count = 0
    while True:
        try:
            return await action()
        except RetryAfter as exc:
            if retry_after_count >= TG_MAX_RETRY_AFTER:
                raise
            retry_after_count += 1
            delay = _retry_after_seconds(exc)
            logger.warning("Telegram flood control on %s: sleeping %.0f s", description, delay)
            await asyncio.sleep(delay)
        except (BadRequest, Forbidden):
            raise  # BadRequest subclasses NetworkError; it must not be retried.
        except (TimedOut, NetworkError) as exc:
            if network_count >= TG_MAX_NETWORK_RETRIES:
                raise
            network_count += 1
            delay = backoff_delay(network_count)
            logger.warning(
                "Telegram %s on %s: retrying in %.1f s (%d/%d)",
                type(exc).__name__, description, delay, network_count, TG_MAX_NETWORK_RETRIES,
            )
            await asyncio.sleep(delay)


async def safe_reply(message: Message, text: str, parse_mode: Optional[str] = None) -> Optional[Message]:
    """Reply to a message; BadRequest is ignored, Forbidden is logged at INFO."""
    try:
        return await tg_call(lambda: message.reply_text(text, parse_mode=parse_mode), "reply")
    except Forbidden:
        logger.info("Chat %s blocked the bot; reply skipped", message.chat_id)
    except BadRequest as exc:
        logger.debug("Ignoring BadRequest on reply: %s", exc)
    return None


async def safe_edit(status: Optional[Message], text: str) -> None:
    """Edit the status message; BadRequest (deleted/not modified) is ignored."""
    if status is None:
        return
    try:
        await tg_call(lambda: status.edit_text(text), "edit status")
    except BadRequest as exc:
        logger.debug("Ignoring BadRequest on edit: %s", exc)


async def safe_delete(status: Optional[Message]) -> None:
    """Delete the status message; BadRequest (already deleted) is ignored."""
    if status is None:
        return
    try:
        await tg_call(status.delete, "delete status")
    except BadRequest as exc:
        logger.debug("Ignoring BadRequest on delete: %s", exc)


async def send_video_file(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    path: Path,
    caption: str,
    audio_only: bool = False,
) -> None:
    """Upload a video (or audio for audio-only sites) as a reply, with retries."""
    common = {
        "chat_id": message.chat_id,
        "caption": caption,
        "reply_parameters": ReplyParameters(
            message_id=message.message_id, allow_sending_without_reply=True
        ),
        "message_thread_id": message.message_thread_id if message.is_topic_message else None,
        "read_timeout": UPLOAD_READ_TIMEOUT,
        "write_timeout": UPLOAD_WRITE_TIMEOUT,
        "connect_timeout": UPLOAD_CONNECT_TIMEOUT,
        "pool_timeout": UPLOAD_POOL_TIMEOUT,
    }

    async def _upload() -> Message:
        # The file is reopened on every retry because a failed upload consumes the stream.
        with open(path, "rb") as media:
            if audio_only:
                return await context.bot.send_audio(audio=media, filename=path.name, **common)
            return await context.bot.send_video(video=media, supports_streaming=True, **common)

    await tg_call(_upload, "send_audio" if audio_only else "send_video")


# =============================================================================
# Abuse protection
# =============================================================================
def get_state(context: ContextTypes.DEFAULT_TYPE) -> BotState:
    """Return the shared in-memory bot state."""
    return context.application.bot_data["state"]


def is_allowed(update: Update, state: BotState) -> bool:
    """Return True if the sender passes the optional ALLOWED_USER_IDS list."""
    allowed = state.settings.allowed_user_ids
    if allowed is None:
        return True
    user = update.effective_user
    return user is not None and user.id in allowed


def try_start_job(state: BotState, user_id: int) -> Optional[str]:
    """Register a new job for a user, or return a rejection text.

    Runs without awaiting, so check-and-set is atomic on the event loop even
    with concurrent update processing.
    """
    if user_id in state.active_users:
        return ACTIVE_JOB_TEXT
    now = time.monotonic()
    elapsed = now - state.last_request.get(user_id, float("-inf"))
    if elapsed < state.settings.user_cooldown_seconds:
        remaining = max(1, int(state.settings.user_cooldown_seconds - elapsed + 0.999))
        return COOLDOWN_TEXT.format(seconds=remaining)
    state.last_request[user_id] = now
    state.active_users.add(user_id)
    return None


# =============================================================================
# Download orchestration (Telegram side only)
# =============================================================================
def _late_cleanup(job_id: str) -> Callable[["asyncio.Future[Any]"], None]:
    """Return a done-callback that cleans up after a worker outlived its job."""

    def _callback(future: "asyncio.Future[Any]") -> None:
        if not future.cancelled() and future.exception() is not None:
            logger.debug("Late worker for job %s ended with an exception", job_id)
        removed = cleanup_job(job_id)
        logger.info("Late cleanup for job %s removed %d file(s)", job_id, removed)

    return _callback


async def run_worker(
    func: Callable[..., T],
    *args: Any,
    cancel_event: threading.Event,
    timeout: float,
    on_timeout: T,
    on_error: T,
    cleanup_prefix: str = "",
) -> T:
    """Run blocking ``func(*args)`` in a worker thread with a timeout.

    On timeout the worker is asked to stop through ``cancel_event``; if it
    keeps running, its files are cleaned up when it finally ends.
    """
    worker = asyncio.ensure_future(asyncio.to_thread(func, *args))
    try:
        return await asyncio.wait_for(asyncio.shield(worker), timeout=timeout)
    except asyncio.TimeoutError:
        cancel_event.set()  # Ask the worker to stop at its next checkpoint.
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=CANCEL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Worker %s still running after cancel; deferring cleanup", cleanup_prefix or "?")
            if cleanup_prefix:
                worker.add_done_callback(_late_cleanup(cleanup_prefix))
        except Exception:  # The worker's own result is irrelevant after a timeout.
            pass
        return on_timeout
    except Exception:  # Workers never raise; this is purely defensive.
        logger.warning("Worker %s raised", cleanup_prefix or func.__name__, exc_info=True)
        return on_error


async def process_download(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    user_id: int,
    platform: str,
    site: str,
    runner: JobRunner,
    state: BotState,
    status: Optional[Message] = None,
) -> None:
    """Download one video for a user and send it back (always cleans up).

    Args:
        message: The user's original message; the video is sent as a reply.
        runner: Blocking ``(job_id, cancel_event) -> DownloadResult``.
        status: Existing status message to reuse, or None to create one.
    """
    job_id = new_job_id(platform)
    started = time.monotonic()
    outcome = ErrorCode.UNKNOWN
    size_mb = 0.0
    cancel_event = threading.Event()
    logger.info("Job %s started: user=%s platform=%s site=%s", job_id, user_id, platform, site)
    try:
        if status is None:
            status = await tg_call(lambda: message.reply_text(STATUS_TEXT.format(platform=site)), "status")
        else:
            await safe_edit(status, STATUS_TEXT.format(platform=site))

        async with state.semaphore:
            result = await run_worker(
                runner, job_id, cancel_event,
                cancel_event=cancel_event,
                timeout=state.settings.job_timeout,
                on_timeout=DownloadResult(None, ErrorCode.TIMEOUT, "job timeout"),
                on_error=DownloadResult(None, ErrorCode.UNKNOWN, "worker raised"),
                cleanup_prefix=job_id,
            )
        outcome = result.error
        if result.detail and outcome is not ErrorCode.OK:
            logger.info("Job %s failure detail: %s", job_id, result.detail)

        path = result.path
        if outcome is ErrorCode.OK:
            if path is None or not path.is_file():
                outcome = ErrorCode.UNKNOWN
                logger.warning("Job %s reported OK but no file exists", job_id)
            else:
                size_bytes = path.stat().st_size
                size_mb = size_bytes / (1024 * 1024)
                if size_bytes > MAX_FILE_SIZE_BYTES:
                    outcome = ErrorCode.TOO_LARGE

        if outcome is ErrorCode.OK and path is not None:
            template = SUCCESS_AUDIO_CAPTION if result.audio_only else SUCCESS_CAPTION
            caption = template.format(platform=site, size=size_mb)
            await send_video_file(context, message, path, caption, audio_only=result.audio_only)
            await safe_delete(status)
        else:
            await safe_edit(status, ERROR_MESSAGES.get(outcome, ERROR_MESSAGES[ErrorCode.UNKNOWN]))
    except Forbidden:
        logger.info("User %s blocked the bot; stopping job %s", user_id, job_id)
    except TelegramError as exc:
        logger.warning("Telegram error in job %s: %s", job_id, type(exc).__name__, exc_info=True)
        if outcome is ErrorCode.OK:
            outcome = ErrorCode.NETWORK  # The download worked but the upload failed.
        try:
            await safe_edit(status, ERROR_MESSAGES[ErrorCode.NETWORK])
        except TelegramError:
            logger.debug("Could not report upload failure for job %s", job_id)
    finally:
        cancel_event.set()
        removed = await asyncio.to_thread(cleanup_job, job_id)
        logger.info(
            "Job %s finished: user=%s platform=%s outcome=%s size=%.2fMB duration=%.1fs cleaned=%d",
            job_id, user_id, platform, outcome.name, size_mb, time.monotonic() - started, removed,
        )


def make_runner(platform: str, url: str, candidate: MediaCandidate, probe: ProbeResult) -> JobRunner:
    """Return the blocking download function for the chosen candidate."""
    if candidate.via_browser:
        return lambda job_id, event: download_browser_stream(candidate, probe, job_id, event)
    downloader = DOWNLOADERS[platform]
    return lambda job_id, event: downloader(url, job_id, event)


# =============================================================================
# Link check (probe) and confirmation
# =============================================================================
async def site_name(platform: str, url: str) -> str:
    """Return a readable site name ("TikTok", "Dailymotion", "example.com")."""
    if platform == GENERIC_PLATFORM:
        # Extractor lookup is pure CPU (regex matching), but the first call compiles
        # ~1,600 patterns, so it runs off the event loop.
        return await asyncio.to_thread(describe_site, url)
    return PLATFORM_NAMES[platform]


async def probe_link(
    url: str, platform: str, state: BotState, status: Optional[Message], force_browser: bool
) -> ProbeResult:
    """Check a link: yt-dlp first, then (for generic sites) a headless browser."""
    timeout_result = ProbeResult(ErrorCode.TIMEOUT, detail="probe timeout")
    error_result = ProbeResult(ErrorCode.UNKNOWN, detail="probe worker raised")
    first: Optional[ProbeResult] = None
    async with state.semaphore:
        if not force_browser:
            event = threading.Event()
            first = await run_worker(
                probe_ytdlp, url, platform, event,
                cancel_event=event, timeout=PROBE_TIMEOUT, on_timeout=timeout_result, on_error=error_result,
            )
            if not (state.settings.browser_fallback and should_try_browser(platform, first)):
                return first
            logger.info("yt-dlp found nothing (%s); trying headless browser", first.error.name)
            await safe_edit(status, BROWSER_TEXT)
        event = threading.Event()
        second = await run_worker(
            probe_with_browser, url, event,
            cancel_event=event, timeout=BROWSER_PROBE_TIMEOUT, on_timeout=timeout_result, on_error=error_result,
        )
    if second.error is ErrorCode.OK or first is None:
        return second
    # Neither found a video: report the more informative error.
    if second.error in (ErrorCode.NOT_FOUND, ErrorCode.UNKNOWN):
        return first
    return second


def format_duration(seconds: Optional[float]) -> str:
    """Return "m:ss" or "h:mm:ss" (or "?" when unknown)."""
    if not seconds:
        return "?"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _size_text(size_bytes: Optional[int]) -> str:
    return f"±{size_bytes / (1024 * 1024):.1f} MB" if size_bytes else ""


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def candidate_line(index: int, candidate: MediaCandidate) -> str:
    """One line describing a browser-found candidate (HTML)."""
    parts: list[str] = []
    if candidate.likely_ad:
        parts.append("⚠️ <b>kemungkinan iklan</b>")
    parts.append(f"⏱ {format_duration(candidate.duration)}")
    if candidate.audio_only:
        parts.append("🎵 audio")
    elif candidate.height:
        parts.append(f"{candidate.height}p")
    if candidate.size_bytes:
        parts.append(_size_text(candidate.size_bytes))
    if candidate.stream_kind:
        parts.append(candidate.stream_kind)
    if candidate.stream_host:
        parts.append(html.escape(candidate.stream_host))
    return f"{index}. " + " · ".join(parts)


def build_preview(site: str, probe: ProbeResult, allow_browser: bool) -> tuple[str, InlineKeyboardMarkup, str]:
    """Return (HTML text, keyboard with "{action}:{token}" placeholders, kind).

    ``kind`` is "photo-bytes" (browser screenshot), "photo-url" (thumbnail)
    or "text". Buttons use the literal token ``TOKEN``, replaced later.
    """
    lines: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []
    if probe.via_browser:
        lines.append("🌐 Aku buka halamannya pakai browser. Gambar ini adalah tampilan halamannya.")
        if probe.page_title:
            lines.append(f"📄 <b>{html.escape(_shorten(probe.page_title, 150))}</b>")
        lines.append("")
        lines.append("Video yang ketemu:")
        for index, candidate in enumerate(probe.candidates, start=1):
            lines.append(candidate_line(index, candidate))
            label = f"{'⚠️ ' if candidate.likely_ad else ''}Video {index} ({format_duration(candidate.duration)})"
            rows.append([InlineKeyboardButton(label, callback_data=f"dl:TOKEN:{index - 1}")])
        if all(candidate.likely_ad for candidate in probe.candidates):
            lines.append("")
            lines.append(
                "⚠️ Semua yang ketemu kelihatan seperti iklan. Video utamanya mungkin butuh login, "
                "klik tertentu, atau situsnya memblokir browser otomatis."
            )
        lines.append("")
        lines.append(CHOOSE_QUESTION)
        kind = "photo-bytes" if probe.screenshot else "text"
    else:
        candidate = probe.candidates[0]
        if candidate.info_unavailable:
            lines.append(f"🎬 Video {html.escape(site)}")
            lines.append("ℹ️ Detail videonya tidak bisa dibaca sekarang, tapi masih bisa dicoba lewat jalur cadangan.")
        else:
            title = candidate.title or "(tanpa judul)"
            lines.append(f"🎬 <b>{html.escape(_shorten(title, MAX_TITLE_CHARS))}</b>")
            source = f"🌐 {html.escape(site)}"
            if candidate.uploader:
                source += f" · 👤 {html.escape(_shorten(candidate.uploader, 60))}"
            lines.append(source)
            facts = [f"⏱ {format_duration(candidate.duration)}"]
            if candidate.audio_only:
                facts.append("🎵 audio saja")
            elif candidate.height:
                facts.append(f"📺 {candidate.height}p")
            if candidate.size_bytes:
                facts.append(f"📦 {_size_text(candidate.size_bytes)}")
            lines.append(" · ".join(facts))
            if candidate.size_bytes and candidate.size_bytes > MAX_FILE_SIZE_BYTES:
                lines.append(
                    f"⚠️ Perkiraan ukurannya di atas {MAX_FILE_SIZE_MB} MB. Nanti aku coba resolusi lebih "
                    "kecil, tapi bisa saja tetap kebesaran."
                )
        lines.append("")
        lines.append(CONFIRM_QUESTION)
        rows.append([InlineKeyboardButton("✅ Ya, unduh", callback_data="dl:TOKEN:0")])
        if allow_browser and probe.can_try_browser:
            rows.append([InlineKeyboardButton("🔍 Bukan ini, cari pakai browser", callback_data="br:TOKEN")])
        kind = "photo-url" if candidate.thumbnail else "text"
    rows.append([InlineKeyboardButton("❌ Batal", callback_data="cx:TOKEN")])
    text = "\n".join(lines)
    if kind != "text" and len(text) > CAPTION_LIMIT:
        kind = "text"  # Too long for a photo caption.
    return text, InlineKeyboardMarkup(rows), kind


def _with_token(markup: InlineKeyboardMarkup, token: str) -> InlineKeyboardMarkup:
    """Replace the TOKEN placeholder in every button's callback data."""
    rows = [
        [InlineKeyboardButton(b.text, callback_data=str(b.callback_data).replace("TOKEN", token)) for b in row]
        for row in markup.inline_keyboard
    ]
    return InlineKeyboardMarkup(rows)


def purge_expired(state: BotState) -> None:
    """Forget confirmation buttons older than PENDING_TTL_SECONDS."""
    now = time.monotonic()
    for token in [t for t, p in state.pending.items() if now - p.created > PENDING_TTL_SECONDS]:
        state.pending.pop(token, None)


async def send_preview(pending: PendingChoice, state: BotState) -> None:
    """Show what was found and ask the user to confirm (stores the choice)."""
    purge_expired(state)
    token = uuid4().hex[:12]
    allow_browser = state.settings.browser_fallback and browser_available()
    text, markup, kind = build_preview(pending.site, pending.probe, allow_browser)
    markup = _with_token(markup, token)
    message = pending.message
    common: dict[str, Any] = {
        "parse_mode": ParseMode.HTML,
        "reply_markup": markup,
        "reply_parameters": ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
    }
    preview: Optional[Message] = None
    if kind != "text":
        photo: Any = pending.probe.screenshot if kind == "photo-bytes" else pending.probe.candidates[0].thumbnail
        try:
            preview = await tg_call(lambda: message.reply_photo(photo=photo, caption=text, **common), "preview photo")
            pending.preview_is_photo = True
        except BadRequest as exc:  # Telegram could not fetch/accept the thumbnail.
            logger.debug("Preview photo rejected (%s); sending text instead", exc)
    if preview is None:
        preview = await tg_call(
            lambda: message.reply_text(text, link_preview_options=NO_LINK_PREVIEW, **common), "preview text"
        )
        pending.preview_is_photo = False
    pending.preview = preview
    pending.preview_html = text
    state.pending[token] = pending


async def finish_preview(pending: PendingChoice, note: str) -> None:
    """Remove the buttons from a preview and append a short note."""
    preview = pending.preview
    if preview is None:
        return
    limit = CAPTION_LIMIT if pending.preview_is_photo else 4096
    body = pending.preview_html.rsplit("\n\n", 1)[0]  # Drop the question line.
    text = f"{body}\n\n{note}"
    if len(text) > limit:
        text = note
    try:
        if pending.preview_is_photo:
            await tg_call(
                lambda: preview.edit_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=None),
                "finish preview",
            )
        else:
            await tg_call(
                lambda: preview.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=None),
                "finish preview",
            )
    except BadRequest as exc:
        logger.debug("Ignoring BadRequest on preview edit: %s", exc)


def auto_pick(probe: ProbeResult) -> Optional[MediaCandidate]:
    """Choose a candidate without asking (confirmation turned off)."""
    if not probe.candidates:
        return None
    best = probe.candidates[0]  # Already ranked: real videos first, then longest.
    if best.likely_ad:
        return None
    return best


async def check_link(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    user_id: int,
    url: str,
    platform: str,
    state: BotState,
    force_browser: bool = False,
) -> None:
    """Probe a link, then ask for confirmation (or download directly)."""
    site = await site_name(platform, url)
    started = time.monotonic()
    status: Optional[Message] = None
    try:
        first_text = BROWSER_FORCED_TEXT if force_browser else CHECKING_TEXT.format(site=site)
        status = await tg_call(lambda: message.reply_text(first_text), "status")
        probe = await probe_link(url, platform, state, status, force_browser)
        logger.info(
            "Probe user=%s platform=%s site=%s outcome=%s candidates=%d browser=%s took=%.1fs detail=%s",
            user_id, platform, site, probe.error.name, len(probe.candidates), probe.via_browser,
            time.monotonic() - started, probe.detail[:200],
        )
        if probe.error is not ErrorCode.OK or not probe.candidates:
            await safe_edit(status, ERROR_MESSAGES.get(probe.error, ERROR_MESSAGES[ErrorCode.UNKNOWN]))
            return
        if not state.settings.confirm_before_download:
            candidate = auto_pick(probe)
            if candidate is None:
                await safe_edit(status, ONLY_ADS_TEXT)
                return
            runner = make_runner(platform, url, candidate, probe)
            await process_download(context, message, user_id, platform, site, runner, state, status)
            return
        await safe_delete(status)
        status = None
        pending = PendingChoice(user_id, message, url, platform, site, probe, time.monotonic())
        await send_preview(pending, state)
    except Forbidden:
        logger.info("User %s blocked the bot; stopping link check", user_id)
    except TelegramError as exc:
        logger.warning("Telegram error while checking a link: %s", type(exc).__name__, exc_info=True)
        try:
            await safe_edit(status, ERROR_MESSAGES[ErrorCode.NETWORK])
        except TelegramError:
            logger.debug("Could not report link-check failure")


# =============================================================================
# Handlers
# =============================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start and /help."""
    message = update.effective_message
    if message is None or not is_allowed(update, get_state(context)):
        return
    await safe_reply(message, WELCOME_TEXT, parse_mode=ParseMode.HTML)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any text message: find a link, check it, ask for confirmation."""
    message = update.message
    user = update.effective_user
    if message is None or user is None or not message.text:
        return
    state = get_state(context)
    if not is_allowed(update, state):
        return

    url = extract_first_url(message)
    platform = detect_platform(url) if url else None
    if url is None or platform is None:
        await safe_reply(message, INVALID_LINK_TEXT)
        return

    rejection = try_start_job(state, user.id)
    if rejection is not None:
        await safe_reply(message, rejection)
        return
    try:
        await check_link(context, message, user.id, url, platform, state)
    finally:
        state.active_users.discard(user.id)


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the confirmation buttons: download, search with browser, cancel."""
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    state = get_state(context)
    if not is_allowed(update, state):
        return
    parts = str(query.data or "").split(":")
    action, token = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
    pending = state.pending.get(token)
    if pending is None or time.monotonic() - pending.created > PENDING_TTL_SECONDS:
        state.pending.pop(token, None)
        await _answer(query, EXPIRED_TEXT, alert=True)
        try:
            await tg_call(lambda: query.edit_message_reply_markup(reply_markup=None), "drop buttons")
        except BadRequest as exc:
            logger.debug("Ignoring BadRequest on expired buttons: %s", exc)
        return
    if user.id != pending.user_id:
        await _answer(query, NOT_YOURS_TEXT, alert=True)
        return

    if action == "cx":
        state.pending.pop(token, None)
        await _answer(query, "Dibatalkan")
        await finish_preview(pending, CANCELLED_NOTE)
        return
    if action not in ("dl", "br"):
        await _answer(query, "")
        return

    # Same check-and-set as for new links: no await between check and set.
    if user.id in state.active_users:
        await _answer(query, ACTIVE_JOB_TEXT, alert=True)
        return
    candidate: Optional[MediaCandidate] = None
    if action == "dl":
        try:
            candidate = pending.probe.candidates[int(parts[2])]
        except (IndexError, ValueError):
            await _answer(query, EXPIRED_TEXT, alert=True)
            return
    state.pending.pop(token, None)
    state.active_users.add(user.id)
    try:
        await _answer(query, "")
        if candidate is not None:
            label = f"video #{int(parts[2]) + 1}" if pending.probe.via_browser else "video ini"
            await finish_preview(pending, CHOSEN_NOTE.format(label=label))
            runner = make_runner(pending.platform, pending.url, candidate, pending.probe)
            await process_download(
                context, pending.message, user.id, pending.platform, pending.site, runner, state
            )
        else:
            await finish_preview(pending, BROWSER_NOTE)
            await check_link(
                context, pending.message, user.id, pending.url, pending.platform, state, force_browser=True
            )
    finally:
        state.active_users.discard(user.id)


async def _answer(query: Any, text: str, alert: bool = False) -> None:
    """Answer a button press (stops the loading spinner); errors are ignored."""
    try:
        await query.answer(text=text or None, show_alert=alert)
    except TelegramError as exc:
        logger.debug("Could not answer callback query: %s", exc)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any unhandled error and send a short apology when possible."""
    error = context.error
    # Connection drops (no internet, DNS failure, 502 from Telegram) are
    # expected on a home PC and PTB retries polling by itself: one WARNING
    # line, no traceback. BadRequest subclasses NetworkError but is a real bug.
    if isinstance(error, NetworkError) and not isinstance(error, BadRequest):
        source = "while polling" if update is None else "while processing an update"
        logger.warning("Telegram connection problem %s: %s: %s", source, type(error).__name__, error)
    else:
        # exc_info gets the exception object: this runs outside an except
        # block, so exc_info=True would log no traceback.
        logger.error("Unhandled error while processing an update", exc_info=error)
    if not isinstance(update, Update) or update.effective_chat is None:
        return
    state = context.application.bot_data.get("state")
    if isinstance(state, BotState) and not is_allowed(update, state):
        return
    try:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=GENERIC_APOLOGY)
    except Exception:  # The error handler itself must never fail.
        logger.debug("Could not send apology message", exc_info=True)


# =============================================================================
# Application lifecycle
# =============================================================================
def build_application(token: str, settings: Settings) -> Application:
    """Create the Application with concurrent updates and all handlers."""

    async def post_init(application: Application) -> None:
        application.bot_data["state"] = BotState(
            settings=settings,
            semaphore=asyncio.Semaphore(settings.max_concurrent_downloads),
        )
        logger.info("Bot @%s is running (long polling)", application.bot.username)

    async def post_shutdown(_application: Application) -> None:
        removed = await asyncio.to_thread(clear_temp_dir)
        logger.info("Shutdown complete; removed %d leftover temp file(s)", removed)

    application = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler(["start", "help"], start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(handle_choice, pattern=r"^(dl|br|cx):"))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    """Run the startup lifecycle and start long polling."""
    # 1. Logging.
    configure_logging()
    # 2. Token.
    token = load_token()
    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN is not set. Export it as an environment variable "
            "or define TELEGRAM_BOT_TOKEN in config.py next to bot.py."
        )
        sys.exit(1)
    redact_token_in_logs(token)
    # 3. FFmpeg.
    configure_ffmpeg()
    # Downloads may only connect to public internet addresses (protects the LAN).
    install_network_guard()
    # 4. Temp directory.
    try:
        removed = prepare_temp_dir()
    except OSError as exc:
        logger.error("Cannot create temp directory: %s", exc)
        sys.exit(1)
    if removed:
        logger.info("Removed %d leftover temp file(s) from a previous run", removed)
    # 5. Application + long polling (SIGINT/SIGTERM trigger a graceful stop).
    application = build_application(token, load_settings())
    try:
        application.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
            stop_signals=(signal.SIGINT, signal.SIGTERM),
        )
    except InvalidToken:
        # PTB's own message contains the token, so it is deliberately not logged.
        logger.error("Telegram rejected TELEGRAM_BOT_TOKEN; check the token with @BotFather")
        sys.exit(1)


if __name__ == "__main__":
    main()
