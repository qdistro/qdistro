"""Quarantine panel — side-panel UI for the download quarantine queue.

Lists every unreleased quarantined download (file name, source URL, scan
result, timestamp) with per-row **Release** and **Delete** actions:

  - **Release** consults the existing polkit gate
    (``quarantine.check_release_authorized``) and, on approval, opens a
    ``QFileDialog`` for the target directory and moves the file there via
    the existing ``quarantine.release`` helper. Bad-scan files are
    refused by that helper.
  - **Delete** removes the quarantined file + sidecar and drops the DB
    row via ``QuarantineStore.delete``.

All non-Qt logic (list shaping, the release authorize→move pipeline, and
delete) lives in :class:`QuarantineController` so it can be unit-tested
headless. The :class:`QuarantinePanel` widget is a thin Qt shell that
drives the controller and is rebuilt after every mutating action.
"""

from __future__ import annotations

import datetime
import logging
import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qdbrowser import quarantine as quar_mod
from qdbrowser.config import Config
from qdbrowser.plugin import CommandProvider, SidePanelProvider
from qdbrowser.quarantine import QuarantineStore

log = logging.getLogger("qdbrowser.quarantine_panel")


def _format_timestamp(ts: int | None) -> str:
    if not ts:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime(
            "%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "?"


def _row_summary(row: dict) -> dict:
    """Shape one DB row into the fields the UI renders.

    Pure function — no Qt, no I/O — so the list rendering is testable.
    """
    return {
        "id": row.get("id"),
        "filename": row.get("filename") or "(unnamed)",
        "source_url": row.get("source_url") or "",
        "scan_result": row.get("scan_result") or "pending",
        "timestamp": _format_timestamp(row.get("fetched_at")),
        # The release() helper refuses 'bad', 'pending', and scanner
        # 'error' rows. 'skipped' remains releasable because that is an
        # explicit policy result, not a scanner failure.
        "releasable": (row.get("scan_result") or "pending")
        not in ("bad", "pending", "error"),
    }


class QuarantineController:
    """Qt-free brains of the panel.

    Holds a :class:`QuarantineStore` and exposes the three operations the
    UI needs. ``release`` takes the target directory as an argument (the
    widget gets it from a ``QFileDialog``) and the authorization decision
    as a callable (defaults to the real polkit gate) so tests can inject
    a fake without spawning ``pkcheck``.
    """

    def __init__(self, store: QuarantineStore):
        self._store = store

    @property
    def store(self) -> QuarantineStore:
        return self._store

    def list_items(self) -> list:
        """Return UI-shaped summaries of the pending quarantine queue."""
        return [_row_summary(r) for r in self._store.list_pending()]

    def release(self, row_id: int, target_dir: str,
                authorize=None) -> str | None:
        """Release ``row_id`` into ``target_dir`` after the polkit gate.

        Returns the final on-disk path on success, ``None`` if the gate
        denied, the file was bad/unknown, or the move failed. The polkit
        check happens *before* any file is touched: ``quarantine.release``
        is only called with ``authorized=True`` once the gate approves.
        """
        if not target_dir:
            return None
        if authorize is None:
            authorize = quar_mod.check_release_authorized
        if not authorize():
            log.warning("quarantine_panel: release denied by polkit id=%s",
                        row_id)
            return None
        # Authorized: hand off to the existing release helper, which
        # re-sanitizes the filename (defence against path traversal) and
        # refuses bad-scan files.
        return quar_mod.release(self._store, row_id, target_dir,
                                authorized=True)

    def delete(self, row_id: int) -> bool:
        """Delete ``row_id`` (file + sidecar + DB row)."""
        return self._store.delete(row_id)


class QuarantinePanel(QWidget):
    """Side-panel widget listing the quarantine queue."""

    def __init__(self, window, controller: QuarantineController):
        super().__init__()
        self._window = window
        self._controller = controller

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._empty = QLabel("No quarantined downloads.")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color: palette(mid);")
        layout.addWidget(self._empty)

        self._list = QListWidget()
        self._list.setSpacing(2)
        layout.addWidget(self._list, 1)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        layout.addWidget(refresh_btn)

        self.refresh()

    def refresh(self):
        self._list.clear()
        items = []
        error = None
        try:
            items = self._controller.list_items()
        except Exception as exc:
            log.warning("quarantine_panel: list failed: %s", exc)
            error = exc
        if error is not None:
            self._empty.setText(
                "Could not read the quarantine queue.\n"
                "See the log for details.")
        else:
            self._empty.setText("No quarantined downloads.")
        self._empty.setVisible(not items)
        self._list.setVisible(bool(items))
        for summary in items:
            self._add_row(summary)

    def _add_row(self, summary: dict):
        row_widget = QWidget()
        rlayout = QVBoxLayout(row_widget)
        rlayout.setContentsMargins(4, 2, 4, 2)
        rlayout.setSpacing(2)

        scan = summary["scan_result"]
        icon = {"clean": "✓", "bad": "⚠", "pending": "…"}.get(scan, "?")
        name = QLabel(f"🔒 {icon} {summary['filename']}")
        name.setStyleSheet("font-weight: 600;")
        name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        rlayout.addWidget(name)

        meta = QLabel(
            f"{summary['source_url']}\n{scan} · {summary['timestamp']}")
        meta.setWordWrap(True)
        meta.setStyleSheet("color: palette(mid); font-size: 11px;")
        meta.setToolTip(summary["source_url"])
        rlayout.addWidget(meta)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        release_btn = QPushButton("Release")
        release_btn.setEnabled(summary["releasable"])
        if not summary["releasable"]:
            release_btn.setToolTip("Failed scan — cannot be released")
        release_btn.clicked.connect(
            lambda _=False, rid=summary["id"]: self._on_release(rid))
        btns.addWidget(release_btn)

        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(
            lambda _=False, rid=summary["id"], fn=summary["filename"]:
                self._on_delete(rid, fn))
        btns.addWidget(delete_btn)
        rlayout.addLayout(btns)

        item = QListWidgetItem()
        item.setSizeHint(row_widget.sizeHint())
        self._list.addItem(item)
        self._list.setItemWidget(item, row_widget)

    def _default_release_dir(self) -> str:
        cfg = Config()
        return cfg.get(
            "downloads", "release_dir",
            default=cfg.get("general", "downloads_dir",
                            default=os.path.expanduser("~/Downloads")))

    def _on_release(self, row_id):
        target = QFileDialog.getExistingDirectory(
            self, "Release to directory", self._default_release_dir())
        if not target:
            return
        result = self._controller.release(row_id, target)
        if result is None:
            QMessageBox.warning(
                self, "Release failed",
                "The file could not be released. Authorization may have "
                "been denied, or the file failed its scan.")
        self.refresh()

    def _on_delete(self, row_id, filename):
        confirm = QMessageBox.question(
            self, "Delete quarantined file",
            f"Permanently delete '{filename}' from quarantine?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if not self._controller.delete(row_id):
            QMessageBox.warning(
                self, "Delete failed",
                f"'{filename}' could not be removed from quarantine. "
                "See the log for details.")
        self.refresh()


class QuarantinePanelPlugin(SidePanelProvider, CommandProvider):
    name = "quarantine_panel"
    description = "Review and release quarantined downloads."
    capabilities = ["side_panel", "command_provider"]
    panel_id = "quarantine"
    panel_label = "Quarantine"
    panel_icon = "🔒"

    def __init__(self):
        super().__init__()
        self._panel: QuarantinePanel | None = None
        self._window = None
        self._controller: QuarantineController | None = None

    def _open_store(self) -> QuarantineController | None:
        if self._controller is not None:
            return self._controller
        cfg = Config()
        if not cfg.get("downloads", "quarantine_enabled", default=True):
            return None
        try:
            q_dir = cfg.get(
                "downloads", "quarantine_dir",
                default=os.path.expanduser(
                    "~/.local/share/qdbrowser/quarantine"))
            self._controller = QuarantineController(QuarantineStore(q_dir))
        except Exception as exc:
            log.warning("quarantine_panel: store init failed: %s", exc)
            return None
        return self._controller

    def build_panel(self, window):
        self._window = window
        controller = self._open_store()
        if controller is None:
            # Quarantine disabled / unavailable — show an empty panel
            # backed by an in-memory store so the widget still renders.
            placeholder = QWidget()
            QVBoxLayout(placeholder).addWidget(
                QLabel("Download quarantine is disabled."))
            self._panel = placeholder
            return placeholder
        self._panel = QuarantinePanel(window, controller)
        return self._panel

    def get_commands(self, window):
        return [
            ("Show quarantine panel",
             lambda: window._side_panel.show_panel(self.panel_id)),
            ("Refresh quarantine queue",
             lambda: self._panel.refresh()
             if isinstance(self._panel, QuarantinePanel) else None),
        ]
