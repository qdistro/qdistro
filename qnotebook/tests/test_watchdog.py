"""Auto-reload on external change."""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook.watchdog import PageWatcher
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()
    yield


@pytest.fixture
def win(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    yield w


def test_silent_reload_when_clean(win, tmp_notebook, qapp):
    win.load_page("Home")
    assert not win.editor.is_dirty()
    # External write
    (tmp_notebook / "Home.md").write_text(
        "# Home\n\nNew content from outside.\n", encoding="utf-8"
    )
    # Call handler directly (FS watchers are flaky on tmpfs / overlayfs).
    win._on_external_page_change(str(tmp_notebook / "Home.md"))
    md = win.editor.markdown()
    assert "New content from outside." in md


def test_no_silent_reload_when_dirty(win, tmp_notebook, qapp, monkeypatch):
    win.load_page("Home")
    from PyQt6.QtGui import QTextCursor
    cur = win.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("\n\nLocal edit.")
    qapp.processEvents()
    assert win.editor.is_dirty()
    # External write
    (tmp_notebook / "Home.md").write_text(
        "# Home\n\nExternal content.\n", encoding="utf-8"
    )
    # Prompt should be invoked; stub it out to assert dirty preserved.
    called = {"prompt": False}
    def fake_prompt():
        called["prompt"] = True
    monkeypatch.setattr(win, "_external_change_prompt", fake_prompt)
    win._on_external_page_change(str(tmp_notebook / "Home.md"))
    assert called["prompt"]
    # Editor not silently reloaded:
    assert "Local edit." in win.editor.markdown()


def test_watcher_watch_and_clear(qapp, tmp_path):
    f = tmp_path / "x.md"
    f.write_text("hi")
    pw = PageWatcher()
    pw.watch(f)
    assert str(f) in pw._fsw.files()
    pw.watch(None)
    assert str(f) not in pw._fsw.files()
