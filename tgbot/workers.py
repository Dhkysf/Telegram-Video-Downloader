"""Blocking worker runner, late cleanup, and downloader dispatch."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional, TypeVar

from downloaders import (
    DownloadResult,
    MediaCandidate,
    ProbeResult,
    cleanup_job,
    download_audio,
    download_browser_stream,
    get_generic,
    get_instagram,
    get_tiktok,
    get_twitter,
    get_youtube,
)
from tgbot.links import GENERIC_PLATFORM

logger = logging.getLogger("video_bot.workers")

T = TypeVar("T")
Downloader = Callable[[str, str, Optional[threading.Event]], DownloadResult]
JobRunner = Callable[[str, threading.Event], DownloadResult]  # (job_id, cancel_event)

CANCEL_GRACE_SECONDS = 15.0  # Time a cancelled worker gets to stop before cleanup.

DOWNLOADERS: dict[str, Downloader] = {
    "tiktok": get_tiktok,
    "twitter": get_twitter,
    "instagram": get_instagram,
    "youtube": get_youtube,
    GENERIC_PLATFORM: get_generic,
}


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


def make_runner(
    platform: str, url: str, candidate: MediaCandidate, probe: ProbeResult, audio: bool = False
) -> JobRunner:
    """Return the blocking download function for the chosen candidate.

    ``audio`` downloads only the audio track (sent with send_audio).
    """
    if candidate.via_browser:
        return lambda job_id, event: download_browser_stream(candidate, probe, job_id, event, audio_only=audio)
    if audio:
        return lambda job_id, event: download_audio(url, job_id, platform, event)
    downloader = DOWNLOADERS[platform]
    return lambda job_id, event: downloader(url, job_id, event)
