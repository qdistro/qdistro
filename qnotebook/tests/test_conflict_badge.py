"""Status-bar conflict badge tests."""

from __future__ import annotations

from pathlib import Path

from qnotebook.window import MainWindow


def test_badge_hidden_with_no_conflicts(qapp, tmp_path: Path, qtbot) -> None:
    (tmp_path / "Home.md").write_text("# Home\n")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    assert not w._conflict_badge.isVisibleTo(w.statusBar())


def test_badge_shows_count_when_conflict_present(qapp, tmp_path: Path, qtbot) -> None:
    (tmp_path / "Home.md").write_text("# Home\n")
    (tmp_path / "Home.sync-conflict-20260415-100000-ABCDEF1.md").write_text("mine\n")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    qapp.processEvents()
    assert w._conflict_badge.text().startswith("\u26a0")
    assert "1" in w._conflict_badge.text()


def test_badge_refreshes_after_resolution(qapp, tmp_path: Path, qtbot) -> None:
    (tmp_path / "Home.md").write_text("# Home\n")
    conflict = tmp_path / "Home.sync-conflict-20260415-100000-ABCDEF1.md"
    conflict.write_text("mine\n")
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    qapp.processEvents()
    # Remove the conflict and refresh
    conflict.unlink()
    w._refresh_conflict_badge()
    assert not w._conflict_badge.isVisibleTo(w.statusBar())
