"""snippets — named text snippets dispatched into the focused tab.

User-defined entries live in JSON at
``~/.config/qterminator/snippets.json``:

    {
      "snippets": [
        {"name": "ssh prod",
         "text": "ssh -A bastion-prod\\n",
         "tags": ["ssh"]},
        {"name": "kubectx",
         "text": "kubectx ${1:context}\\n",
         "tags": ["k8s"],
         "confirm_send": false}
      ]
    }

Snippets surface as a context-menu submenu "Snippets" and via a
fuzzy-match picker dialog opened with the configured hotkey
(default ``Ctrl+Shift+I``). ``${1:label}`` markers in the snippet
text prompt the user before sending — useful for an SSH host you
type repeatedly with one varying field. By default sends are
guarded by a confirmation dialog (the same posture as a multiline
paste); a snippet may opt out with ``"confirm_send": false`` for
trusted one-liners.
"""

import json
import os
import re

from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from qterminator import config as _config_mod
from qterminator.config import Config
from qterminator.plugin import MenuProvider

PLACEHOLDER_RE = re.compile(r"\$\{(\d+):([^}]*)\}")


def _snippets_path() -> str:
    # Re-read the module attribute each call so test monkeypatching
    # of CONFIG_DIR takes effect.
    return os.path.join(_config_mod.CONFIG_DIR, "snippets.json")


def load_snippets(path: str | None = None) -> list[dict]:
    """Load snippets from JSON. Malformed input returns []."""
    path = path or _snippets_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("snippets")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        text = entry.get("text")
        if not isinstance(name, str) or not isinstance(text, str):
            continue
        out.append({
            "name": name,
            "text": text,
            "tags": list(entry.get("tags") or []),
            "confirm_send": bool(entry.get("confirm_send", True)),
        })
    return out


def expand_placeholders(text: str, fill: callable) -> str | None:
    """Walk ``${N:label}`` markers and substitute via ``fill(label)``.

    ``fill`` is called once per *distinct* placeholder index, in
    document order. If ``fill`` returns None, expansion aborts and
    None is returned to the caller (user-cancelled). The same index
    re-used elsewhere is filled with the value from its first
    occurrence."""
    seen: dict[str, str] = {}
    aborted = False

    def repl(m: re.Match) -> str:
        nonlocal aborted
        if aborted:
            return ""
        idx, label = m.group(1), m.group(2)
        if idx in seen:
            return seen[idx]
        value = fill(label)
        if value is None:
            aborted = True
            return ""
        seen[idx] = value
        return value

    expanded = PLACEHOLDER_RE.sub(repl, text)
    if aborted:
        return None
    return expanded


class SnippetPickerDialog(QDialog):
    """Fuzzy-match picker over the loaded snippets. Double-click or
    Enter selects; Esc cancels. Substring match against name+tags."""

    def __init__(self, snippets: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Snippets")
        self._snippets = snippets
        self._selected: dict | None = None
        self._build_ui()
        self._refilter("")

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Type to filter…")
        self._filter.textChanged.connect(self._refilter)
        layout.addWidget(self._filter)
        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(self._accept_current)
        layout.addWidget(self._list)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Send")
        ok.clicked.connect(self._accept_current)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        layout.addLayout(btns)
        self.resize(420, 320)

    def _refilter(self, needle: str):
        needle = needle.lower().strip()
        self._list.clear()
        for s in self._snippets:
            hay = " ".join([s["name"], *s.get("tags", [])]).lower()
            if not needle or needle in hay:
                item = QListWidgetItem(s["name"])
                item.setData(0x0100, s)  # Qt.ItemDataRole.UserRole
                self._list.addItem(item)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _accept_current(self, *_):
        item = self._list.currentItem()
        if item is None:
            return
        self._selected = item.data(0x0100)
        self.accept()

    def selected(self) -> dict | None:
        return self._selected


def _confirm_send(parent, snippet: dict, expanded: str) -> bool:
    """Ask before sending. ``confirm_send=false`` skips entirely."""
    if not snippet.get("confirm_send", True):
        return True
    preview = expanded[:300]
    if len(expanded) > 300:
        preview += "…"
    res = QMessageBox.question(
        parent, "Send Snippet",
        f"Send snippet '{snippet['name']}'?\n\n{preview}",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    return res == QMessageBox.StandardButton.Yes


def send_snippet(window, terminal, snippet: dict,
                 prompt_fn=None, confirm_fn=None) -> bool:
    """Expand placeholders, confirm, and send.

    ``prompt_fn(label) -> str | None`` is consulted per placeholder
    (default uses QInputDialog). ``confirm_fn(snippet, expanded) ->
    bool`` runs the send-confirmation gate (default is the standard
    Yes/No dialog). Returns True if the snippet was sent."""
    if prompt_fn is None:
        def prompt_fn(label, w=window):
            text, ok = QInputDialog.getText(
                w, "Snippet Parameter", label, text="",
            )
            return text if ok else None
    if confirm_fn is None:
        def confirm_fn(s, expanded, w=window):
            return _confirm_send(w, s, expanded)
    expanded = expand_placeholders(snippet["text"], prompt_fn)
    if expanded is None:
        return False
    if not confirm_fn(snippet, expanded):
        return False
    try:
        terminal.send_text(expanded)
    except Exception:
        return False
    return True


class SnippetsPlugin(MenuProvider):
    name = "snippets"
    description = "Named text snippets dispatched into the focused tab"
    version = "0.1"
    category = "Snippets"
    capabilities = ["menu_provider", "snippets"]

    DEFAULT_HOTKEY = "Ctrl+Shift+I"

    def __init__(self):
        super().__init__()
        self._window = None
        self._snippets: list[dict] = []
        self._shortcut: QShortcut | None = None

    def activate(self, app_controller):
        self._window = app_controller
        cfg = Config()
        enabled = bool(cfg.get(
            "plugins", "snippets", "enabled", default=True,
        ))
        if not enabled:
            return
        self.reload()
        hotkey = cfg.get(
            "plugins", "snippets", "hotkey", default=self.DEFAULT_HOTKEY,
        )
        from PyQt6.QtCore import QObject as _QObj
        if hotkey and isinstance(app_controller, _QObj):
            self._shortcut = QShortcut(QKeySequence(hotkey), app_controller)
            self._shortcut.activated.connect(self.open_picker)
        # Expose service so tests / other plugins can reload.
        if not hasattr(app_controller, "snippets"):
            app_controller.snippets = self

    def deactivate(self):
        if self._shortcut is not None:
            try:
                self._shortcut.activated.disconnect()
                self._shortcut.setParent(None)
            except (TypeError, RuntimeError):
                pass
            self._shortcut = None
        if (self._window is not None
                and getattr(self._window, "snippets", None) is self):
            try:
                del self._window.snippets
            except AttributeError:
                pass

    # -- service --

    def reload(self) -> None:
        self._snippets = load_snippets()

    @property
    def snippets(self) -> list[dict]:
        return list(self._snippets)

    # -- menu --

    def get_menu_items(self, terminal):
        items = []
        if not self._snippets:
            items.append(("(no snippets configured)", None))
            return items
        for s in self._snippets:
            items.append((
                s["name"],
                lambda t=terminal, snip=s: self._send(t, snip),
            ))
        items.append(("---", None))
        items.append(("Open Picker…", lambda: self.open_picker()))
        return items

    # -- picker / send --

    def open_picker(self):
        if self._window is None:
            return
        terminal = getattr(self._window, "_active_terminal", None)
        if terminal is None:
            return
        dlg = SnippetPickerDialog(self._snippets, parent=self._window)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        snip = dlg.selected()
        if snip is not None:
            self._send(terminal, snip)

    def _send(self, terminal, snippet: dict) -> bool:
        return send_snippet(self._window, terminal, snippet)
