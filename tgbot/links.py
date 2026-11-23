"""URL extraction and platform detection."""

from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urlparse

from telegram import Message, MessageEntity

from downloaders import describe_site, is_blocked_host

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

URL_REGEX = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
URL_TRAILING_CHARS = ".,;:!?)]}>\"'»”’"
SCHEME_REGEX = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


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


async def site_name(platform: str, url: str) -> str:
    """Return a readable site name ("TikTok", "Dailymotion", "example.com")."""
    if platform == GENERIC_PLATFORM:
        # Extractor lookup is pure CPU (regex matching), but the first call compiles
        # ~1,600 patterns, so it runs off the event loop.
        return await asyncio.to_thread(describe_site, url)
    return PLATFORM_NAMES[platform]
