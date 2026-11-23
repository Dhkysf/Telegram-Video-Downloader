"""Probe, cache/single-flight, and download orchestration flows."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from telegram import Message
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from downloaders import (
    MAX_FILE_SIZE_BYTES,
    DownloadResult,
    ErrorCode,
    ProbeResult,
    cleanup_job,
    new_job_id,
    probe_with_browser,
    probe_ytdlp,
    should_try_browser,
)
from tgbot.links import site_name
from tgbot.preview import auto_pick, send_preview
from tgbot.state import BotState, PendingChoice, _cache_get, _cache_put, probe_cache_key
from tgbot.telegram_io import safe_delete, safe_edit, safe_reply, send_video_file, tg_call
from tgbot.texts import (
    BROWSER_FORCED_TEXT,
    BROWSER_TEXT,
    CHECKING_TEXT,
    ERROR_MESSAGES,
    ONLY_ADS_TEXT,
    STATUS_AUDIO_TEXT,
    STATUS_TEXT,
    SUCCESS_AUDIO_CAPTION,
    SUCCESS_CAPTION,
)
from tgbot.workers import JobRunner, make_runner, run_worker

logger = logging.getLogger("video_bot.flows")

PROBE_TIMEOUT = 90.0  # Checking a link with yt-dlp.
BROWSER_PROBE_TIMEOUT = 180.0  # Includes waiting for the single browser slot.


async def process_download(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    user_id: int,
    platform: str,
    site: str,
    runner: JobRunner,
    state: BotState,
    status: Optional[Message] = None,
    audio: bool = False,
) -> None:
    """Download one video for a user and send it back (always cleans up).

    Args:
        message: The user's original message; the video is sent as a reply.
        runner: Blocking ``(job_id, cancel_event) -> DownloadResult``.
        status: Existing status message to reuse, or None to create one.
        audio: The user asked for audio only (changes the status text).
    """
    job_id = new_job_id(platform)
    started = time.monotonic()
    outcome = ErrorCode.UNKNOWN
    size_mb = 0.0
    cancel_event = threading.Event()
    status_text = (STATUS_AUDIO_TEXT if audio else STATUS_TEXT).format(platform=site)
    logger.info(
        "Job %s started: user=%s platform=%s site=%s mode=%s",
        job_id, user_id, platform, site, "audio" if audio else "video",
    )
    try:
        if status is None:
            status = await tg_call(lambda: message.reply_text(status_text), "status")
        else:
            await safe_edit(status, status_text)

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
        if outcome is ErrorCode.OK:
            outcome = ErrorCode.UNKNOWN
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
        if outcome is ErrorCode.OK:
            state.jobs_ok += 1
        else:
            state.jobs_failed += 1
        removed = await asyncio.to_thread(cleanup_job, job_id)
        logger.info(
            "Job %s finished: user=%s platform=%s outcome=%s size=%.2fMB duration=%.1fs cleaned=%d",
            job_id, user_id, platform, outcome.name, size_mb, time.monotonic() - started, removed,
        )


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
    key = probe_cache_key(url, platform, force_browser)
    started = time.monotonic()
    status: Optional[Message] = None
    try:
        first_text = BROWSER_FORCED_TEXT if force_browser else CHECKING_TEXT.format(site=site)
        cached = _cache_get(state, key)
        if cached is None:
            status = await tg_call(lambda: message.reply_text(first_text), "status")
            cached = _cache_get(state, key)
        if cached is not None:
            probe = cached
            logger.info(
                "probe cache hit: user=%s platform=%s site=%s mode=%s candidates=%d browser=%s",
                user_id, platform, site, key[2], len(probe.candidates), probe.via_browser,
            )
        else:
            inflight = state.in_flight_probes.get(key)
            if inflight is not None:
                probe = await asyncio.shield(inflight)
            else:
                future: asyncio.Future[ProbeResult] = asyncio.get_running_loop().create_future()
                state.in_flight_probes[key] = future
                probe_result: Optional[ProbeResult] = None
                try:
                    probe_result = await probe_link(url, platform, state, status, force_browser)
                    _cache_put(state, key, probe_result)
                finally:
                    if state.in_flight_probes.get(key) is future:
                        state.in_flight_probes.pop(key, None)
                    if not future.done():
                        future.set_result(
                            probe_result
                            if probe_result is not None
                            else ProbeResult(ErrorCode.UNKNOWN, detail="probe aborted")
                        )
                probe = probe_result
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
                if status is None:
                    await safe_reply(message, ONLY_ADS_TEXT)
                else:
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
