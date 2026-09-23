"""Sessions: named save/restore of the tab tree."""

from __future__ import annotations

import json
import os

from PyQt6.QtWidgets import QInputDialog, QMessageBox

from qdbrowser.config import CONFIG_DIR
from qdbrowser.layout import restore_layout, serialize_layout
from qdbrowser.plugin import CommandProvider

SESSIONS_DIR = os.path.join(CONFIG_DIR, "sessions")


def _path(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return os.path.join(SESSIONS_DIR, safe + ".json")


def list_sessions() -> list:
    if not os.path.isdir(SESSIONS_DIR):
        return []
    return [f[:-5] for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")]


def save_session(window, name: str):
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    with open(_path(name), "w") as f:
        json.dump(serialize_layout(window._tabs), f, indent=2)


def load_session(window, name: str) -> bool:
    p = _path(name)
    if not os.path.exists(p):
        return False
    with open(p) as f:
        data = json.load(f)
    while window._tabs.count() > 0:
        window._tabs.removeTab(0)
    restore_layout(window, data)
    return True


class SessionsPlugin(CommandProvider):
    name = "sessions"
    description = "Named session save/restore."
    capabilities = ["command_provider"]

    def __init__(self):
        super().__init__()
        self._window = None

    def activate(self, window):
        self._window = window

    def get_commands(self, window):
        out = [
            ("Save session as…", self._save_as),
            ("Manage sessions…", self._list),
        ]
        for name in list_sessions():
            out.append((f"Load session: {name}",
                        lambda n=name: load_session(window, n)))
        return out

    def _save_as(self):
        name, ok = QInputDialog.getText(self._window, "Save session", "Name:")
        if ok and name.strip():
            save_session(self._window, name.strip())

    def _list(self):
        sessions = list_sessions()
        msg = "\n".join(sessions) or "(no sessions saved)"
        QMessageBox.information(self._window, "Sessions", msg)
