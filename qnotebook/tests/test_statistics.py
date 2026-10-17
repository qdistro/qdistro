"""Notebook statistics dashboard."""

from __future__ import annotations

from pathlib import Path

from qnotebook.index import Index
from qnotebook.notebook import Notebook
from qnotebook.statistics import compute_stats


def test_stats_counts_basic(tmp_notebook: Path, qapp):
    nb = Notebook(tmp_notebook)
    idx = Index(nb)
    idx.rebuild()
    stats = compute_stats(nb, idx)
    assert stats["total_pages"] == 4
    assert stats["total_words"] > 0
    assert stats["total_chars"] > 0
    assert stats["total_links"] >= 2  # Home -> Sub:Child, Home -> Other
    assert len(stats["recent_days"]) == 30
    idx.close()


def test_stats_identifies_orphans(tmp_path: Path, qapp):
    root = tmp_path / "nb"
    root.mkdir()
    (root / "Lonely.md").write_text("# Lonely\n\nno links, no one links to me.\n", encoding="utf-8")
    (root / "A.md").write_text("# A\n\n[[B]]\n", encoding="utf-8")
    (root / "B.md").write_text("# B\n", encoding="utf-8")
    nb = Notebook(root)
    idx = Index(nb)
    idx.rebuild()
    stats = compute_stats(nb, idx)
    assert "Lonely" in stats["orphans"]
    assert "A" not in stats["orphans"]  # has outbound
    assert "B" not in stats["orphans"]  # has inbound
    idx.close()


def test_stats_most_linked(tmp_path: Path, qapp):
    root = tmp_path / "nb"
    root.mkdir()
    (root / "Hub.md").write_text("# Hub\n", encoding="utf-8")
    (root / "A.md").write_text("# A\n\n[[Hub]]\n", encoding="utf-8")
    (root / "B.md").write_text("# B\n\n[[Hub]]\n", encoding="utf-8")
    (root / "C.md").write_text("# C\n\n[[Hub]]\n", encoding="utf-8")
    nb = Notebook(root)
    idx = Index(nb)
    idx.rebuild()
    stats = compute_stats(nb, idx)
    top = stats["most_linked"]
    assert top[0][0] == "Hub"
    assert top[0][1] == 3
    idx.close()


def test_activity_chart_bars_follow_the_palette(qapp, qtbot):
    """The dashboard's bar chart takes the palette and recolours on a live
    light/dark switch (black outlines vanished on a dark background)."""
    from PyQt6.QtGui import QColor, QPalette
    from qnotebook.statistics import build_activity_chart

    chart = build_activity_chart([("d1", 0), ("d2", 3), ("d3", 1)])
    qtbot.addWidget(chart)

    def palette(text: str, highlight: str) -> QPalette:
        pal = QPalette(chart.palette())
        pal.setColor(QPalette.ColorRole.WindowText, QColor(text))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(highlight))
        return pal

    chart.setPalette(palette("#101010", "#2060c0"))
    assert len(chart.bars) == 3
    assert all(r.pen().color() == QColor("#101010") for r in chart.bars)
    assert all(r.brush().color() == QColor("#2060c0") for r in chart.bars)
    chart.setPalette(palette("#f0f0f0", "#80b0ff"))
    assert all(r.pen().color() == QColor("#f0f0f0") for r in chart.bars)
    assert all(r.brush().color() == QColor("#80b0ff") for r in chart.bars)
