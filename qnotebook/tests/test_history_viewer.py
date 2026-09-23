from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook import versioning
from qnotebook.history_viewer import HistoryViewer, render_diff
from qnotebook.window import MainWindow

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()


def _make_repo_notebook(tmp_path: Path) -> Path:
    root = tmp_path / "nb"
    root.mkdir()
    (root / "Home.md").write_text("# Home\n\nv1\n", encoding="utf-8")
    versioning.init_repo(root)
    versioning.commit_page(root, "Home")
    (root / "Home.md").write_text("# Home\n\nv2\n", encoding="utf-8")
    versioning.commit_page(root, "Home")
    (root / "Home.md").write_text("# Home\n\nv3\n", encoding="utf-8")
    versioning.commit_page(root, "Home")
    return root


def test_render_diff_shows_changes():
    out = render_diff("a\nb\n", "a\nc\n")
    assert "-b" in out
    assert "+c" in out


def test_page_history_lists_commits(tmp_path: Path):
    root = _make_repo_notebook(tmp_path)
    history = versioning.page_history(root, "Home.md")
    assert len(history) >= 3
    sha = history[0][0]
    assert len(sha) == 40


def test_page_at_revision(tmp_path: Path):
    root = _make_repo_notebook(tmp_path)
    history = versioning.page_history(root, "Home.md")
    oldest_sha = history[-1][0]
    text = versioning.page_at_revision(root, oldest_sha, "Home.md")
    assert text is not None
    assert "v1" in text


def test_history_viewer_widget_loads(qapp, tmp_path: Path, qtbot):
    root = _make_repo_notebook(tmp_path)
    dlg = HistoryViewer(root, "Home", "Home.md", "current text", parent=None)
    qtbot.addWidget(dlg)
    assert dlg.commit_count_loaded() >= 3
    dlg.list.setCurrentRow(0)
    qapp.processEvents()
    assert dlg.selected_text() is not None


def test_window_opens_history_when_versioning_on(
    qapp, tmp_path: Path, qtbot, monkeypatch
):
    root = _make_repo_notebook(tmp_path)
    w = MainWindow()
    w.open_notebook(str(root))
    qtbot.addWidget(w)
    w.load_page("Home")
    # Patch QMessageBox to avoid modal in test (none expected here since repo exists)
    opened = {}
    from qnotebook import history_viewer as hv

    class StubDlg:
        def __init__(self, *a, **kw):
            opened["yes"] = True
        def exec(self):
            return 0

    monkeypatch.setattr(hv, "HistoryViewer", StubDlg)
    w._open_page_history()
    assert opened.get("yes")
