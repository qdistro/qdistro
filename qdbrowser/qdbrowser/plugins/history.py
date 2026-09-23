"""History: persistent visit log + side panel."""

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
    QVBoxLayout,
    QWidget,
)

from qdbrowser.config import CONFIG_DIR
from qdbrowser.plugin import CommandProvider, PageObserver, SidePanelProvider

HISTORY_PATH = os.path.join(CONFIG_DIR, "history.jsonl")
MAX_HISTORY = 10000


class _Store:
    def __init__(self):
        self._records: list = []
        self._load()

    def _load(self):
        if not os.path.exists(HISTORY_PATH):
            return
        try:
            with open(HISTORY_PATH) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("_fixup") == "title":
                        # Apply to the latest visit of this URL.
                        url = rec.get("url")
                        new_title = rec.get("title", "")
                        for r in reversed(self._records):
                            if r.get("url") == url:
                                r["title"] = new_title
                                break
                        continue
                    self._records.append(rec)
        except Exception:
            pass
        if len(self._records) > MAX_HISTORY:
            self._records = self._records[-MAX_HISTORY:]

    def add(self, url: str, title: str = ""):
        if not url or url.startswith("about:") or url.startswith("data:"):
            return
        rec = {"url": url, "title": title, "ts": time.time()}
        self._records.append(rec)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            with open(HISTORY_PATH, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        if len(self._records) > MAX_HISTORY:
            self._records = self._records[-MAX_HISTORY:]

    def all(self):
        return list(reversed(self._records))

    def update_title(self, url: str, title: str) -> None:
        """Append a title-only line for ``url`` to the JSONL log.

        Best-effort: a full disk or permission error is logged at
        warning level (matches ``add()`` semantics) — never propagates
        into the Qt signal-dispatch path that called us.
        """
        if not url:
            return
        rec = {"url": url, "title": title, "ts": time.time(),
               "_fixup": "title"}
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(HISTORY_PATH, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as exc:
            import logging
            logging.getLogger("qdbrowser.history").warning(
                "could not persist history fixup: %s", exc)


class HistoryPanel(QWidget):
    def __init__(self, window, store: _Store):
        super().__init__()
        self._window = window
        self._store = store
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter history…")
        self._filter.textChanged.connect(self._refresh)
        layout.addWidget(self._filter)

        self._list = QListWidget()
        self._list.itemActivated.connect(self._open_current)
        layout.addWidget(self._list, 1)

        row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh)
        row.addWidget(refresh_btn)
        layout.addLayout(row)

        self._refresh()

    def _refresh(self):
        self._list.clear()
        q = self._filter.text().lower().strip()
        for r in self._store.all()[:500]:
            label = f"{r.get('title') or '(no title)'}  —  {r.get('url','')}"
            if q and q not in label.lower():
                continue
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, r)
            self._list.addItem(item)

    def _open_current(self, item):
        r = item.data(Qt.ItemDataRole.UserRole)
        if r and self._window._active_webview:
            self._window._active_webview.navigate(r.get("url", ""))


class HistoryPlugin(SidePanelProvider, PageObserver, CommandProvider):
    name = "history"
    capabilities = ["side_panel", "page_observer", "command_provider"]
    # Visits are written to ``history.jsonl``; never record private
    # (off-the-record) browsing — the window observer wiring honours
    # this flag and skips OTR webviews.
    persistent = True
    panel_id = "history"
    panel_label = "History"
    panel_icon = "H"

    def __init__(self):
        super().__init__()
        self._store = _Store()
        self._panel = None

    def activate(self, window):
        self._window = window

    def build_panel(self, window):
        self._panel = HistoryPanel(window, self._store)
        return self._panel

    def on_title_changed(self, webview, title):
        # Update the most recent matching record's title and persist it
        # (the JSONL log is append-only, so a fixup line wins on read
        # via "last wins" in ``_Store._load``).
        #
        # Defence in depth: the window normally never wires this observer
        # to off-the-record views, but never persist anything for a
        # private webview even if something does reach here.
        if getattr(webview, "is_off_the_record", False):
            return
        if not self._store._records:
            return
        last = self._store._records[-1]
        if last.get("url") == webview.url() and not last.get("title"):
            last["title"] = title
            try:
                self._store.update_title(last["url"], title)
            except OSError as exc:
                import logging
                logging.getLogger("qdbrowser.history").warning(
                    "could not persist title: %s", exc)

    def on_navigation(self, webview, url):
        # Defence in depth: never record private (off-the-record)
        # browsing, even if the observer somehow gets wired to an OTR
        # webview.
        if getattr(webview, "is_off_the_record", False):
            return
        self._store.add(url, webview.title())
        if self._panel:
            self._panel._refresh()

    def get_commands(self, window):
        out = [("Show history panel",
                lambda: window._side_panel.show_panel(self.panel_id))]
        for r in self._store.all()[:30]:
            title = r.get("title") or r.get("url", "")
            url = r.get("url", "")
            out.append((
                f"History: {title}",
                lambda u=url: (
                    window._active_webview.navigate(u)
                    if window._active_webview else None
                ),
            ))
        return out
