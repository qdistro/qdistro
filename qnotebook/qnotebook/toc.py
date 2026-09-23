"""Table of Contents dock: heading hierarchy of the current page."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class Heading:
    level: int
    text: str
    line: int  # 0-indexed


def parse_headings(md_text: str) -> list[Heading]:
    """Return headings in document order. Skips fenced code blocks."""
    out: list[Heading] = []
    in_fence = False
    for idx, line in enumerate(md_text.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if m:
            out.append(Heading(level=len(m.group(1)), text=m.group(2).strip(), line=idx))
    return out


class TocDock(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.itemActivated.connect(self._on_item_activated)
        self.tree.itemClicked.connect(self._on_item_activated)
        v.addWidget(self.tree, 1)
        self._on_heading_activated: Callable[[int], None] = lambda _line: None

    def set_on_activated(self, cb: Callable[[int], None]) -> None:
        self._on_heading_activated = cb

    def refresh(self, md_text: str) -> None:
        self.tree.clear()
        headings = parse_headings(md_text)
        stack: list[tuple[int, QTreeWidgetItem]] = []
        for h in headings:
            item = QTreeWidgetItem([h.text or "(empty)"])
            item.setData(0, Qt.ItemDataRole.UserRole, h.line)
            while stack and stack[-1][0] >= h.level:
                stack.pop()
            if stack:
                stack[-1][1].addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            stack.append((h.level, item))
        self.tree.expandAll()

    def _on_item_activated(self, item: QTreeWidgetItem, _col: int) -> None:
        line = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(line, int):
            self._on_heading_activated(line)
