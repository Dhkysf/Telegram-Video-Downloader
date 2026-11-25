"""Process supervisor, log rotation, yt-dlp updater, and single-instance IPC."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import threading
import time
from typing import BinaryIO, Callable, Optional

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_STOP_TIMEOUT = 15.0
DEFAULT_READY_TIMEOUT = 120.0
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

_BOT_CMD_RE = re.compile(r'(?:^|[\s"\'/\\])bot\.py(?:$|[\s"\'])', re.IGNORECASE)


def _cmdline_has_bot_py(cmdline: str) -> bool:
    """Return True if ``cmdline`` references ``bot.py`` as a token/path."""
    return bool(_BOT_CMD_RE.search(cmdline))


def _is_python_exe_name(name: str) -> bool:
    """Return True if ``name`` is a Windows Python executable basename."""
    lower = name.lower()
    return lower.startswith("python") and lower.endswith(".exe")


class BotStatus(str, Enum):
    """Lifecycle states shown by the tray UI."""

    STARTING = "Starting"
    RUNNING = "Running"
    STOPPING = "Stopping"
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


def _win32_list_processes() -> list[tuple[int, int, str]]:
    """Return ``[(pid, parent_pid, exe_name), ...]`` using Win32 Toolhelp32."""
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

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    result: list[tuple[int, int, str]] = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                result.append((pe.th32ProcessID, pe.th32ParentProcessID, pe.szExeFile))
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)
    return result


def _win32_query_process(pid: int) -> Optional[tuple[str, str]]:
    """Return ``(exe_path, cmdline)`` for a live Windows process, or None."""
    import ctypes
    from ctypes import wintypes

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.c_void_p),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    ]
    ntdll.NtQueryInformationProcess.restype = wintypes.LONG

    handle = k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None

        buf = ctypes.create_unicode_buffer(4096)
        sz = wintypes.DWORD(4096)
        exe_path = buf.value if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(sz)) else ""

        ret_len = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(ret_len))
        cmdline = ""
        if ret_len.value > 0:
            raw = ctypes.create_string_buffer(ret_len.value)
            status = ntdll.NtQueryInformationProcess(
                handle, 60, raw, ret_len.value, ctypes.byref(ret_len)
            )
            if status == 0:
                us = UNICODE_STRING.from_buffer(raw)
                if us.Buffer and us.Length:
                    cmdline = ctypes.wstring_at(us.Buffer, us.Length // 2)
        return exe_path, cmdline
    finally:
        k32.CloseHandle(handle)


def _is_descendant_pid_win32(candidate_pid: int, ancestor_pid: int) -> bool:
    """Return True if ``candidate_pid`` is ``ancestor_pid`` or a descendant of it."""
    if candidate_pid == ancestor_pid:
        return True
    parent_map = {pid: ppid for pid, ppid, _name in _win32_list_processes()}
    cur = candidate_pid
    visited: set[int] = set()
    while cur > 0 and cur not in visited:
        visited.add(cur)
        parent = parent_map.get(cur)
        if parent is None or parent <= 0 or parent == cur:
            return False
        if parent == ancestor_pid:
            return True
        cur = parent
    return False


def _kill_tree_win32(pid: int) -> None:
    """Terminate ``pid`` and all its descendants using Win32 Toolhelp32."""
    import ctypes
    from ctypes import wintypes

    children: dict[int, list[int]] = {}
    for proc_pid, parent_pid, _name in _win32_list_processes():
        children.setdefault(parent_pid, []).append(proc_pid)

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

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

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
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
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
        self.ready_timeout = ready_timeout
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
        self.ready_file = self.project_root / "bot.ready"
        self.pid_file = self.project_root / "bot.pid"
        self.console_log = self.project_root / "bot.console.log"
        self.console_prev_log = self.project_root / "bot.console.prev.log"
        self.update_log = self.project_root / "bot.update.log"

        self._lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._stop_requested = False
        self._suppress_stopped = False
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
            self._stop_locked(wait=True, suppress_stopped=True)
            if not self._start_locked():
                with self._lock:
                    self._suppress_stopped = False

    def _start_locked(self) -> bool:
        with self._lock:
            if self._supervisor is not None and self._supervisor.is_alive():
                return False
            self._stop_requested = False
            self._suppress_stopped = False
            self._wake_event.clear()
            thread = threading.Thread(
                target=self._run_loop,
                name="bot-supervisor",
                daemon=True,
            )
            self._supervisor = thread
            thread.start()
            return True

    def _stop_locked(self, wait: bool, *, suppress_stopped: bool = False) -> None:
        with self._lock:
            self._suppress_stopped = suppress_stopped
            self._stop_requested = True
            self._wake_event.set()
            sup = self._supervisor
        if wait and sup is not None and sup.is_alive() and sup is not threading.current_thread():
            sup.join(timeout=self.stop_timeout + 15.0)

    def _emit_stopped_if_allowed(self, last_exit: Optional[int]) -> None:
        with self._lock:
            if self._suppress_stopped:
                return
        self._set_snapshot(BotStatus.STOPPED, last_exit_code=last_exit)

    def _is_ready_for_process(self, proc_pid: int) -> bool:
        ready_pid = _read_pid_file(self.ready_file)
        if ready_pid is None:
            return False
        if ready_pid == proc_pid:
            return True
        if os.name == "nt":
            return _is_descendant_pid_win32(ready_pid, proc_pid)
        bot_pid = _read_pid_file(self.pid_file)
        return bot_pid is not None and ready_pid == bot_pid

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
            BotStatus.STARTING,
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
            if self._is_stop_requested():
                self._set_snapshot(
                    BotStatus.STOPPING,
                    last_exit_code=self.snapshot().last_exit_code,
                    detail="Stopping...",
                )
                _kill_tree(proc.pid, self._log)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                break
            if self._monotonic() >= deadline:
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
            self.ready_file.unlink(missing_ok=True)

    def _run_loop(self) -> None:
        backoff_index = 0
        last_exit: Optional[int] = self.snapshot().last_exit_code

        while not self._is_stop_requested():
            self._maybe_update_ytdlp()
            if self._is_stop_requested():
                self._emit_stopped_if_allowed(last_exit)
                return

            self.stop_request_file.unlink(missing_ok=True)
            self.ready_file.unlink(missing_ok=True)
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
            ready_mono: Optional[float] = None
            is_ready = False
            warned_not_ready = False
            self._set_snapshot(
                BotStatus.STARTING,
                pid=proc.pid,
                started_at=None,
                last_exit_code=last_exit,
            )

            while proc.poll() is None:
                if self._is_stop_requested():
                    self._set_snapshot(
                        BotStatus.STOPPING,
                        pid=proc.pid,
                        started_at=ready_mono,
                        last_exit_code=last_exit,
                        detail="Stopping...",
                    )
                    self._stop_process(proc)
                    break
                if not is_ready:
                    if self._is_ready_for_process(proc.pid):
                        is_ready = True
                        ready_mono = self._monotonic()
                        self._set_snapshot(
                            BotStatus.RUNNING,
                            pid=proc.pid,
                            started_at=ready_mono,
                            last_exit_code=last_exit,
                        )
                    elif not warned_not_ready and (self._monotonic() - start_mono) >= self.ready_timeout:
                        warned_not_ready = True
                        self._log.warning(
                            "Bot PID %d did not report ready within %.0fs; still starting",
                            proc.pid,
                            self.ready_timeout,
                        )
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass

            self.ready_file.unlink(missing_ok=True)
            last_exit = proc.returncode
            uptime = self._monotonic() - start_mono

            if self._is_stop_requested():
                self._emit_stopped_if_allowed(last_exit)
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

        self._emit_stopped_if_allowed(last_exit)


class SingleInstance:
    """Single-instance lock + authenticated localhost IPC to wake an existing window."""

    def __init__(self, lock_path: Path = PROJECT_ROOT / "tray.lock") -> None:
        self.lock_path = Path(lock_path)
        self.ipc_path = self.lock_path.with_suffix(".ipc")
        self._handle: Optional[BinaryIO] = None
        self._sock: Optional[socket.socket] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def acquire(self, on_command: Callable[[str], None]) -> bool:
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

        allowed = {"SHOW", "START", "SHOW_START", "STOP"}

        def _serve() -> None:
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
                    parts = data.split(" ", 1)
                    if len(parts) == 2 and parts[0] in allowed and parts[1] == nonce:
                        try:
                            on_command(parts[0])
                        except Exception:
                            pass

        self._thread = threading.Thread(target=_serve, name="tray-ipc", daemon=True)
        self._thread.start()
        return True

    def notify_existing(self, command: str = "SHOW") -> bool:
        """Send an authenticated command to an already-running instance."""
        for _ in range(5):
            try:
                raw = self.ipc_path.read_text(encoding="ascii").strip()
                port_s, nonce = raw.split(" ", 1)
                port = int(port_s)
                with socket.create_connection(("127.0.0.1", port), timeout=2.0) as conn:
                    conn.sendall(f"{command} {nonce}\n".encode("ascii"))
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


def _is_lock_held(lock_path: Path) -> bool:
    """Return True if ``lock_path`` exists and is currently locked by another process."""
    try:
        handle = open(lock_path, "r+b")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        _lock_file(handle)
    except OSError:
        handle.close()
        return True
    try:
        _unlock_file(handle)
    except OSError:
        pass
    handle.close()
    return False


def _read_pid_file(pid_path: Path) -> Optional[int]:
    """Read a positive integer PID from ``pid_path``, or return None."""
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def stop_standalone_bot(
    project_root: Path = PROJECT_ROOT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
) -> bool:
    """Stop a standalone ``bot.py`` (running without the tray app) via ``stop.request``.

    Returns True if a running standalone bot was found and stopped.
    """
    root = Path(project_root).resolve()
    lock_path = root / "bot.lock"
    pid_path = root / "bot.pid"
    stop_file = root / "stop.request"
    if not _is_lock_held(lock_path):
        stop_file.unlink(missing_ok=True)
        pid_path.unlink(missing_ok=True)
        return False

    try:
        stop_file.write_text("stop\n", encoding="utf-8")
    except OSError:
        pass

    deadline = time.monotonic() + stop_timeout
    while time.monotonic() < deadline:
        if not _is_lock_held(lock_path):
            stop_file.unlink(missing_ok=True)
            return True
        time.sleep(0.15)

    if os.name == "nt":
        self_pid = os.getpid()
        killed_pids: list[int] = []

        pid = _read_pid_file(pid_path)
        if pid is not None and pid != self_pid:
            info = _win32_query_process(pid)
            if info is not None and _cmdline_has_bot_py(info[1]):
                _kill_tree(pid, logger)
                killed_pids.append(pid)
            else:
                logger.info("Ignoring stale or non-bot PID %d from %s", pid, pid_path.name)

        if not killed_pids:
            root_norm = os.path.normcase(os.path.realpath(str(root)))
            root_prefix = root_norm.rstrip("\\/") + os.sep
            procs = _win32_list_processes()

            direct_pids: set[int] = set()
            for p_pid, _ppid, name in procs:
                if p_pid == self_pid or not _is_python_exe_name(name):
                    continue
                info = _win32_query_process(p_pid)
                if info is None:
                    continue
                exe, cmd = info
                if not _cmdline_has_bot_py(cmd):
                    continue
                exe_norm = os.path.normcase(os.path.realpath(exe)) if exe else ""
                cmd_norm = os.path.normcase(cmd.replace("/", "\\"))
                if exe_norm.startswith(root_prefix) or root_prefix in cmd_norm:
                    direct_pids.add(p_pid)

            child_pids = [
                p_pid
                for p_pid, ppid, name in procs
                if p_pid != self_pid
                and p_pid not in direct_pids
                and _is_python_exe_name(name)
                and ppid in direct_pids
            ]

            for target_pid in [*child_pids, *sorted(direct_pids)]:
                if _win32_query_process(target_pid) is not None:
                    _kill_tree(target_pid, logger)
                    killed_pids.append(target_pid)

        if killed_pids:
            logger.info(
                "Force-killed standalone bot process tree(s); PIDs: %s",
                ", ".join(str(p) for p in killed_pids),
            )
            release_deadline = time.monotonic() + 2.0
            while time.monotonic() < release_deadline:
                if not _is_lock_held(lock_path):
                    break
                time.sleep(0.05)

    stop_file.unlink(missing_ok=True)
    if not _is_lock_held(lock_path):
        pid_path.unlink(missing_ok=True)
        (root / "bot.ready").unlink(missing_ok=True)
        return True
    return False
