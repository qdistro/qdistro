"""Snapshots dialog: pre-save snapshot list + diff + restore."""

from __future__ import annotations

import difflib
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import snapshots


def _unified(old: str, new: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile="snapshot", tofile="current", n=3,
    ))


class SnapshotsDialog(QDialog):
    def __init__(self, root: Path, page_path: Path,
                 current_text: str, on_restore=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Snapshots: {page_path.name}")
        self.resize(900, 600)
        self._root = root
        self._page_path = page_path
        self._current = current_text
        self._on_restore = on_restore
        self._selected: snapshots.Snapshot | None = None

        self._list = QListWidget()
        self._diff = QTextEdit()
        self._diff.setReadOnly(True)
        self._info = QLabel("No snapshots yet.")

        btn_restore = QPushButton("Restore")
        btn_close = QPushButton("Close")
        btn_restore.clicked.connect(self._restore)
        btn_close.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._info, 1)
        btn_row.addWidget(btn_restore)
        btn_row.addWidget(btn_close)

        split = QSplitter()
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("Timestamps (most recent first):"))
        lv.addWidget(self._list)
        split.addWidget(left)
        split.addWidget(self._diff)
        split.setSizes([250, 650])

        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        layout.addLayout(btn_row)

        self._list.currentItemChanged.connect(self._show)
        self.reload()

    def reload(self) -> None:
        self._list.clear()
        self._snapshots = snapshots.list_snapshots(self._root, self._page_path)
        if not self._snapshots:
            self._info.setText("No snapshots yet.")
            return
        self._info.setText(f"{len(self._snapshots)} snapshot(s).")
        for snap in self._snapshots:
            item = QListWidgetItem(snap.iso)
            item.setData(0x0100, snap)
            self._list.addItem(item)
        self._list.setCurrentRow(0)

    def _show(self, current, _prev) -> None:
        if current is None:
            self._diff.setPlainText("")
            self._selected = None
            return
        snap = current.data(0x0100)
        self._selected = snap
        try:
            old = snap.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            old = ""
        self._diff.setPlainText(_unified(old, self._current))

    def _restore(self) -> None:
        if self._selected is None:
            return
        reply = QMessageBox.question(
            self, "Restore snapshot",
            f"Restore snapshot {self._selected.iso}?\n"
            "(A snapshot of the current file will be taken first.)"
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        snapshots.restore(self._root, self._selected)
        if self._on_restore:
            self._on_restore()
        self.accept()
