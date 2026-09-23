"""Notebook statistics + dashboard dialog.

`compute_stats(notebook, index)` returns a plain dict so tests (and
non-UI callers) can consume it without spinning up a QDialog.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any


def _word_count(text: str) -> int:
    return len([w for w in text.split() if w])


def compute_stats(notebook, index, top_k: int = 10) -> dict[str, Any]:
    total_pages = 0
    total_words = 0
    total_chars = 0
    inbound_counts: dict[str, int] = {}
    outbound_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    words_per_page: dict[str, int] = {}
    edits_per_day: dict[str, int] = {}

    for p in notebook.pages():
        total_pages += 1
        txt = notebook.get_page(p.path)
        total_chars += len(txt)
        wc = _word_count(txt)
        total_words += wc
        words_per_page[p.path] = wc
        f = notebook.file_for(p.path)
        try:
            ts = _dt.date.fromtimestamp(f.stat().st_mtime).isoformat()
            edits_per_day[ts] = edits_per_day.get(ts, 0) + 1
        except Exception:
            pass
        outbound_counts[p.path] = len(index.forward_links(p.path))
        inbound_counts[p.path] = len(index.backlinks(p.path))

    total_links = sum(outbound_counts.values())
    total_backlinks = sum(inbound_counts.values())
    tags_rows = index.tags()
    total_tags = len(tags_rows)
    for t, n in tags_rows:
        tag_counts[t] = n

    most_linked = sorted(
        inbound_counts.items(), key=lambda t: (-t[1], t[0])
    )[:top_k]
    most_linked = [(p, n) for p, n in most_linked if n > 0]
    orphans = [
        p for p in notebook.pages()
        if inbound_counts.get(p.path, 0) == 0 and outbound_counts.get(p.path, 0) == 0
    ]
    orphans_paths = [o.path for o in orphans]

    # Most-tagged pages: count tags per page via index.
    pages_tags: dict[str, int] = {}
    for t, _n in tags_rows:
        for pg in index.pages_with_tag(t):
            pages_tags[pg] = pages_tags.get(pg, 0) + 1
    most_tagged = sorted(pages_tags.items(), key=lambda t: (-t[1], t[0]))[:top_k]

    # Recent activity: last 30 days bar counts.
    today = _dt.date.today()
    recent_days: list[tuple[str, int]] = []
    for i in range(29, -1, -1):
        d = today - _dt.timedelta(days=i)
        recent_days.append((d.isoformat(), edits_per_day.get(d.isoformat(), 0)))

    return {
        "total_pages": total_pages,
        "total_words": total_words,
        "total_chars": total_chars,
        "total_tags": total_tags,
        "total_links": total_links,
        "total_backlinks": total_backlinks,
        "most_linked": most_linked,
        "orphans": orphans_paths,
        "most_tagged": most_tagged,
        "recent_days": recent_days,
    }


# ---- Dialog (lazy-imported from window.py) ----

def show_dashboard(parent, notebook, index) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QBrush, QColor, QPen
    from PyQt6.QtWidgets import (
        QDialog,
        QDialogButtonBox,
        QGraphicsScene,
        QGraphicsView,
        QLabel,
        QListWidget,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
    stats = compute_stats(notebook, index)
    dlg = QDialog(parent)
    dlg.setWindowTitle("Notebook Statistics")
    dlg.resize(720, 520)
    v = QVBoxLayout(dlg)

    summary = QLabel(
        f"Pages: {stats['total_pages']}   "
        f"Words: {stats['total_words']}   "
        f"Chars: {stats['total_chars']}   "
        f"Tags: {stats['total_tags']}   "
        f"Links: {stats['total_links']}"
    )
    v.addWidget(summary)

    tabs = QTabWidget(dlg)
    v.addWidget(tabs, 1)

    def _list_tab(title: str, rows: list) -> QWidget:
        w = QWidget()
        wv = QVBoxLayout(w)
        lst = QListWidget(w)
        for item in rows:
            if isinstance(item, tuple):
                lst.addItem(f"{item[0]}  ({item[1]})")
            else:
                lst.addItem(str(item))
        wv.addWidget(lst)
        tabs.addTab(w, title)
        return w

    _list_tab("Most linked", stats["most_linked"])
    _list_tab("Orphans", stats["orphans"])
    _list_tab("Most tagged", stats["most_tagged"])

    # Recent activity bar chart (QGraphicsScene).
    chart = QWidget()
    cv = QVBoxLayout(chart)
    scene = QGraphicsScene(chart)
    view = QGraphicsView(scene, chart)
    cv.addWidget(view)
    bars = stats["recent_days"]
    max_n = max((n for _d, n in bars), default=1) or 1
    bar_w = 18
    gap = 2
    h = 180
    pen = QPen(Qt.GlobalColor.black)
    brush = QBrush(QColor("#4a90d9"))
    for i, (_d, n) in enumerate(bars):
        bh = int((n / max_n) * h) if max_n else 0
        scene.addRect(i * (bar_w + gap), h - bh, bar_w, bh, pen, brush)
    tabs.addTab(chart, "Last 30 days")

    btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    btns.rejected.connect(dlg.reject)
    btns.accepted.connect(dlg.accept)
    v.addWidget(btns)
    dlg.exec()
