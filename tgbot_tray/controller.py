"""Process supervisor, log rotation, yt-dlp updater, and single-instance IPC."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from typing import BinaryIO, Callable, Optional

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_STOP_TIMEOUT = 15.0
DEFAULT_BACKOFF_SECONDS: tuple[float, ...] = (10.0, 30.0, 60.0, 300.0)
DEFAULT_STABLE_RUN_SECONDS = 600.0
DEFAULT_UPDATE_INTERVAL_SECONDS = 86400.0
DEFAULT_UPDATE_TIMEOUT = 90.0

WIN_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
WIN_BOT_FLAGS = (
    (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    if os.name == "nt"
    else 0
)

logger = logging.getLogger("tray_app.controller")


class BotStatus(str, Enum):
    """Lifecycle states shown by the tray UI."""

    RUNNING = "Running"
    STOPPED = "Stopped"
    RESTARTING = "Restarting"
    CRASHED = "Crashed"


@dataclass(frozen=True)
class ControllerSnapshot:
    """Immutable view of the controller state for UI rendering."""

    status: BotStatus
    pid: Optional[int]
    started_at: Optional[float]
    last_exit_code: Optional[int]
    next_restart_at: Optional[float]
    detail: str = ""


def format_uptime(seconds: float) -> str:
    """Format non-negative seconds as ``HH:MM:SS`` or ``Xd HH:MM:SS``."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _kill_tree_win32(pid: int) -> None:
    """Terminate ``pid`` and all its descendants using Win32 Toolhelp32."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snap == wintypes.HANDLE(-1).value:
        return
    children: dict[int, list[int]] = {}
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                children.setdefault(pe.th32ParentProcessID, []).append(pe.th32ProcessID)
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)

    tree: list[int] = []
    visited: set[int] = set()
    stack = [pid]
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        tree.append(cur)
        stack.extend(children.get(cur, ()))

    for target_pid in reversed(tree):
        handle = k32.OpenProcess(0x0001, False, target_pid)
        if handle:
            try:
                k32.TerminateProcess(handle, 1)
            finally:
                k32.CloseHandle(handle)


def _kill_tree(pid: int, log: logging.Logger) -> None:
    """Force-terminate ``pid`` and all its child processes."""
    if os.name == "nt":
        try:
            res = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=WIN_NO_WINDOW,
            )
            if res.returncode == 0:
                return
            log.warning("taskkill exited %d for PID %d; falling back to Win32", res.returncode, pid)
        except Exception as exc:
            log.warning("taskkill failed for PID %d (%s); falling back to Win32", pid, exc)
        _kill_tree_win32(pid)
    else:
        import signal

        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _lock_file(handle: BinaryIO) -> None:
    """Acquire a non-blocking exclusive lock on byte 0 of ``handle``."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    """Release the lock held on byte 0 of ``handle``."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BotController:
    """Supervises ``bot.py`` with graceful stop, backoff restarts, and yt-dlp updates."""

    def __init__(
        self,
        project_root: Path = PROJECT_ROOT,
        python_exe: Optional[Path] = None,
        bot_script: Optional[Path] = None,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
        backoff_schedule: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
        stable_run_seconds: float = DEFAULT_STABLE_RUN_SECONDS,
        update_interval_seconds: float = DEFAULT_UPDATE_INTERVAL_SECONDS,
        update_command: Optional[list[str]] = None,
        on_state_change: Optional[Callable[[ControllerSnapshot], None]] = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        wall_clock_fn: Callable[[], float] = time.time,
        logger_inst: Optional[logging.Logger] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.python_exe = (
            Path(python_exe).resolve()
            if python_exe is not None
            else (self.project_root / ".venv" / "Scripts" / "python.exe").resolve()
        )
        self.bot_script = (
            Path(bot_script).resolve()
            if bot_script is not None
            else (self.project_root / "bot.py").resolve()
        )
        self.stop_timeout = stop_timeout
        self.backoff_schedule = backoff_schedule or DEFAULT_BACKOFF_SECONDS
        self.stable_run_seconds = stable_run_seconds
        self.update_interval_seconds = update_interval_seconds
        self.update_command = (
            list(update_command)
            if update_command is not None
            else [
                str(self.python_exe),
                "-m",
                "pip",
                "install",
                "-U",
                "--timeout",
                "30",
                "--disable-pip-version-check",
                "yt-dlp",
            ]
        )
        self._on_state_change = on_state_change
        self._monotonic = monotonic_fn
        self._wall_clock = wall_clock_fn
        self._log = logger_inst or logger

        self.stop_request_file = self.project_root / "stop.request"
        self.console_log = self.project_root / "bot.console.log"
        self.console_prev_log = self.project_root / "bot.console.prev.log"
        self.update_log = self.project_root / "bot.update.log"

        self._lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_requested = False
        self._supervisor: Optional[threading.Thread] = None
        self._last_update_at: Optional[float] = None
        self._snapshot = ControllerSnapshot(
            status=BotStatus.STOPPED,
            pid=None,
            started_at=None,
            last_exit_code=None,
            next_restart_at=None,
        )

    def snapshot(self) -> ControllerSnapshot:
        """Return the latest immutable controller snapshot."""
        with self._lock:
            return self._snapshot

    def start(self) -> bool:
        """Start the bot supervisor if not already running."""
        with self._action_lock:
            return self._start_locked()

    def stop(self, wait: bool = True) -> None:
        """Request a graceful stop (falling back to tree kill) and halt restarts."""
        with self._action_lock:
            self._stop_locked(wait=wait)

    def restart(self) -> None:
        """Stop the running bot (if any) and start a fresh run."""
        with self._action_lock:
            self._stop_locked(wait=True)
            self._start_locked()

    def _start_locked(self) -> bool:
        with self._lock:
            if self._supervisor is not None and self._supervisor.is_alive():
                return False
            self._stop_requested = False
            self._wake_event.clear()
            thread = threading.Thread(
                target=self._run_loop,
                name="bot-supervisor",
                daemon=True,
            )
            self._supervisor = thread
            thread.start()
            return True

    def _stop_locked(self, wait: bool) -> None:
        with self._lock:
            self._stop_requested = True
            self._wake_event.set()
            sup = self._supervisor
        if wait and sup is not None and sup.is_alive() and sup is not threading.current_thread():
            sup.join(timeout=self.stop_timeout + 15.0)

    def _set_snapshot(
        self,
        status: BotStatus,
        *,
        pid: Optional[int] = None,
        started_at: Optional[float] = None,
        last_exit_code: Optional[int] = None,
        next_restart_at: Optional[float] = None,
        detail: str = "",
    ) -> None:
        with self._lock:
            self._snapshot = ControllerSnapshot(
                status=status,
                pid=pid,
                started_at=started_at,
                last_exit_code=last_exit_code,
                next_restart_at=next_restart_at,
                detail=detail,
            )
            snap = self._snapshot
        if self._on_state_change is not None:
            try:
                self._on_state_change(snap)
            except Exception as exc:
                self._log.warning("State change callback failed: %s", exc)

    def _is_stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    def _maybe_update_ytdlp(self) -> None:
        """Run yt-dlp update at most once per ``update_interval_seconds``; non-fatal."""
        now = self._wall_clock()
        last = self._last_update_at
        if last is None and self.update_log.is_file():
            try:
                last = self.update_log.stat().st_mtime
            except OSError:
                last = None
        if last is not None and (now - last) < self.update_interval_seconds:
            return

        self._last_update_at = now
        self._set_snapshot(
            BotStatus.RESTARTING,
            last_exit_code=self.snapshot().last_exit_code,
            detail="Updating yt-dlp...",
        )
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.update_log.write_text(f"[{stamp}] Updating yt-dlp...\n", encoding="utf-8")
            log_f = open(self.update_log, "ab")
        except OSError as exc:
            self._log.warning("Could not open %s: %s", self.update_log.name, exc)
            return

        try:
            proc = subprocess.Popen(
                self.update_command,
                cwd=str(self.project_root),
                stdout=log_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=WIN_NO_WINDOW,
            )
        except OSError as exc:
            log_f.close()
            self._log.warning("yt-dlp update failed to start: %s", exc)
            return
        finally:
            if not log_f.closed:
                log_f.close()

        deadline = self._monotonic() + DEFAULT_UPDATE_TIMEOUT
        while proc.poll() is None:
            if self._is_stop_requested() or self._monotonic() >= deadline:
                _kill_tree(proc.pid, self._log)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                break
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass

        if proc.returncode not in (0, None):
            self._log.warning("yt-dlp update exited with code %s; continuing", proc.returncode)
            try:
                with open(self.update_log, "a", encoding="utf-8") as fh:
                    fh.write(f"yt-dlp update failed (exit code {proc.returncode}), continuing.\n")
            except OSError:
                pass

    def _rotate_console_log(self) -> None:
        if self.console_log.exists():
            try:
                os.replace(self.console_log, self.console_prev_log)
            except OSError as exc:
                self._log.warning("Could not rotate console log: %s", exc)

    def _stop_process(self, proc: subprocess.Popen[bytes]) -> None:
        try:
            self.stop_request_file.write_text("stop\n", encoding="utf-8")
        except OSError as exc:
            self._log.warning("Could not write %s: %s", self.stop_request_file.name, exc)

        try:
            proc.wait(timeout=self.stop_timeout)
        except subprocess.TimeoutExpired:
            self._log.warning(
                "Bot PID %d did not exit within %.1fs; force-killing process tree",
                proc.pid,
                self.stop_timeout,
            )
            _kill_tree(proc.pid, self._log)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        finally:
            self.stop_request_file.unlink(missing_ok=True)

    def _run_loop(self) -> None:
        backoff_index = 0
        last_exit: Optional[int] = self.snapshot().last_exit_code

        while not self._is_stop_requested():
            self._maybe_update_ytdlp()
            if self._is_stop_requested():
                self._set_snapshot(BotStatus.STOPPED, last_exit_code=last_exit)
                return

            self.stop_request_file.unlink(missing_ok=True)
            self._rotate_console_log()

            try:
                log_f = open(self.console_log, "wb")
            except OSError as exc:
                self._log.error("Cannot open %s: %s", self.console_log.name, exc)
                self._set_snapshot(BotStatus.CRASHED, last_exit_code=-1, detail=str(exc))
                return

            try:
                proc = subprocess.Popen(
                    [str(self.python_exe), str(self.bot_script)],
                    cwd=str(self.project_root),
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=WIN_BOT_FLAGS,
                )
            except OSError as exc:
                self._log.error("Failed to launch bot: %s", exc)
                self._set_snapshot(BotStatus.CRASHED, last_exit_code=-1, detail=str(exc))
                return
            finally:
                log_f.close()

            start_mono = self._monotonic()
            self._set_snapshot(
                BotStatus.RUNNING,
                pid=proc.pid,
                started_at=start_mono,
                last_exit_code=last_exit,
            )

            while proc.poll() is None:
                if self._is_stop_requested():
                    self._set_snapshot(
                        BotStatus.RUNNING,
                        pid=proc.pid,
                        started_at=start_mono,
                        last_exit_code=last_exit,
                        detail="Stopping...",
                    )
                    self._stop_process(proc)
                    break
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass

            last_exit = proc.returncode
            uptime = self._monotonic() - start_mono

            if self._is_stop_requested():
                self._set_snapshot(BotStatus.STOPPED, last_exit_code=last_exit)
                return

            if last_exit == 1:
                self._log.error("Bot exited with fatal code 1; not restarting")
                self._set_snapshot(
                    BotStatus.CRASHED,
                    last_exit_code=1,
                    detail="Fatal exit (code 1)",
                )
                return

            if uptime >= self.stable_run_seconds:
                backoff_index = 0
            delay = self.backoff_schedule[min(backoff_index, len(self.backoff_schedule) - 1)]
            backoff_index = min(backoff_index + 1, len(self.backoff_schedule) - 1)

            self._set_snapshot(
                BotStatus.RESTARTING,
                last_exit_code=last_exit,
                next_restart_at=self._monotonic() + delay,
                detail=f"Restarting in {int(delay)}s",
            )
            self._wake_event.wait(delay)

        self._set_snapshot(BotStatus.STOPPED, last_exit_code=last_exit)


class SingleInstance:
    """Single-instance lock + authenticated localhost IPC to wake an existing window."""

    def __init__(self, lock_path: Path = PROJECT_ROOT / "tray.lock") -> None:
        self.lock_path = Path(lock_path)
        self.ipc_path = self.lock_path.with_suffix(".ipc")
        self._handle: Optional[BinaryIO] = None
        self._sock: Optional[socket.socket] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def acquire(self, on_activate: Callable[[], None]) -> bool:
        """Acquire the lock and start the wake-up listener; return False if already held."""
        try:
            handle = open(self.lock_path, "a+b")
        except OSError:
            return False
        try:
            _lock_file(handle)
        except OSError:
            handle.close()
            return False

        self._handle = handle
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        sock.settimeout(0.5)
        self._sock = sock
        port = sock.getsockname()[1]
        nonce = secrets.token_hex(16)
        try:
            self.ipc_path.write_text(f"{port} {nonce}\n", encoding="ascii")
        except OSError:
            pass

        def _serve() -> None:
            expected = f"SHOW {nonce}"
            while not self._stop_event.is_set():
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with conn:
                    conn.settimeout(1.0)
                    try:
                        data = conn.recv(128).decode("ascii", errors="ignore").strip()
                    except OSError:
                        continue
                    if data == expected:
                        try:
                            on_activate()
                        except Exception:
                            pass

        self._thread = threading.Thread(target=_serve, name="tray-ipc", daemon=True)
        self._thread.start()
        return True

    def notify_existing(self) -> bool:
        """Signal an already-running instance to show its window."""
        for _ in range(5):
            try:
                raw = self.ipc_path.read_text(encoding="ascii").strip()
                port_s, nonce = raw.split(" ", 1)
                port = int(port_s)
                with socket.create_connection(("127.0.0.1", port), timeout=2.0) as conn:
                    conn.sendall(f"SHOW {nonce}\n".encode("ascii"))
                return True
            except (OSError, ValueError):
                time.sleep(0.1)
        return False

    def release(self) -> None:
        """Stop the IPC listener and release the lock file."""
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self.ipc_path.unlink(missing_ok=True)
        if self._handle is not None:
            try:
                _unlock_file(self._handle)
            except OSError:
                pass
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
