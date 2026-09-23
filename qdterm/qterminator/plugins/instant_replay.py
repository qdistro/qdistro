"""instant_replay — scrub backward through the terminal history.

iTerm2's Cmd+Opt+B equivalent: rewind time to see exactly what was
on screen N seconds ago. The plugin overlays the terminal with a
transient view that lets you scrub backward, then Escape/Enter
drops you back to live.

Configuration (config.toml):
    [plugins.instant_replay]
    enabled = false          # default false; opt-in
    hotkey = "Ctrl+Shift+P"  # default
    buffer_size_mb = 1       # default, size in MB

Usage:
- Press the hotkey to enter replay mode
- Arrow keys: Left/Right to step by chunk, Shift+Left/Right to jump ~1s
- Home/End: jump to start/end of buffer
- Escape or Enter: exit replay mode
"""

import time

try:
    import pyte
except ModuleNotFoundError:
    pyte = None

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QPlainTextEdit, QWidget

from qterminator.config import Config
from qterminator.plugin import Plugin

# ---------------------------------------------------------------------------
# Replay state machine
# ---------------------------------------------------------------------------

class ReplayState:
    """Manages the replay state for one terminal."""

    def __init__(self, shadow_handle, terminal):
        self._shadow = shadow_handle
        self._terminal = terminal
        self._chunks: list[tuple[int, bytes, float]] = []  # (seq, bytes, timestamp)
        self._cols = 80
        self._rows = 24
        self._screen = None
        self._stream = None
        self._start_time = time.time()

        # Capture chunks from shadow screen
        self._capture_chunks()

        # Snapshot the seq at the moment we entered replay. As new
        # chunks arrive on the live terminal, ``shadow.latest_seq``
        # grows past this and ``new_chunks_since_start`` reports the
        # delta — used for the status-bar freeze indicator.
        self._live_seq = self._chunks[-1][0] if self._chunks else 0

        # Start at the end (live view); each Left rewinds one chunk.
        self._current_index = max(0, len(self._chunks) - 1)

    def _capture_chunks(self):
        """Copy chunks from the shadow screen for replay.

        ShadowScreen now records ``(seq, bytes, monotonic_ts)`` per
        chunk; we use the monotonic timestamps directly so scrubbing
        by seconds reflects real elapsed time. Test fakes may still
        return 2-tuples — handle both shapes.
        """
        self._chunks = []

        try:
            qtw = self._terminal.term
            self._cols = int(qtw.screenColumnsCount() or 0) or 80
            metrics = qtw.fontMetrics()
            line_h = max(1, metrics.height())
            self._rows = max(1, qtw.height() // line_h)
        except Exception:
            pass

        now = time.monotonic()
        for entry in self._shadow.chunks():
            if len(entry) >= 3:
                seq, raw, ts = entry[0], entry[1], entry[2]
            else:
                seq, raw = entry[0], entry[1]
                ts = now
            self._chunks.append((seq, raw, ts))

    def _rebuild_screen(self, up_to_index: int):
        """Rebuild pyte screen from chunks 0..up_to_index."""
        self._screen = pyte.Screen(self._cols, self._rows)
        self._stream = pyte.Stream(self._screen)

        for i in range(up_to_index + 1):
            _, raw, _ = self._chunks[i]
            try:
                text = raw.decode("utf-8", errors="replace")
                self._stream.feed(text)
            except Exception:
                pass

    def current_text(self) -> str:
        """Get the current replay screen as text."""
        if not self._chunks:
            return ""

        if self._current_index < 0:
            self._current_index = 0
        if self._current_index >= len(self._chunks):
            self._current_index = len(self._chunks) - 1

        self._rebuild_screen(self._current_index)

        if self._screen is None:
            return ""

        # Convert pyte display to text
        lines = self._screen.display
        return "\n".join("".join(lines[i]) for i in range(len(lines)))

    def current_time_offset(self) -> float:
        """Seconds before live tail at the cursor (negative = past).

        Computed from monotonic per-chunk timestamps captured by
        ShadowScreen, so this is exact for chunks within the ring
        buffer (test fakes that omit timestamps return 0.0).
        """
        if not self._chunks:
            return 0.0
        _, _, latest_ts = self._chunks[-1]
        _, _, cur_ts = self._chunks[self._current_index]
        return -(latest_ts - cur_ts)

    def step_back(self):
        """Step back one chunk."""
        if self._current_index > 0:
            self._current_index -= 1

    def step_forward(self):
        """Step forward one chunk."""
        if self._current_index < len(self._chunks) - 1:
            self._current_index += 1

    def jump_back_seconds(self, seconds: float):
        """Walk back to the first chunk at least ``seconds`` before
        the current cursor, using real per-chunk timestamps."""
        if not self._chunks:
            return
        _, _, cur_ts = self._chunks[self._current_index]
        target = cur_ts - seconds
        i = self._current_index
        while i > 0 and self._chunks[i][2] > target:
            i -= 1
        self._current_index = i

    def jump_forward_seconds(self, seconds: float):
        """Walk forward to the first chunk at least ``seconds`` after
        the current cursor, using real per-chunk timestamps."""
        if not self._chunks:
            return
        _, _, cur_ts = self._chunks[self._current_index]
        target = cur_ts + seconds
        i = self._current_index
        last = len(self._chunks) - 1
        while i < last and self._chunks[i][2] < target:
            i += 1
        self._current_index = i

    def jump_to_start(self):
        """Jump to the oldest available chunk."""
        self._current_index = 0

    def jump_to_end(self):
        """Jump to the latest (live) position."""
        self._current_index = len(self._chunks) - 1

    @property
    def at_start(self) -> bool:
        return self._current_index == 0

    @property
    def at_end(self) -> bool:
        return self._current_index >= len(self._chunks) - 1

    @property
    def new_chunks_since_start(self) -> int:
        """Number of new chunks that arrived while in replay mode."""
        return max(0, self._shadow.latest_seq - self._live_seq)


# ---------------------------------------------------------------------------
# Overlay widget
# ---------------------------------------------------------------------------

class ReplayOverlay(QPlainTextEdit):
    """Translucent overlay for instant replay."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        # Style
        self.setStyleSheet(
            "QPlainTextEdit { "
            "background-color: rgba(30, 30, 30, 220); "
            "color: #cccccc; "
            "font-family: monospace; "
            "font-size: 12px; "
            "border: none; "
            "}"
        )

        # Make it fill the parent
        self.setGeometry(parent.geometry())

        # Status bar at bottom
        self._status_bar_height = 24

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep status bar visible at bottom
        self.setViewportMargins(0, 0, 0, self._status_bar_height)

    def update_status(self, state: ReplayState):
        """Update the status bar text. Uses real monotonic per-chunk
        timestamps from ShadowScreen, so ``T-12.4s`` is exact."""
        offset = state.current_time_offset()
        if offset < 0:
            time_str = f"T{offset:+.1f}s"
        else:
            time_str = "live"

        nav = "← / → step  ⇧← / ⇧→ ±1s"
        if state.at_start:
            nav = "← (start)"
        elif state.at_end:
            nav = "→ (live)"

        new_chunks = state.new_chunks_since_start
        new_indicator = f"  (+{new_chunks} new)" if new_chunks > 0 else ""

        self._status_text = f"Replay: {time_str}   {nav}   ⏎ exit{new_indicator}"

    def paintEvent(self, event):
        super().paintEvent(event)
        # Draw status bar in bottom margin
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QColor, QFont, QPainter

        painter = QPainter(self)
        painter.fillRect(
            0, self.height() - self._status_bar_height,
            self.width(), self._status_bar_height,
            QColor(20, 20, 20)
        )

        painter.setPen(QColor("#888888"))
        font = QFont("monospace", 10)
        painter.setFont(font)
        status = getattr(self, '_status_text', "Replay mode")
        painter.drawText(
            QRect(10, self.height() - self._status_bar_height,
                  self.width() - 20, self._status_bar_height),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            status
        )


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class InstantReplayPlugin(Plugin):
    name = "instant_replay"
    description = (
        "Scrub backward through terminal history like a DVR. "
        "Press hotkey to enter replay, arrows to navigate, "
        "Escape/Enter to exit."
    )
    version = "0.1"
    capabilities = []

    def __init__(self):
        super().__init__()
        self._window = None
        self._overlay: ReplayOverlay | None = None
        self._replay_state: ReplayState | None = None
        self._shortcut = None
        self._event_filter = None

    def activate(self, app_controller):
        if pyte is None:
            return
        cfg = Config()
        enabled = cfg.get("plugins", "instant_replay", "enabled", default=False)
        if not enabled:
            return

        self._window = app_controller
        self._setup_shortcut(app_controller)

    def _setup_shortcut(self, app_controller):
        """Set up the hotkey to enter replay mode."""
        try:
            cfg = Config()
            hotkey = cfg.get(
                "plugins", "instant_replay", "hotkey",
                default="Ctrl+Shift+P"
            )

            # Use Qt's built-in parser
            seq = QKeySequence.fromString(hotkey)
            if seq.isEmpty():
                seq = QKeySequence("Ctrl+Shift+P")

            shortcut = QShortcut(seq, app_controller)
            shortcut.activated.connect(lambda: self._enter_replay(app_controller))
            self._shortcut = shortcut
        except Exception as e:
            print(f"Failed to set up instant replay shortcut: {e}")
            self._shortcut = None

    def _enter_replay(self, app_controller):
        """Enter replay mode for the focused terminal."""
        # ``currentWidget`` lives on the QTabWidget at MainWindow._tabs.
        # Calling it on the MainWindow itself raises AttributeError, so
        # the hotkey would crash before reaching replay logic.
        tabs = getattr(app_controller, "_tabs", None)
        if tabs is None:
            return
        focused = tabs.currentWidget()
        if focused is None:
            return

        terminals = list(focused.find_terminals())
        if not terminals:
            return

        terminal = terminals[0]

        shadow_screens = getattr(app_controller, "shadow_screens", None)
        if shadow_screens is None:
            return

        try:
            handle = shadow_screens.acquire(terminal)
        except Exception:
            return

        self._replay_state = ReplayState(handle, terminal)

        term_widget = terminal._term
        self._overlay = ReplayOverlay(term_widget)

        # Tie overlay lifetime to the terminal — if the user closes
        # the tab/split mid-replay, force-exit so we don't paint into
        # a freed parent or call release() on a recycled handle.
        try:
            term_widget.destroyed.connect(self._on_parent_destroyed)
        except Exception:
            pass

        self._overlay.show()

        # The overlay is read-only + WA_TransparentForMouseEvents, so
        # it won't grab focus on its own. Filter on QApplication so
        # arrow/Esc/Enter actually reach _handle_key regardless of
        # which child widget currently has focus.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        else:
            self._overlay.installEventFilter(self)

        self._update_replay_display()

    def _on_parent_destroyed(self, *_):
        """Slot fired when the terminal widget hosting the overlay is
        destroyed. Tear replay down immediately to avoid touching a
        freed C++ object on the next paint/keypress."""
        self._exit_replay()

    def _exit_replay(self):
        """Exit replay mode. Idempotent: safe to call from a parent's
        destroyed signal even if _enter_replay already cleaned up."""
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:
                pass

        if self._overlay is not None:
            try:
                self._overlay.close()
                self._overlay.deleteLater()
            except Exception:
                pass
            self._overlay = None

        if self._replay_state is not None:
            try:
                self._replay_state._shadow.release()
            except Exception:
                pass
            self._replay_state = None

    def _update_replay_display(self):
        """Update the overlay with current replay content."""
        if self._overlay is None or self._replay_state is None:
            return

        text = self._replay_state.current_text()
        self._overlay.setPlainText(text)
        self._overlay.update_status(self._replay_state)

    def eventFilter(self, obj, event):
        """Handle key events in replay mode."""
        if event.type() == event.Type.KeyPress:
            return self._handle_key(event)
        return super().eventFilter(obj, event)

    def _handle_key(self, event) -> bool:
        """Handle a keypress during replay."""
        if self._replay_state is None:
            return False

        key = event.key()
        modifiers = event.modifiers()
        handled = True

        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._exit_replay()
        elif key == Qt.Key.Key_Left:
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                self._replay_state.jump_back_seconds(1.0)
            else:
                self._replay_state.step_back()
            self._update_replay_display()
        elif key == Qt.Key.Key_Right:
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                self._replay_state.jump_forward_seconds(1.0)
            else:
                self._replay_state.step_forward()
            self._update_replay_display()
        elif key == Qt.Key.Key_Home:
            self._replay_state.jump_to_start()
            self._update_replay_display()
        elif key == Qt.Key.Key_End:
            self._replay_state.jump_to_end()
            self._update_replay_display()
        else:
            handled = False

        return handled

    def deactivate(self):
        self._exit_replay()

        if self._shortcut is not None:
            try:
                self._shortcut.deleteLater()
            except Exception:
                pass
            self._shortcut = None

        self._window = None
