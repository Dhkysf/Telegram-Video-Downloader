#!/usr/bin/env python3
"""Offline smoke tests for bot flows (stdlib + python-telegram-bot only)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from telegram import Chat, Message, MessageEntity, Update, User
from telegram.constants import ParseMode
from telegram.error import Forbidden, NetworkError

from downloaders import DownloadResult, ErrorCode, MediaCandidate, ProbeResult, TEMP_DIR
from downloaders.ytdlp import build_audio_format_selector
import tgbot.flows
import tgbot.handlers
import tgbot.links
import tgbot.preview
import tgbot.settings
import tgbot.state
import tgbot.telegram_io
import tgbot.texts
import tgbot.workers


class _HTMLValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            raise AssertionError(f"Mismatched HTML tag </{tag}> (stack={self.stack})")
        self.stack.pop()


def assert_valid_html(text: str) -> None:
    parser = _HTMLValidator()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise AssertionError(f"Unclosed HTML tags: {parser.stack}")


class FakeSentMessage:
    def __init__(self, text: str = "", caption: str = "", photo: object = None, **kwargs: object) -> None:
        self.text = text
        self.caption = caption
        self.photo = photo
        self.kwargs = kwargs
        self.reply_markup = kwargs.get("reply_markup")
        self.parse_mode = kwargs.get("parse_mode")
        self.edits: list[tuple[str, str, dict[str, object]]] = []
        self.deleted = False

    async def edit_text(self, text: str, **kwargs: object) -> "FakeSentMessage":
        self.text = text
        self.reply_markup = kwargs.get("reply_markup", self.reply_markup)
        self.edits.append(("text", text, kwargs))
        return self

    async def edit_caption(self, caption: str = "", **kwargs: object) -> "FakeSentMessage":
        self.caption = caption
        self.reply_markup = kwargs.get("reply_markup", self.reply_markup)
        self.edits.append(("caption", caption, kwargs))
        return self

    async def delete(self) -> None:
        self.deleted = True


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []
        self.markup_edits: list[object] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_reply_markup(self, reply_markup: object = None) -> None:
        self.markup_edits.append(reply_markup)


class FakeBot:
    def __init__(self) -> None:
        self.videos: list[dict[str, object]] = []
        self.audios: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []

    async def send_video(self, video: object, **kwargs: object) -> FakeSentMessage:
        data = video.read()  # type: ignore[attr-defined]
        self.videos.append({"bytes": data, **kwargs})
        return FakeSentMessage(caption=str(kwargs.get("caption", "")))

    async def send_audio(self, audio: object, **kwargs: object) -> FakeSentMessage:
        data = audio.read()  # type: ignore[attr-defined]
        self.audios.append({"bytes": data, **kwargs})
        return FakeSentMessage(caption=str(kwargs.get("caption", "")))

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> FakeSentMessage:
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return FakeSentMessage(text=text, **kwargs)


def make_message(
    text: str,
    user_id: int = 100,
    chat_id: int = 100,
    message_id: int = 1,
    entities: tuple[MessageEntity, ...] = (),
) -> Message:
    user = User(user_id, f"user{user_id}", False)
    chat = Chat(chat_id, "private")
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
        entities=entities,
    )


def make_state(**overrides: object) -> tgbot.state.BotState:
    settings_kwargs: dict[str, object] = {
        "max_concurrent_downloads": 2,
        "job_timeout": 30.0,
        "user_cooldown_seconds": 10.0,
        "allowed_user_ids": None,
        "confirm_before_download": True,
        "browser_fallback": True,
    }
    for key in list(settings_kwargs):
        if key in overrides:
            settings_kwargs[key] = overrides.pop(key)
    settings = tgbot.settings.Settings(**settings_kwargs)  # type: ignore[arg-type]
    state = tgbot.state.BotState(
        settings=settings,
        semaphore=asyncio.Semaphore(settings.max_concurrent_downloads),
    )
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


def make_context(state: tgbot.state.BotState, fake_bot: FakeBot | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={"state": state}),
        bot=fake_bot or FakeBot(),
    )


def extract_callbacks(markup: object) -> list[str]:
    return [
        str(btn.callback_data)
        for row in markup.inline_keyboard  # type: ignore[attr-defined]
        for btn in row
    ]


class SmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self.sent: list[FakeSentMessage] = []

        async def fake_reply_text(_msg: Message, text: str, **kwargs: object) -> FakeSentMessage:
            sent = FakeSentMessage(text=text, **kwargs)
            self.sent.append(sent)
            return sent

        async def fake_reply_photo(_msg: Message, photo: object, caption: str = "", **kwargs: object) -> FakeSentMessage:
            sent = FakeSentMessage(caption=caption, photo=photo, **kwargs)
            self.sent.append(sent)
            return sent

        self._patches = [
            patch.object(Message, "reply_text", fake_reply_text),
            patch.object(Message, "reply_photo", fake_reply_photo),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        leftovers = list(TEMP_DIR.iterdir()) if TEMP_DIR.exists() else []
        self.assertEqual(leftovers, [], f"TEMP_DIR not empty: {leftovers}")

    def test_link_extraction_and_platform_detection(self) -> None:
        msg1 = make_message("lihat video ini https://www.youtube.com/watch?v=abc123 ya")
        self.assertEqual(tgbot.links.extract_first_url(msg1), "https://www.youtube.com/watch?v=abc123")

        ent_url = MessageEntity(MessageEntity.URL, 6, 10)
        msg2 = make_message("check youtu.be/x now", entities=(ent_url,))
        self.assertEqual(tgbot.links.extract_first_url(msg2), "https://youtu.be/x")

        ent_link = MessageEntity(MessageEntity.TEXT_LINK, 0, 4, url="https://tiktok.com/@a/video/1")
        msg3 = make_message("klik di sini", entities=(ent_link,))
        self.assertEqual(tgbot.links.extract_first_url(msg3), "https://tiktok.com/@a/video/1")

        self.assertEqual(tgbot.links.detect_platform("https://x.com@evil.com/v"), "generic")
        self.assertEqual(tgbot.links.detect_platform("https://www.tiktok.com/@a/video/1"), "tiktok")
        self.assertEqual(tgbot.links.detect_platform("https://x.com/i/status/1"), "twitter")
        self.assertEqual(tgbot.links.detect_platform("https://www.instagram.com/reel/1/"), "instagram")
        self.assertEqual(tgbot.links.detect_platform("https://youtu.be/abc"), "youtube")
        self.assertEqual(tgbot.links.detect_platform("https://example.com/video.mp4"), "generic")

        for local_url in (
            "http://localhost/video.mp4",
            "http://127.0.0.1:8080/v",
            "http://192.168.1.10/v",
            "http://[::1]/v",
            "http://169.254.169.254/latest",
            "http://nas.local/v",
            "http://router.lan/v",
        ):
            self.assertIsNone(tgbot.links.detect_platform(local_url), f"Expected None for {local_url}")

        probe = ProbeResult(
            ErrorCode.OK,
            candidates=[MediaCandidate(url="https://youtube.com/watch?v=1", title="A <script> & B", duration=65, height=720)],
        )
        html_text, _markup, kind = tgbot.preview.build_preview("YouTube", probe, allow_browser=False)
        self.assertEqual(kind, "text")
        self.assertIn("A &lt;script&gt; &amp; B", html_text)
        assert_valid_html(html_text)

    async def test_cooldown_and_active_job(self) -> None:
        state = make_state(user_cooldown_seconds=30.0)
        ctx = make_context(state)
        probe_calls = 0

        def fake_probe(*_args: object) -> ProbeResult:
            nonlocal probe_calls
            probe_calls += 1
            return ProbeResult(ErrorCode.NOT_FOUND)

        with patch.object(tgbot.flows, "probe_ytdlp", fake_probe):
            state.active_users.add(100)
            msg = make_message("https://youtube.com/watch?v=1", user_id=100)
            await tgbot.handlers.handle_text(Update(1, message=msg), ctx)
            self.assertEqual(self.sent[-1].text, tgbot.texts.ACTIVE_JOB_TEXT)
            self.assertEqual(probe_calls, 0)
            state.active_users.discard(100)

            await tgbot.handlers.handle_text(Update(2, message=msg), ctx)
            self.assertEqual(probe_calls, 1)
            await tgbot.handlers.handle_text(Update(3, message=msg), ctx)
            self.assertEqual(probe_calls, 1)
            self.assertIn("Pelan-pelan ya", self.sent[-1].text)
        self.assertEqual(state.active_users, set())

    async def test_confirm_then_download(self) -> None:
        state = make_state()
        fake_bot = FakeBot()
        ctx = make_context(state, fake_bot)
        probe = ProbeResult(
            ErrorCode.OK,
            candidates=[MediaCandidate(url="https://youtube.com/watch?v=abc", title="Video <1> & Test", duration=12, height=720)],
        )

        def fake_yt(url: str, job_id: str, _cancel: object) -> DownloadResult:
            out = TEMP_DIR / f"{job_id}.mp4"
            out.write_bytes(b"MP4DATA")
            return DownloadResult(out, ErrorCode.OK)

        with (
            patch.object(tgbot.flows, "probe_ytdlp", return_value=probe),
            patch.dict(tgbot.workers.DOWNLOADERS, {"youtube": fake_yt}),
        ):
            msg = make_message("https://youtube.com/watch?v=abc", user_id=100)
            await tgbot.handlers.handle_text(Update(1, message=msg), ctx)
            self.assertEqual(len(state.pending), 1)
            token = next(iter(state.pending))
            callbacks = extract_callbacks(self.sent[-1].reply_markup)
            self.assertIn(f"dl:{token}:0", callbacks)
            self.assertIn(f"cx:{token}", callbacks)

            query = FakeQuery(f"dl:{token}:0")
            choice_update = SimpleNamespace(callback_query=query, effective_user=User(100, "u", False))
            await tgbot.handlers.handle_choice(choice_update, ctx)

        self.assertEqual(len(fake_bot.videos), 1)
        self.assertEqual(fake_bot.videos[0]["bytes"], b"MP4DATA")
        self.assertEqual(state.jobs_ok, 1)
        self.assertEqual(state.pending, {})
        self.assertEqual(state.active_users, set())

    async def test_browser_choice_flow(self) -> None:
        state = make_state()
        fake_bot = FakeBot()
        ctx = make_context(state, fake_bot)
        yt_probe = ProbeResult(
            ErrorCode.OK,
            candidates=[MediaCandidate(url="https://example.com/watch", title="Generic", duration=10)],
            can_try_browser=True,
        )
        br_probe = ProbeResult(
            ErrorCode.OK,
            candidates=[
                MediaCandidate(url="https://cdn.example.com/1.m3u8", duration=15, height=480, stream_kind="HLS", via_browser=True),
                MediaCandidate(url="https://cdn.example.com/2.mp4", duration=90, height=720, stream_kind="MP4", via_browser=True),
            ],
            via_browser=True,
            screenshot=b"PNGDATA",
            page_title="Demo Page",
        )

        def fake_br_dl(_cand: object, _probe: object, job_id: str, _cancel: object, **_kw: object) -> DownloadResult:
            out = TEMP_DIR / f"{job_id}.mp4"
            out.write_bytes(b"BROWSERMP4")
            return DownloadResult(out, ErrorCode.OK)

        with (
            patch.object(tgbot.flows, "probe_ytdlp", return_value=yt_probe),
            patch.object(tgbot.flows, "probe_with_browser", return_value=br_probe),
            patch.object(tgbot.workers, "download_browser_stream", fake_br_dl),
            patch.object(tgbot.preview, "browser_available", return_value=True),
        ):
            msg = make_message("https://example.com/watch", user_id=100)
            await tgbot.handlers.handle_text(Update(1, message=msg), ctx)
            token1 = next(iter(state.pending))
            self.assertIn(f"br:{token1}", extract_callbacks(self.sent[-1].reply_markup))

            q_br = FakeQuery(f"br:{token1}")
            await tgbot.handlers.handle_choice(SimpleNamespace(callback_query=q_br, effective_user=User(100, "u", False)), ctx)
            self.assertEqual(len(state.pending), 1)
            token2 = next(iter(state.pending))
            self.assertEqual(self.sent[-1].photo, b"PNGDATA")
            self.assertIn(f"dl:{token2}:1", extract_callbacks(self.sent[-1].reply_markup))

            q_dl = FakeQuery(f"dl:{token2}:1")
            await tgbot.handlers.handle_choice(SimpleNamespace(callback_query=q_dl, effective_user=User(100, "u", False)), ctx)

        self.assertEqual(len(fake_bot.videos), 1)
        self.assertEqual(fake_bot.videos[0]["bytes"], b"BROWSERMP4")
        self.assertEqual(state.jobs_ok, 1)
        self.assertEqual(state.active_users, set())

    async def test_cancel_other_user_and_expired_token(self) -> None:
        state = make_state()
        ctx = make_context(state)
        probe = ProbeResult(ErrorCode.OK, candidates=[MediaCandidate(url="https://youtube.com/watch?v=1", title="Vid", duration=5)])

        with patch.object(tgbot.flows, "probe_ytdlp", return_value=probe):
            await tgbot.handlers.handle_text(Update(1, message=make_message("https://youtube.com/watch?v=1")), ctx)
            token = next(iter(state.pending))

            q_other = FakeQuery(f"dl:{token}:0")
            await tgbot.handlers.handle_choice(SimpleNamespace(callback_query=q_other, effective_user=User(200, "other", False)), ctx)
            self.assertEqual(q_other.answers, [(tgbot.texts.NOT_YOURS_TEXT, True)])
            self.assertIn(token, state.pending)

            q_cancel = FakeQuery(f"cx:{token}")
            await tgbot.handlers.handle_choice(SimpleNamespace(callback_query=q_cancel, effective_user=User(100, "owner", False)), ctx)
            self.assertEqual(q_cancel.answers, [("Dibatalkan", False)])
            self.assertNotIn(token, state.pending)
            self.assertIn(tgbot.texts.CANCELLED_NOTE, self.sent[-1].text)

            state.last_request.clear()
            await tgbot.handlers.handle_text(Update(2, message=make_message("https://youtube.com/watch?v=2")), ctx)
            token2 = next(iter(state.pending))
            state.pending[token2].created = time.monotonic() - 1000
            q_exp = FakeQuery(f"dl:{token2}:0")
            await tgbot.handlers.handle_choice(SimpleNamespace(callback_query=q_exp, effective_user=User(100, "owner", False)), ctx)
            self.assertEqual(q_exp.answers, [(tgbot.texts.EXPIRED_TEXT, True)])
            self.assertEqual(q_exp.markup_edits, [None])
            self.assertNotIn(token2, state.pending)
        self.assertEqual(state.active_users, set())

    async def test_probe_cache_hit_miss(self) -> None:
        state = make_state(user_cooldown_seconds=0.0)
        ctx = make_context(state)
        calls = 0

        def fake_probe(*_args: object) -> ProbeResult:
            nonlocal calls
            calls += 1
            return ProbeResult(ErrorCode.OK, candidates=[MediaCandidate(url="https://youtube.com/watch?v=abc", title="Cached", duration=10)])

        with patch.object(tgbot.flows, "probe_ytdlp", fake_probe):
            await tgbot.handlers.handle_text(Update(1, message=make_message("https://youtube.com/watch?v=abc")), ctx)
            await tgbot.handlers.handle_text(Update(2, message=make_message("https://YOUTUBE.COM/watch?v=abc#frag")), ctx)
            self.assertEqual(calls, 1)

            key = tgbot.state.probe_cache_key("https://youtube.com/watch?v=abc", "youtube", False)
            created, cached_probe = state.probe_cache[key]
            state.probe_cache[key] = (created - 400, cached_probe)
            await tgbot.handlers.handle_text(Update(3, message=make_message("https://youtube.com/watch?v=abc")), ctx)
            self.assertEqual(calls, 2)

        with patch.object(tgbot.flows, "probe_ytdlp", return_value=ProbeResult(ErrorCode.NOT_FOUND)):
            state.probe_cache.clear()
            await tgbot.handlers.handle_text(Update(4, message=make_message("https://youtube.com/watch?v=missing")), ctx)
            self.assertEqual(state.probe_cache, {})
        self.assertEqual(state.active_users, set())

    async def test_ping_health_and_allow_list(self) -> None:
        state = make_state(max_concurrent_downloads=1, allowed_user_ids=frozenset({100}), jobs_ok=2, jobs_failed=1)
        ctx = make_context(state)

        # Hold the single download slot to verify /ping and /health never block on it.
        await state.semaphore.acquire()
        try:
            await tgbot.handlers.ping_command(Update(1, message=make_message("/ping", user_id=999)), ctx)
            await tgbot.handlers.start_command(Update(2, message=make_message("/start", user_id=999)), ctx)
            await tgbot.handlers.handle_text(Update(3, message=make_message("https://youtube.com/watch?v=1", user_id=999)), ctx)
            await tgbot.handlers.health_command(Update(4, message=make_message("/health", user_id=999)), ctx)
            self.assertEqual(self.sent, [])

            await tgbot.handlers.ping_command(Update(5, message=make_message("/ping", user_id=100)), ctx)
            self.assertIn("Pong!", self.sent[-1].text)

            fake_info = {
                "yt_dlp_version": "2026.10.26",
                "browser_available": False,
                "playwright_version": None,
                "ffmpeg_available": True,
                "temp_files": 0,
                "temp_bytes": 0,
                "disk_free_bytes": 10 * 1024**3,
            }
            with patch.object(tgbot.handlers, "runtime_info", return_value=fake_info):
                await asyncio.wait_for(
                    tgbot.handlers.health_command(Update(6, message=make_message("/health", user_id=100)), ctx),
                    timeout=1.0,
                )
            health_html = self.sent[-1].text
            self.assertEqual(self.sent[-1].parse_mode, ParseMode.HTML)
            self.assertIn("Status Bot", health_html)
            self.assertIn("<b>2</b> sukses / <b>1</b> gagal", health_html)
            assert_valid_html(health_html)

            now = time.monotonic()
            state.pending["live"] = tgbot.state.PendingChoice(100, make_message("https://youtube.com/watch?v=1"), "https://youtube.com/watch?v=1", "youtube", "YouTube", ProbeResult(ErrorCode.OK), now)
            state.pending["expired"] = tgbot.state.PendingChoice(100, make_message("https://youtube.com/watch?v=2"), "https://youtube.com/watch?v=2", "youtube", "YouTube", ProbeResult(ErrorCode.OK), now - tgbot.state.PENDING_TTL_SECONDS - 10)
            state.probe_cache[("https://youtube.com/watch?v=1", "youtube", "auto")] = (now, ProbeResult(ErrorCode.OK))
            state.probe_cache[("https://youtube.com/watch?v=2", "youtube", "auto")] = (now - tgbot.state.PROBE_CACHE_TTL_SECONDS - 10, ProbeResult(ErrorCode.OK))
            health_counts = tgbot.handlers.build_health_text(state, fake_info)
            self.assertIn("Menunggu konfirmasi: <b>1</b> · cache info: <b>1</b>", health_counts)
            self.assertEqual(len(state.pending), 2)
            self.assertEqual(len(state.probe_cache), 2)
        finally:
            state.semaphore.release()

    async def test_audio_button_and_selector(self) -> None:
        state = make_state()
        fake_bot = FakeBot()
        ctx = make_context(state, fake_bot)
        probe = ProbeResult(
            ErrorCode.OK,
            candidates=[MediaCandidate(url="https://youtube.com/watch?v=aud", title="Song", duration=60, height=720)],
        )

        def fake_dl_audio(_url: str, job_id: str, _platform: str, _cancel: object) -> DownloadResult:
            out = TEMP_DIR / f"{job_id}.m4a"
            out.write_bytes(b"M4ADATA")
            return DownloadResult(out, ErrorCode.OK, audio_only=True)

        with (
            patch.object(tgbot.flows, "probe_ytdlp", return_value=probe),
            patch.object(tgbot.workers, "download_audio", fake_dl_audio),
        ):
            await tgbot.handlers.handle_text(Update(1, message=make_message("https://youtube.com/watch?v=aud", user_id=100)), ctx)
            token = next(iter(state.pending))
            callbacks = extract_callbacks(self.sent[-1].reply_markup)
            self.assertIn(f"au:{token}:0", callbacks)

            q_au = FakeQuery(f"au:{token}:0")
            await tgbot.handlers.handle_choice(SimpleNamespace(callback_query=q_au, effective_user=User(100, "u", False)), ctx)

        self.assertEqual(len(fake_bot.audios), 1)
        self.assertEqual(fake_bot.audios[0]["bytes"], b"M4ADATA")
        self.assertEqual(fake_bot.videos, [])
        self.assertEqual(state.jobs_ok, 1)
        self.assertEqual(state.active_users, set())

        audio_only_probe = ProbeResult(ErrorCode.OK, candidates=[MediaCandidate(url="https://soundcloud.com/a/b", title="Audio", audio_only=True)])
        _, markup_ao, _ = tgbot.preview.build_preview("SoundCloud", audio_only_probe, allow_browser=False)
        self.assertFalse(any(cb.startswith("au:") for cb in extract_callbacks(markup_ao)))

        unavail_probe = ProbeResult(ErrorCode.OK, candidates=[MediaCandidate(url="https://tiktok.com/@a/video/1", info_unavailable=True)])
        _, markup_un, _ = tgbot.preview.build_preview("TikTok", unavail_probe, allow_browser=False)
        self.assertFalse(any(cb.startswith("au:") for cb in extract_callbacks(markup_un)))

        for can_extract in (True, False):
            for last_resort in (True, False):
                self.assertTrue(build_audio_format_selector(can_extract, last_resort))

    async def test_single_flight_concurrent_probes(self) -> None:
        state = make_state()
        ctx = make_context(state)
        calls = 0

        def fake_probe(*_args: object) -> ProbeResult:
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return ProbeResult(ErrorCode.OK, candidates=[MediaCandidate(url="https://youtube.com/watch?v=sf1", title="SF", duration=10)])

        with patch.object(tgbot.flows, "probe_ytdlp", fake_probe):
            await asyncio.gather(
                tgbot.handlers.handle_text(Update(1, message=make_message("https://youtube.com/watch?v=sf1", user_id=100)), ctx),
                tgbot.handlers.handle_text(Update(2, message=make_message("https://youtube.com/watch?v=sf1", user_id=200)), ctx),
            )

        self.assertEqual(calls, 1)
        self.assertEqual(len(state.pending), 2)
        self.assertEqual({p.user_id for p in state.pending.values()}, {100, 200})
        self.assertEqual(state.in_flight_probes, {})
        self.assertEqual(state.active_users, set())

    async def test_single_flight_first_status_reply_fails(self) -> None:
        state = make_state()
        ctx = make_context(state)
        calls = 0

        def fake_probe(*_args: object) -> ProbeResult:
            nonlocal calls
            calls += 1
            return ProbeResult(ErrorCode.OK, candidates=[MediaCandidate(url="https://youtube.com/watch?v=sf2", title="SF2", duration=10)])

        async def flaky_reply_text(msg: Message, text: str, **kwargs: object) -> FakeSentMessage:
            if msg.from_user and msg.from_user.id == 100:
                raise NetworkError("boom")
            sent = FakeSentMessage(text=text, **kwargs)
            self.sent.append(sent)
            return sent

        with (
            patch.object(tgbot.telegram_io, "backoff_delay", return_value=0.0),
            patch.object(Message, "reply_text", flaky_reply_text),
            patch.object(tgbot.flows, "probe_ytdlp", fake_probe),
        ):
            await asyncio.gather(
                tgbot.handlers.handle_text(Update(1, message=make_message("https://youtube.com/watch?v=sf2", user_id=100)), ctx),
                tgbot.handlers.handle_text(Update(2, message=make_message("https://youtube.com/watch?v=sf2", user_id=200)), ctx),
            )

        self.assertEqual(calls, 1)
        self.assertEqual(len(state.pending), 1)
        self.assertEqual(next(iter(state.pending.values())).user_id, 200)
        self.assertEqual(state.in_flight_probes, {})
        self.assertEqual(state.active_users, set())

    async def test_tiktok_info_unavailable_not_cached(self) -> None:
        state = make_state(user_cooldown_seconds=0.0)
        ctx = make_context(state)
        calls = 0

        def fake_probe(*_args: object) -> ProbeResult:
            nonlocal calls
            calls += 1
            return ProbeResult(ErrorCode.OK, candidates=[MediaCandidate(url="https://www.tiktok.com/@a/video/1", info_unavailable=True)])

        with patch.object(tgbot.flows, "probe_ytdlp", fake_probe):
            await tgbot.handlers.handle_text(Update(1, message=make_message("https://www.tiktok.com/@a/video/1")), ctx)
            await tgbot.handlers.handle_text(Update(2, message=make_message("https://www.tiktok.com/@a/video/1")), ctx)

        self.assertEqual(calls, 2)
        self.assertEqual(state.probe_cache, {})
        self.assertEqual(state.active_users, set())

    async def test_forbidden_upload_and_status_delete(self) -> None:
        def ok_runner(_job_id: str, _cancel: object) -> DownloadResult:
            out = TEMP_DIR / f"{_job_id}.mp4"
            out.write_bytes(b"MP4")
            return DownloadResult(out, ErrorCode.OK)

        class ForbiddenBot(FakeBot):
            async def send_video(self, video: object, **kwargs: object) -> FakeSentMessage:
                raise Forbidden("bot was blocked")

        state1 = make_state()
        ctx1 = make_context(state1, ForbiddenBot())
        await tgbot.flows.process_download(ctx1, make_message("https://youtube.com/watch?v=1"), 100, "youtube", "YouTube", ok_runner, state1)
        self.assertEqual(state1.jobs_failed, 1)
        self.assertEqual(state1.jobs_ok, 0)

        class ForbiddenDeleteStatus(FakeSentMessage):
            async def delete(self) -> None:
                raise Forbidden("bot was blocked")

        state2 = make_state()
        bot2 = FakeBot()
        ctx2 = make_context(state2, bot2)
        await tgbot.flows.process_download(
            ctx2, make_message("https://youtube.com/watch?v=2"), 100, "youtube", "YouTube", ok_runner, state2,
            status=ForbiddenDeleteStatus(text="Mengunduh..."),  # type: ignore[arg-type]
        )
        self.assertEqual(len(bot2.videos), 1)
        self.assertEqual(state2.jobs_ok, 1)
        self.assertEqual(state2.jobs_failed, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
