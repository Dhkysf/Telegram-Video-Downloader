"""In-memory bot state, abuse protection, pending choices, and probe cache."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from telegram import Message, Update
from telegram.ext import ContextTypes

from downloaders import ErrorCode, ProbeResult
from tgbot.settings import Settings
from tgbot.texts import ACTIVE_JOB_TEXT, COOLDOWN_TEXT

ProbeCacheKey = tuple[str, str, str]

PENDING_TTL_SECONDS = 15 * 60  # How long confirmation buttons stay valid.
PENDING_MAX_ENTRIES = 100
PROBE_CACHE_TTL_SECONDS = 300
PROBE_CACHE_MAX_ENTRIES = 50


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
    probe_cache: dict[ProbeCacheKey, tuple[float, ProbeResult]] = field(default_factory=dict)
    in_flight_probes: dict[ProbeCacheKey, asyncio.Future[ProbeResult]] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    jobs_ok: int = 0
    jobs_failed: int = 0


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


def purge_expired(state: BotState) -> None:
    """Forget expired confirmation buttons and stale cooldown timestamps."""
    now = time.monotonic()
    for token in [t for t, p in state.pending.items() if now - p.created > PENDING_TTL_SECONDS]:
        state.pending.pop(token, None)
    cooldown = state.settings.user_cooldown_seconds
    for uid in [u for u, ts in state.last_request.items() if now - ts >= cooldown]:
        state.last_request.pop(uid, None)


def probe_cache_key(url: str, platform: str, force_browser: bool) -> ProbeCacheKey:
    """Return the normalized cache key (url, platform, mode) for a probe."""
    parsed = urlsplit(url)
    userinfo, sep, hostport = parsed.netloc.rpartition("@")
    netloc = f"{userinfo}{sep}{hostport.lower()}" if sep else hostport.lower()
    normalized = urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))
    return (normalized, platform, "browser" if force_browser else "auto")


def _cache_get(state: BotState, key: ProbeCacheKey) -> Optional[ProbeResult]:
    """Return a cached ProbeResult if present and within TTL, else None."""
    entry = state.probe_cache.get(key)
    if entry is None:
        return None
    created, probe = entry
    if time.monotonic() - created >= PROBE_CACHE_TTL_SECONDS:
        state.probe_cache.pop(key, None)
        return None
    return probe


def _cache_put(state: BotState, key: ProbeCacheKey, probe: ProbeResult) -> None:
    """Cache a successful ProbeResult, purging expired and oldest entries."""
    if (
        probe.error is not ErrorCode.OK
        or not probe.candidates
        or any(c.info_unavailable for c in probe.candidates)
    ):
        return
    now = time.monotonic()
    for k in [k for k, (created, _) in state.probe_cache.items() if now - created >= PROBE_CACHE_TTL_SECONDS]:
        state.probe_cache.pop(k, None)
    state.probe_cache.pop(key, None)
    state.probe_cache[key] = (now, probe)
    while len(state.probe_cache) > PROBE_CACHE_MAX_ENTRIES:
        state.probe_cache.pop(next(iter(state.probe_cache)))
