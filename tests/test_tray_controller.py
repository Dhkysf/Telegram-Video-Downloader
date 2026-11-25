#!/usr/bin/env python3
"""Offline unit tests for tgbot_tray.controller and bot-side stop.request watcher."""

from __future__ import annotations

import asyncio
import ctypes
import io
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tgbot.settings import clear_ready_file, clear_stop_request, watch_stop_request, write_ready_file
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
ready_file = root / "bot.ready"
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
    ready_file.write_text(f"{os.getpid()}\\n", encoding="ascii")
    for _ in range(200):
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            ready_file.unlink(missing_ok=True)
            print("graceful_exit", flush=True)
            sys.exit(0)
        time.sleep(0.05)
    sys.exit(0)
elif mode == "delayed_ready":
    allow_file = root / "allow.ready"
    for _ in range(200):
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            sys.exit(0)
        if allow_file.exists():
            ready_file.write_text(f"{os.getpid()}\\n", encoding="ascii")
            break
        time.sleep(0.05)
    for _ in range(200):
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            ready_file.unlink(missing_ok=True)
            sys.exit(0)
        time.sleep(0.05)
    sys.exit(0)
elif mode == "never_ready":
    for _ in range(200):
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            sys.exit(0)
        time.sleep(0.05)
    sys.exit(0)
elif mode == "fail_then_ready":
    runs = len(count_file.read_text(encoding="utf-8").splitlines())
    if runs == 1:
        sys.exit(2)
    ready_file.write_text(f"{os.getpid()}\\n", encoding="ascii")
    for _ in range(200):
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            ready_file.unlink(missing_ok=True)
            sys.exit(0)
        time.sleep(0.05)
    sys.exit(0)
elif mode == "spawn_and_ignore":
    ready_file.write_text(f"{os.getpid()}\\n", encoding="ascii")
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
            elif snap.status == BotStatus.STARTING and snap.pid is not None and len(recorded_delays) == 5:
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
            ([], (False, False, False, False, False)),
            (["--hidden"], (True, False, False, False, False)),
            (["--start"], (False, True, False, False, False)),
            (["--hidden", "--start"], (True, True, False, False, False)),
            (["--stop"], (False, False, True, False, False)),
            (["--selftest"], (False, False, False, True, False)),
            (["--regen-icon"], (False, False, False, False, True)),
        ]:
            ns = tray_app.parse_args(argv)
            self.assertEqual((ns.hidden, ns.start, ns.stop, ns.selftest, ns.regen_icon), expected)

    def test_venv_relaunch_decision(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("tray_app", PROJECT_ROOT / "tray_app.pyw")
        assert spec is not None and spec.loader is not None
        tray_app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tray_app)

        spawned: list[tuple[list[str], str, dict[str, str]]] = []
        errors: list[tuple[str, str]] = []

        def fake_popen(cmd, cwd=None, env=None):
            spawned.append((list(cmd), str(cwd), dict(env or {})))
            return None

        # 1. Already in project venv -> no spawn.
        res = tray_app.ensure_venv(
            ["--hidden"],
            project_root=self.root,
            prefix=self.root / ".venv",
            popen_fn=fake_popen,
            show_error_fn=lambda t, m: errors.append((t, m)),
        )
        self.assertIsNone(res)
        self.assertEqual(spawned, [])

        # 2. Outside venv, pythonw.exe missing -> error dialog, no spawn.
        res = tray_app.ensure_venv(
            [],
            project_root=self.root,
            prefix=self.root / "sys_python",
            popen_fn=fake_popen,
            show_error_fn=lambda t, m: errors.append((t, m)),
        )
        self.assertEqual(res, 1)
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
        self.assertEqual(res, 0)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(
            spawned[0][0],
            [str(pythonw.resolve()), str(script.resolve()), "--hidden", "--start"],
        )
        self.assertEqual(spawned[0][1], str(self.root.resolve()))
        self.assertEqual(spawned[0][2].get("TVB_RELAUNCHED"), "1")

    def test_ensure_venv_cli_wait_and_relaunch_guard(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("tray_app", PROJECT_ROOT / "tray_app.pyw")
        assert spec is not None and spec.loader is not None
        tray_app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tray_app)

        scripts_dir = self.root / ".venv" / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        python_exe = scripts_dir / "python.exe"
        pythonw_exe = scripts_dir / "pythonw.exe"
        python_exe.write_text("", encoding="ascii")
        pythonw_exe.write_text("", encoding="ascii")
        script = self.root / "tray_app.pyw"

        # Case-insensitive venv path check.
        upper_venv = Path(str(self.root / ".VENV").upper())
        self.assertTrue(tray_app.is_in_project_venv(self.root, prefix=upper_venv))

        # --stop uses python.exe and subprocess.run(..., timeout=60), returning child's exit code.
        with (
            patch.object(tray_app.subprocess, "Popen") as mock_popen,
            patch.object(
                tray_app.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0),
            ) as mock_run,
        ):
            rc = tray_app.ensure_venv(
                ["--stop"],
                project_root=self.root,
                script_path=script,
                prefix=self.root / "sys_python",
            )
        self.assertEqual(rc, 0)
        mock_popen.assert_not_called()
        mock_run.assert_called_once()
        run_args, run_kwargs = mock_run.call_args
        self.assertEqual(
            run_args[0],
            [str(python_exe.resolve()), str(script.resolve()), "--stop"],
        )
        self.assertEqual(run_kwargs["timeout"], 60)
        self.assertEqual(run_kwargs["env"].get("TVB_RELAUNCHED"), "1")

        # --selftest propagates non-zero exit code.
        with patch.object(
            tray_app.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=2),
        ):
            rc = tray_app.ensure_venv(
                ["--selftest"],
                project_root=self.root,
                script_path=script,
                prefix=self.root / "sys_python",
            )
        self.assertEqual(rc, 2)

        # TimeoutExpired -> returns 1 and writes a message to stderr.
        stderr_buf = io.StringIO()
        with (
            patch.object(
                tray_app.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="python.exe", timeout=60),
            ),
            patch.object(tray_app.sys, "stderr", stderr_buf),
        ):
            rc = tray_app.ensure_venv(
                ["--stop"],
                project_root=self.root,
                script_path=script,
                prefix=self.root / "sys_python",
            )
        self.assertEqual(rc, 1)
        self.assertIn("Timed out", stderr_buf.getvalue())

        # TVB_RELAUNCHED=1 prevents relaunch loop for both GUI and CLI.
        with (
            patch.dict(os.environ, {"TVB_RELAUNCHED": "1"}),
            patch.object(tray_app.subprocess, "Popen") as mock_popen,
            patch.object(tray_app.subprocess, "run") as mock_run,
        ):
            self.assertIsNone(
                tray_app.ensure_venv([], project_root=self.root, prefix=self.root / "sys_python")
            )
            self.assertIsNone(
                tray_app.ensure_venv(
                    ["--stop"], project_root=self.root, prefix=self.root / "sys_python"
                )
            )
            mock_popen.assert_not_called()
            mock_run.assert_not_called()

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

    @unittest.skipUnless(os.name == "nt", "Windows process-tree test")
    def test_stop_standalone_bot_relative_shim_tree_via_pid(self) -> None:
        bot_py = self.root / "bot.py"
        bot_py.write_text(
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            "from tgbot.settings import instance_lock\n"
            "root = Path('.').resolve()\n"
            "with instance_lock(root / 'bot.lock') as ok:\n"
            "    assert ok\n"
            "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "    (root / 'grandchild.pid').write_text(str(child.pid), encoding='ascii')\n"
            "    time.sleep(60)\n",
            encoding="utf-8",
        )
        shim_py = self.root / "shim.py"
        shim_py.write_text(
            "import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, 'bot.py'])\n"
            "( __import__('pathlib').Path('bot_child.pid') ).write_text(str(p.pid), encoding='ascii')\n"
            "p.wait()\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        shim_proc = subprocess.Popen(
            [sys.executable, str(shim_py)],
            cwd=str(self.root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        bot_pid: int | None = None
        grandchild_pid: int | None = None
        try:
            self.assertTrue(_wait_until(lambda: (self.root / "bot.pid").is_file()))
            self.assertTrue(_wait_until(lambda: (self.root / "grandchild.pid").is_file()))
            bot_pid = int((self.root / "bot.pid").read_text(encoding="ascii").strip())
            grandchild_pid = int((self.root / "grandchild.pid").read_text(encoding="ascii").strip())
            self.assertTrue(_is_pid_alive(shim_proc.pid))
            self.assertTrue(_is_pid_alive(bot_pid))
            self.assertTrue(_is_pid_alive(grandchild_pid))

            self.assertTrue(stop_standalone_bot(self.root, stop_timeout=0.3))
            self.assertTrue(_wait_until(lambda: not _is_pid_alive(bot_pid), timeout=3.0))
            self.assertTrue(_wait_until(lambda: not _is_pid_alive(grandchild_pid), timeout=3.0))
            self.assertTrue(_wait_until(lambda: shim_proc.poll() is not None, timeout=3.0))
            self.assertFalse((self.root / "bot.pid").exists())
            self.assertFalse((self.root / "stop.request").exists())
        finally:
            for pid in (grandchild_pid, bot_pid, shim_proc.pid):
                if pid is not None and _is_pid_alive(pid):
                    _kill_tree_win32(pid)
            try:
                shim_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    @unittest.skipUnless(
        os.name == "nt"
        and (PROJECT_ROOT / ".venv" / "Scripts" / "python.exe").is_file()
        and (PROJECT_ROOT / ".venv" / "pyvenv.cfg").is_file(),
        "Windows venv shim test",
    )
    def test_stop_standalone_bot_fallback_relative_venv_shim_no_pid(self) -> None:
        venv_scripts = self.root / ".venv" / "Scripts"
        venv_scripts.mkdir(parents=True)
        venv_python = venv_scripts / "python.exe"
        shutil.copy2(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe", venv_python)
        shutil.copy2(PROJECT_ROOT / ".venv" / "pyvenv.cfg", self.root / ".venv" / "pyvenv.cfg")

        (self.root / "bot.py").write_text(
            "import msvcrt, os, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "root = Path('.').resolve()\n"
            "fh = open(root / 'bot.lock', 'a+b')\n"
            "fh.seek(0); msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "(root / 'grandchild.pid').write_text(str(child.pid), encoding='ascii')\n"
            "(root / 'real.pid').write_text(str(os.getpid()), encoding='ascii')\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        shim_proc = subprocess.Popen(
            [r".venv\Scripts\python.exe", "bot.py"],
            executable=str(venv_python),
            cwd=str(self.root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        real_pid: int | None = None
        grandchild_pid: int | None = None
        try:
            self.assertTrue(_wait_until(lambda: (self.root / "real.pid").is_file()))
            self.assertTrue(_wait_until(lambda: (self.root / "grandchild.pid").is_file()))
            real_pid = int((self.root / "real.pid").read_text(encoding="ascii").strip())
            grandchild_pid = int((self.root / "grandchild.pid").read_text(encoding="ascii").strip())
            self.assertNotEqual(shim_proc.pid, real_pid)
            self.assertFalse((self.root / "bot.pid").exists())
            self.assertTrue(_is_pid_alive(real_pid))
            self.assertTrue(_is_pid_alive(grandchild_pid))

            self.assertTrue(stop_standalone_bot(self.root, stop_timeout=0.3))
            self.assertTrue(_wait_until(lambda: not _is_pid_alive(real_pid), timeout=3.0))
            self.assertTrue(_wait_until(lambda: not _is_pid_alive(grandchild_pid), timeout=3.0))
            self.assertTrue(_wait_until(lambda: shim_proc.poll() is not None, timeout=3.0))
        finally:
            for pid in (grandchild_pid, real_pid, shim_proc.pid):
                if pid is not None and _is_pid_alive(pid):
                    _kill_tree_win32(pid)
            try:
                shim_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    @unittest.skipUnless(os.name == "nt", "Windows process-tree test")
    def test_stop_standalone_bot_ignores_stale_pid_without_bot_py(self) -> None:
        innocent = subprocess.Popen(
            [sys.executable, "-c", "import os, time; print(os.getpid(), flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert innocent.stdout is not None
        innocent_real_pid = int(innocent.stdout.readline().strip())
        lock_file = self.root / "bot.lock"
        handle = open(lock_file, "a+b")
        _lock_file(handle)
        (self.root / "bot.pid").write_text(f"{innocent_real_pid}\n", encoding="ascii")
        try:
            self.assertFalse(stop_standalone_bot(self.root, stop_timeout=0.2))
            self.assertTrue(_is_pid_alive(innocent_real_pid))
            self.assertIsNone(innocent.poll())
        finally:
            try:
                _unlock_file(handle)
            except OSError:
                pass
            handle.close()
            if _is_pid_alive(innocent_real_pid):
                _kill_tree_win32(innocent_real_pid)
            if _is_pid_alive(innocent.pid):
                _kill_tree_win32(innocent.pid)
            try:
                innocent.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    @unittest.skipUnless(os.name == "nt", "Windows process-tree test")
    def test_stop_standalone_bot_never_kills_bot_in_other_folder(self) -> None:
        other_root = self.root.parent / f"{self.root.name}_other"
        other_root.mkdir(parents=True, exist_ok=True)
        other_bot = other_root / "bot.py"
        other_bot.write_text(
            "import os, time; print(os.getpid(), flush=True); time.sleep(60)\n",
            encoding="utf-8",
        )
        other_proc = subprocess.Popen(
            [sys.executable, str(other_bot)],
            cwd=str(other_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert other_proc.stdout is not None
        other_real_pid = int(other_proc.stdout.readline().strip())
        handle = open(self.root / "bot.lock", "a+b")
        _lock_file(handle)
        try:
            self.assertFalse(stop_standalone_bot(self.root, stop_timeout=0.2))
            self.assertTrue(_is_pid_alive(other_real_pid))
            self.assertIsNone(other_proc.poll())
        finally:
            try:
                _unlock_file(handle)
            except OSError:
                pass
            handle.close()
            if _is_pid_alive(other_real_pid):
                _kill_tree_win32(other_real_pid)
            if _is_pid_alive(other_proc.pid):
                _kill_tree_win32(other_proc.pid)
            try:
                other_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            shutil.rmtree(other_root, ignore_errors=True)

    def test_app_icon_sizes(self) -> None:
        from PIL import Image

        ico_path = PROJECT_ROOT / "assets" / "app.ico"
        self.assertTrue(ico_path.is_file())
        with Image.open(ico_path) as img:
            self.assertEqual(img.format, "ICO")
            self.assertEqual(img.info.get("sizes"), {(16, 16), (32, 32), (48, 48), (256, 256)})
            rgba = img.convert("RGBA")
            cx, cy = rgba.width // 2, rgba.height // 2
            self.assertEqual(rgba.getpixel((cx, cy))[:3], (34, 158, 217))

    def test_starting_until_ready_and_stale_ready_ignored(self) -> None:
        os.environ["FAKE_MODE"] = "delayed_ready"
        (self.root / "bot.ready").write_text("999999\n", encoding="ascii")
        ctrl = self._make_controller(stop_timeout=3.0)
        self.addCleanup(lambda: ctrl.stop(wait=True))

        ctrl.start()
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.STARTING and ctrl.snapshot().pid is not None))
        self.assertFalse((self.root / "bot.ready").exists())

        # A live unrelated PID (this test process) must not mark the bot as running.
        (self.root / "bot.ready").write_text(f"{os.getpid()}\n", encoding="ascii")
        time.sleep(0.25)
        self.assertEqual(ctrl.snapshot().status, BotStatus.STARTING)

        (self.root / "allow.ready").write_text("go\n", encoding="ascii")
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.RUNNING))
        ctrl.stop(wait=True)
        self.assertEqual(ctrl.snapshot().status, BotStatus.STOPPED)

    def test_never_ready_stays_starting_and_warns_once(self) -> None:
        os.environ["FAKE_MODE"] = "never_ready"
        warnings: list[str] = []

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= logging.WARNING:
                    warnings.append(record.getMessage())

        test_logger = logging.getLogger("test_never_ready")
        test_logger.setLevel(logging.WARNING)
        test_logger.propagate = False
        handler = _ListHandler()
        test_logger.addHandler(handler)
        self.addCleanup(lambda: test_logger.removeHandler(handler))

        statuses: list[BotStatus] = []
        ctrl = self._make_controller(
            ready_timeout=0.2,
            logger_inst=test_logger,
            on_state_change=lambda snap: statuses.append(snap.status),
        )
        self.addCleanup(lambda: ctrl.stop(wait=True))

        ctrl.start()
        self.assertTrue(_wait_until(lambda: any("did not report ready" in w for w in warnings), timeout=3.0))
        time.sleep(0.35)
        self.assertEqual(ctrl.snapshot().status, BotStatus.STARTING)
        self.assertEqual(sum(1 for w in warnings if "did not report ready" in w), 1)
        self.assertEqual(len((self.root / "runs.txt").read_text(encoding="utf-8").splitlines()), 1)

        ctrl.stop(wait=True)
        self.assertEqual(ctrl.snapshot().status, BotStatus.STOPPED)
        self.assertIn(BotStatus.STOPPING, statuses)
        self.assertEqual(statuses[-1], BotStatus.STOPPED)

    def test_crash_during_starting_and_restart_flow(self) -> None:
        os.environ["FAKE_MODE"] = "fail_then_ready"
        statuses: list[BotStatus] = []
        ctrl = self._make_controller(
            backoff_schedule=(0.1,),
            on_state_change=lambda snap: statuses.append(snap.status),
        )
        self.addCleanup(lambda: ctrl.stop(wait=True))

        ctrl.start()
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.RUNNING, timeout=5.0))
        ctrl.stop(wait=True)
        for expected in (BotStatus.STARTING, BotStatus.RESTARTING, BotStatus.RUNNING, BotStatus.STOPPING, BotStatus.STOPPED):
            self.assertIn(expected, statuses)

    def test_stop_emits_stopping_then_stopped(self) -> None:
        os.environ["FAKE_MODE"] = "graceful"
        statuses: list[BotStatus] = []
        ctrl = self._make_controller(
            stop_timeout=3.0,
            on_state_change=lambda snap: statuses.append(snap.status),
        )
        self.addCleanup(lambda: ctrl.stop(wait=True))

        ctrl.start()
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.RUNNING))
        ctrl.stop(wait=True)
        deduped = [s for i, s in enumerate(statuses) if i == 0 or s != statuses[i - 1]]
        self.assertEqual(
            deduped,
            [BotStatus.STARTING, BotStatus.RUNNING, BotStatus.STOPPING, BotStatus.STOPPED],
        )

    def test_ytdlp_update_uses_starting_status(self) -> None:
        os.environ["FAKE_MODE"] = "graceful"
        statuses: list[tuple[BotStatus, str]] = []
        ctrl = self._make_controller(
            update_interval_seconds=86400.0,
            update_command=[sys.executable, "-c", "import time; time.sleep(0.1)"],
            on_state_change=lambda snap: statuses.append((snap.status, snap.detail)),
        )
        self.addCleanup(lambda: ctrl.stop(wait=True))

        ctrl.start()
        self.assertTrue(_wait_until(lambda: ctrl.snapshot().status == BotStatus.RUNNING))
        ctrl.stop(wait=True)
        self.assertIn((BotStatus.STARTING, "Updating yt-dlp..."), statuses)
        self.assertNotIn(BotStatus.RESTARTING, [s for s, _ in statuses])

    def test_write_and_clear_ready_file_bot_side(self) -> None:
        ready_file = self.root / "bot.ready"
        self.assertFalse(clear_ready_file(ready_file))
        write_ready_file(ready_file)
        self.assertEqual(ready_file.read_text(encoding="ascii").strip(), str(os.getpid()))
        self.assertTrue(clear_ready_file(ready_file))
        self.assertFalse(ready_file.exists())
        self.assertFalse(clear_ready_file(ready_file))

    def test_status_colors_covers_every_bot_status(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("tray_app", PROJECT_ROOT / "tray_app.pyw")
        assert spec is not None and spec.loader is not None
        tray_app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tray_app)

        self.assertEqual(set(tray_app.STATUS_COLORS), set(BotStatus))
        self.assertEqual(tray_app.STATUS_COLORS[BotStatus.RUNNING], "#229ED9")
        self.assertEqual(tray_app.STATUS_COLORS[BotStatus.STOPPED], "#E74C3C")
        self.assertEqual(tray_app.STATUS_COLORS[BotStatus.CRASHED], "#E74C3C")
        for s in (BotStatus.STARTING, BotStatus.RESTARTING, BotStatus.STOPPING):
            self.assertEqual(tray_app.STATUS_COLORS[s], "#F1C40F")

    def test_tray_icon_sync_with_fake_icon(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("tray_app", PROJECT_ROOT / "tray_app.pyw")
        assert spec is not None and spec.loader is not None
        tray_app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tray_app)

        class FakeIcon:
            def __init__(self, name, icon, title, menu) -> None:
                self.name = name
                self._icon = icon
                self._icon_handle = icon
                self._title = title
                self.menu = menu
                self._visible = False
                self.displayed_icon = None
                self.displayed_title = None

            @property
            def visible(self) -> bool:
                return self._visible

            @visible.setter
            def visible(self, value: bool) -> None:
                if value and not self._visible:
                    # Mirrors pystray: _show() uses cached _icon_handle, reads current _title.
                    self.displayed_icon = self._icon_handle
                    self.displayed_title = self._title
                    self._visible = True
                elif not value:
                    self._visible = False

            @property
            def icon(self):
                return self._icon

            @icon.setter
            def icon(self, value) -> None:
                self._icon = value
                if self._visible:
                    self._icon_handle = value
                    self.displayed_icon = value

            @property
            def title(self) -> str:
                return self._title

            @title.setter
            def title(self, value: str) -> None:
                self._title = value
                if self._visible:
                    self.displayed_title = value

            def update_menu(self) -> None:
                pass

        fake_icon_holder: list[FakeIcon] = []

        def _make_fake(self_app):
            ic = FakeIcon(
                "telegram_video_bot",
                self_app._icons[tray_app.STATUS_COLORS[BotStatus.STOPPED]],
                f"Telegram Video Bot - {BotStatus.STOPPED.value}",
                None,
            )
            fake_icon_holder.append(ic)
            return ic

        with patch.object(tray_app.TrayApp, "_build_tray_icon", _make_fake):
            app = tray_app.TrayApp(selftest=False, start_hidden=True)
        app.root.withdraw()
        self.addCleanup(lambda: (setattr(app, "_closing", True), app.root.destroy()))

        fake_icon = fake_icon_holder[0]

        # 1. Status changes before _on_icon_ready: icon is not visible yet, so _shown_icon_key stays None.
        app._apply_snapshot(ControllerSnapshot(BotStatus.RUNNING, 123, 1.0, None, None))
        self.assertIsNone(app._shown_icon_key)
        self.assertIsNone(fake_icon.displayed_icon)

        # Setup callback runs -> visible becomes True -> _sync_icon runs on Tk thread.
        app._on_icon_ready(fake_icon)
        app.root.update()
        self.assertEqual(app._shown_icon_key, BotStatus.RUNNING)
        self.assertEqual(tray_app.STATUS_COLORS[app._shown_icon_key], "#229ED9")
        self.assertIs(fake_icon.displayed_icon, app._icons["#229ED9"])
        self.assertEqual(fake_icon.displayed_title, "Telegram Video Bot - Running")

        # 2. Status changes after visible -> updated immediately, including same-colour tooltip changes.
        app._apply_snapshot(ControllerSnapshot(BotStatus.STOPPING, 123, 1.0, None, None))
        self.assertEqual(app._shown_icon_key, BotStatus.STOPPING)
        self.assertIs(fake_icon.displayed_icon, app._icons["#F1C40F"])
        self.assertEqual(fake_icon.displayed_title, "Telegram Video Bot - Stopping")

        app._apply_snapshot(ControllerSnapshot(BotStatus.RESTARTING, None, None, 2, 10.0))
        self.assertEqual(app._shown_icon_key, BotStatus.RESTARTING)
        self.assertEqual(fake_icon.displayed_title, "Telegram Video Bot - Restarting")

        # 3. _tick heals a forced mismatch.
        app._apply_snapshot(ControllerSnapshot(BotStatus.RUNNING, 123, 1.0, None, None))
        app._shown_icon_key = BotStatus.STOPPED
        fake_icon.displayed_icon = app._icons["#E74C3C"]
        with patch.object(app.root, "after", lambda *_a, **_kw: None):
            app._tick()
        self.assertEqual(app._shown_icon_key, BotStatus.RUNNING)
        self.assertIs(fake_icon.displayed_icon, app._icons["#229ED9"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
