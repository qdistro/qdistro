"""Mouse gestures — hold right-button + drag to trace a gesture.

Strokes are reduced to L/R/U/D direction characters; matched against
``[gestures.bindings]`` in config (e.g. ``"DR" -> "reopen_tab"``).

We install an application-level event filter so we see right-button
events even when the WebView's renderer would otherwise consume them.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QApplication

from qdbrowser.config import Config
from qdbrowser.plugin import Plugin

log = logging.getLogger("qdbrowser.mouse_gestures")


class _GestureFilter(QObject):
    def __init__(self, plugin):
        super().__init__()
        self._plugin = plugin
        self._tracking = False
        self._points: list = []
        self._last_dir: str = ""

    def eventFilter(self, obj, event):  # noqa: N802
        t = event.type()
        if t == QEvent.Type.MouseButtonPress and \
                event.button() == Qt.MouseButton.RightButton:
            self._tracking = True
            self._points = [event.position().toPoint()]
            self._last_dir = ""
            return False
        if t == QEvent.Type.MouseMove and self._tracking:
            self._points.append(event.position().toPoint())
            return False
        if t == QEvent.Type.MouseButtonRelease and \
                event.button() == Qt.MouseButton.RightButton and self._tracking:
            self._tracking = False
            gesture = self._compute_gesture()
            if gesture:
                self._plugin._fire(gesture)
                return True  # swallow the right-click so no context menu
        return False

    def _compute_gesture(self) -> str:
        # Reduce point list to direction transitions.
        if len(self._points) < 6:
            return ""
        dirs: list = []
        threshold = 25
        ax, ay = self._points[0].x(), self._points[0].y()
        for p in self._points[1:]:
            dx = p.x() - ax
            dy = p.y() - ay
            if abs(dx) < threshold and abs(dy) < threshold:
                continue
            if abs(dx) > abs(dy):
                d = "R" if dx > 0 else "L"
            else:
                d = "D" if dy > 0 else "U"
            if not dirs or dirs[-1] != d:
                dirs.append(d)
            ax, ay = p.x(), p.y()
        return "".join(dirs)


class MouseGesturesPlugin(Plugin):
    name = "mouse_gestures"
    description = "Right-click+drag gestures."
    capabilities = ["mouse_gestures"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._filter = None
        self._bindings: dict = {}

    def activate(self, window):
        cfg = Config()
        if not cfg.get("gestures", "enabled", default=True):
            return
        self._window = window
        self._bindings = cfg.get("gestures", "bindings", default={}) or {}
        self._filter = _GestureFilter(self)
        QApplication.instance().installEventFilter(self._filter)

    def deactivate(self):
        if self._filter is not None:
            QApplication.instance().removeEventFilter(self._filter)
            self._filter = None

    def _fire(self, gesture: str):
        action = self._bindings.get(gesture)
        if not action or self._window is None:
            return
        # Look up the bound method lazily; the action name maps to a
        # window method by name, so a window without that method just
        # silently no-ops.
        method_by_action = {
            "back": "_go_back",
            "forward": "_go_forward",
            "reload": "_reload",
            "new_tab": "new_tab",
            "close_tab": "_close_current_tab",
            "reopen_tab": "_reopen_last_tab",
            "next_tab": lambda w: w._cycle_tab(1),
            "prev_tab": lambda w: w._cycle_tab(-1),
            "command_palette": "_open_command_palette",
        }
        target = method_by_action.get(action)
        if target is None:
            return
        try:
            if callable(target):
                target(self._window)
            else:
                fn = getattr(self._window, target, None)
                if fn is not None:
                    fn()
        except Exception as exc:
            log.warning("action %s failed: %s", action, exc)
