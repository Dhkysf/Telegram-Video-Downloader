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
            patch.object(tgbot.handlers, "browser_available", return_value=True),
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

    def test_ytdlp_external_fd_guard_and_match_filter(self) -> None:
        import io
        import yt_dlp
        from yt_dlp.dependencies import Cryptodome
        from yt_dlp.downloader import get_suitable_downloader
        from yt_dlp.downloader.external import FFmpegFD
        from yt_dlp.downloader.hls import HlsFD
        from yt_dlp.downloader.rtmp import RtmpFD

        from downloaders.common import BlockedAddressError, install_network_guard, public_network_only
        from downloaders.ytdlp import _YtdlpLogger, _build_options, _match_filter

        install_network_guard()
        opts = _build_options("testjob", 720, False, _YtdlpLogger(), None, None)
        self.assertTrue(opts["hls_prefer_native"])
        self.assertEqual(opts["external_downloader"], {"default": "native"})
        self.assertIsNotNone(Cryptodome.AES)

        self.assertIs(get_suitable_downloader({"url": "https://example.com/v.m3u8", "protocol": "m3u8"}, opts), HlsFD)
        self.assertIsNone(_match_filter({"url": "https://example.com/v.mp4", "protocol": "https"}))
        self.assertIsNone(_match_filter({"url": "https://example.com/v.m3u8", "protocol": "m3u8"}))
        self.assertIsNone(_match_filter({"url": "https://example.com/v", "protocol": "https+http_dash_segments"}))
        self.assertIsNone(_match_filter({"is_live": False}, incomplete=True))
        self.assertIn("live", _match_filter({"is_live": True}, incomplete=True) or "")
        self.assertIn("live", _match_filter({"live_status": "is_upcoming"}, incomplete=True) or "")
        self.assertIn("unsupported protocol", _match_filter({"url": "rtmp://example.com/live", "protocol": "rtmp"}) or "")
        self.assertIn("unsupported protocol", _match_filter({"url": "mms://example.com/v", "protocol": "mms"}) or "")
        self.assertIn("unsupported protocol", _match_filter({"url": "https://example.com/v", "protocol": "https+rtmp"}) or "")

        with public_network_only():
            with self.assertRaises(BlockedAddressError):
                FFmpegFD(yt_dlp.YoutubeDL(opts), opts).real_download(str(TEMP_DIR / "x.mp4"), {"url": "https://example.com/v.m3u8"})
            with self.assertRaises(BlockedAddressError):
                RtmpFD(yt_dlp.YoutubeDL(opts), opts).real_download(str(TEMP_DIR / "x.flv"), {"url": "rtmp://example.com/v"})

        aes_manifest = (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key"\n'
            "#EXTINF:2.0,\nhttps://example.com/seg.ts\n#EXT-X-ENDLIST\n"
        )
        self.assertTrue(HlsFD.can_download(aes_manifest, {"url": "https://example.com/v.m3u8"}))

        class FakeUrlH:
            def __init__(self, url: str, data: bytes) -> None:
                self.url = url
                self.headers = {"Content-length": str(len(data))}
                self._buf = io.BytesIO(data)

            def read(self, amt: int = -1) -> bytes:
                return self._buf.read(amt)

            def close(self) -> None:
                self._buf.close()

        key = b"0123456789abcdef"
        iv = b"\x00" * 16
        cipher = Cryptodome.AES.new(key, Cryptodome.AES.MODE_CBC, iv)  # type: ignore[union-attr]
        encrypted_seg = cipher.encrypt(b"TSDATA" + b"\x0a" * 10)

        ydl = yt_dlp.YoutubeDL(opts)
        with patch.object(ydl, "urlopen", side_effect=lambda req: FakeUrlH(req.url if hasattr(req, "url") else str(req), key if "key" in str(getattr(req, "url", req)) else encrypted_seg)):
            out_file = TEMP_DIR / "aes_test.mp4"
            try:
                with public_network_only(), patch.object(FFmpegFD, "real_download", side_effect=AssertionError("FFmpegFD must not run")):
                    ok = HlsFD(ydl, opts).real_download(str(out_file), {"url": "https://example.com/v.m3u8", "ext": "mp4", "hls_media_playlist_data": aes_manifest})
                self.assertTrue(ok)
                self.assertTrue(out_file.exists())
            finally:
                for p in TEMP_DIR.glob("aes_test.mp4*"):
                    p.unlink(missing_ok=True)

        with patch("yt_dlp.downloader.hls.Cryptodome.AES", None), patch.object(ydl, "urlopen", return_value=FakeUrlH("https://example.com/v.m3u8", aes_manifest.encode())), public_network_only():
            with self.assertRaises(BlockedAddressError):
                HlsFD(ydl, opts).real_download(str(TEMP_DIR / "aes_blocked.mp4"), {"url": "https://example.com/v.m3u8", "ext": "mp4"})

    async def test_crafted_callback_br_and_negative_index_rejected(self) -> None:
        state = make_state(browser_fallback=False)
        ctx = make_context(state)
        probe_gen = ProbeResult(
            ErrorCode.OK,
            candidates=[
                MediaCandidate(url="https://example.com/v.mp4", title="Main", duration=10),
                MediaCandidate(url="https://ads.example.com/ad.mp4", title="Ad", duration=5, likely_ad=True),
            ],
            can_try_browser=True,
        )
        with patch.object(tgbot.flows, "probe_ytdlp", return_value=probe_gen), patch.object(tgbot.flows, "probe_with_browser") as br_mock:
            await tgbot.handlers.handle_text(Update(1, message=make_message("https://example.com/watch")), ctx)
            tok = next(iter(state.pending))
            q_br = FakeQuery(f"br:{tok}")
            await tgbot.handlers.handle_choice(SimpleNamespace(callback_query=q_br, effective_user=User(100, "u", False)), ctx)
            self.assertEqual(q_br.answers, [(tgbot.texts.EXPIRED_TEXT, True)])
            self.assertIn(tok, state.pending)
            br_mock.assert_not_called()

            res = await tgbot.flows.probe_link("https://example.com/watch", "generic", state, None, force_browser=True)
            self.assertEqual(res.error, ErrorCode.UNSUPPORTED)
            br_mock.assert_not_called()

            for bad_data in (f"dl:{tok}:-1", f"au:{tok}:-1", f"dl:{tok}:99"):
                q_bad = FakeQuery(bad_data)
                await tgbot.handlers.handle_choice(SimpleNamespace(callback_query=q_bad, effective_user=User(100, "u", False)), ctx)
                self.assertEqual(q_bad.answers, [(tgbot.texts.EXPIRED_TEXT, True)])
            self.assertEqual(state.active_users, set())

        state2 = make_state(browser_fallback=True, user_cooldown_seconds=0.0)
        ctx2 = make_context(state2)
        probe_yt = ProbeResult(ErrorCode.OK, candidates=[MediaCandidate(url="https://youtube.com/watch?v=1", title="YT")], can_try_browser=False)
        with patch.object(tgbot.flows, "probe_ytdlp", return_value=probe_yt), patch.object(tgbot.flows, "probe_with_browser") as br_mock2:
            await tgbot.handlers.handle_text(Update(2, message=make_message("https://youtube.com/watch?v=1")), ctx2)
            tok2 = next(iter(state2.pending))
            q_br2 = FakeQuery(f"br:{tok2}")
            await tgbot.handlers.handle_choice(SimpleNamespace(callback_query=q_br2, effective_user=User(100, "u", False)), ctx2)
            self.assertEqual(q_br2.answers, [(tgbot.texts.EXPIRED_TEXT, True)])
            br_mock2.assert_not_called()

    def test_nat64_and_scrape_tiktok_network_guard(self) -> None:
        import requests

        from downloaders.common import BLOCKED_ADDRESS_MESSAGE, BlockedAddressError, is_blocked_host, is_network_guard_active, is_public_ip
        from downloaders.tiktok import scrape_tiktok

        for bad_ip in ("64:ff9b::127.0.0.1", "64:ff9b::192.168.1.1", "64:ff9b::8.8.8.8", "64:ff9b:1::1", "64:ff9b:1:abcd::127.0.0.1", "::ffff:127.0.0.1"):
            self.assertFalse(is_public_ip(bad_ip), f"Expected False for {bad_ip}")
        for good_ip in ("8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"):
            self.assertTrue(is_public_ip(good_ip), f"Expected True for {good_ip}")
        self.assertTrue(is_blocked_host("[64:ff9b::127.0.0.1]"))
        self.assertIsNone(tgbot.links.detect_platform("http://[64:ff9b::127.0.0.1]/v"))

        calls = 0
        guard_seen = False

        def fake_request(_self: object, _method: str, _url: str, **_kw: object) -> object:
            nonlocal calls, guard_seen
            calls += 1
            guard_seen = is_network_guard_active()
            raise requests.ConnectionError(BlockedAddressError(BLOCKED_ADDRESS_MESSAGE))

        self.assertFalse(is_network_guard_active())
        with patch.object(requests.Session, "request", fake_request):
            res = scrape_tiktok("https://www.tiktok.com/@a/video/1", "tiktok_guard_test")
        self.assertTrue(guard_seen)
        self.assertFalse(is_network_guard_active())
        self.assertEqual(calls, 1)
        self.assertEqual(res.error, ErrorCode.UNSUPPORTED)

    def test_instance_lock_and_main_exit_codes(self) -> None:
        import os
        import subprocess
        from contextlib import contextmanager
        from telegram.error import InvalidToken
        import bot as bot_mod

        lock_path = TEMP_DIR / "test_bot.lock"
        try:
            with tgbot.settings.instance_lock(lock_path) as ok1:
                self.assertTrue(ok1)
                with tgbot.settings.instance_lock(lock_path) as ok2:
                    self.assertFalse(ok2)
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import sys\nfrom pathlib import Path\n"
                            "from tgbot.settings import instance_lock\n"
                            "with instance_lock(Path(sys.argv[1])) as ok: print(ok)"
                        ),
                        str(lock_path),
                    ],
                    capture_output=True,
                    text=True,
                    cwd=str(PROJECT_ROOT),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), "False")
            self.assertEqual(lock_path.read_text(encoding="ascii").strip(), str(os.getpid()))

            with tgbot.settings.instance_lock(lock_path) as ok3:
                self.assertTrue(ok3)
        finally:
            lock_path.unlink(missing_ok=True)

        @contextmanager
        def fake_lock(acquired: bool):
            yield acquired

        class DummyApp:
            def __init__(self, exc: Exception) -> None:
                self._exc = exc

            def run_polling(self, **_kw: object) -> None:
                raise self._exc

        with (
            patch.object(bot_mod, "configure_logging"),
            patch.object(bot_mod, "instance_lock", return_value=fake_lock(False)),
            patch.object(bot_mod, "prepare_temp_dir") as prep_mock,
        ):
            with self.assertRaises(SystemExit) as cm1:
                bot_mod.main()
            self.assertEqual(cm1.exception.code, 1)
            prep_mock.assert_not_called()

        for exc, expected_code in ((InvalidToken("bad"), 1), (NetworkError("offline"), 2)):
            with (
                patch.object(bot_mod, "configure_logging"),
                patch.object(bot_mod, "instance_lock", return_value=fake_lock(True)),
                patch.object(bot_mod, "load_token", return_value="123:FAKE"),
                patch.object(bot_mod, "redact_token_in_logs"),
                patch.object(bot_mod, "configure_ffmpeg"),
                patch.object(bot_mod, "install_network_guard"),
                patch.object(bot_mod, "prepare_temp_dir", return_value=0),
                patch.object(bot_mod, "load_settings", return_value=make_state().settings),
                patch.object(bot_mod, "build_application", return_value=DummyApp(exc)),
            ):
                with self.assertRaises(SystemExit) as cm:
                    bot_mod.main()
                self.assertEqual(cm.exception.code, expected_code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
