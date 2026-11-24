#!/usr/bin/env python3
"""Offline unit tests for tgbot_tray.controller and bot-side stop.request watcher."""

from __future__ import annotations

import asyncio
import ctypes
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tgbot.settings import clear_stop_request, watch_stop_request
from tgbot_tray.controller import (
    BotController,
    BotStatus,
    ControllerSnapshot,
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_STABLE_RUN_SECONDS,
    SingleInstance,
    _kill_tree_win32,
    _lock_file,
    _unlock_file,
    stop_standalone_bot,
)

FAKE_BOT_SCRIPT = """\
import os
import subprocess
import sys
import time
from pathlib import Path

mode = os.environ.get("FAKE_MODE", "sleep")
root = Path(os.environ.get("FAKE_ROOT", ".")).resolve()
stop_file = root / "stop.request"
count_file = root / "runs.txt"

with open(count_file, "a", encoding="utf-8") as fh:
    fh.write(f"run:{mode}\\n")
    fh.flush()

print(f"fake_bot started mode={mode}", flush=True)

if mode == "exit1":
    sys.exit(1)
elif mode == "exit2":
    sys.exit(2)
elif mode == "graceful":
    for _ in range(200):
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            print("graceful_exit", flush=True)
            sys.exit(0)
        time.sleep(0.05)
    sys.exit(0)
elif mode == "spawn_and_ignore":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (root / "child.pid").write_text(str(child.pid), encoding="ascii")
    time.sleep(60)
"""


def _is_pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is still alive on Windows or POSIX."""
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TrayControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bot_script = self.root / "fake_bot.py"
        self.bot_script.write_text(FAKE_BOT_SCRIPT, encoding="utf-8")
        self._old_env = {
            k: os.environ.get(k)
            for k in ("FAKE_MODE", "FAKE_ROOT")
        }
        os.environ["FAKE_ROOT"] = str(self.root)

    def tearDown(self) -> None:
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _make_controller(self, **kwargs) -> BotController:
        defaults = dict(
            project_root=self.root,
            python_exe=Path(sys.executable),
            bot_script=self.bot_script,
            update_interval_seconds=86400.0,
            update_command=[sys.executable, "-c", "print('update ok')"],
        )
        defaults.update(kwargs)
        return BotController(**defaults)

    def test_graceful_stop_and_console_log_rotation(self) -> None:
        os.environ["FAKE_MODE"] = "graceful"
        ctrl = self._make_controller(stop_timeout=5.0)
        self.addCleanup(lambda: ctrl.stop(wait=True))

        self.assertTrue(ctrl.start())
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.RUNNING))
        self.assertTrue(_wait_until(lambda: (self.root / "runs.txt").exists()))

        ctrl.stop(wait=True)
        snap = ctrl.snapshot()
        self.assertEqual(snap.status, BotStatus.STOPPED)
        self.assertEqual(snap.last_exit_code, 0)
        self.assertFalse((self.root / "stop.request").exists())
        self.assertIn("graceful_exit", (self.root / "bot.console.log").read_text(encoding="utf-8"))

        # Second start rotates bot.console.log -> bot.console.prev.log.
        self.assertTrue(ctrl.start())
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.RUNNING))
        ctrl.stop(wait=True)
        self.assertTrue((self.root / "bot.console.prev.log").is_file())
        self.assertIn("graceful_exit", (self.root / "bot.console.prev.log").read_text(encoding="utf-8"))

    def test_forced_tree_kill_leaves_no_child_alive(self) -> None:
        os.environ["FAKE_MODE"] = "spawn_and_ignore"
        ctrl = self._make_controller(stop_timeout=0.5)
        self.addCleanup(lambda: ctrl.stop(wait=True))

        ctrl.start()
        self.assertTrue(_wait_until(lambda: (self.root / "child.pid").is_file()))
        child_pid = int((self.root / "child.pid").read_text(encoding="ascii").strip())
        self.addCleanup(lambda: _kill_tree_win32(child_pid) if os.name == "nt" and _is_pid_alive(child_pid) else None)
        self.assertTrue(_is_pid_alive(child_pid))

        ctrl.stop(wait=True)
        self.assertEqual(ctrl.snapshot().status, BotStatus.STOPPED)
        self.assertTrue(_wait_until(lambda: not _is_pid_alive(child_pid), timeout=3.0))

    def test_backoff_sequence_and_stable_reset(self) -> None:
        self.assertEqual(DEFAULT_BACKOFF_SECONDS, (10.0, 30.0, 60.0, 300.0))
        self.assertEqual(DEFAULT_STABLE_RUN_SECONDS, 600.0)

        os.environ["FAKE_MODE"] = "exit2"
        recorded_delays: list[float] = []
        now = [100.0]

        def fake_mono() -> float:
            return now[0]

        def on_change(snap: ControllerSnapshot) -> None:
            if snap.status == BotStatus.RESTARTING and snap.next_restart_at is not None:
                recorded_delays.append(round(snap.next_restart_at - now[0], 2))
            elif snap.status == BotStatus.RUNNING and len(recorded_delays) == 5:
                # Simulate a 700s stable run during the 6th run so backoff resets.
                now[0] += 700.0

        ctrl = self._make_controller(
            backoff_schedule=(0.05, 0.15, 0.30, 0.50),
            stable_run_seconds=600.0,
            monotonic_fn=fake_mono,
            on_state_change=on_change,
        )
        self.addCleanup(lambda: ctrl.stop(wait=True))

        ctrl.start()
        self.assertTrue(_wait_until(lambda: len(recorded_delays) >= 6, timeout=8.0))
        ctrl.stop(wait=True)
        self.assertEqual(recorded_delays[:6], [0.05, 0.15, 0.30, 0.50, 0.50, 0.05])

    def test_no_restart_on_exit_code_1(self) -> None:
        os.environ["FAKE_MODE"] = "exit1"
        ctrl = self._make_controller(backoff_schedule=(0.05, 0.1))
        self.addCleanup(lambda: ctrl.stop(wait=True))

        ctrl.start()
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.CRASHED))
        time.sleep(0.2)
        snap = ctrl.snapshot()
        self.assertEqual(snap.status, BotStatus.CRASHED)
        self.assertEqual(snap.last_exit_code, 1)
        runs = (self.root / "runs.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(runs), 1)

    def test_no_restart_after_user_stop_during_backoff(self) -> None:
        os.environ["FAKE_MODE"] = "exit2"
        ctrl = self._make_controller(backoff_schedule=(60.0,))
        self.addCleanup(lambda: ctrl.stop(wait=True))

        ctrl.start()
        self.assertTrue(
            _wait_until(
                lambda: (
                    ctrl.snapshot().status == BotStatus.RESTARTING
                    and ctrl.snapshot().next_restart_at is not None
                )
            )
        )
        t0 = time.monotonic()
        ctrl.stop(wait=True)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.0)
        self.assertEqual(ctrl.snapshot().status, BotStatus.STOPPED)
        runs = (self.root / "runs.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(runs), 1)

    def test_ytdlp_update_throttled_once_per_24h_and_nonfatal(self) -> None:
        os.environ["FAKE_MODE"] = "graceful"
        wall = [1_000_000.0]
        count_file = self.root / "updates.txt"
        update_cmd = [
            sys.executable,
            "-c",
            f"from pathlib import Path; import sys; p = Path(r'{count_file}'); p.write_text((p.read_text() if p.exists() else '') + 'u\\n'); sys.exit(1)",
        ]
        ctrl = self._make_controller(
            wall_clock_fn=lambda: wall[0],
            update_interval_seconds=86400.0,
            update_command=update_cmd,
        )
        self.addCleanup(lambda: ctrl.stop(wait=True))

        # 1st start: update runs (and fails with exit 1, non-fatal -> bot still starts).
        ctrl.start()
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.RUNNING))
        ctrl.stop(wait=True)
        self.assertEqual(len(count_file.read_text().splitlines()), 1)
        self.assertIn("exit code 1", (self.root / "bot.update.log").read_text(encoding="utf-8"))

        # 12h later: update skipped.
        wall[0] += 12 * 3600
        ctrl.start()
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.RUNNING))
        ctrl.stop(wait=True)
        self.assertEqual(len(count_file.read_text().splitlines()), 1)

        # 25h later: update runs again.
        wall[0] += 13 * 3600
        ctrl.start()
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.RUNNING))
        ctrl.stop(wait=True)
        self.assertEqual(len(count_file.read_text().splitlines()), 2)

    def test_watch_stop_request_bot_side(self) -> None:
        stop_file = self.root / "stop.request"
        self.assertFalse(clear_stop_request(stop_file))
        stop_file.write_text("stop\n", encoding="utf-8")
        self.assertTrue(clear_stop_request(stop_file))
        self.assertFalse(stop_file.exists())

        called = []

        async def _run() -> None:
            task = asyncio.create_task(
                watch_stop_request(lambda: called.append(True), path=stop_file, interval=0.05)
            )
            await asyncio.sleep(0.08)
            stop_file.write_text("stop\n", encoding="utf-8")
            await asyncio.wait_for(task, timeout=2.0)

        asyncio.run(_run())
        self.assertEqual(called, [True])
        self.assertFalse(stop_file.exists())

    def test_single_instance_lock_and_ipc_wake(self) -> None:
        lock_file = self.root / "tray.lock"
        received: list[str] = []
        woken = threading.Event()
        first = SingleInstance(lock_file)
        second = SingleInstance(lock_file)
        self.addCleanup(first.release)

        def _on_cmd(cmd: str) -> None:
            received.append(cmd)
            woken.set()

        self.assertTrue(first.acquire(_on_cmd))
        self.assertFalse(second.acquire(lambda _cmd: None))
        self.assertTrue(second.notify_existing("SHOW"))
        self.assertTrue(woken.wait(timeout=2.0))
        self.assertEqual(received, ["SHOW"])

        for cmd in ("START", "SHOW_START", "STOP"):
            woken.clear()
            self.assertTrue(second.notify_existing(cmd))
            self.assertTrue(woken.wait(timeout=2.0))
        self.assertEqual(received, ["SHOW", "START", "SHOW_START", "STOP"])
        first.release()

    def test_parse_args_flags(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("tray_app", PROJECT_ROOT / "tray_app.pyw")
        assert spec is not None and spec.loader is not None
        tray_app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tray_app)

        for argv, expected in [
            ([], (False, False, False, False)),
            (["--hidden"], (True, False, False, False)),
            (["--start"], (False, True, False, False)),
            (["--hidden", "--start"], (True, True, False, False)),
            (["--stop"], (False, False, True, False)),
            (["--selftest"], (False, False, False, True)),
        ]:
            ns = tray_app.parse_args(argv)
            self.assertEqual((ns.hidden, ns.start, ns.stop, ns.selftest), expected)

    def test_venv_relaunch_decision(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("tray_app", PROJECT_ROOT / "tray_app.pyw")
        assert spec is not None and spec.loader is not None
        tray_app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tray_app)

        spawned: list[tuple[list[str], str]] = []
        errors: list[tuple[str, str]] = []

        def fake_popen(cmd, cwd=None, env=None):
            spawned.append((list(cmd), str(cwd)))
            return None

        # 1. Already in project venv -> no spawn.
        res = tray_app.ensure_venv(
            ["--hidden"],
            project_root=self.root,
            prefix=self.root / ".venv",
            popen_fn=fake_popen,
            show_error_fn=lambda t, m: errors.append((t, m)),
        )
        self.assertEqual(res, "ok")
        self.assertEqual(spawned, [])

        # 2. Outside venv, pythonw.exe missing -> error dialog, no spawn.
        res = tray_app.ensure_venv(
            [],
            project_root=self.root,
            prefix=self.root / "sys_python",
            popen_fn=fake_popen,
            show_error_fn=lambda t, m: errors.append((t, m)),
        )
        self.assertEqual(res, "missing_venv")
        self.assertEqual(len(errors), 1)
        self.assertIn("python -m venv .venv", errors[0][1])
        self.assertIn("pip install -r requirements.txt", errors[0][1])

        # 3. Outside venv, pythonw.exe present -> relaunches with absolute paths.
        pythonw = self.root / ".venv" / "Scripts" / "pythonw.exe"
        pythonw.parent.mkdir(parents=True, exist_ok=True)
        pythonw.write_text("", encoding="ascii")
        script = self.root / "tray_app.pyw"
        res = tray_app.ensure_venv(
            ["--hidden", "--start"],
            project_root=self.root,
            script_path=script,
            prefix=self.root / "sys_python",
            popen_fn=fake_popen,
            show_error_fn=lambda t, m: errors.append((t, m)),
        )
        self.assertEqual(res, "relaunched")
        self.assertEqual(len(spawned), 1)
        self.assertEqual(
            spawned[0][0],
            [str(pythonw.resolve()), str(script.resolve()), "--hidden", "--start"],
        )
        self.assertEqual(spawned[0][1], str(self.root.resolve()))

    def test_stop_standalone_bot(self) -> None:
        # No bot.lock -> returns False.
        self.assertFalse(stop_standalone_bot(self.root, stop_timeout=1.0))

        # Simulate standalone bot holding bot.lock and exiting when stop.request appears.
        lock_file = self.root / "bot.lock"
        stop_file = self.root / "stop.request"
        handle = open(lock_file, "a+b")
        _lock_file(handle)
        released = threading.Event()

        def _fake_bot() -> None:
            try:
                _wait_until(stop_file.exists, timeout=3.0)
            finally:
                _unlock_file(handle)
                handle.close()
                released.set()

        t = threading.Thread(target=_fake_bot, daemon=True)
        t.start()
        self.assertTrue(stop_standalone_bot(self.root, stop_timeout=3.0))
        self.assertTrue(released.wait(timeout=2.0))
        self.assertFalse(stop_file.exists())

    def test_app_icon_sizes(self) -> None:
        from PIL import Image

        ico_path = PROJECT_ROOT / "assets" / "app.ico"
        self.assertTrue(ico_path.is_file())
        with Image.open(ico_path) as img:
            self.assertEqual(img.format, "ICO")
            self.assertEqual(img.info.get("sizes"), {(16, 16), (32, 32), (48, 48), (256, 256)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
