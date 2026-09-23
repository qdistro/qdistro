"""Notes panel: Vivaldi-style page-attached + freeform notes."""

from __future__ import annotations

import json
import os
import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from qdbrowser.config import CONFIG_DIR
from qdbrowser.plugin import CommandProvider, SidePanelProvider

NOTES_PATH = os.path.join(CONFIG_DIR, "notes.json")


def _load() -> list:
    if not os.path.exists(NOTES_PATH):
        return []
    try:
        with open(NOTES_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save(notes):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(NOTES_PATH, "w") as f:
        json.dump(notes, f, indent=2)


class NotesPanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self._window = window
        self._notes = _load()
        self._current = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        row = QHBoxLayout()
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self._new_note)
        row.addWidget(new_btn)
        attach_btn = QPushButton("New from page")
        attach_btn.clicked.connect(self._new_note_from_page)
        row.addWidget(attach_btn)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete_current)
        row.addWidget(del_btn)
        layout.addLayout(row)

        self._title = QLineEdit()
        self._title.setPlaceholderText("Title")
        self._title.textChanged.connect(self._on_title_changed)
        layout.addWidget(self._title)

        self._list = QListWidget()
        self._list.itemClicked.connect(self._select)
        layout.addWidget(self._list, 1)

        self._body = QTextEdit()
        self._body.textChanged.connect(self._on_body_changed)
        layout.addWidget(self._body, 2)

        self._refresh()

    def _refresh(self):
        self._list.clear()
        for n in self._notes:
            label = n.get("title") or n.get("body", "")[:40] or "(empty)"
            if n.get("url"):
                label = f"📎 {label}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, n)
            self._list.addItem(item)

    def _new_note(self):
        n = {"title": "Untitled", "body": "", "ts": time.time(), "url": None}
        self._notes.insert(0, n)
        _save(self._notes)
        self._refresh()
        self._list.setCurrentRow(0)
        self._select(self._list.item(0))

    def _new_note_from_page(self):
        wv = getattr(self._window, "_active_webview", None)
        if wv is None:
            return
        n = {"title": wv.title() or wv.url(),
             "body": "", "ts": time.time(),
             "url": wv.url()}
        self._notes.insert(0, n)
        _save(self._notes)
        self._refresh()
        self._list.setCurrentRow(0)
        self._select(self._list.item(0))

    def _delete_current(self):
        if self._current is None:
            return
        # ``is`` is fragile here — Qt may return a copy of the dict from
        # QListWidgetItem.data(UserRole). Compare by content.
        target = self._current
        self._notes = [n for n in self._notes if n != target]
        self._current = None
        _save(self._notes)
        self._refresh()
        self._title.clear()
        self._body.clear()

    def _select(self, item):
        n = item.data(Qt.ItemDataRole.UserRole)
        self._current = n
        self._title.blockSignals(True)
        self._body.blockSignals(True)
        self._title.setText(n.get("title", ""))
        self._body.setPlainText(n.get("body", ""))
        self._title.blockSignals(False)
        self._body.blockSignals(False)

    def _on_title_changed(self, text):
        if self._current is not None:
            self._current["title"] = text
            _save(self._notes)
            self._refresh()

    def _on_body_changed(self):
        if self._current is not None:
            self._current["body"] = self._body.toPlainText()
            _save(self._notes)


class NotesPlugin(SidePanelProvider, CommandProvider):
    name = "notes"
    capabilities = ["side_panel", "command_provider"]
    panel_id = "notes"
    panel_label = "Notes"
    panel_icon = "N"

    def __init__(self):
        super().__init__()
        self._panel = None

    def build_panel(self, window):
        self._panel = NotesPanel(window)
        return self._panel

    def get_commands(self, window):
        return [
            ("New note", lambda: self._panel._new_note()
             if self._panel else None),
            ("New note from current page",
             lambda: self._panel._new_note_from_page()
             if self._panel else None),
            ("Show notes panel",
             lambda: window._side_panel.show_panel(self.panel_id)),
        ]
