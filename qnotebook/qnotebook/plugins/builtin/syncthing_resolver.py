"""Bundled plugin: Syncthing conflict resolver.

Adds a ``Plugins -> Syncthing Conflicts...`` action that opens a list of
currently-present conflict files with per-conflict actions (keep mine,
keep theirs, merge, save both, skip).
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QAction
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

from ...conflict_resolver import ResolverActions
from ...sync_conflict import ConflictFile, scan


class ResolverDialog(QDialog):
    def __init__(self, root: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Syncthing conflicts")
        self.resize(1000, 600)
        self._root = root

        self._list = QListWidget()
        self._view = QTextEdit()
        self._view.setReadOnly(True)

        split = QSplitter()
        left = QWidget()
        lv = QVBoxLayout(left)
        self._info = QLabel("...")
        lv.addWidget(self._info)
        lv.addWidget(self._list)
        split.addWidget(left)
        split.addWidget(self._view)
        split.setSizes([320, 680])

        actions = QHBoxLayout()
        for label, cb in [
            ("Keep &mine", self._keep_mine),
            ("Keep &theirs", self._keep_theirs),
            ("Sa&ve both", self._save_both),
            ("Mer&ge...", self._merge),
            ("&Skip", self._skip),
            ("&Close", self.reject),
        ]:
            b = QPushButton(label)
            b.clicked.connect(cb)
            actions.addWidget(b)

        top = QVBoxLayout(self)
        top.addWidget(split, 1)
        top.addLayout(actions)

        self._list.currentRowChanged.connect(self._show)
        self.reload()

    def reload(self) -> None:
        self._list.clear()
        self._conflicts = scan(self._root)
        self._info.setText(f"{len(self._conflicts)} conflict(s)")
        for cf in self._conflicts:
            try:
                rel = cf.path.relative_to(self._root)
            except ValueError:
                rel = cf.path
            item = QListWidgetItem(f"{rel} — {cf.device} — {cf.iso}")
            item.setData(0x0100, cf)
            self._list.addItem(item)
        if self._conflicts:
            self._list.setCurrentRow(0)

    def _selected(self) -> ConflictFile | None:
        it = self._list.currentItem()
        if it is None:
            return None
        return it.data(0x0100)

    def _show(self, _row: int) -> None:
        cf = self._selected()
        if cf is None:
            self._view.setPlainText("")
            return
        import difflib
        try:
            ours = cf.original.read_text(encoding="utf-8", errors="replace")
        except OSError:
            ours = ""
        theirs = cf.path.read_text(encoding="utf-8", errors="replace")
        diff = "".join(difflib.unified_diff(
            ours.splitlines(keepends=True),
            theirs.splitlines(keepends=True),
            fromfile=str(cf.original.name),
            tofile=cf.path.name,
            n=3,
        ))
        self._view.setPlainText(diff or "(no diff)")

    def _keep_mine(self):
        cf = self._selected()
        if cf:
            ResolverActions.keep_mine(cf, self._root)
            self.reload()

    def _keep_theirs(self):
        cf = self._selected()
        if cf:
            ResolverActions.keep_theirs(cf, self._root)
            self.reload()

    def _save_both(self):
        cf = self._selected()
        if cf:
            ResolverActions.save_both(cf, self._root)
            self.reload()

    def _skip(self):
        row = self._list.currentRow()
        if row + 1 < self._list.count():
            self._list.setCurrentRow(row + 1)

    def _merge(self):
        cf = self._selected()
        if cf is None:
            return
        result = ResolverActions.merge(cf, self._root)
        if result and result.ok:
            QMessageBox.information(self, "Merge", "Clean 3-way merge applied.")
        elif result and result.conflict:
            from ...merge_dialog import MergeDialog
            dlg = MergeDialog(result.base, result.ours, result.theirs,
                              page_name=cf.original.name, parent=self)
            if dlg.exec() and dlg.outcome == MergeDialog.RESULT_MERGED:
                from ... import safe_save as _ss
                _ss.atomic_write(cf.original, dlg.result_bytes)
                try:
                    cf.path.unlink()
                except FileNotFoundError:
                    pass
        self.reload()


class Plugin:
    name = "Syncthing Conflict Resolver"
    description = "Review and resolve *.sync-conflict-* files."

    def setup(self, window) -> None:
        if not hasattr(window, "m_plugins") or window.m_plugins is None:
            return
        act = QAction("Syncthing &Conflicts...", window)

        def open_dialog():
            if window.notebook is None:
                return
            dlg = ResolverDialog(window.notebook.root, parent=window)
            dlg.exec()

        act.triggered.connect(open_dialog)
        window.m_plugins.addAction(act)
