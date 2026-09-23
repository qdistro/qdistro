"""Page History viewer: lists git commits touching a page, shows diff,
allows restoring an old revision."""

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

from . import versioning


def render_diff(old: str, new: str) -> str:
    """Unified diff text for display in a read-only QTextEdit."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile="old",
        tofile="new",
        n=3,
    )
    return "".join(diff)


class HistoryViewer(QDialog):
    """Modal: browse git commits for a single page; click → show diff;
    restore button writes the old content back."""

    def __init__(
        self,
        notebook_root: Path,
        page_path: str,
        page_file_rel: str,
        current_text: str,
        on_restore=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"History: {page_path}")
        self.resize(900, 600)
        self._root = notebook_root
        self._page = page_path
        self._rel = page_file_rel
        self._current = current_text
        self._on_restore = on_restore
        self._selected_sha: str | None = None
        self._selected_text: str | None = None

        v = QVBoxLayout(self)
        split = QSplitter(self)
        # Commit list
        left = QWidget(self)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("Commits", left))
        self.list = QListWidget(left)
        self.list.itemSelectionChanged.connect(self._on_selection)
        lv.addWidget(self.list, 1)
        split.addWidget(left)
        # Diff side
        right = QWidget(self)
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("Revision (left) vs current (right)", right))
        diff_split = QSplitter(right)
        self.old_view = QTextEdit(right)
        self.old_view.setReadOnly(True)
        self.new_view = QTextEdit(right)
        self.new_view.setReadOnly(True)
        self.new_view.setPlainText(current_text)
        diff_split.addWidget(self.old_view)
        diff_split.addWidget(self.new_view)
        rv.addWidget(diff_split, 1)
        rv.addWidget(QLabel("Unified diff", right))
        self.diff_view = QTextEdit(right)
        self.diff_view.setReadOnly(True)
        rv.addWidget(self.diff_view, 1)
        split.addWidget(right)
        split.setSizes([260, 640])
        v.addWidget(split, 1)

        btns = QHBoxLayout()
        self.btn_restore = QPushButton("Restore this version", self)
        self.btn_restore.setEnabled(False)
        self.btn_restore.clicked.connect(self._restore)
        self.btn_close = QPushButton("Close", self)
        self.btn_close.clicked.connect(self.accept)
        btns.addWidget(self.btn_restore)
        btns.addStretch(1)
        btns.addWidget(self.btn_close)
        v.addLayout(btns)

        self._populate()

    def _populate(self) -> None:
        commits = versioning.page_history(self._root, self._rel)
        for sha, date, subject in commits:
            item = QListWidgetItem(f"{sha[:8]}  {date}\n{subject}")
            item.setData(0x0100, sha)  # Qt.UserRole
            self.list.addItem(item)

    def commit_count_loaded(self) -> int:
        return self.list.count()

    def _on_selection(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        sha = item.data(0x0100)
        text = versioning.page_at_revision(self._root, str(sha), self._rel)
        if text is None:
            text = ""
        self._selected_sha = str(sha)
        self._selected_text = text
        self.old_view.setPlainText(text)
        self.diff_view.setPlainText(render_diff(text, self._current))
        self.btn_restore.setEnabled(True)

    def selected_text(self) -> str | None:
        return self._selected_text

    def _restore(self) -> None:
        if self._selected_text is None:
            return
        reply = QMessageBox.question(
            self,
            "Restore Version",
            f"Replace current contents of {self._page} with the selected revision?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._on_restore:
            self._on_restore(self._selected_text)
        self.accept()
