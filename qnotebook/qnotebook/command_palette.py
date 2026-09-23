"""Command palette: searchable list of all menu actions.

Ctrl+Shift+P opens a modal similar to the quick switcher but listing every
QAction in the menu bar. Selecting one triggers the action."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QKeyEvent
from PyQt6.QtWidgets import (
    QDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from .quickswitcher import rank


def collect_actions(menubar) -> list[QAction]:
    """Walk a QMenuBar and return every non-separator, non-submenu action
    whose text is non-empty."""
    seen_ids: set[int] = set()
    out: list[QAction] = []

    def visit(menu):
        for a in menu.actions():
            if a.isSeparator():
                continue
            if a.menu() is not None:
                visit(a.menu())
                continue
            if id(a) in seen_ids:
                continue
            if not a.text():
                continue
            seen_ids.add(id(a))
            out.append(a)

    for m_action in menubar.actions():
        if m_action.menu() is not None:
            visit(m_action.menu())
    return out


def _label(a: QAction) -> str:
    text = a.text().replace("&", "")
    sc = a.shortcut().toString()
    if sc:
        return f"{text}  ({sc})"
    return text


class CommandPalette(QDialog):
    """Fuzzy-search modal over menu actions."""

    def __init__(self, actions: list[QAction], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Command Palette")
        self.setModal(True)
        self.resize(560, 420)
        self._actions = list(actions)
        self._labels = [_label(a) for a in self._actions]
        self._chosen: QAction | None = None
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        self.input = QLineEdit(self)
        self.input.setPlaceholderText("Type a command...")
        self.input.textChanged.connect(self._refresh)
        self.input.returnPressed.connect(self._accept_current)
        v.addWidget(self.input)
        self.list = QListWidget(self)
        self.list.itemActivated.connect(lambda _i: self._accept_current())
        v.addWidget(self.list, 1)
        self._refresh("")

    def chosen(self) -> QAction | None:
        return self._chosen

    def _refresh(self, query: str) -> None:
        self.list.clear()
        if query:
            ranked = rank(query, self._labels)
        else:
            ranked = [(0, lbl) for lbl in self._labels]
        by_label = {lbl: a for lbl, a in zip(self._labels, self._actions, strict=True)}
        for _score, lbl in ranked[:300]:
            item = QListWidgetItem(lbl)
            self.list.addItem(item)
            item.setData(Qt.ItemDataRole.UserRole, by_label[lbl])
        if self.list.count() > 0:
            self.list.setCurrentRow(0)

    def _accept_current(self) -> None:
        item = self.list.currentItem() or (
            self.list.item(0) if self.list.count() > 0 else None
        )
        if item is None:
            self.reject()
            return
        self._chosen = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            row = self.list.currentRow()
            if e.key() == Qt.Key.Key_Down:
                self.list.setCurrentRow(min(row + 1, self.list.count() - 1))
            else:
                self.list.setCurrentRow(max(row - 1, 0))
            e.accept()
            return
        super().keyPressEvent(e)
