"""Quick command palette (Ctrl+E). Fuzzy across tabs, bookmarks, history,
settings, and any CommandProvider plugin.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from qdbrowser.plugin import Plugin

log = logging.getLogger("qdbrowser.command_palette")


class CommandPaletteDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self.setWindowTitle("Quick command")
        self.setModal(True)
        self.resize(640, 420)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self._input = QLineEdit()
        self._input.setPlaceholderText(
            "Type to filter… (commands, tabs, bookmarks, history)")
        self._input.textChanged.connect(self._refilter)
        self._input.returnPressed.connect(self._activate_current)
        layout.addWidget(self._input)

        self._list = QListWidget()
        self._list.itemActivated.connect(self._on_activated)
        layout.addWidget(self._list, 1)

        self._entries: list = []   # list of (label, callback)
        self._gather()
        self._refilter("")

        self._input.setFocus()

    def _gather(self):
        entries = []

        # Tabs
        for i in range(self._window._tabs.count()):
            split = self._window._tabs.widget(i)
            views = split.find_webviews() if hasattr(split, "find_webviews") else []
            title = self._window._tabs.tabText(i)
            for wv in views:
                entries.append((
                    f"Switch tab: {title}",
                    lambda idx=i, w=wv: self._switch_to(idx, w),
                ))

        # Built-in commands
        builtin = [
            ("New tab", lambda: self._window.new_tab()),
            ("Close current tab", self._window._close_current_tab),
            ("Reopen closed tab", self._window._reopen_last_tab),
            ("Reload", self._window._reload),
            ("Hard reload (bypass cache)", self._window._hard_reload),
            ("Back", self._window._go_back),
            ("Forward", self._window._go_forward),
            ("Home", self._window._home),
            ("Find in page", self._window._find_in_page),
            ("Toggle DevTools", self._window._toggle_devtools),
            ("View page source", self._window._view_source),
            ("Toggle fullscreen", self._window._toggle_fullscreen),
            ("Zoom in", lambda: self._window._zoom_step(0.1)),
            ("Zoom out", lambda: self._window._zoom_step(-0.1)),
            ("Reset zoom", lambda: self._window._zoom_set(1.0)),
            ("Toggle reader mode", self._window._toggle_reader),
            ("Save session", self._window.save_session),
            ("Toggle side panel", self._window._toggle_side_panel),
            ("Split right",
             lambda: self._window._split(Qt.Orientation.Horizontal)),
            ("Split down",
             lambda: self._window._split(Qt.Orientation.Vertical)),
            ("Close split", self._window._close_active_split),
            ("Quit", self._window.close),
        ]
        entries.extend(builtin)

        # Plugin-contributed entries
        for prov in self._window.plugins.get_command_providers():
            try:
                for item in prov.get_commands(self._window):
                    if isinstance(item, tuple) and len(item) == 2:
                        entries.append(item)
            except Exception as exc:
                log.warning("provider %s failed: %s",
                            type(prov).__name__, exc)

        self._entries = entries

    def _refilter(self, query: str):
        self._list.clear()
        q = query.lower().strip()
        for label, cb in self._entries:
            if q and not _fuzzy(q, label.lower()):
                continue
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, cb)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)

    def _activate_current(self):
        item = self._list.currentItem()
        if item is None:
            # Fall back to the first item if any.
            if self._list.count():
                item = self._list.item(0)
            else:
                # If query starts with http(s), navigate.
                text = self._input.text()
                if text and self._window._active_webview:
                    self._window._active_webview.navigate(text)
                self.accept()
                return
        self._on_activated(item)

    def _on_activated(self, item):
        cb = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
        try:
            cb()
        except Exception as exc:
            log.warning("action failed: %s", exc)

    def _switch_to(self, tab_idx, wv):
        self._window._tabs.setCurrentIndex(tab_idx)
        self._window._set_active_webview(wv)

    def keyPressEvent(self, event: QKeyEvent):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            # Forward to list.
            self._list.event(event)
            return
        super().keyPressEvent(event)


def _fuzzy(query: str, target: str) -> bool:
    """Plain substring + ordered-char fallback for fuzzy matching."""
    if query in target:
        return True
    i = 0
    for ch in target:
        if i < len(query) and ch == query[i]:
            i += 1
    return i == len(query)


class CommandPalettePlugin(Plugin):
    name = "command_palette"
    description = "Ctrl+E quick command palette."
    capabilities = ["command_palette"]

    def __init__(self):
        super().__init__()
        self._window = None

    def activate(self, window):
        self._window = window
        # Expose a callable so the window can trigger it.
        window.command_palette = self

    def open(self):
        if not self._window:
            return
        dlg = CommandPaletteDialog(self._window)
        dlg.exec()
