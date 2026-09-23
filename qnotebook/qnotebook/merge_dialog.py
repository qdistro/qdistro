"""3-pane merge conflict dialog.

Surfaces a SafeWriter.SaveResult(status="conflict") to the user. Panes show
(base / ours / theirs); the diff view underneath lets the user pick each
hunk. Actions:
  - Accept this hunk from ours / theirs
  - Save as .conflict (write file with git-style markers)
  - Cancel save (editor stays dirty)
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


@dataclass
class Hunk:
    o_start: int
    o_end: int
    a_start: int  # ours
    a_end: int
    b_start: int  # theirs
    b_end: int
    choice: str = "ours"  # "ours" | "theirs"


def compute_hunks(base: bytes, ours: bytes, theirs: bytes) -> list[Hunk]:
    o = base.splitlines(keepends=True)
    a = ours.splitlines(keepends=True)
    b = theirs.splitlines(keepends=True)
    oa = difflib.SequenceMatcher(a=o, b=a, autojunk=False)
    ob = difflib.SequenceMatcher(a=o, b=b, autojunk=False)
    oa_ops = [op for op in oa.get_opcodes() if op[0] != "equal"]
    ob_ops = [op for op in ob.get_opcodes() if op[0] != "equal"]

    # Simpler: emit one hunk per OA op and one per OB op that overlaps.
    hunks: list[Hunk] = []
    for (_t, i1, i2, j1, j2) in oa_ops:
        # Find matching OB op in the same base range (if any)
        b1, b2 = i1, i2
        bj1, bj2 = -1, -1
        for (_t2, k1, k2, l1, l2) in ob_ops:
            if not (k2 <= i1 or k1 >= i2):
                b1 = min(b1, k1)
                b2 = max(b2, k2)
                bj1 = l1 if bj1 < 0 else min(bj1, l1)
                bj2 = max(bj2, l2)
        if bj1 < 0:
            # no theirs edit — skip (ours is trivially accepted)
            continue
        hunks.append(Hunk(
            o_start=b1, o_end=b2,
            a_start=j1, a_end=j2,
            b_start=bj1, b_end=bj2,
        ))
    # Theirs-only hunks
    for (_t, i1, i2, j1, j2) in ob_ops:
        overlaps = False
        for h in hunks:
            if not (h.o_end <= i1 or h.o_start >= i2):
                overlaps = True
                break
        if overlaps:
            continue
        # Find ours equivalent range
        hunks.append(Hunk(
            o_start=i1, o_end=i2,
            a_start=i1, a_end=i1,  # no ours change
            b_start=j1, b_end=j2,
            choice="theirs",
        ))
    hunks.sort(key=lambda h: h.o_start)
    return hunks


def apply_choices(base: bytes, ours: bytes, theirs: bytes,
                  hunks: list[Hunk]) -> bytes:
    o = base.splitlines(keepends=True)
    a = ours.splitlines(keepends=True)
    b = theirs.splitlines(keepends=True)
    out: list[bytes] = []
    cursor = 0
    for h in sorted(hunks, key=lambda x: x.o_start):
        # Copy unchanged lines before this hunk
        out.extend(o[cursor:h.o_start])
        if h.choice == "ours":
            out.extend(a[h.a_start:h.a_end])
        else:
            out.extend(b[h.b_start:h.b_end])
        cursor = h.o_end
    out.extend(o[cursor:])
    return b"".join(out)


def conflict_marker_bytes(base: bytes, ours: bytes, theirs: bytes) -> bytes:
    """Write git-style conflict markers — the whole file wrapped."""
    parts = [b"<<<<<<< ours\n", ours]
    if not ours.endswith(b"\n"):
        parts.append(b"\n")
    parts.extend([b"||||||| base\n", base])
    if not base.endswith(b"\n"):
        parts.append(b"\n")
    parts.extend([b"=======\n", theirs])
    if not theirs.endswith(b"\n"):
        parts.append(b"\n")
    parts.append(b">>>>>>> theirs\n")
    return b"".join(parts)


class MergeDialog(QDialog):
    """3-pane conflict resolver."""

    CHOICE_OURS = "ours"
    CHOICE_THEIRS = "theirs"

    RESULT_MERGED = 1
    RESULT_CONFLICT_FILE = 2
    RESULT_CANCEL = 0

    def __init__(self, base: bytes, ours: bytes, theirs: bytes,
                 page_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Merge conflict: {page_name}" if page_name else "Merge conflict")
        self.resize(1100, 700)
        self._base = base
        self._ours = ours
        self._theirs = theirs
        self._hunks: list[Hunk] = compute_hunks(base, ours, theirs)
        self.result_bytes: bytes = b""
        self.outcome: int = self.RESULT_CANCEL

        # Three panes on top
        panes = QSplitter(Qt.Orientation.Horizontal)
        self._p_base = self._make_pane("Base (common ancestor)", base)
        self._p_ours = self._make_pane("Ours (your edits)", ours)
        self._p_theirs = self._make_pane("Theirs (on disk)", theirs)
        panes.addWidget(self._p_base)
        panes.addWidget(self._p_ours)
        panes.addWidget(self._p_theirs)

        # Hunk list + diff below
        self._hunk_list = QListWidget()
        for i, h in enumerate(self._hunks):
            item = QListWidgetItem(self._hunk_label(i, h))
            self._hunk_list.addItem(item)
        self._hunk_list.currentRowChanged.connect(self._on_hunk_select)

        self._diff = QTextEdit()
        self._diff.setReadOnly(True)

        hunk_split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel(f"{len(self._hunks)} conflicting hunk(s):"))
        lv.addWidget(self._hunk_list)
        row = QHBoxLayout()
        btn_take_ours = QPushButton("Accept ours")
        btn_take_theirs = QPushButton("Accept theirs")
        btn_take_ours.clicked.connect(lambda: self._set_choice(self.CHOICE_OURS))
        btn_take_theirs.clicked.connect(lambda: self._set_choice(self.CHOICE_THEIRS))
        row.addWidget(btn_take_ours)
        row.addWidget(btn_take_theirs)
        lv.addLayout(row)
        hunk_split.addWidget(left)
        hunk_split.addWidget(self._diff)
        hunk_split.setSizes([300, 800])

        main_split = QSplitter(Qt.Orientation.Vertical)
        main_split.addWidget(panes)
        main_split.addWidget(hunk_split)
        main_split.setSizes([350, 350])

        # Bottom actions
        actions = QHBoxLayout()
        btn_save_merged = QPushButton("&Save merged")
        btn_save_conflict = QPushButton("Save as .&conflict")
        btn_cancel = QPushButton("&Cancel")
        btn_save_merged.clicked.connect(self._accept_merged)
        btn_save_conflict.clicked.connect(self._save_conflict_file)
        btn_cancel.clicked.connect(self._cancel)
        actions.addStretch(1)
        actions.addWidget(btn_save_merged)
        actions.addWidget(btn_save_conflict)
        actions.addWidget(btn_cancel)

        top = QVBoxLayout(self)
        top.addWidget(main_split, 1)
        top.addLayout(actions)

        if self._hunks:
            self._hunk_list.setCurrentRow(0)

    # Public for tests
    def set_hunk_choice(self, index: int, choice: str) -> None:
        if 0 <= index < len(self._hunks):
            self._hunks[index].choice = choice
            self._hunk_list.item(index).setText(self._hunk_label(index, self._hunks[index]))

    def merged_bytes(self) -> bytes:
        return apply_choices(self._base, self._ours, self._theirs, self._hunks)

    def conflict_bytes(self) -> bytes:
        return conflict_marker_bytes(self._base, self._ours, self._theirs)

    # Internals
    def _make_pane(self, title: str, data: bytes) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(title))
        t = QTextEdit()
        t.setPlainText(data.decode("utf-8", errors="replace"))
        t.setReadOnly(True)
        v.addWidget(t)
        return w

    def _hunk_label(self, i: int, h: Hunk) -> str:
        return f"#{i+1} base L{h.o_start+1}-{h.o_end}  [{h.choice}]"

    def _on_hunk_select(self, row: int) -> None:
        if row < 0 or row >= len(self._hunks):
            return
        h = self._hunks[row]
        o = self._base.splitlines(keepends=True)
        a = self._ours.splitlines(keepends=True)
        b = self._theirs.splitlines(keepends=True)
        diff = difflib.unified_diff(
            [x.decode("utf-8", errors="replace") for x in a[h.a_start:h.a_end]],
            [x.decode("utf-8", errors="replace") for x in b[h.b_start:h.b_end]],
            fromfile="ours", tofile="theirs", n=0,
        )
        base_block = "".join(x.decode("utf-8", errors="replace")
                             for x in o[h.o_start:h.o_end])
        self._diff.setPlainText(
            f"--- base ---\n{base_block}\n"
            f"--- diff ours vs theirs ---\n{''.join(diff)}"
        )

    def _set_choice(self, choice: str) -> None:
        row = self._hunk_list.currentRow()
        if row < 0:
            return
        self.set_hunk_choice(row, choice)

    def _accept_merged(self) -> None:
        self.result_bytes = self.merged_bytes()
        self.outcome = self.RESULT_MERGED
        self.accept()

    def _save_conflict_file(self) -> None:
        self.result_bytes = self.conflict_bytes()
        self.outcome = self.RESULT_CONFLICT_FILE
        self.accept()

    def _cancel(self) -> None:
        self.outcome = self.RESULT_CANCEL
        self.reject()
