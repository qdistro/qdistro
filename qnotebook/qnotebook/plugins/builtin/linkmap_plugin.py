"""Bundled plugin: exposes the link map dock under Plugins menu."""

from __future__ import annotations


class Plugin:
    name = "Link Map (plugin)"
    description = "Toggle the link map dock from the Plugins menu."

    def setup(self, window) -> None:
        from PyQt6.QtGui import QAction
        if not hasattr(window, "linkmap_dock"):
            return
        m = getattr(window, "m_plugins", None)
        if m is None:
            return
        act = QAction("Toggle Link &Map", window)
        act.triggered.connect(
            lambda: window.linkmap_dock.setVisible(not window.linkmap_dock.isVisible())
        )
        m.addAction(act)
