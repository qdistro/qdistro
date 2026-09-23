"""Bookmarks: side-panel list + add/remove/open."""

from __future__ import annotations

import json
import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qdbrowser.config import CONFIG_DIR
from qdbrowser.plugin import CommandProvider, SidePanelProvider

BOOKMARKS_PATH = os.path.join(CONFIG_DIR, "bookmarks.json")


def _load() -> list:
    if not os.path.exists(BOOKMARKS_PATH):
        return []
    try:
        with open(BOOKMARKS_PATH) as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save(bookmarks: list):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(BOOKMARKS_PATH, "w") as f:
        json.dump(bookmarks, f, indent=2)


class BookmarksPanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self._window = window
        self._bookmarks = _load()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter…")
        self._filter.textChanged.connect(self._refresh)
        layout.addWidget(self._filter)

        self._list = QListWidget()
        self._list.itemActivated.connect(self._open_current)
        layout.addWidget(self._list, 1)

        row = QHBoxLayout()
        add_btn = QPushButton("Add current")
        add_btn.clicked.connect(self._add_current)
        row.addWidget(add_btn)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete_current)
        row.addWidget(del_btn)
        layout.addLayout(row)

        self._refresh()

    def all(self):
        return list(self._bookmarks)

    def _refresh(self):
        self._list.clear()
        q = self._filter.text().lower().strip()
        for b in self._bookmarks:
            label = f"{b.get('title','')}  —  {b.get('url','')}"
            if q and q not in label.lower():
                continue
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, b)
            self._list.addItem(item)

    def _add_current(self):
        wv = getattr(self._window, "_active_webview", None)
        if wv is None:
            return
        self._bookmarks.append({"title": wv.title(), "url": wv.url()})
        _save(self._bookmarks)
        self._refresh()

    def _delete_current(self):
        item = self._list.currentItem()
        if not item:
            return
        b = item.data(Qt.ItemDataRole.UserRole)
        self._bookmarks = [x for x in self._bookmarks if x != b]
        _save(self._bookmarks)
        self._refresh()

    def _open_current(self, item):
        b = item.data(Qt.ItemDataRole.UserRole)
        if b and self._window._active_webview:
            self._window._active_webview.navigate(b.get("url", ""))


class BookmarksPlugin(SidePanelProvider, CommandProvider):
    name = "bookmarks"
    description = "Bookmark side panel + command palette entries."
    capabilities = ["side_panel", "command_provider"]
    panel_id = "bookmarks"
    panel_label = "Bookmarks"
    panel_icon = "B"

    def __init__(self):
        super().__init__()
        self._panel = None
        self._window = None

    def activate(self, window):
        self._window = window

    def build_panel(self, window):
        self._panel = BookmarksPanel(window)
        return self._panel

    def get_commands(self, window):
        out = [
            ("Bookmark this page",
             lambda: self._panel._add_current() if self._panel else None),
            ("Show bookmarks panel",
             lambda: window._side_panel.show_panel(self.panel_id)),
        ]
        if self._panel:
            for b in self._panel.all():
                title = b.get("title") or b.get("url", "")
                url = b.get("url", "")
                out.append((
                    f"Open: {title}",
                    lambda u=url: (
                        window._active_webview.navigate(u)
                        if window._active_webview else None
                    ),
                ))
        return out
