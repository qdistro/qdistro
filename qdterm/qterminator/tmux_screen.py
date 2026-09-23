"""tmux-backed screen snapshots.

``ShadowScreen`` is still the raw-stream owner for listeners and tail()
because tmux does not expose "bytes since sequence N". For tmux-backed
terminals, this adapter delegates the stream API to a normal ShadowScreen
and reads the visible grid from tmux itself via ``capture-pane``.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

from qterminator.shadow_screen import ShadowScreen


class TmuxScreen:
    """ShadowScreen-compatible wrapper using tmux for ``snapshot()``."""

    CACHE_SECONDS = 0.050

    def __init__(self, terminal_widget, session: str):
        self._session = session
        self._fallback = ShadowScreen(terminal_widget)
        self._cache_seq = -1
        self._cache_at = 0.0
        self._cache = None

    # -- lifecycle called by ShadowScreenRegistry --

    def _attach_signal(self):
        self._fallback._attach_signal()

    def _detach_signal(self):
        self._fallback._detach_signal()

    # -- raw stream API delegated to the fallback shadow --

    @property
    def latest_seq(self) -> int:
        return self._fallback.latest_seq

    def tail(self, since: int = 0):
        return self._fallback.tail(since)

    def chunks(self):
        return self._fallback.chunks()

    def add_listener(self, cb: Callable[[int, bytes], None]):
        self._fallback.add_listener(cb)

    def remove_listener(self, cb: Callable[[int, bytes], None]):
        self._fallback.remove_listener(cb)

    def feed(self, text: str):
        self._fallback.feed(text)

    # -- tmux snapshot API --

    def snapshot(self) -> dict:
        now = time.monotonic()
        seq = self.latest_seq
        if (
            self._cache is not None
            and self._cache_seq == seq
            and now - self._cache_at < self.CACHE_SECONDS
        ):
            return self._cache
        try:
            snap = self._tmux_snapshot()
        except Exception:
            snap = self._fallback.snapshot()
        self._cache = snap
        self._cache_seq = seq
        self._cache_at = now
        return snap

    def _tmux_snapshot(self) -> dict:
        pane = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", self._session],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if pane.returncode != 0:
            raise RuntimeError(pane.stderr.strip() or "tmux capture-pane failed")
        meta = subprocess.run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                self._session,
                "#{cursor_x},#{cursor_y},#{pane_width},#{pane_height}",
            ],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if meta.returncode != 0:
            raise RuntimeError(meta.stderr.strip() or "tmux display-message failed")
        x_s, y_s, cols_s, rows_s = (meta.stdout.strip().split(",") + ["0"] * 4)[:4]
        cols = int(cols_s or 0) or 80
        rows = int(rows_s or 0) or 24
        lines = pane.stdout.splitlines()
        if len(lines) < rows:
            lines.extend([""] * (rows - len(lines)))
        lines = [line[:cols].ljust(cols) for line in lines[:rows]]
        return {
            "cols": cols,
            "rows": rows,
            "cursor": {"x": int(x_s or 0), "y": int(y_s or 0)},
            "lines": lines,
            "source": "tmux",
            "tmux_session": self._session,
        }
