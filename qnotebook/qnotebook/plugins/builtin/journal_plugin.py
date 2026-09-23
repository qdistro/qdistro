"""Bundled plugin: thin wrapper exposing the journal/calendar dock under
the Plugins menu. Built-in calendar wiring stays untouched."""

from __future__ import annotations


class Plugin:
    name = "Journal (plugin)"
    description = "Toggle the calendar/journal dock from the Plugins menu."

    def setup(self, window) -> None:
        from PyQt6.QtGui import QAction
        if not hasattr(window, "calendar_dock"):
            return
        m = getattr(window, "m_plugins", None)
        if m is None:
            return
        act = QAction("Toggle &Journal Calendar", window)
        act.triggered.connect(
            lambda: window.calendar_dock.setVisible(not window.calendar_dock.isVisible())
        )
        m.addAction(act)
