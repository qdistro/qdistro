"""Quick switcher: Ctrl+P modal that fuzzy-matches page paths."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)


def fuzzy_score(query: str, candidate: str) -> int | None:
    """Return a score (higher better) or None for no match.

    Subsequence match required (case-insensitive). Bonuses: prefix,
    contiguous matches, basename-only matches.
    """
    if not query:
        return 0
    q = query.lower()
    c = candidate.lower()
    base = c.rsplit(":", 1)[-1]
    if q in base:
        # Strong basename hit: prefix > contains.
        if base.startswith(q):
            return 10_000 + (1000 - len(candidate))
        return 5_000 + (1000 - len(candidate))
    if q in c:
        return 2_000 + (1000 - len(candidate))
    # Subsequence
    score = 0
    ci = 0
    last_match = -2
    contig = 0
    for ch in q:
        found = c.find(ch, ci)
        if found == -1:
            return None
        if found == last_match + 1:
            contig += 1
            score += 5 + contig * 2
        else:
            contig = 0
            score += 1
        last_match = found
        ci = found + 1
    return score + (500 - len(candidate))


def rank(query: str, candidates: list[str]) -> list[tuple[int, str]]:
    scored: list[tuple[int, str]] = []
    for cand in candidates:
        s = fuzzy_score(query, cand)
        if s is not None:
            scored.append((s, cand))
    scored.sort(key=lambda t: (-t[0], t[1].lower()))
    return scored


class QuickSwitcher(QDialog):
    """Fuzzy-match modal page picker."""

    def __init__(self, pages: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Go to Page")
        self.setModal(True)
        self.resize(480, 360)
        self._pages = list(pages)
        self._chosen: str | None = None
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        self.input = QLineEdit(self)
        self.input.setPlaceholderText("Type to filter pages...")
        self.input.textChanged.connect(self._refresh)
        self.input.returnPressed.connect(self._accept_current)
        v.addWidget(self.input)
        self.list = QListWidget(self)
        self.list.itemActivated.connect(lambda _i: self._accept_current())
        v.addWidget(self.list, 1)
        self._refresh("")

    def chosen(self) -> str | None:
        return self._chosen

    def _refresh(self, query: str) -> None:
        self.list.clear()
        results = rank(query, self._pages) if query else [(0, p) for p in self._pages]
        for _score, p in results[:200]:
            self.list.addItem(QListWidgetItem(p))
        if self.list.count() > 0:
            self.list.setCurrentRow(0)

    def _accept_current(self) -> None:
        item = self.list.currentItem()
        if item is None and self.list.count() > 0:
            item = self.list.item(0)
        if item is None:
            self.reject()
            return
        self._chosen = item.text()
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
