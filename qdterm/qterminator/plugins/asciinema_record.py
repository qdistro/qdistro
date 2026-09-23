"""asciicast v3 session recorder.

Per-tab terminal session recording in the asciinema v3 format
(newline-delimited JSON: one header line + one event line per chunk).
Output side hangs off ``ShadowScreenRegistry`` so we share the same
``receivedData`` subscription as ``agent_control``. Input side
optionally taps ``QTermWidget.sendData`` (default: off — recording
keystrokes captures passwords).

Format reference: https://docs.asciinema.org/manual/asciicast/v3/

Cast files are append-mode, line-flushed: a process crash mid-record
leaves a parseable file (every line after the header stands alone).

Service is exposed via ``app_controller.asciinema_recorder`` so
``agent_control`` (and any MCP wrapper above it) can drive
``start`` / ``stop`` programmatically.

Configuration (config.toml):

    [plugins.asciinema_record]
    enabled = true                            # default false
    save_dir = "~/Videos/qterminator"
    auto_record = false                       # start on every new tab
"""

import json
import os
import time
from datetime import datetime

from PyQt6.QtWidgets import QFileDialog, QMessageBox

from qterminator.config import Config
from qterminator.plugin import MenuProvider

ASCIICAST_VERSION = 3


def _default_save_dir() -> str:
    home = os.path.expanduser("~")
    return os.path.join(home, "Videos", "qterminator")


def _default_filename() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"qterminator-{ts}.cast"


class Recording:
    """One active recording attached to a terminal."""

    def __init__(self, terminal, path: str, capture_input: bool,
                 shadow_handle, file_obj, started_at: float,
                 cols: int, rows: int):
        self.terminal = terminal
        self.path = path
        self.capture_input = capture_input
        self._handle = shadow_handle
        self._file = file_obj
        self._started = started_at
        self._cols = cols
        self._rows = rows
        self._bytes_written = 0
        self._event_count = 0
        self._input_signal_conn = None
        self._closed = False
        # Listener registered with the ShadowScreen
        self._output_listener = None

    @property
    def started_at(self) -> float:
        return self._started

    @property
    def duration(self) -> float:
        return time.monotonic() - self._started

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    @property
    def event_count(self) -> int:
        return self._event_count

    def _emit(self, code: str, data) -> None:
        if self._closed:
            return
        interval = round(time.monotonic() - self._started, 6)
        # asciicast v3 event tuple: [interval, code, data]
        line = json.dumps([interval, code, data], ensure_ascii=False) + "\n"
        self._file.write(line)
        self._file.flush()
        self._bytes_written += len(line)
        self._event_count += 1

    # listeners
    def _on_output(self, _seq: int, raw: bytes):
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return
        self._emit("o", text)

    def _on_input(self, raw: bytes, _n: int):
        # QTermWidget.sendData signal signature is (const char*, int).
        try:
            text = bytes(raw).decode("utf-8", errors="replace")
        except Exception:
            return
        self._emit("i", text)

    def emit_resize(self, cols: int, rows: int):
        if cols == self._cols and rows == self._rows:
            return
        self._cols, self._rows = cols, rows
        self._emit("r", f"{cols}x{rows}")

    def close(self, exit_status: int | None = None) -> str:
        if self._closed:
            return self.path
        # Emit exit marker before closing — v3 'x' event.
        if exit_status is not None:
            self._emit("x", str(int(exit_status)))
        self._closed = True
        try:
            self._file.flush()
            self._file.close()
        except OSError:
            pass
        return self.path


class AsciinemaRecorderService:
    """Public service exposed via ``app_controller.asciinema_recorder``."""

    def __init__(self, save_dir: str, registry):
        self.save_dir = save_dir
        self._registry = registry
        # tab_id -> Recording
        self._active: dict[int, Recording] = {}

    def is_recording(self, terminal) -> bool:
        return id(terminal) in self._active

    def get_recording(self, terminal) -> Recording | None:
        return self._active.get(id(terminal))

    def active_recordings(self) -> list:
        return list(self._active.values())

    def start(self, terminal, path: str | None = None,
              capture_input: bool = False) -> Recording:
        """Start recording the terminal. Raises RuntimeError if already
        recording. Returns the live :class:`Recording`."""
        tid = id(terminal)
        if tid in self._active:
            raise RuntimeError("already recording")

        if path is None:
            os.makedirs(self.save_dir, exist_ok=True)
            path = os.path.join(self.save_dir, _default_filename())

        qtw = terminal.term
        try:
            cols = int(qtw.screenColumnsCount() or 80) or 80
        except Exception:
            cols = 80
        try:
            metrics = qtw.fontMetrics()
            rows = max(1, qtw.height() // max(1, metrics.height()))
        except Exception:
            rows = 24

        header = {
            "version": ASCIICAST_VERSION,
            "term": {"cols": cols, "rows": rows,
                     "type": os.environ.get("TERM", "xterm-256color")},
            "timestamp": int(time.time()),
            "env": {
                "SHELL": os.environ.get("SHELL", "/bin/bash"),
                "TERM": os.environ.get("TERM", "xterm-256color"),
            },
        }
        # Open append so writes are crash-safe; flush after every line.
        f = open(path, "a", encoding="utf-8")
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
        f.flush()

        # Acquire a ShadowScreen handle for output. Refcounted: shared
        # with agent_control without double-pyte.
        handle = self._registry.acquire(terminal)

        rec = Recording(
            terminal=terminal, path=path, capture_input=capture_input,
            shadow_handle=handle, file_obj=f, started_at=time.monotonic(),
            cols=cols, rows=rows,
        )
        rec._output_listener = lambda seq, raw, r=rec: r._on_output(seq, raw)
        handle.add_listener(rec._output_listener)

        if capture_input:
            # Direct Qt connection to QTermWidget.sendData
            rec._input_signal_conn = qtw.sendData.connect(rec._on_input)

        self._active[tid] = rec
        return rec

    def stop(self, terminal, exit_status: int | None = None) -> str:
        tid = id(terminal)
        rec = self._active.pop(tid, None)
        if rec is None:
            raise RuntimeError("not recording")
        # Tear down listeners / signals.
        if rec._output_listener:
            try:
                rec._handle.remove_listener(rec._output_listener)
            except Exception:
                pass
        if rec._input_signal_conn is not None:
            try:
                terminal.term.sendData.disconnect(rec._input_signal_conn)
            except (TypeError, RuntimeError):
                pass
        rec._handle.release()
        return rec.close(exit_status=exit_status)

    def stop_all(self):
        for rec in list(self._active.values()):
            try:
                self.stop(rec.terminal)
            except Exception:
                pass


class AsciinemaRecordPlugin(MenuProvider):
    name = "asciinema_record"
    description = "Record terminal sessions to asciicast v3 .cast files"
    version = "0.1"
    category = "Export"
    capabilities = ["menu_provider", "asciinema_record"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: AsciinemaRecorderService | None = None
        self._auto_record = False

    def activate(self, app_controller):
        self._window = app_controller
        cfg = Config()
        save_dir = os.path.expanduser(cfg.get(
            "plugins", "asciinema_record", "save_dir",
            default=_default_save_dir(),
        ))
        self._auto_record = bool(cfg.get(
            "plugins", "asciinema_record", "auto_record", default=False,
        ))
        registry = getattr(app_controller, "shadow_screens", None)
        if registry is None:
            # Window predates the registry; refuse to start rather than
            # build a parallel pyte/output subscription.
            raise RuntimeError(
                "asciinema_record requires MainWindow.shadow_screens"
            )
        self._service = AsciinemaRecorderService(save_dir, registry)
        if not hasattr(app_controller, "asciinema_recorder"):
            app_controller.asciinema_recorder = self._service

    def deactivate(self):
        if self._service is not None:
            self._service.stop_all()
        if self._window is not None and \
                getattr(self._window, "asciinema_recorder", None) is self._service:
            try:
                del self._window.asciinema_recorder
            except AttributeError:
                pass
        self._service = None

    # -- menu --

    def get_menu_items(self, terminal):
        if self._service is None:
            return []
        items = []
        if self._service.is_recording(terminal):
            rec = self._service.get_recording(terminal)
            items.append((
                f"Stop Recording ({os.path.basename(rec.path)})",
                lambda t=terminal: self._stop(t),
            ))
        else:
            items.append((
                "Start Recording…",
                lambda t=terminal: self._start_dialog(t),
            ))
            items.append((
                "Start Recording (capture input too)…",
                lambda t=terminal: self._start_dialog(t, capture_input=True),
            ))
        items.append((
            "Open Recordings Folder",
            lambda: self._open_folder(),
        ))
        return items

    def _start_dialog(self, terminal, capture_input: bool = False):
        os.makedirs(self._service.save_dir, exist_ok=True)
        suggested = os.path.join(self._service.save_dir, _default_filename())
        path, _ = QFileDialog.getSaveFileName(
            terminal, "Save Recording", suggested,
            "asciicast files (*.cast);;All files (*)",
        )
        if not path:
            return
        try:
            self._service.start(terminal, path=path,
                                capture_input=capture_input)
        except RuntimeError as e:
            QMessageBox.warning(terminal, "Recording", str(e))

    def _stop(self, terminal):
        try:
            self._service.stop(terminal)
        except RuntimeError as e:
            QMessageBox.warning(terminal, "Recording", str(e))

    def _open_folder(self):
        # Best-effort: xdg-open. Don't fail if it's missing.
        import subprocess
        try:
            subprocess.Popen(
                ["xdg-open", self._service.save_dir],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass
