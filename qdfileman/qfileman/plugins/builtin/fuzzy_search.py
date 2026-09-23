"""Fuzzy file finder plugin for QFileMan.

A right-click → "Fuzzy Find…" opens a single dialog with a search
field and a results list. As the user types, the list is filtered by
a subsequence-match scorer ported in spirit from fzf:

* Every character of the query must appear in order in the candidate.
* Consecutive matches score higher.
* Word-boundary matches score higher (after ``/``, ``-``, ``_``, ``.``).
* Earlier matches score higher than later ones.
* Case-insensitive when the query is all-lowercase, case-sensitive
  otherwise (the standard "smart case" rule).

The walk is bounded by :data:`MAX_ENTRIES` and skips directories
whose name starts with ``.`` so a fuzzy-find in ``$HOME`` doesn't
trip over caches.

If ``fzf`` is available on PATH the user can press a button to hand
off to it for a real terminal experience, but the dialog scorer is
the default — it works everywhere without spawning a process.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


MAX_ENTRIES = 50_000
MAX_RESULTS = 500


# Characters that mark a word boundary for the scoring bonus.
_BOUNDARY = set("/-_. ")


def smart_case_query(query: str) -> tuple[str, bool]:
    """Return ``(query, case_insensitive)`` per the smart-case rule."""
    return (query, query == query.lower())


def fuzzy_score(query: str, candidate: str) -> int | None:
    """Score ``candidate`` against ``query`` or return ``None`` if no match.

    Higher is better. The algorithm is a single left-to-right pass; it
    does not produce optimal alignments (fzf has a Smith-Waterman
    variant for that) but it's predictable and fast enough for the tens
    of thousands of entries we expect.
    """
    if not query:
        return 0
    q, ci = smart_case_query(query)
    if ci:
        haystack = candidate.lower()
    else:
        haystack = candidate

    score = 0
    prev_match = -2  # so the first match never counts as consecutive
    qi = 0
    for hi, ch in enumerate(haystack):
        if qi >= len(q):
            break
        if ch == q[qi]:
            bonus = 0
            if hi == 0 or candidate[hi - 1] in _BOUNDARY:
                bonus += 10   # word-boundary match
            if hi == prev_match + 1:
                bonus += 5    # consecutive match
            # Mild penalty for matches deep in the string so prefix
            # matches outrank suffix matches.
            score += 20 + bonus - min(hi, 20)
            prev_match = hi
            qi += 1

    if qi < len(q):
        return None
    return score


def rank_candidates(query: str, candidates: Iterable[str]) -> list[tuple[int, str]]:
    """Return scored, sorted ``(score, path)`` pairs, best first."""
    scored: list[tuple[int, str]] = []
    for c in candidates:
        s = fuzzy_score(query, os.path.basename(c))
        if s is not None:
            scored.append((s, c))
    scored.sort(key=lambda p: (-p[0], p[1]))
    return scored[:MAX_RESULTS]


def walk_files(root: str, *, max_entries: int = MAX_ENTRIES,
               include_hidden: bool = False) -> list[str]:
    """Return up to ``max_entries`` filesystem paths under ``root``.

    Skips ``.`` directories by default. Stops as soon as the cap is
    hit; the order matches :func:`os.walk` (depth-first, lexicographic
    inside a directory if the OS gives us that — we don't sort).
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if not include_hidden:
            # Mutate in place so os.walk skips them.
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not include_hidden and name.startswith("."):
                continue
            out.append(os.path.join(dirpath, name))
            if len(out) >= max_entries:
                return out
    return out


class FuzzySearchPlugin(MenuProvider):
    name = "fuzzy_search"
    description = "Fuzzy-find files under the current directory"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path:
            return []
        return [("Fuzzy Find...", self._open_dialog)]

    def _open_dialog(self, path: str) -> None:
        from PyQt6.QtWidgets import (
            QApplication,
            QDialog,
            QDialogButtonBox,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QVBoxLayout,
        )

        root = path if os.path.isdir(path) else os.path.dirname(path) or "."
        try:
            entries = walk_files(root)
        except OSError as e:
            log.warning("walk failed: %s", e)
            return

        dlg = QDialog()
        dlg.setWindowTitle(f"Fuzzy Find in {root}")
        dlg.resize(700, 500)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"{len(entries)} files indexed", dlg))
        search = QLineEdit(dlg)
        search.setPlaceholderText("type to filter…")
        layout.addWidget(search)
        results = QListWidget(dlg)
        layout.addWidget(results, 1)

        def refresh() -> None:
            results.clear()
            for _score, p in rank_candidates(search.text(), entries):
                rel = os.path.relpath(p, root)
                item = QListWidgetItem(rel)
                item.setData(0x0100, p)  # Qt.UserRole = 0x0100
                results.addItem(item)
            if results.count() > 0:
                results.setCurrentRow(0)

        search.textChanged.connect(refresh)
        results.itemActivated.connect(lambda _i: dlg.accept())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel,
            parent=dlg,
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        refresh()
        search.setFocus()

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        item = results.currentItem()
        if item is None:
            return
        target = item.data(0x0100)
        # Open with xdg-open; the file manager itself doesn't have a
        # "select-this-path" entry point from a plugin, so launching is
        # the most useful default.
        import subprocess
        try:
            subprocess.Popen(["xdg-open", target])
        except OSError as e:
            log.warning("xdg-open failed: %s", e)
            QApplication.clipboard().setText(target)
