"""Shared shadow VT100 screen + raw-stream buffer per terminal.

A plugin that wants to know "what does the user see right now" — the
rendered character grid, the cursor position — or "what bytes has the
PTY emitted since I last looked" should not implement that on its own.
Doing so means N plugins each subscribing to ``receivedData`` and
running their own ``pyte.Stream`` over the same input, which doesn't
scale and produces inconsistent snapshots across plugins.

This module gives every consumer a *handle* to a single shared
``ShadowScreen`` per terminal. The registry refcounts handles: the
signal connection and the pyte machinery only exist while at least one
consumer is holding a handle, and they're freed automatically when the
last consumer releases.

Typical use from a plugin:

    def activate(self, app_controller):
        self._handle = app_controller.shadow_screens.acquire(terminal)
        self._handle.add_listener(self._on_data)

    def deactivate(self):
        self._handle.remove_listener(self._on_data)
        self._handle.release()

The pyte screen itself is *lazy* — it isn't constructed until the first
``snapshot()`` call. Plugins that only care about the raw byte stream
(via listeners or ``tail``) pay nothing for the VT emulator. After the
first snapshot, every subsequent byte is fed through pyte too, so
state stays consistent.
"""

import time
from collections.abc import Callable


class ShadowScreen:
    """Per-terminal shadow VT100 screen + raw-byte ring buffer.

    Construct via ``ShadowScreenRegistry.acquire``, not directly.
    """

    #: Soft cap on the rolling raw-stream buffer. Anything beyond this is
    #: dropped from the head; ``tail(since)`` with a very old ``since``
    #: will see truncation.
    BUFFER_LIMIT = 1_048_576  # 1 MiB

    def __init__(self, terminal_widget):
        self._term = terminal_widget
        # Sequence counter; first chunk has seq 1.
        self._seq = 0
        # Rolling ring of (seq, raw_bytes, monotonic_ts). Monotonic so
        # the time axis survives wall-clock jumps (NTP, suspend/resume)
        # — consumers like instant_replay use it to scrub by seconds.
        self._stream: list[tuple[int, bytes, float]] = []
        self._stream_bytes = 0
        # Lazy pyte state — constructed on first snapshot().
        self._screen = None
        self._pyte_stream = None
        # Subscriber callbacks. Called as cb(seq, raw_bytes) on each chunk.
        self._listeners: list[Callable[[int, bytes], None]] = []
        # Qt signal connection token (returned by QObject.connect).
        self._signal_conn = None

    # -- lifecycle (called by the registry; not part of the public API) --

    def _attach_signal(self):
        qtw = self._term.term
        self._signal_conn = qtw.receivedData.connect(self._on_received)

    def _detach_signal(self):
        if self._signal_conn is None:
            return
        try:
            self._term.term.receivedData.disconnect(self._signal_conn)
        except (TypeError, RuntimeError):
            # disconnect can raise if the underlying QObject is gone.
            pass
        self._signal_conn = None

    # -- input path --

    def _on_received(self, text: str):
        raw = text.encode("utf-8", errors="replace")
        self._seq += 1
        seq = self._seq
        self._stream.append((seq, raw, time.monotonic()))
        self._stream_bytes += len(raw)
        while self._stream_bytes > self.BUFFER_LIMIT and len(self._stream) > 1:
            old = self._stream.pop(0)
            self._stream_bytes -= len(old[1])
        if self._pyte_stream is not None:
            try:
                self._pyte_stream.feed(text)
            except Exception:
                # A malformed escape sequence shouldn't take the plugin
                # down; pyte will resync on the next valid input.
                pass
        for cb in list(self._listeners):
            try:
                cb(seq, raw)
            except Exception:
                # A bad listener must not poison other listeners.
                pass

    def feed(self, text: str):
        """Public entry point for tests that want to bypass the Qt signal."""
        self._on_received(text)

    # -- pyte (lazy) --

    def _ensure_pyte(self):
        if self._screen is not None:
            return
        import pyte
        qtw = self._term.term
        try:
            cols = int(qtw.screenColumnsCount() or 0) or 80
        except Exception:
            cols = 80
        try:
            metrics = qtw.fontMetrics()
            line_h = max(1, metrics.height())
            rows = max(1, qtw.height() // line_h)
        except Exception:
            rows = 24
        self._screen = pyte.Screen(cols, rows)
        self._pyte_stream = pyte.Stream(self._screen)
        # Replay buffered output so the first snapshot reflects history.
        for _, raw, _ts in self._stream:
            try:
                self._pyte_stream.feed(raw.decode("utf-8", errors="replace"))
            except Exception:
                pass

    # -- public read API --

    @property
    def latest_seq(self) -> int:
        return self._seq

    def tail(self, since: int = 0) -> tuple[int, bytes]:
        """Return ``(latest_seq, concatenated_bytes_after_since)``.

        If ``since`` is older than the buffer's head, the returned bytes
        start from the oldest available chunk — caller must check
        ``latest_seq`` to detect gaps from buffer truncation.
        """
        out = bytearray()
        latest = since
        for s, raw, _ts in self._stream:
            if s > since:
                out.extend(raw)
                latest = s
        return latest, bytes(out)

    def snapshot(self) -> dict:
        """Rendered grid + cursor. Triggers pyte construction on first call."""
        self._ensure_pyte()
        s = self._screen
        return {
            "cols": s.columns,
            "rows": s.lines,
            "cursor": {"x": s.cursor.x, "y": s.cursor.y},
            "lines": list(s.display),
        }

    def chunks(self) -> list[tuple[int, bytes, float]]:
        """Return a shallow copy of the ``(seq, bytes, monotonic_ts)`` ring.

        Use this when you need chunk boundaries with timing (e.g.
        instant_replay scrubs by seconds, command_telemetry stamps
        per-line wall-clock). For "everything since seq N as a single
        blob," use ``tail(since)`` instead.
        """
        return list(self._stream)

    # -- listener subscription --

    def add_listener(self, cb: Callable[[int, bytes], None]):
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[int, bytes], None]):
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass


class ShadowScreenHandle:
    """Refcount-aware handle. Always call ``release()`` exactly once."""

    def __init__(self, registry: "ShadowScreenRegistry", shadow: ShadowScreen):
        self._registry = registry
        self._shadow = shadow
        self._released = False

    @property
    def shadow(self) -> ShadowScreen:
        return self._shadow

    @property
    def released(self) -> bool:
        return self._released

    # Convenience delegates so consumers don't always need ``.shadow.``.
    @property
    def latest_seq(self) -> int:
        return self._shadow.latest_seq

    def tail(self, since: int = 0):
        return self._shadow.tail(since)

    def snapshot(self):
        return self._shadow.snapshot()

    def chunks(self):
        return self._shadow.chunks()

    def add_listener(self, cb):
        self._shadow.add_listener(cb)

    def remove_listener(self, cb):
        self._shadow.remove_listener(cb)

    def release(self):
        if self._released:
            return
        self._released = True
        self._registry._release(self._shadow)


class ShadowScreenRegistry:
    """One ``ShadowScreen`` per terminal, refcounted across plugins.

    The Qt signal handler on ``receivedData`` is attached on the first
    ``acquire`` and detached on the last ``release`` — so a terminal
    that no plugin is observing pays zero overhead, and N plugins
    observing the same terminal share one pyte instance.
    """

    def __init__(self):
        # id(terminal_widget) -> [ShadowScreen, refcount]
        self._shadows: dict[int, list] = {}
        self._tmux_resolver = None

    def set_tmux_resolver(self, resolver):
        """Install a callable returning a tmux session for a terminal.

        Passing ``None`` disables tmux-native snapshots. The registry keeps
        the hook here so consumers can continue to ask for the same handle
        API without knowing whether a terminal is tmux-backed.
        """
        self._tmux_resolver = resolver

    def acquire(self, terminal_widget) -> ShadowScreenHandle:
        tid = id(terminal_widget)
        entry = self._shadows.get(tid)
        if entry is None:
            shadow = None
            if self._tmux_resolver is not None:
                try:
                    session = self._tmux_resolver(terminal_widget)
                except Exception:
                    session = None
                if session:
                    from qterminator.tmux_screen import TmuxScreen
                    shadow = TmuxScreen(terminal_widget, session)
            if shadow is None:
                shadow = ShadowScreen(terminal_widget)
            shadow._attach_signal()
            entry = [shadow, 0]
            self._shadows[tid] = entry
        entry[1] += 1
        return ShadowScreenHandle(self, entry[0])

    def _release(self, shadow: ShadowScreen):
        for tid, entry in list(self._shadows.items()):
            if entry[0] is shadow:
                entry[1] -= 1
                if entry[1] <= 0:
                    shadow._detach_signal()
                    self._shadows.pop(tid)
                return

    def active_count(self) -> int:
        """Number of terminals currently being observed (for diagnostics)."""
        return len(self._shadows)

    def refcount(self, terminal_widget) -> int:
        """Live refcount for ``terminal_widget``, or 0 if none."""
        entry = self._shadows.get(id(terminal_widget))
        return entry[1] if entry else 0

    def shutdown(self):
        """Detach all signal handlers — call from MainWindow tear-down."""
        for entry in list(self._shadows.values()):
            entry[0]._detach_signal()
        self._shadows.clear()
