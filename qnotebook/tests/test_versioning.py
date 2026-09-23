from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook import versioning
from qnotebook.window import MainWindow


def _has_git() -> bool:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(not _has_git(), reason="git not installed")


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings-ver")
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d)
    )
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()
    yield


def test_init_and_commit_page(tmp_notebook: Path):
    assert versioning.init_repo(tmp_notebook)
    assert versioning.is_repo(tmp_notebook)
    assert versioning.commit_page(tmp_notebook, "Home")
    assert versioning.commit_count(tmp_notebook) == 1


def test_commit_noop_when_clean(tmp_notebook: Path):
    versioning.init_repo(tmp_notebook)
    versioning.commit_page(tmp_notebook, "Home")
    # Second commit with no changes should be a no-op (returns False)
    assert not versioning.commit_page(tmp_notebook, "Home")


def test_commit_page_includes_rung_in_message(tmp_notebook: Path):
    versioning.init_repo(tmp_notebook)
    (tmp_notebook / "Home.md").write_text("hello\n")
    assert versioning.commit_page(tmp_notebook, "Home", rung="trivial")
    res = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    )
    assert "[trivial]" in res.stdout


def test_nb_settings_default_versioning_on_for_new_notebooks(tmp_path: Path):
    from qnotebook import nb_settings
    assert nb_settings.is_new_notebook(tmp_path)
    assert nb_settings.get(tmp_path, "versioning_enabled") is True


def test_nb_settings_roundtrip(tmp_path: Path):
    from qnotebook import nb_settings
    nb_settings.set_value(tmp_path, "versioning_enabled", False)
    assert nb_settings.get(tmp_path, "versioning_enabled") is False
    assert not nb_settings.is_new_notebook(tmp_path)


def test_new_notebook_auto_enables_versioning(qapp, tmp_path: Path, qtbot):
    from qnotebook import nb_settings
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_path))
    assert nb_settings.get(tmp_path, "versioning_enabled") is True
    assert versioning.is_repo(tmp_path)


def test_save_commit_message_tagged_with_rung(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    qtbot.addWidget(w)
    w.open_notebook(str(tmp_notebook))
    w.act_toggle_versioning.setChecked(True)
    w._toggle_versioning(True)
    w.load_page("Home")
    from PyQt6.QtGui import QTextCursor
    cur = w.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("\nnew line\n")
    qapp.processEvents()
    w._save_current()
    # The commit is dispatched to a background worker thread — drain it.
    assert versioning.wait_for_pending_commits(10000)
    res = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    )
    # Any of the rungs we might take — at minimum the message has a bracket tag
    assert "[" in res.stdout and "]" in res.stdout


def test_window_save_commits_when_versioning_on(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    w.act_toggle_versioning.setChecked(True)
    w._toggle_versioning(True)
    assert versioning.is_repo(tmp_notebook)
    w.load_page("Home")
    from PyQt6.QtGui import QTextCursor
    cur = w.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("\n\nextra line\n")
    qapp.processEvents()
    before = versioning.commit_count(tmp_notebook)
    w._save_current()
    # Commit runs on a background worker thread; wait for it to land.
    assert versioning.wait_for_pending_commits(10000)
    after = versioning.commit_count(tmp_notebook)
    assert after == before + 1


def test_rename_page_drains_pending_commit(qapp, tmp_notebook: Path, qtbot):
    """A queued save-commit must land at the OLD path before a rename moves the
    file — otherwise the deferred path-scoped commit would record a spurious
    deletion and leave the saved content uncommitted at the new path."""
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    w.act_toggle_versioning.setChecked(True)
    w._toggle_versioning(True)
    w.load_page("Home")
    from PyQt6.QtGui import QTextCursor
    cur = w.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("\n\nrename-race line\n")
    qapp.processEvents()
    w._save_current()                 # dispatches the commit async
    w.rename_page("Home", "Renamed")  # must drain the commit first
    # The save was committed against the OLD path before the rename.
    log = subprocess.run(
        ["git", "log", "--all", "--pretty=%s", "--", "Home.md"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout
    assert "edit: Home" in log
    # The pre-rename content is recoverable from history (not lost).
    saved = subprocess.run(
        ["git", "log", "-1", "--pretty=%H", "--", "Home.md"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout.strip()
    assert saved
    content = subprocess.run(
        ["git", "show", f"{saved}:Home.md"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout
    assert "rename-race line" in content


def test_delete_page_drains_pending_commit(qapp, tmp_notebook: Path, qtbot):
    """A queued save-commit must land before the page is deleted, so the saved
    content remains recoverable from version history."""
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    w.act_toggle_versioning.setChecked(True)
    w._toggle_versioning(True)
    w.load_page("Home")
    from PyQt6.QtGui import QTextCursor
    cur = w.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("\n\ndelete-race line\n")
    qapp.processEvents()
    w._save_current()           # dispatches the commit async
    w.delete_page("Home")       # must drain the commit first
    saved = subprocess.run(
        ["git", "log", "-1", "--pretty=%H", "--", "Home.md"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout.strip()
    assert saved, "saved content was lost — delete raced the queued commit"
    content = subprocess.run(
        ["git", "show", f"{saved}:Home.md"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout
    assert "delete-race line" in content


def test_commit_page_async_dispatches_off_thread(qapp, tmp_notebook: Path):
    """commit_page_async returns immediately (dispatched) and the commit
    lands once the pool drains — never blocking the caller's thread."""
    from PyQt6.QtCore import QThread
    versioning.init_repo(tmp_notebook)
    (tmp_notebook / "Home.md").write_text("async edit\n")

    seen: dict = {}

    def _done(result: bool) -> None:
        seen["result"] = result
        seen["thread"] = QThread.currentThread()

    dispatched = versioning.commit_page_async(
        tmp_notebook, "Home", rung="trivial", done=_done
    )
    assert dispatched is True  # a Qt event loop is available under qapp
    assert versioning.wait_for_pending_commits(10000)

    assert seen.get("result") is True
    # The callback fired on a worker thread, not the main/GUI thread.
    assert seen["thread"] is not QThread.currentThread()
    assert versioning.commit_count(tmp_notebook) == 1


def test_commit_page_async_serializes_commits(qapp, tmp_notebook: Path):
    """Rapid-fire async commits to the SAME page drain cleanly (single
    serialized worker, no concurrent index.lock failures) and the final
    content lands in HEAD.

    Same-page commits may coalesce: a worker that runs after several quick
    edits to one page sees only the latest on-disk bytes, so the count is
    between 1 and 8. That is fine -- the latest content is always committed and
    nothing is lost. (Cross-page attribution is covered separately.)"""
    versioning.init_repo(tmp_notebook)
    home = tmp_notebook / "Home.md"
    for i in range(8):
        home.write_text(f"line {i}\n")
        versioning.commit_page_async(tmp_notebook, "Home", rung="trivial")
    assert versioning.wait_for_pending_commits(20000)
    count = versioning.commit_count(tmp_notebook)
    assert 1 <= count <= 8
    # Home is fully committed (not dirty); the latest content is in HEAD.
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "Home.md"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    )
    assert status.stdout.strip() == ""
    head = subprocess.run(
        ["git", "show", "HEAD:Home.md"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    )
    assert head.stdout == "line 7\n"


def test_commit_page_stages_only_its_own_page(tmp_notebook: Path):
    """commit_page must not sweep an unrelated page's pending edit into this
    page's commit (the bug that async dispatch would otherwise expose:
    deferred commits running git add -A over a moved-on working tree)."""
    versioning.init_repo(tmp_notebook)
    (tmp_notebook / "Home.md").write_text("home v1\n")
    (tmp_notebook / "Other.md").write_text("other v1\n")
    # Commit Home — Other's pending change must stay uncommitted.
    assert versioning.commit_page(tmp_notebook, "Home", rung="trivial")
    head_files = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout.split()
    assert "Home.md" in head_files
    assert "Other.md" not in head_files
    # Other is still dirty and gets its own correctly-tagged commit.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    )
    assert "Other.md" in status.stdout
    assert versioning.commit_page(tmp_notebook, "Other", rung="git-merge-file")
    msg = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout
    assert "edit: Other [git-merge-file]" in msg


def test_commit_page_ignores_preexisting_staged_content(tmp_notebook: Path):
    """A foreign change already sitting in the index must NOT be swept into
    this page's commit (the commit is pathspec-scoped, not whole-index)."""
    versioning.init_repo(tmp_notebook)
    # Pre-stage an unrelated file, as if a prior operation left the index dirty.
    (tmp_notebook / "Other.md").write_text("foreign staged\n")
    subprocess.run(["git", "add", "Other.md"], cwd=str(tmp_notebook), check=True)
    # Now save Home.
    (tmp_notebook / "Home.md").write_text("home edit\n")
    assert versioning.commit_page(tmp_notebook, "Home", rung="trivial")
    head_files = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout.split()
    assert "Home.md" in head_files
    assert "Other.md" not in head_files  # foreign staged content not swept in


def test_commit_page_first_commit_with_pathspec(tmp_notebook: Path):
    """The very first commit (no HEAD yet) still works with a scoped pathspec."""
    versioning.init_repo(tmp_notebook)
    (tmp_notebook / "Home.md").write_text("first\n")
    assert versioning.commit_page(tmp_notebook, "Home", rung="trivial")
    assert versioning.commit_count(tmp_notebook) == 1


def test_commit_page_captures_page_resources(tmp_notebook: Path):
    """A page's co-located _resources/ (inserted images/attachments) must be
    versioned together with the markdown that references it — otherwise the
    save commit points at an image git never preserved."""
    versioning.init_repo(tmp_notebook)
    resdir = tmp_notebook / "_resources"
    resdir.mkdir()
    (resdir / "pic.png").write_bytes(b"\x89PNG fake\n")
    (tmp_notebook / "Home.md").write_text("# Home\n\n![](_resources/pic.png)\n")
    assert versioning.commit_page(tmp_notebook, "Home", rung="trivial")
    head_files = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout.split()
    assert "Home.md" in head_files
    assert "_resources/pic.png" in head_files


def test_commit_page_records_resource_deletion(tmp_notebook: Path):
    """If a previously-committed _resources/ is removed and the page is saved,
    the deletion must be recorded — version history must match disk state."""
    versioning.init_repo(tmp_notebook)
    resdir = tmp_notebook / "_resources"
    resdir.mkdir()
    (resdir / "pic.png").write_bytes(b"\x89PNG fake\n")
    (tmp_notebook / "Home.md").write_text("# Home\n\n![](_resources/pic.png)\n")
    assert versioning.commit_page(tmp_notebook, "Home", rung="trivial")
    # Now remove the resource dir and re-save the page without the reference.
    import shutil as _shutil
    _shutil.rmtree(resdir)
    (tmp_notebook / "Home.md").write_text("# Home\n\nno image now\n")
    assert versioning.commit_page(tmp_notebook, "Home", rung="trivial")
    # HEAD records the resource deletion (the dir is gone but was tracked).
    name_status = subprocess.run(
        ["git", "show", "--name-status", "--pretty=format:", "HEAD"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout
    assert "D\t_resources/pic.png" in name_status
    # And the resource is no longer present at HEAD.
    present = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout
    assert "_resources/pic.png" not in present


def test_commit_page_without_resources_dir(tmp_notebook: Path):
    """When the page has no _resources/ dir, the commit still succeeds (the
    nonexistent pathspec must not abort the add/commit)."""
    versioning.init_repo(tmp_notebook)
    assert not (tmp_notebook / "_resources").exists()
    (tmp_notebook / "Home.md").write_text("no resources\n")
    assert versioning.commit_page(tmp_notebook, "Home", rung="trivial")
    head_files = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout.split()
    assert head_files == ["Home.md"]


def test_commit_page_async_multipage_attribution(qapp, tmp_notebook: Path):
    """Two different pages saved before the worker drains each get their own
    commit with the correct page name + rung — no coalescing across pages."""
    versioning.init_repo(tmp_notebook)
    (tmp_notebook / "Home.md").write_text("home async\n")
    (tmp_notebook / "Other.md").write_text("other async\n")
    versioning.commit_page_async(tmp_notebook, "Home", rung="trivial")
    versioning.commit_page_async(tmp_notebook, "Other", rung="wiggle")
    assert versioning.wait_for_pending_commits(20000)
    subjects = subprocess.run(
        ["git", "log", "--pretty=%s"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout
    assert "edit: Home [trivial]" in subjects
    assert "edit: Other [wiggle]" in subjects
    # Both saved pages are committed (no longer dirty); unrelated fixture pages
    # that were never saved through versioning stay untracked, which is fine.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(tmp_notebook), capture_output=True, text=True,
    ).stdout
    assert "Home.md" not in status
    assert " Other.md" not in status and status.count("Other.md") == 0


def test_commit_page_async_inline_without_qt(tmp_notebook, monkeypatch):
    """With no Qt event loop available, the commit runs inline (and is
    reported as not-dispatched) rather than being silently dropped."""
    versioning.init_repo(tmp_notebook)
    (tmp_notebook / "Home.md").write_text("inline edit\n")
    monkeypatch.setattr(versioning, "_commit_pool", lambda: None)
    seen = {}
    dispatched = versioning.commit_page_async(
        tmp_notebook, "Home", done=lambda r: seen.setdefault("result", r)
    )
    assert dispatched is False
    assert seen["result"] is True
    assert versioning.commit_count(tmp_notebook) == 1
