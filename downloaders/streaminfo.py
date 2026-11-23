"""Inspect streams captured by the headless browser.

For every captured URL this module reads just enough to describe it to the
user (duration, resolution, size) and to spot advertisements:

* HLS (.m3u8): playlist text -> duration (sum of #EXTINF), resolution, DRM.
* DASH (.mpd): manifest -> mediaPresentationDuration, heights, DRM.
* MP4: the first/last few hundred KB -> "mvhd" (duration) and "tkhd" (size).

All requests run under the public-network guard. Blocking; run in a worker
thread. Never raises to the caller.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests

from .browser import BrowserCapture, CapturedStream
from .common import (
    HTTP_CONNECT_TIMEOUT,
    ErrorCode,
    MediaCandidate,
    ProbeResult,
    looks_like_ad_url,
    public_network_only,
)

logger = logging.getLogger(__name__)

ANALYSIS_DEADLINE_SECONDS: float = 30.0
ANALYSIS_READ_TIMEOUT: float = 10.0
MAX_TEXT_BYTES: int = 2 * 1024 * 1024  # Playlists/manifests larger than this are ignored.
MP4_HEAD_BYTES: int = 256 * 1024
MP4_TAIL_BYTES: int = 512 * 1024
MAX_CHOICES: int = 5

# A stream this short, next to one at least SHORT_AD_RATIO times longer, is probably an ad.
SHORT_AD_SECONDS: float = 60.0
SHORT_AD_RATIO: float = 3.0
MAIN_VIDEO_MIN_SECONDS: float = 90.0

HLS_CONTENT_TYPES: tuple[str, ...] = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl")
DASH_CONTENT_TYPE: str = "application/dash+xml"
HLS_DRM_PATTERN = re.compile(
    r"#EXT-X-(SESSION-)?KEY:[^\n]*(METHOD=SAMPLE-AES|KEYFORMAT=\"(com\.widevine|com\.microsoft\.playready|"
    r"com\.apple\.streamingkeydelivery))",
    re.IGNORECASE,
)
HLS_STREAM_INF = re.compile(r"#EXT-X-STREAM-INF:([^\n]*)\n\s*([^\s#][^\n]*)", re.IGNORECASE)
HLS_RESOLUTION = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.IGNORECASE)
HLS_BANDWIDTH = re.compile(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", re.IGNORECASE)
HLS_EXTINF = re.compile(r"#EXTINF:\s*([\d.]+)", re.IGNORECASE)
DASH_DURATION = re.compile(r'mediaPresentationDuration="P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?"')
DASH_HEIGHT = re.compile(r'\bheight="(\d+)"')
DASH_DYNAMIC = re.compile(r'<MPD[^>]*\btype="dynamic"', re.IGNORECASE)
DASH_DRM = re.compile(r"<ContentProtection", re.IGNORECASE)


@dataclass
class _StreamInfo:
    """What could be learned about one stream."""

    kind: str = ""
    duration: Optional[float] = None
    height: Optional[int] = None
    size: Optional[int] = None
    audio_only: bool = False
    drm: bool = False
    live: bool = False
    usable: bool = True


class _Deadline:
    """Stops the analysis when the time budget or the job is used up."""

    def __init__(self, seconds: float, cancel_event: Optional[threading.Event]) -> None:
        """Start the budget now."""
        self._end = time.monotonic() + seconds
        self._cancel = cancel_event

    def expired(self) -> bool:
        """Return True when analysis should stop."""
        cancelled = self._cancel is not None and self._cancel.is_set()
        return cancelled or time.monotonic() > self._end


def _session(capture: BrowserCapture) -> requests.Session:
    """Return a requests session that looks like the browser that found the streams."""
    session = requests.Session()
    if capture.user_agent:
        session.headers["User-Agent"] = capture.user_agent
    if capture.final_url:
        session.headers["Referer"] = capture.final_url
    for cookie in capture.cookies:
        try:
            session.cookies.set(
                str(cookie["name"]), str(cookie["value"]),
                domain=str(cookie.get("domain", "")), path=str(cookie.get("path") or "/"),
            )
        except (KeyError, TypeError):
            continue
    return session


def _get_text(session: requests.Session, url: str) -> Optional[str]:
    """Download a small text file (playlist/manifest), or None on failure."""
    try:
        with session.get(url, timeout=(HTTP_CONNECT_TIMEOUT, ANALYSIS_READ_TIMEOUT), stream=True) as response:
            if response.status_code != 200:
                return None
            body = response.raw.read(MAX_TEXT_BYTES + 1, decode_content=True)
    except (requests.RequestException, OSError):
        return None
    if len(body) > MAX_TEXT_BYTES:
        return None
    return body.decode("utf-8", errors="replace")


def _get_range(session: requests.Session, url: str, byte_range: str) -> tuple[bytes, Optional[int], str]:
    """Fetch a byte range. Returns (data, total size if known, content type)."""
    limit = MP4_TAIL_BYTES
    try:
        with session.get(
            url,
            headers={"Range": f"bytes={byte_range}"},
            timeout=(HTTP_CONNECT_TIMEOUT, ANALYSIS_READ_TIMEOUT),
            stream=True,
        ) as response:
            if response.status_code not in (200, 206):
                return b"", None, ""
            total: Optional[int] = None
            content_range = response.headers.get("Content-Range", "")
            if "/" in content_range and content_range.rsplit("/", 1)[1].isdigit():
                total = int(content_range.rsplit("/", 1)[1])
            elif response.status_code == 200 and response.headers.get("Content-Length", "").isdigit():
                total = int(response.headers["Content-Length"])
            data = response.raw.read(limit, decode_content=False)  # Stops even if Range was ignored.
            return data, total, response.headers.get("Content-Type", "").lower()
    except (requests.RequestException, OSError):
        return b"", None, ""


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "big")


def _parse_mp4(data: bytes) -> tuple[Optional[float], Optional[int], bool]:
    """Return (duration, height, has_movie_header) from raw MP4 bytes."""
    duration: Optional[float] = None
    index = data.find(b"mvhd")
    if index >= 4 and index + 36 <= len(data):
        if data[index + 4] == 1:
            timescale = _u32(data, index + 24)
            length = int.from_bytes(data[index + 28:index + 36], "big")
        else:
            timescale = _u32(data, index + 16)
            length = _u32(data, index + 20)
        if timescale:
            duration = length / timescale or None
    height: Optional[int] = None
    start = 0
    while True:
        index = data.find(b"tkhd", start)
        if index < 4:
            break
        box_end = index - 4 + _u32(data, index - 4)
        if box_end <= len(data) and box_end - index > 12:
            track_width = _u32(data, box_end - 8) >> 16
            track_height = _u32(data, box_end - 4) >> 16
            if 0 < track_height < 10000 and 0 < track_width < 10000:
                # Quality = shortest side (portrait videos count as their width).
                height = max(height or 0, min(track_width, track_height))
        start = index + 4
    return duration, height, b"mvhd" in data


def _inspect_hls(session: requests.Session, url: str, deadline: _Deadline) -> _StreamInfo:
    text = _get_text(session, url)
    if not text or "#EXTM3U" not in text:
        return _StreamInfo(kind="HLS", usable=False)
    info = _StreamInfo(kind="HLS", drm=bool(HLS_DRM_PATTERN.search(text)))
    variants = HLS_STREAM_INF.findall(text)
    media_text = text
    if variants:
        # Quality = shortest side, so a 720x1280 portrait video counts as 720p.
        heights = [
            min(int(m.group(1)), int(m.group(2))) for attrs, _ in variants if (m := HLS_RESOLUTION.search(attrs))
        ]
        info.height = max(heights) if heights else None
        codecs = [attrs.lower() for attrs, _ in variants if "codecs=" in attrs.lower()]
        video_markers = ("avc", "hvc", "hev", "vp0", "vp9", "av01")
        info.audio_only = (
            not heights and bool(codecs) and not any(m in c for c in codecs for m in video_markers)
        )

        def bandwidth(item: tuple[str, str]) -> int:
            match = HLS_BANDWIDTH.search(item[0])
            return int(match.group(1)) if match else 0

        _attrs, variant_url = max(variants, key=bandwidth)
        media_text = "" if deadline.expired() else (_get_text(session, urljoin(url, variant_url.strip())) or "")
        info.drm = info.drm or bool(HLS_DRM_PATTERN.search(media_text))
    durations = [float(value) for value in HLS_EXTINF.findall(media_text)]
    if durations:
        info.duration = sum(durations)
        info.live = "#EXT-X-ENDLIST" not in media_text and "#EXT-X-PLAYLIST-TYPE:VOD" not in media_text
    return info


def _inspect_dash(session: requests.Session, url: str) -> _StreamInfo:
    text = _get_text(session, url)
    if not text or "<MPD" not in text:
        return _StreamInfo(kind="DASH", usable=False)
    info = _StreamInfo(kind="DASH", drm=bool(DASH_DRM.search(text)), live=bool(DASH_DYNAMIC.search(text)))
    match = DASH_DURATION.search(text)
    if match:
        days, hours, minutes, seconds = (float(part) if part else 0.0 for part in match.groups())
        info.duration = days * 86400 + hours * 3600 + minutes * 60 + seconds or None
    heights = [int(h) for h in DASH_HEIGHT.findall(text)]
    info.height = max(heights) if heights else None
    return info


def _inspect_file(session: requests.Session, stream: CapturedStream, deadline: _Deadline) -> _StreamInfo:
    path = urlparse(stream.url).path.lower()
    kind = "WEBM" if path.endswith(".webm") or "webm" in stream.content_type else "MP4"
    head, total, content_type = _get_range(session, stream.url, f"0-{MP4_HEAD_BYTES - 1}")
    if not head or content_type.startswith(("text/", "image/", "application/json")):
        return _StreamInfo(kind=kind, usable=False)
    info = _StreamInfo(kind=kind, size=total)
    if kind == "MP4":
        duration, height, has_header = _parse_mp4(head)
        if not has_header and b"moof" in head[:4096]:
            return _StreamInfo(kind=kind, usable=False)  # A fragment, not a whole file.
        if not has_header and total and total > MP4_HEAD_BYTES and not deadline.expired():
            tail, _total, _ctype = _get_range(session, stream.url, f"-{MP4_TAIL_BYTES}")
            duration, height, _has = _parse_mp4(tail)
        info.duration, info.height = duration, height
    return info


def _inspect(session: requests.Session, stream: CapturedStream, deadline: _Deadline) -> _StreamInfo:
    """Inspect one captured stream according to its type."""
    path = urlparse(stream.url).path.lower()
    content_type = stream.content_type.lower()
    if path.endswith(".m3u8") or content_type.startswith(HLS_CONTENT_TYPES):
        info = _inspect_hls(session, stream.url, deadline)
    elif path.endswith(".mpd") or content_type.startswith(DASH_CONTENT_TYPE):
        info = _inspect_dash(session, stream.url)
    else:
        info = _inspect_file(session, stream, deadline)
    info.duration = info.duration or stream.duration_hint
    info.height = info.height or stream.height_hint
    return info


def _hls_variant_keys(session: requests.Session, streams: list[CapturedStream]) -> set[str]:
    """Return host+path keys of media playlists referenced by captured master playlists.

    A player loads the master playlist and then one of its variants; both
    show up in the capture but are the same video.
    """
    keys: set[str] = set()
    for stream in streams:
        if not urlparse(stream.url).path.lower().endswith(".m3u8") and not stream.content_type.lower().startswith(
            HLS_CONTENT_TYPES
        ):
            continue
        text = _get_text(session, stream.url) or ""
        for _attrs, variant in HLS_STREAM_INF.findall(text):
            parsed = urlparse(urljoin(stream.url, variant.strip()))
            keys.add(f"{parsed.netloc}{parsed.path}")
    return keys


def analyze_capture(capture: BrowserCapture, cancel_event: Optional[threading.Event] = None) -> ProbeResult:
    """Turn captured streams into ranked candidates for the user to choose from.

    Args:
        capture: Result of :func:`downloaders.browser.capture_page`.
        cancel_event: Set by the caller to abort.

    Returns:
        A ProbeResult (``via_browser=True``); this function never raises.
    """
    base = {
        "via_browser": True,
        "screenshot": capture.screenshot,
        "page_title": capture.title,
        "page_url": capture.final_url,
        "user_agent": capture.user_agent,
        "cookies": tuple(capture.cookies),
    }
    if capture.error is not ErrorCode.OK:
        return ProbeResult(capture.error, detail=capture.detail, **base)
    try:
        with public_network_only(), _session(capture) as session:
            candidates, skipped = _analyze(session, capture, _Deadline(ANALYSIS_DEADLINE_SECONDS, cancel_event))
    except Exception as exc:  # Defensive: the caller must never see an exception.
        logger.warning("Stream analysis crashed", exc_info=True)
        return ProbeResult(ErrorCode.UNKNOWN, detail=f"analysis: {type(exc).__name__}", **base)
    if not candidates:
        if skipped.get("drm"):
            return ProbeResult(ErrorCode.UNSUPPORTED, detail="only DRM-protected streams", **base)
        if skipped.get("live"):
            return ProbeResult(ErrorCode.LIVE, detail="only live streams", **base)
        return ProbeResult(ErrorCode.NOT_FOUND, detail="no usable stream", **base)
    return ProbeResult(ErrorCode.OK, tuple(candidates), detail=f"browser: {len(candidates)} candidate(s)", **base)


def _analyze(
    session: requests.Session, capture: BrowserCapture, deadline: _Deadline
) -> tuple[list[MediaCandidate], dict[str, int]]:
    """Inspect streams, drop unusable ones, flag ads and rank the rest."""
    variant_keys = _hls_variant_keys(session, capture.streams)
    skipped: dict[str, int] = {}
    found: list[tuple[CapturedStream, _StreamInfo]] = []
    for stream in capture.streams:
        if deadline.expired():
            break
        parsed = urlparse(stream.url)
        if f"{parsed.netloc}{parsed.path}" in variant_keys:
            continue  # Variant of a master playlist that is already listed.
        info = _inspect(session, stream, deadline)
        reason = "drm" if info.drm else "live" if info.live else "" if info.usable else "unusable"
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        found.append((stream, info))

    longest = max((info.duration or 0.0 for _s, info in found), default=0.0)
    candidates: list[MediaCandidate] = []
    for stream, info in found:
        short_next_to_long = (
            info.duration is not None
            and info.duration <= SHORT_AD_SECONDS
            and longest >= MAIN_VIDEO_MIN_SECONDS
            and longest >= SHORT_AD_RATIO * info.duration
        )
        candidates.append(
            MediaCandidate(
                url=stream.url,
                title=capture.title,
                duration=info.duration,
                height=info.height,
                size_bytes=info.size,
                audio_only=info.audio_only,
                likely_ad=looks_like_ad_url(stream.url, stream.frame_url) or short_next_to_long,
                stream_host=(urlparse(stream.url).hostname or "").removeprefix("www."),
                stream_kind=info.kind,
                via_browser=True,
            )
        )
    candidates.sort(key=_rank)
    return candidates[:MAX_CHOICES], skipped


def _rank(candidate: MediaCandidate) -> tuple[Any, ...]:
    """Sort key: real videos first, then longest, then highest resolution."""
    return (candidate.likely_ad, candidate.audio_only, -(candidate.duration or 0.0), -(candidate.height or 0))
