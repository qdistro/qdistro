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
