"""timestamps — show per-line timestamps in terminal.

Display timestamps in a margin or on hover for each line in the
terminal. Shows HH:MM:SS for each line where a linefeed was observed.

Configuration (config.toml):
    [plugins.timestamps]
    enabled = false         # default false; opt-in
    show_margin = true      # default true - show in margin
    margin_width = 80       # width in pixels for timestamp margin
    format = "%H:%M:%S"     # time format (strftime format)
    show_on_hover = true    # show full ISO timestamp on hover
"""

import time

from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QWidget

from qterminator.config import Config
from qterminator.plugin import Plugin


class TimestampMargin(QWidget):
    """Widget that displays timestamps in a margin."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timestamps = []  # list of (y_position, timestamp)
        self._line_height = 16
        self.setFixedWidth(80)

    def set_line_height(self, height):
        self._line_height = height

    def set_timestamps(self, timestamps):
        self._timestamps = timestamps
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(30, 30, 30))

        painter.setPen(QColor(100, 100, 100))
        font = QFont("monospace", 9)
        painter.setFont(font)

        for y_pos, ts in self._timestamps:
            # Convert timestamp to string
            try:
                time_str = time.strftime("%H:%M:%S", time.localtime(ts))
            except (ValueError, OSError):
                time_str = "--:--:--"

            painter.drawText(4, y_pos + self._line_height - 2, time_str)


class TimestampsPlugin(Plugin):
    name = "timestamps"
    description = (
        "Show per-line timestamps in a margin or on hover. "
        "Displays when each line was output."
    )
    version = "0.1"
    capabilities = []

    def __init__(self):
        super().__init__()
        self._window = None
        self._margins = {}  # terminal id -> margin widget
        self._timestamps = {}  # terminal id -> list of (line_number, timestamp)
        self._handles = {}  # terminal id -> (ShadowScreenHandle, listener_callable)
        self._line_height = 16
        self._original_connect = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = cfg.get("plugins", "timestamps", "enabled", default=False)
        if not enabled:
            return

        self._window = app_controller

        # Attach to existing terminals
        tabs = getattr(app_controller, "_tabs", None)
        if tabs is not None:
            for i in range(tabs.count()):
                split = tabs.widget(i)
                for term in split.find_terminals():
                    self._attach_terminal(term)

        # Wrap _connect_terminal for new terminals. Stash the original
        # so deactivate can restore it — otherwise toggling the plugin
        # off and on stacks wrappers and keeps the dead service alive.
        orig = getattr(app_controller, "_connect_terminal", None)
        if orig is not None:
            self._original_connect = orig

            def wrapped(terminal, _orig=orig, _self=self):
                _orig(terminal)
                _self._attach_terminal(terminal)
            app_controller._connect_terminal = wrapped

    def _attach_terminal(self, terminal):
        """Attach to a terminal to track timestamps.

        The shadow-screen listener is set up unconditionally so the
        hover tooltip (``show_on_hover`` config) still has data to
        show even when ``show_margin`` is off. The margin widget is
        only built when ``show_margin`` is true.
        """
        tid = id(terminal)
        if tid in self._handles:
            return

        # Always wire the listener so timestamps accumulate.
        self._setup_listener(terminal)

        cfg = Config()
        show_margin = cfg.get("plugins", "timestamps", "show_margin", default=True)
        if show_margin:
            # Parent the margin to the terminal widget so it doesn't
            # briefly show as a top-level window before deactivate
            # cleans it up. (Margin is not yet inserted into the
            # splitter — this scope-cut is documented in the module
            # header.)
            try:
                parent_term = terminal.term
            except AttributeError:
                parent_term = None
            margin = TimestampMargin(parent_term)
            self._margins[tid] = margin

    def _setup_listener(self, terminal):
        """Set up listener to capture line timestamps. Stores the handle
        + listener so deactivate / detach_terminal can unwind cleanly —
        leaking the handle keeps the ShadowScreen refcount stuck."""
        shadow_screens = getattr(self._window, "shadow_screens", None)
        if shadow_screens is None:
            return

        try:
            handle = shadow_screens.acquire(terminal)

            def on_data(seq, raw):
                self._on_terminal_data(terminal, seq, raw)

            handle.add_listener(on_data)
            self._handles[id(terminal)] = (handle, on_data)
        except Exception:
            pass

    def _on_terminal_data(self, terminal, seq, raw):
        """Called when the terminal receives a PTY chunk.

        Records one timestamp per newline observed in the chunk; a
        bulk burst (``cat bigfile`` → 10k lines in one chunk) used
        to collapse to a single timestamp entry, drifting the
        line-number index relative to what the user sees.
        """
        if not isinstance(raw, (bytes, bytearray)):
            return
        count = raw.count(b"\n")
        if count == 0:
            return

        tid = id(terminal)
        bucket = self._timestamps.setdefault(tid, [])
        now = time.time()
        start = len(bucket)
        for k in range(count):
            bucket.append((start + k, now))

        if tid in self._margins:
            self._update_margin(terminal)

    def _update_margin(self, terminal):
        """Update the timestamp margin display."""
        tid = id(terminal)

        if tid not in self._margins:
            return

        margin = self._margins[tid]
        timestamps = self._timestamps.get(tid, [])

        # Calculate positions
        positions = []
        line_height = self._line_height
        for i, ts in enumerate(timestamps):
            y_pos = i * line_height
            positions.append((y_pos, ts[1]))

        margin.set_timestamps(positions)

    def detach_terminal(self, terminal):
        """Detach from a terminal: drop the listener, release the
        ShadowScreen handle, destroy the margin widget."""
        tid = id(terminal)

        h = self._handles.pop(tid, None)
        if h is not None:
            handle, listener = h
            try:
                handle.remove_listener(listener)
            except Exception:
                pass
            try:
                handle.release()
            except Exception:
                pass

        margin = self._margins.pop(tid, None)
        if margin:
            margin.deleteLater()

        self._timestamps.pop(tid, None)

    def deactivate(self):
        # Release every per-terminal listener/handle before tearing
        # down state. Otherwise the ShadowScreenRegistry stays pinned
        # and the lambda closures keep self alive forever.
        for _tid, (handle, listener) in list(self._handles.items()):
            try:
                handle.remove_listener(listener)
            except Exception:
                pass
            try:
                handle.release()
            except Exception:
                pass
        self._handles.clear()

        for margin in self._margins.values():
            margin.deleteLater()
        self._margins.clear()
        self._timestamps.clear()

        if self._original_connect is not None and self._window is not None:
            try:
                self._window._connect_terminal = self._original_connect
            except AttributeError:
                pass
        self._original_connect = None
        self._window = None
