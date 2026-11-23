"""Telegram API retry wrapper and message/upload helpers."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional, TypeVar

from telegram import Message, ReplyParameters
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from downloaders import backoff_delay

logger = logging.getLogger("video_bot.telegram_io")

T = TypeVar("T")

# --- Telegram retry policy ------------------------------------------------------
TG_MAX_RETRY_AFTER = 2
TG_MAX_NETWORK_RETRIES = 2
UPLOAD_READ_TIMEOUT = 60.0
UPLOAD_WRITE_TIMEOUT = 120.0
UPLOAD_CONNECT_TIMEOUT = 30.0
UPLOAD_POOL_TIMEOUT = 30.0


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
    """Edit the status message; BadRequest and Forbidden are ignored."""
    if status is None:
        return
    try:
        await tg_call(lambda: status.edit_text(text), "edit status")
    except Forbidden:
        logger.info("Chat %s blocked the bot; status edit skipped", getattr(status, "chat_id", "?"))
    except BadRequest as exc:
        logger.debug("Ignoring BadRequest on edit: %s", exc)


async def safe_delete(status: Optional[Message]) -> None:
    """Delete the status message; BadRequest and Forbidden are ignored."""
    if status is None:
        return
    try:
        await tg_call(status.delete, "delete status")
    except Forbidden:
        logger.info("Chat %s blocked the bot; status delete skipped", getattr(status, "chat_id", "?"))
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
