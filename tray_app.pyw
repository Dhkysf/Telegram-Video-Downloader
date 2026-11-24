#!/usr/bin/env python3
"""Windows system-tray controller UI for Telegram Video Downloader Bot."""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Callable, Optional

from tgbot_tray import (
    BotController,
    BotStatus,
    ControllerSnapshot,
    SingleInstance,
    format_uptime,
    stop_standalone_bot,
)

if TYPE_CHECKING:
    from PIL import Image
    import pystray

PROJECT_ROOT: Path = Path(__file__).resolve().parent
SETTINGS_FILE: Path = PROJECT_ROOT / "tray_settings.json"
TRAY_LOG_FILE: Path = PROJECT_ROOT / "tray.log"
APP_ICON_FILE: Path = PROJECT_ROOT / "assets" / "app.ico"
TRAY_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
RELAUNCH_ENV_VAR = "TVB_RELAUNCHED"
RELAUNCH_CLI_TIMEOUT = 60

STATUS_COLORS: dict[BotStatus, str] = {
    BotStatus.RUNNING: "#2ecc71",
    BotStatus.STOPPED: "#e74c3c",
    BotStatus.CRASHED: "#e74c3c",
    BotStatus.RESTARTING: "#f1c40f",
}

logger = logging.getLogger("tray_app")


def is_in_project_venv(
    project_root: Path = PROJECT_ROOT,
    prefix: Optional[Path] = None,
) -> bool:
    """Return True if the current Python interpreter is inside ``<project_root>/.venv``."""
    actual = os.path.normcase(os.path.realpath(prefix or sys.prefix))
    expected = os.path.normcase(os.path.realpath(Path(project_root) / ".venv"))
    return actual == expected


def _show_tk_error(title: str, message: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(title, message, parent=root)
    root.destroy()


def ensure_venv(
    argv: list[str],
    *,
    project_root: Path = PROJECT_ROOT,
    script_path: Optional[Path] = None,
    prefix: Optional[Path] = None,
    popen_fn: Optional[Callable[..., object]] = None,
    run_fn: Optional[Callable[..., subprocess.CompletedProcess[bytes]]] = None,
    show_error_fn: Callable[[str, str], None] = _show_tk_error,
) -> Optional[int]:
    """Re-launch under ``.venv\\Scripts\\python[w].exe`` if not already in the project venv.

    Returns ``None`` when already in the venv (or already relaunched), or an
    exit code to return from ``main()`` when relaunched or missing.
    """
    if is_in_project_venv(project_root, prefix) or os.environ.get(RELAUNCH_ENV_VAR):
        return None

    cli_mode = any(arg in ("--stop", "--selftest") for arg in argv)
    exe_name = "python.exe" if cli_mode else "pythonw.exe"
    root = Path(project_root).resolve()
    venv_exe = (root / ".venv" / "Scripts" / exe_name).resolve()
    if venv_exe.is_file():
        env = os.environ.copy()
        env[RELAUNCH_ENV_VAR] = "1"
        target_script = str((script_path or Path(__file__)).resolve())
        cmd = [str(venv_exe), target_script, *argv]
        if cli_mode:
            runner = run_fn or subprocess.run
            try:
                result = runner(cmd, cwd=str(root), env=env, timeout=RELAUNCH_CLI_TIMEOUT)
            except subprocess.TimeoutExpired:
                if sys.stderr is not None:
                    print(
                        f"Timed out after {RELAUNCH_CLI_TIMEOUT}s waiting for {exe_name}.",
                        file=sys.stderr,
                    )
                return 1
            return int(result.returncode)
        spawner = popen_fn or subprocess.Popen
        spawner(cmd, cwd=str(root), env=env)
        return 0

    msg = (
        f"Could not find .venv\\Scripts\\{exe_name}.\n\n"
        "Create the virtual environment and install dependencies first:\n"
        "  python -m venv .venv\n"
        "  .venv\\Scripts\\python -m pip install -r requirements.txt"
    )
    if cli_mode:
        if sys.stderr is not None:
            print(msg, file=sys.stderr)
    else:
        show_error_fn("Virtual environment missing", msg)
    return 1


class _TokenRedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secret: str) -> None:
        super().__init__(fmt)
        self._secret = secret

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(self._secret, "[REDACTED_TOKEN]")


def configure_tray_logging() -> None:
    """Configure size-rotated ``tray.log`` (safe under pythonw.exe where stderr is None)."""
    handlers: list[logging.Handler] = []
    try:
        handlers.append(
            RotatingFileHandler(
                TRAY_LOG_FILE,
                maxBytes=512 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )
    except OSError:
        pass
    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        handlers.append(logging.StreamHandler())
    if not handlers:
        handlers.append(logging.NullHandler())

    logging.basicConfig(format=TRAY_LOG_FORMAT, level=logging.INFO, handlers=handlers)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        for handler in logging.getLogger().handlers:
            handler.setFormatter(_TokenRedactingFormatter(TRAY_LOG_FORMAT, token))


def load_tray_settings(path: Path = SETTINGS_FILE) -> dict[str, bool]:
    """Load tray settings JSON, returning safe defaults on missing/corrupt files."""
    defaults = {"auto_start": True}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("auto_start"), bool):
            return {"auto_start": data["auto_start"]}
    except (OSError, json.JSONDecodeError):
        pass
    return defaults


def save_tray_settings(settings: dict[str, bool], path: Path = SETTINGS_FILE) -> None:
    """Atomically write tray settings JSON."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Could not save %s: %s", path.name, exc)
        tmp.unlink(missing_ok=True)


def make_tray_icon(color_hex: str) -> "Image.Image":
    """Draw a 64x64 status circle icon at runtime using Pillow."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, 58, 58), fill=color_hex, outline="#1f2937", width=4)
    return image


def ensure_app_icon(path: Path = APP_ICON_FILE) -> Path:
    """Create ``assets/app.ico`` (16/32/48/256 px) if it does not already exist."""
    if path.is_file():
        return path
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((24, 24, 232, 232), fill="#2ecc71", outline="#1f2937", width=16)
    image.save(path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    return path


class TrayApp:
    """Tkinter status window + pystray icon controller."""

    def __init__(
        self,
        instance: Optional[SingleInstance] = None,
        *,
        selftest: bool = False,
        start_hidden: bool = False,
        force_start: bool = False,
    ) -> None:
        self._instance = instance
        self._selftest = selftest
        self._start_hidden = start_hidden
        self._force_start = force_start
        self._closing = False
        self._action_busy = False
        self._settings = load_tray_settings()
        self._auto_start: bool = self._settings["auto_start"]

        self.controller = BotController(
            project_root=PROJECT_ROOT,
            on_state_change=self._on_controller_state,
            logger_inst=logging.getLogger("tray_app.controller"),
        )

        self._icons: dict[str, "Image.Image"] = {
            color: make_tray_icon(color)
            for color in set(STATUS_COLORS.values())
        }

        self.root = tk.Tk()
        self.root.title("Telegram Video Bot")
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(default=str(ensure_app_icon()))
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.root.bind("<Unmap>", self._on_unmap)

        self._build_window()
        self._icon = self._build_tray_icon()
        self._apply_snapshot(self.controller.snapshot())

    def _build_window(self) -> None:
        frame = ttk.Frame(self.root, padding=14)
        frame.grid(row=0, column=0, sticky="nsew")

        status_row = ttk.Frame(frame)
        status_row.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self._dot_canvas = tk.Canvas(
            status_row,
            width=14,
            height=14,
            highlightthickness=0,
            bd=0,
        )
        self._dot_id = self._dot_canvas.create_oval(2, 2, 12, 12, fill="#e74c3c", outline="")
        self._dot_canvas.pack(side="left", padx=(0, 8))

        self._status_var = tk.StringVar(value="Stopped")
        ttk.Label(status_row, textvariable=self._status_var, font=("Segoe UI", 11, "bold")).pack(
            side="left"
        )

        self._detail_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self._detail_var, foreground="#555555").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )

        self._uptime_var = tk.StringVar(value="00:00:00")
        self._exit_var = tk.StringVar(value="-")

        ttk.Label(frame, text="Uptime:").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Label(frame, textvariable=self._uptime_var).grid(row=2, column=1, sticky="w", padx=(8, 0))

        ttk.Label(frame, text="Last exit code:").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Label(frame, textvariable=self._exit_var).grid(row=3, column=1, sticky="w", padx=(8, 0))

        btn_row1 = ttk.Frame(frame)
        btn_row1.grid(row=4, column=0, columnspan=2, pady=(12, 4), sticky="ew")
        self._start_btn = ttk.Button(btn_row1, text="Start", command=self._do_start, width=10)
        self._stop_btn = ttk.Button(btn_row1, text="Stop", command=self._do_stop, width=10)
        self._restart_btn = ttk.Button(btn_row1, text="Restart", command=self._do_restart, width=10)
        self._start_btn.pack(side="left", padx=(0, 6))
        self._stop_btn.pack(side="left", padx=(0, 6))
        self._restart_btn.pack(side="left")

        btn_row2 = ttk.Frame(frame)
        btn_row2.grid(row=5, column=0, columnspan=2, pady=(4, 0), sticky="ew")
        ttk.Button(btn_row2, text="Open log", command=self._open_log, width=15).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(btn_row2, text="Open folder", command=self._open_folder, width=15).pack(
            side="left"
        )

    def _build_tray_icon(self) -> "pystray.Icon":
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda _icon, _item: self._schedule_ui(self.show_window), default=True),
            pystray.MenuItem(
                "Start",
                lambda _icon, _item: self._schedule_ui(self._do_start),
                enabled=lambda _item: self._can_start(),
            ),
            pystray.MenuItem(
                "Stop",
                lambda _icon, _item: self._schedule_ui(self._do_stop),
                enabled=lambda _item: self._can_stop(),
            ),
            pystray.MenuItem(
                "Restart",
                lambda _icon, _item: self._schedule_ui(self._do_restart),
                enabled=lambda _item: not self._action_busy,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open log", lambda _icon, _item: self._schedule_ui(self._open_log)),
            pystray.MenuItem("Open folder", lambda _icon, _item: self._schedule_ui(self._open_folder)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start bot when app opens",
                lambda _icon, _item: self._schedule_ui(self._toggle_auto_start),
                checked=lambda _item: self._auto_start,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda _icon, _item: self._schedule_ui(self._on_exit)),
        )
        return pystray.Icon(
            "telegram_video_bot",
            self._icons[STATUS_COLORS[BotStatus.STOPPED]],
            "Telegram Video Bot - Stopped",
            menu,
        )

    def _schedule_ui(self, fn: Callable[[], None]) -> None:
        if self._closing:
            return
        try:
            self.root.after(0, fn)
        except tk.TclError:
            pass

    def handle_ipc_command(self, command: str) -> None:
        """Dispatch an IPC command from a second launch onto the Tk thread."""
        if command == "SHOW":
            self._schedule_ui(self.show_window)
        elif command == "START":
            self._schedule_ui(self._do_start)
        elif command == "SHOW_START":
            self._schedule_ui(lambda: (self.show_window(), self._do_start()))
        elif command == "STOP":
            self._schedule_ui(self._do_stop)

    def _on_controller_state(self, snap: ControllerSnapshot) -> None:
        self._schedule_ui(lambda: self._apply_snapshot(snap))

    def _can_start(self) -> bool:
        if self._action_busy:
            return False
        return self.controller.snapshot().status in (BotStatus.STOPPED, BotStatus.CRASHED)

    def _can_stop(self) -> bool:
        if self._action_busy:
            return False
        return self.controller.snapshot().status in (BotStatus.RUNNING, BotStatus.RESTARTING)

    def _apply_snapshot(self, snap: ControllerSnapshot) -> None:
        color = STATUS_COLORS[snap.status]
        self._dot_canvas.itemconfig(self._dot_id, fill=color)
        self._status_var.set(snap.status.value)
        self._detail_var.set(snap.detail)
        self._exit_var.set("-" if snap.last_exit_code is None else str(snap.last_exit_code))
        if snap.status == BotStatus.RUNNING and snap.started_at is not None:
            self._uptime_var.set(format_uptime(time.monotonic() - snap.started_at))
        else:
            self._uptime_var.set("00:00:00")

        self._start_btn.configure(state="normal" if self._can_start() else "disabled")
        self._stop_btn.configure(state="normal" if self._can_stop() else "disabled")
        self._restart_btn.configure(state="disabled" if self._action_busy else "normal")

        if not self._selftest:
            self._icon.icon = self._icons[color]
            self._icon.title = f"Telegram Video Bot - {snap.status.value}"
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def _tick(self) -> None:
        if self._closing:
            return
        snap = self.controller.snapshot()
        if snap.status == BotStatus.RUNNING and snap.started_at is not None:
            self._uptime_var.set(format_uptime(time.monotonic() - snap.started_at))
        self.root.after(1000, self._tick)

    def _run_action(self, fn: Callable[[], object]) -> None:
        if self._action_busy or self._closing:
            return
        self._action_busy = True
        self._apply_snapshot(self.controller.snapshot())

        def _worker() -> None:
            try:
                fn()
            finally:
                def _done() -> None:
                    self._action_busy = False
                    self._apply_snapshot(self.controller.snapshot())

                self._schedule_ui(_done)

        threading.Thread(target=_worker, daemon=True).start()

    def _do_start(self) -> None:
        self._run_action(self.controller.start)

    def _do_stop(self) -> None:
        self._run_action(lambda: self.controller.stop(wait=True))

    def _do_restart(self) -> None:
        self._run_action(self.controller.restart)

    def _toggle_auto_start(self) -> None:
        self._auto_start = not self._auto_start
        self._settings["auto_start"] = self._auto_start
        save_tray_settings(self._settings)
        if not self._selftest:
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def _open_log(self) -> None:
        for candidate in (PROJECT_ROOT / "bot.log", PROJECT_ROOT / "bot.console.log"):
            if candidate.is_file():
                os.startfile(str(candidate))
                return
        messagebox.showinfo("Open log", "No log file exists yet.", parent=self.root)

    def _open_folder(self) -> None:
        os.startfile(str(PROJECT_ROOT))

    def _on_unmap(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if event.widget is self.root and not self._closing:
            try:
                if self.root.state() == "iconic":
                    self.root.after(0, self.hide_window)
            except tk.TclError:
                pass

    def hide_window(self) -> None:
        """Hide the window from the taskbar while keeping the tray icon."""
        if not self._closing:
            self.root.withdraw()

    def show_window(self) -> None:
        """Restore and focus the status window."""
        if self._closing:
            return
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        self.root.attributes("-topmost", True)
        self.root.after(50, lambda: self.root.attributes("-topmost", False))

    def _on_exit(self) -> None:
        if self._closing:
            return
        snap = self.controller.snapshot()
        if snap.status in (BotStatus.RUNNING, BotStatus.RESTARTING):
            self.show_window()
            if not messagebox.askyesno(
                "Exit",
                "The bot is currently running. Stop the bot and exit?",
                parent=self.root,
            ):
                return

        self._closing = True
        self._action_busy = True
        self._detail_var.set("Stopping...")
        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="disabled")
        self._restart_btn.configure(state="disabled")

        def _shutdown() -> None:
            self.controller.stop(wait=True)

            def _finish() -> None:
                try:
                    self._icon.stop()
                except Exception:
                    pass
                if self._instance is not None:
                    self._instance.release()
                self.root.destroy()

            try:
                self.root.after(0, _finish)
            except tk.TclError:
                pass

        threading.Thread(target=_shutdown, daemon=True).start()

    def run(self) -> None:
        """Start the tray icon thread, optional auto-start, and Tk mainloop."""
        if self._start_hidden:
            self.root.withdraw()
        threading.Thread(target=self._icon.run, name="pystray", daemon=True).start()
        self.root.after(1000, self._tick)
        if self._force_start or self._auto_start:
            self._do_start()
        self.root.mainloop()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line flags for ``tray_app.pyw``."""
    parser = argparse.ArgumentParser(description="Telegram Video Bot Tray Controller")
    parser.add_argument(
        "--hidden",
        action="store_true",
        help="Start in the system tray without showing the window.",
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="Start the bot immediately when the tray app opens.",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop the running bot (via tray IPC or standalone stop.request) and exit.",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Build the UI objects and exit 0 without starting the bot.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for ``tray_app.pyw``."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    exit_code = ensure_venv(raw_argv)
    if exit_code is not None:
        return exit_code

    args = parse_args(raw_argv)

    if args.selftest:
        app = TrayApp(selftest=True)
        app.root.update_idletasks()
        assert len(app._icons) == 3
        assert app._icon.menu is not None
        assert APP_ICON_FILE.is_file()
        app.root.destroy()
        return 0

    configure_tray_logging()
    instance = SingleInstance()

    if args.stop:
        if instance.notify_existing("STOP"):
            return 0
        stop_standalone_bot(PROJECT_ROOT)
        return 0

    if args.start and args.hidden:
        ipc_cmd = "START"
    elif args.start:
        ipc_cmd = "SHOW_START"
    else:
        ipc_cmd = "SHOW"

    app_holder: list[TrayApp] = []
    if not instance.acquire(lambda cmd: app_holder[0].handle_ipc_command(cmd) if app_holder else None):
        instance.notify_existing(ipc_cmd)
        return 0

    try:
        app = TrayApp(instance=instance, start_hidden=args.hidden, force_start=args.start)
        app_holder.append(app)
        app.run()
    finally:
        instance.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
