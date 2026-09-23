"""Web panels — pin a site to the side panel as a narrow web view
(Vivaldi-style). Config holds the list; this plugin builds one panel per
entry.
"""

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
from qdbrowser.webview import WebView

WEB_PANELS_PATH = os.path.join(CONFIG_DIR, "web_panels.json")


def _load() -> list:
    if not os.path.exists(WEB_PANELS_PATH):
        return []
    try:
        with open(WEB_PANELS_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(panels: list):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(WEB_PANELS_PATH, "w") as f:
        json.dump(panels, f, indent=2)


class WebPanelHost(QWidget):
    def __init__(self, window):
        super().__init__()
        self._window = window
        self._panels = _load()
        self._current_webview = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        top = QHBoxLayout()
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("Add a web panel URL")
        self._url_edit.returnPressed.connect(self._add)
        top.addWidget(self._url_edit, 1)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add)
        top.addWidget(add_btn)
        layout.addLayout(top)

        self._list = QListWidget()
        self._list.setMaximumHeight(120)
        self._list.itemClicked.connect(self._show)
        self._refresh()
        layout.addWidget(self._list)

        # Slot for the active panel webview.
        self._slot = QWidget()
        self._slot_layout = QVBoxLayout(self._slot)
        self._slot_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._slot, 1)

    def _refresh(self):
        self._list.clear()
        for p in self._panels:
            it = QListWidgetItem(p.get("name") or p.get("url"))
            it.setData(Qt.ItemDataRole.UserRole, p)
            self._list.addItem(it)

    def _add(self):
        url = self._url_edit.text().strip()
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
        self._panels.append({"url": url, "name": url})
        _save(self._panels)
        self._url_edit.clear()
        self._refresh()

    def _show(self, item):
        p = item.data(Qt.ItemDataRole.UserRole)
        # Clear slot.
        if self._current_webview is not None:
            self._current_webview.setParent(None)
            self._current_webview.deleteLater()
            self._current_webview = None
        wv = WebView(url=p.get("url", ""))
        self._slot_layout.addWidget(wv)
        self._current_webview = wv


class WebPanelsPlugin(SidePanelProvider, CommandProvider):
    name = "web_panels"
    capabilities = ["side_panel", "command_provider"]
    panel_id = "web_panels"
    panel_label = "Web panels"
    panel_icon = "W"

    def __init__(self):
        super().__init__()
        self._host = None

    def build_panel(self, window):
        self._host = WebPanelHost(window)
        return self._host

    def get_commands(self, window):
        return [("Show web-panels",
                 lambda: window._side_panel.show_panel(self.panel_id))]
