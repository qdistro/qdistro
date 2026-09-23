"""paste_history — bounded ring of recent clipboard entries.

Watches ``QApplication.clipboard().dataChanged`` and pushes each new
text payload onto a ``collections.deque`` capped at
``MAX_ENTRIES``. The buffer persists to JSON at
``~/.config/qterminator/paste-history.json`` after each change
(write-tmp + os.rename for atomicity); a fresh plugin load restores
the recent ring without any user action.

Secrets opt-out: clipboard payloads that look like obvious
credentials are not stored at all. We're deliberately conservative
here — the heuristic is "looks like a private key / a long
opaque base64 blob with no whitespace" rather than a full DLP
ruleset. Better to skip a real-but-rare match than to persist a
key onto disk.

Picker: a list dialog opened with the configured hotkey
(default ``Ctrl+Shift+H``). Selecting an entry routes through the
existing dangerous-paste-confirmation in
``TerminalWidget._confirm_dangerous_paste`` so a multi-line picker
selection still warns before executing — same posture as a normal
paste."""

import json
import os
import re
import tempfile
from collections import deque

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from qterminator import config as _config_mod
from qterminator.config import Config
from qterminator.plugin import MenuProvider

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    # Long base64-ish blob with no whitespace — likely a token.
    re.compile(r"\A[A-Za-z0-9+/=_\-]{40,}\Z"),
]


def _history_path() -> str:
    return os.path.join(_config_mod.CONFIG_DIR, "paste-history.json")


def looks_like_secret(text: str) -> bool:
    """Return True if ``text`` matches any of the conservative
    secret-shape heuristics. Empty / very short strings always
    return False."""
    if not text or len(text) < 20:
        return False
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return True
    return False


def load_history(path: str | None = None) -> list[str]:
    path = path or _history_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        entries = data.get("entries")
    else:
        entries = data
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, str)]


def save_history(entries: list[str], path: str | None = None) -> None:
    """Atomic save: write to a sibling tmpfile and rename."""
    path = path or _history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # NamedTemporaryFile delete=False so we can rename it after close.
    fd, tmp = tempfile.mkstemp(
        prefix=".paste-history-", suffix=".json",
        dir=os.path.dirname(path),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f)
        os.rename(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class PasteHistoryService:
    """Backing store + clipboard listener. Exposed via
    ``app_controller.paste_history``."""

    MAX_ENTRIES = 50

    def __init__(self, window, max_entries: int = MAX_ENTRIES,
                 path: str | None = None):
        self._window = window
        self._path = path or _history_path()
        self._max = max(1, int(max_entries))
        self._buf: deque[str] = deque(maxlen=self._max)
        self._loaded = False
        self._listening = False
        self._clipboard = None

    def load(self) -> None:
        self._buf = deque(load_history(self._path), maxlen=self._max)
        self._loaded = True

    def attach(self) -> None:
        """Subscribe to QApplication.clipboard() — idempotent."""
        if self._listening:
            return
        self._clipboard = QApplication.clipboard()
        if self._clipboard is None:
            return
        self._clipboard.dataChanged.connect(self._on_clipboard_changed)
        self._listening = True

    def detach(self) -> None:
        if not self._listening or self._clipboard is None:
            self._listening = False
            return
        try:
            self._clipboard.dataChanged.disconnect(self._on_clipboard_changed)
        except (TypeError, RuntimeError):
            pass
        self._listening = False
        self._clipboard = None

    def _on_clipboard_changed(self) -> None:
        if self._clipboard is None:
            return
        text = self._clipboard.text()
        self.push(text)

    def push(self, text: str) -> bool:
        """Insert ``text`` at the head if it's new and not a secret.
        Returns True if accepted, False if filtered. Persists to disk
        on accept."""
        if not text:
            return False
        if looks_like_secret(text):
            return False
        # Dedupe: re-pushing the most-recent entry is a no-op.
        if self._buf and self._buf[-1] == text:
            return False
        # If the text exists earlier, drop the older copy so it
        # bubbles up to the head — same UX as shell history.
        try:
            existing = list(self._buf)
            existing.remove(text)
            self._buf = deque(existing, maxlen=self._max)
        except ValueError:
            pass
        self._buf.append(text)
        try:
            save_history(list(self._buf), self._path)
        except OSError:
            pass
        return True

    @property
    def entries(self) -> list[str]:
        """Newest first."""
        return list(reversed(self._buf))

    def clear(self) -> None:
        self._buf.clear()
        try:
            save_history([], self._path)
        except OSError:
            pass


class PasteHistoryDialog(QDialog):
    """List + preview pane. Double-click or Enter on a row sends it
    back through the dangerous-paste-confirmation path on the active
    terminal."""

    def __init__(self, entries: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Paste History")
        self._entries = entries
        self._selected: str | None = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self._list = QListWidget()
        for entry in self._entries:
            preview = entry.replace("\n", " ↵ ")[:80]
            item = QListWidgetItem(preview or "(empty)")
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)
        self._list.currentRowChanged.connect(self._update_preview)
        self._list.itemDoubleClicked.connect(self._accept_current)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        top.addWidget(self._list, 2)
        top.addWidget(self._preview, 3)
        layout.addLayout(top)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        send = QPushButton("Paste")
        send.clicked.connect(self._accept_current)
        btns.addWidget(cancel)
        btns.addWidget(send)
        layout.addLayout(btns)
        self.resize(700, 400)
        self._update_preview(self._list.currentRow())

    def _update_preview(self, row: int):
        if row < 0:
            self._preview.setPlainText("")
            return
        entry = self._list.item(row).data(Qt.ItemDataRole.UserRole)
        self._preview.setPlainText(entry or "")

    def _accept_current(self, *_):
        item = self._list.currentItem()
        if item is None:
            return
        self._selected = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def selected(self) -> str | None:
        return self._selected


class PasteHistoryPlugin(MenuProvider):
    name = "paste_history"
    description = (
        "Ring buffer of the last 50 clipboard entries, browsable "
        "via a picker dialog."
    )
    version = "0.1"
    category = "Edit"
    capabilities = ["menu_provider", "paste_history"]

    DEFAULT_HOTKEY = "Ctrl+Shift+H"

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: PasteHistoryService | None = None
        self._shortcut = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = bool(cfg.get(
            "plugins", "paste_history", "enabled", default=True,
        ))
        if not enabled:
            return
        max_entries = int(cfg.get(
            "plugins", "paste_history", "max_entries",
            default=PasteHistoryService.MAX_ENTRIES,
        ))
        self._window = app_controller
        self._service = PasteHistoryService(app_controller, max_entries=max_entries)
        self._service.load()
        self._service.attach()
        if not hasattr(app_controller, "paste_history"):
            app_controller.paste_history = self._service
        hotkey = cfg.get(
            "plugins", "paste_history", "hotkey", default=self.DEFAULT_HOTKEY,
        )
        from PyQt6.QtCore import QObject as _QObj
        if hotkey and isinstance(app_controller, _QObj):
            self._shortcut = QShortcut(QKeySequence(hotkey), app_controller)
            self._shortcut.activated.connect(self.open_picker)

    def deactivate(self):
        if self._shortcut is not None:
            try:
                self._shortcut.activated.disconnect()
                self._shortcut.setParent(None)
            except (TypeError, RuntimeError):
                pass
            self._shortcut = None
        if self._service is not None:
            self._service.detach()
        if (self._window is not None
                and getattr(self._window, "paste_history", None) is self._service):
            try:
                del self._window.paste_history
            except AttributeError:
                pass
        self._service = None

    # -- menu --

    def get_menu_items(self, terminal):
        return [
            ("Paste History…", lambda: self.open_picker()),
        ]

    # -- picker --

    def open_picker(self):
        if self._window is None or self._service is None:
            return
        terminal = getattr(self._window, "_active_terminal", None)
        if terminal is None:
            return
        dlg = PasteHistoryDialog(self._service.entries, parent=self._window)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dlg.selected()
        if selected is None:
            return
        # Route through the same paste path the user would normally
        # take — clipboard set + dangerous-paste confirmation.
        cb = QApplication.clipboard()
        cb.setText(selected)
        terminal.paste_clipboard()
