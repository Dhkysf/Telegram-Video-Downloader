"""Runtime settings, token loading, FFmpeg check, and logging setup."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shutil
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Optional

from downloaders import PROJECT_ROOT, browser_available

logger = logging.getLogger("video_bot.settings")

# --- Defaults (overridable with environment variables) ------------------------
DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
DEFAULT_JOB_TIMEOUT = 300
DEFAULT_USER_COOLDOWN_SECONDS = 10
CONFIG_FILE: Path = PROJECT_ROOT / "config.py"
FALSE_VALUES = frozenset({"0", "false", "no", "off", "tidak"})

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_FILE: Path = PROJECT_ROOT / "bot.log"
LOCK_FILE: Path = PROJECT_ROOT / "bot.lock"
STOP_REQUEST_FILE: Path = PROJECT_ROOT / "stop.request"
LOG_MAX_BYTES = 1024 * 1024  # Rotate at 1 MB...
LOG_BACKUP_COUNT = 5  # ...keeping bot.log.1 .. bot.log.5 (max ~6 MB total).


@dataclass(frozen=True)
class Settings:
    """Runtime settings read from the environment at startup."""

    max_concurrent_downloads: int
    job_timeout: float
    user_cooldown_seconds: float
    allowed_user_ids: Optional[frozenset[int]]  # None means everyone is allowed.
    confirm_before_download: bool = True
    browser_fallback: bool = True


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
    tray_app.pyw), the echo is skipped so the redirect target stays small.
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


def _lock_file(handle: BinaryIO) -> None:
    """Acquire a non-blocking exclusive lock on ``handle``."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    """Release the lock held on ``handle``."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def instance_lock(path: Path = LOCK_FILE) -> Iterator[bool]:
    """Acquire a single-instance lock file, yielding True if acquired."""
    try:
        handle = open(path, "a+b")
    except OSError as exc:
        logger.error("Cannot open lock file %s: %s", path.name, exc)
        yield False
        return
    try:
        _lock_file(handle)
    except OSError:
        handle.close()
        logger.error("Another instance of the bot is already running (%s); exiting", path.name)
        yield False
        return
    try:
        try:
            handle.seek(0)
            handle.truncate(0)
            handle.write(f"{os.getpid()}\n".encode("ascii"))
            handle.flush()
        except OSError:
            pass
        yield True
    finally:
        try:
            _unlock_file(handle)
        except OSError:
            pass
        handle.close()


def clear_stop_request(path: Path = STOP_REQUEST_FILE) -> bool:
    """Remove ``path`` if it exists; return True if a stop request was present."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Could not remove %s: %s", path.name, exc)
        return False


async def watch_stop_request(
    on_stop: Callable[[], None],
    path: Path = STOP_REQUEST_FILE,
    interval: float = 1.0,
) -> None:
    """Poll for ``path`` and invoke ``on_stop`` once when found."""
    while True:
        if clear_stop_request(path):
            logger.info("Stop requested via %s; shutting down gracefully", path.name)
            on_stop()
            return
        await asyncio.sleep(interval)


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
