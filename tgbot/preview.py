"""Preview formatting, confirmation UI, and auto-pick logic."""

from __future__ import annotations

import html
import logging
from typing import Any, Optional
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, ReplyParameters
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError

from downloaders import MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_MB, MediaCandidate, ProbeResult, browser_available
from tgbot.state import PENDING_MAX_ENTRIES, BotState, PendingChoice, purge_expired
from tgbot.telegram_io import tg_call
from tgbot.texts import AUDIO_BUTTON_TEXT, CHOOSE_QUESTION, CONFIRM_QUESTION

logger = logging.getLogger("video_bot.preview")

MAX_TITLE_CHARS = 200
CAPTION_LIMIT = 1024  # Telegram limit for photo captions.
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


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
            row = [InlineKeyboardButton(label, callback_data=f"dl:TOKEN:{index - 1}")]
            if not candidate.audio_only:
                row.append(InlineKeyboardButton(f"🎵 Audio {index}", callback_data=f"au:TOKEN:{index - 1}"))
            rows.append(row)
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
        # Audio-only sources need no extra button; TikTok without info would rely
        # on the video-only scraper, so audio is not offered there either.
        if not candidate.audio_only and not candidate.info_unavailable:
            rows.append([InlineKeyboardButton(AUDIO_BUTTON_TEXT, callback_data="au:TOKEN:0")])
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
    while len(state.pending) > PENDING_MAX_ENTRIES:
        state.pending.pop(next(iter(state.pending)))


async def finish_preview(pending: PendingChoice, note: str) -> bool:
    """Remove the buttons from a preview and append a short note.

    Returns False when the user blocked the bot (Forbidden), True otherwise.
    """
    preview = pending.preview
    if preview is None:
        return True
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
    except Forbidden:
        logger.info("User %s blocked the bot; skipping preview edit", pending.user_id)
        return False
    except BadRequest as exc:
        logger.debug("Ignoring BadRequest on preview edit: %s", exc)
    except TelegramError as exc:
        logger.warning("Ignoring %s on preview edit: %s", type(exc).__name__, exc)
    return True


def auto_pick(probe: ProbeResult) -> Optional[MediaCandidate]:
    """Choose a candidate without asking (confirmation turned off)."""
    if not probe.candidates:
        return None
    best = probe.candidates[0]  # Already ranked: real videos first, then longest.
    if best.likely_ad:
        return None
    return best
