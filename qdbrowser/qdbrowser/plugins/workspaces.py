"""Workspaces: groups of sessions you can hop between."""

from __future__ import annotations

import json
import os

from PyQt6.QtWidgets import QInputDialog, QMessageBox

from qdbrowser.config import CONFIG_DIR
from qdbrowser.layout import restore_layout, serialize_layout
from qdbrowser.plugin import CommandProvider

WORKSPACES_PATH = os.path.join(CONFIG_DIR, "workspaces.json")


def _load() -> dict:
    if not os.path.exists(WORKSPACES_PATH):
        return {}
    try:
        with open(WORKSPACES_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(WORKSPACES_PATH, "w") as f:
        json.dump(data, f, indent=2)


class WorkspacesPlugin(CommandProvider):
    name = "workspaces"
    capabilities = ["command_provider"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._data = _load()

    def activate(self, window):
        self._window = window

    def get_commands(self, window):
        out = [
            ("Workspace: save current as…", self._save_current),
            ("Workspaces: list", self._list),
        ]
        for name in self._data:
            out.append((f"Workspace: switch to {name}",
                        lambda n=name: self._switch_to(n)))
        return out

    def _save_current(self):
        name, ok = QInputDialog.getText(
            self._window, "Workspace", "Workspace name:")
        if not ok or not name.strip():
            return
        self._data[name.strip()] = serialize_layout(self._window._tabs)
        _save(self._data)

    def _switch_to(self, name):
        data = self._data.get(name)
        if not data:
            return
        while self._window._tabs.count() > 0:
            self._window._tabs.removeTab(0)
        restore_layout(self._window, data)

    def _list(self):
        msg = "\n".join(self._data.keys()) or "(no workspaces)"
        QMessageBox.information(self._window, "Workspaces", msg)
