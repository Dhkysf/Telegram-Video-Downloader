"""Telegram command, message, callback, and error handlers."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any, Optional

from telegram import Update, __version__ as PTB_VERSION
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import ContextTypes

from downloaders import MediaCandidate, runtime_info
from tgbot.flows import check_link, process_download
from tgbot.links import detect_platform, extract_first_url
from tgbot.preview import finish_preview
from tgbot.state import PENDING_TTL_SECONDS, BotState, get_state, is_allowed, try_start_job
from tgbot.telegram_io import safe_reply, tg_call
from tgbot.texts import (
    ACTIVE_JOB_TEXT,
    BROWSER_NOTE,
    CANCELLED_NOTE,
    CHOSEN_NOTE,
    EXPIRED_TEXT,
    GENERIC_APOLOGY,
    INVALID_LINK_TEXT,
    NOT_YOURS_TEXT,
    PING_TEXT,
    WELCOME_TEXT,
)
from tgbot.workers import make_runner

logger = logging.getLogger("video_bot.handlers")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start and /help."""
    message = update.effective_message
    if message is None or not is_allowed(update, get_state(context)):
        return
    await safe_reply(message, WELCOME_TEXT, parse_mode=ParseMode.HTML)


def format_uptime(seconds: float) -> str:
    """Return compact Indonesian uptime (e.g. '3j 12m' or '45d')."""
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}h {hours}j {minutes}m"
    if hours:
        return f"{hours}j {minutes}m"
    if minutes:
        return f"{minutes}m {secs}d"
    return f"{secs}d"


def build_health_text(state: BotState, info: dict[str, Any]) -> str:
    """Build the HTML /health status message from state and runtime_info()."""
    esc = lambda v: html.escape(str(v))
    uptime = format_uptime(time.monotonic() - state.started_at)
    max_slots = state.settings.max_concurrent_downloads
    sem_val = getattr(state.semaphore, "_value", None)
    in_use = max(0, max_slots - sem_val) if isinstance(sem_val, int) else "?"

    ffmpeg_val = info.get("ffmpeg_available")
    ffmpeg_str = "ya" if ffmpeg_val is True else ("tidak" if ffmpeg_val is False else "?")

    browser_ok = bool(info.get("browser_available"))
    if not state.settings.browser_fallback:
        browser_str = "nonaktif"
    elif browser_ok:
        browser_str = "aktif"
    else:
        browser_str = "tidak terpasang"

    confirm_str = "aktif" if state.settings.confirm_before_download else "nonaktif"
    pw_ver = info.get("playwright_version") if browser_ok else None
    pw_str = pw_ver or "tidak terpasang"

    temp_files = info.get("temp_files")
    temp_bytes = info.get("temp_bytes")
    if isinstance(temp_files, int) and isinstance(temp_bytes, int):
        temp_str = f"{temp_files} file ({temp_bytes / (1024 * 1024):.1f} MB)"
    else:
        temp_str = "?"

    disk_free = info.get("disk_free_bytes")
    disk_str = f"{disk_free / (1024 ** 3):.1f} GB" if isinstance(disk_free, int) else "?"

    return "\n".join([
        "🩺 <b>Status Bot</b>",
        f"⏱ Uptime: <code>{esc(uptime)}</code>",
        f"📊 Unduhan (sejak mulai): <b>{state.jobs_ok}</b> sukses / <b>{state.jobs_failed}</b> gagal",
        f"⚙️ Aktif: <b>{len(state.active_users)}</b> job · slot unduh: <b>{esc(in_use)}/{max_slots}</b>",
        f"🗂 Menunggu konfirmasi: <b>{len(state.pending)}</b> · cache info: <b>{len(state.probe_cache)}</b>",
        f"🔧 FFmpeg: <b>{esc(ffmpeg_str)}</b> · Browser: <b>{esc(browser_str)}</b> · Konfirmasi: <b>{esc(confirm_str)}</b>",
        f"📦 Versi: PTB <code>{esc(PTB_VERSION)}</code> · yt-dlp <code>{esc(info.get('yt_dlp_version') or '?')}</code> · Playwright <code>{esc(pw_str)}</code>",
        f"💾 Folder sementara: <code>{esc(temp_str)}</code> · sisa disk: <code>{esc(disk_str)}</code>",
    ])


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ping: instant liveness reply without blocking."""
    message = update.effective_message
    state = get_state(context)
    if message is None or not is_allowed(update, state):
        return
    uptime = format_uptime(time.monotonic() - state.started_at)
    await safe_reply(message, PING_TEXT.format(uptime=uptime))


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /health: report bot health without waiting on download/browser slots."""
    message = update.effective_message
    state = get_state(context)
    if message is None or not is_allowed(update, state):
        return
    info = await asyncio.to_thread(runtime_info)
    await safe_reply(message, build_health_text(state, info), parse_mode=ParseMode.HTML)


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
    """Handle the confirmation buttons: download video/audio, search with browser, cancel."""
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
    if action not in ("dl", "au", "br"):
        await _answer(query, "")
        return

    # Same check-and-set as for new links: no await between check and set.
    if user.id in state.active_users:
        await _answer(query, ACTIVE_JOB_TEXT, alert=True)
        return
    candidate: Optional[MediaCandidate] = None
    audio = action == "au"
    if action in ("dl", "au"):
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
            if audio:
                label = f"audio dari {label}"
            await finish_preview(pending, CHOSEN_NOTE.format(label=label))
            runner = make_runner(pending.platform, pending.url, candidate, pending.probe, audio=audio)
            await process_download(
                context, pending.message, user.id, pending.platform, pending.site, runner, state,
                audio=audio,
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
