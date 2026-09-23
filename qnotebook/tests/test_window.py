from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory, monkeypatch):
    # Point QSettings at a tmp directory so tests don't pollute user settings.
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d)
    )
    # Clear any lingering state
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


def test_open_notebook(win, tmp_notebook: Path):
    assert win.notebook is not None
    assert win.notebook.root == tmp_notebook.resolve()
    assert win.model is not None


def test_loads_initial_page(win):
    assert win._current_page is not None


def test_load_page_by_name(win):
    win.load_page("Other")
    assert win._current_page == "Other"
    assert "Other" in win.editor.markdown()


def test_save_page(win, tmp_notebook: Path, qapp):
    win.load_page("Home")
    from PyQt6.QtGui import QTextCursor
    cur = win.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("\n\nAppended.")
    qapp.processEvents()
    assert win.editor.is_dirty()
    win._save_current()
    assert not win.editor.is_dirty()
    content = (tmp_notebook / "Home.md").read_text(encoding="utf-8")
    assert "Appended." in content


def test_follow_wikilink(win):
    win.load_page("Home")
    win._on_link_activated("Sub:Child")
    assert win._current_page == "Sub:Child"


def test_follow_wikilink_path_form(win):
    win.load_page("Home")
    win._on_link_activated("Sub/Child")
    assert win._current_page == "Sub:Child"


def test_back_forward(win):
    win.load_page("Home")
    win.load_page("Other")
    assert win._current_page == "Other"
    win._go_back()
    assert win._current_page == "Home"
    win._go_forward()
    assert win._current_page == "Other"


def test_backlinks_populated(win):
    win.load_page("Sub:Child")
    items = [win.backlinks_list.item(i).text() for i in range(win.backlinks_list.count())]
    assert "Home" in items


def test_backlinks_empty_for_unreferenced(win):
    win.load_page("Home")
    items = [win.backlinks_list.item(i).text() for i in range(win.backlinks_list.count())]
    assert "Sub:Child" in items  # Sub:Child links back to Home


def test_find_bar_finds_match(win, qapp):
    win.load_page("Other")
    win.show()
    win._open_find()
    qapp.processEvents()
    assert win.find_bar.isVisible()
    win.find_bar.input.setText("bold")
    qapp.processEvents()
    cur = win.editor.textCursor()
    assert cur.selectedText() == "bold"
    assert "1 match" in win.find_bar.count_label.text()


def test_find_bar_next_wraps(win, qapp):
    win.load_page("Home")
    from PyQt6.QtGui import QTextCursor
    cur = win.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("\n\nWelcome again.\n")
    qapp.processEvents()
    win._open_find()
    win.find_bar.input.setText("Welcome")
    qapp.processEvents()
    # Advance forward to second match
    assert win.find_bar.find_next()
    sel1 = win.editor.textCursor().position()
    # Next find from here should wrap to first match
    assert win.find_bar.find_next()
    sel2 = win.editor.textCursor().position()
    assert sel1 != sel2


def test_find_bar_no_match_zero_count(win, qapp):
    win.load_page("Home")
    win._open_find()
    win.find_bar.input.setText("zzzzznotthereatall")
    qapp.processEvents()
    assert "0 match" in win.find_bar.count_label.text()


def test_find_bar_escape_closes(win, qapp):
    win.load_page("Home")
    win.show()
    win._open_find()
    qapp.processEvents()
    assert win.find_bar.isVisible()
    win.find_bar.close_bar()
    qapp.processEvents()
    assert not win.find_bar.isVisible()


def test_save_updates_index(win, tmp_notebook: Path, qapp):
    win.load_page("Other")
    from PyQt6.QtGui import QTextCursor
    cur = win.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("\n\nSee [[NewTarget]].")
    qapp.processEvents()
    win._save_current()
    assert win.index.backlinks("NewTarget") == ["Other"]


def test_save_merge_ladder_runs_off_gui_thread(win, tmp_notebook: Path,
                                               qapp, qtbot, monkeypatch):
    from PyQt6.QtCore import QThread
    from PyQt6.QtGui import QTextCursor
    from qnotebook import safe_save

    win.load_page("Home")
    cur = win.editor.textCursor()
    cur.select(QTextCursor.SelectionType.Document)
    cur.insertText("editor edit\n")
    qapp.processEvents()

    # Make the on-disk copy diverge after load so the save must leave the
    # pure-Python fast path. The fake merge records the worker thread.
    win._page_watcher.watch(None)
    (tmp_notebook / "Home.md").write_text("external edit\n", encoding="utf-8")
    seen = {}
    main_thread = QThread.currentThread()

    monkeypatch.setattr(safe_save, "HAS_GIT_MERGE_FILE", True)
    monkeypatch.setattr(safe_save, "HAS_WIGGLE", False)
    monkeypatch.setattr(safe_save, "HAS_MERGIRAF", False)

    def fake_git_merge(_base, _ours, _theirs):
        seen["thread"] = QThread.currentThread()
        return True, b"merged by worker\n"

    monkeypatch.setattr(safe_save, "_git_merge_file", fake_git_merge)

    win._save_current()
    assert win._pending_save_merges
    qtbot.waitUntil(lambda: not win._pending_save_merges, timeout=5000)
    qapp.processEvents()

    assert seen["thread"] is not main_thread
    assert (tmp_notebook / "Home.md").read_text(encoding="utf-8") == "merged by worker\n"
    assert win.editor.markdown() == "merged by worker\n"
    assert not win.editor.is_dirty()
    assert win._page_watcher._current == str(tmp_notebook / "Home.md")


def test_save_changed_during_async_merge_preserves_external_edits(
    win, tmp_notebook: Path, qapp, qtbot, monkeypatch,
):
    from PyQt6.QtGui import QTextCursor
    from qnotebook import safe_save

    win.load_page("Home")
    cur = win.editor.textCursor()
    cur.select(QTextCursor.SelectionType.Document)
    cur.insertText("editor first\n")
    qapp.processEvents()

    win._page_watcher.watch(None)
    (tmp_notebook / "Home.md").write_text("external edit\n", encoding="utf-8")

    first_started = threading.Event()
    release_first = threading.Event()
    calls = []

    monkeypatch.setattr(safe_save, "HAS_GIT_MERGE_FILE", True)
    monkeypatch.setattr(safe_save, "HAS_WIGGLE", False)
    monkeypatch.setattr(safe_save, "HAS_MERGIRAF", False)

    def fake_git_merge(_base, ours, theirs):
        calls.append((bytes(ours), bytes(theirs)))
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(5)
        return True, b"external edit\n" + bytes(ours)

    monkeypatch.setattr(safe_save, "_git_merge_file", fake_git_merge)

    win._save_current()
    assert first_started.wait(5)

    cur = win.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("editor second\n")
    qapp.processEvents()
    release_first.set()

    qtbot.waitUntil(
        lambda: len(calls) >= 2 and not win._pending_save_merges,
        timeout=5000,
    )
    qapp.processEvents()

    disk = (tmp_notebook / "Home.md").read_text(encoding="utf-8")
    assert "external edit" in disk
    assert "editor second" in disk
    assert "external edit" in win.editor.markdown()
    assert "editor second" in win.editor.markdown()
    assert not win.editor.is_dirty()


def test_close_event_drains_pending_merges_before_commits(win, monkeypatch):
    from PyQt6.QtGui import QCloseEvent
    from qnotebook import versioning

    calls = []
    monkeypatch.setattr(
        win, "_drain_pending_save_merges",
        lambda timeout: calls.append(("merges", timeout)) or True,
    )
    monkeypatch.setattr(
        versioning, "wait_for_pending_commits",
        lambda timeout: calls.append(("commits", timeout)) or True,
    )

    win.closeEvent(QCloseEvent())
    assert calls[:2] == [("merges", -1), ("commits", -1)]


# ---- recent + bookmarks ----


def test_recent_pages_tracked(win):
    win.load_page("Other")
    win.load_page("Home")
    win.load_page("Sub")
    # Most recent first
    assert win._recent[:3] == ["Sub", "Home", "Other"]


def test_recent_deduped(win):
    win.load_page("Home")
    win.load_page("Other")
    win.load_page("Home")  # re-visit
    assert win._recent[0] == "Home"
    assert win._recent.count("Home") == 1


def test_bookmark_toggle(win):
    win.load_page("Home")
    win._toggle_bookmark_current()
    assert "Home" in win._bookmarks
    win._toggle_bookmark_current()
    assert "Home" not in win._bookmarks


def test_status_bar_shows_word_count(win):
    win.load_page("Home")
    status = win._status_label.text()
    assert "words" in status


def test_insert_date_at_cursor(win):
    from datetime import date
    win.load_page("Home")
    from PyQt6.QtGui import QTextCursor
    cur = win.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    win.editor.setTextCursor(cur)
    win._insert_date()
    assert date.today().isoformat() in win.editor.markdown()


def test_bookmark_persisted(win, tmp_notebook: Path, qapp, qtbot):
    win.load_page("Home")
    win._toggle_bookmark_current()
    # New MainWindow on same notebook should see the bookmark
    w2 = MainWindow()
    w2.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w2)
    assert "Home" in w2._bookmarks


# ---- search highlighting + jump ----


def test_search_hit_jump_highlights_all_matches(win, qapp):
    win.load_page("Home")
    win.search_dock_widget.input.setText("Welcome")
    win._on_search_hit_activated("Home", 3)
    sels = win.editor.extraSelections()
    assert len(sels) >= 1


def test_escape_clears_search_highlights(win, qapp):
    win.load_page("Home")
    win._highlight_all_occurrences("Welcome")
    assert len(win.editor.extraSelections()) >= 1
    win.editor.escapePressed.emit()
    qapp.processEvents()
    assert win.editor.extraSelections() == []


# ---- polish ----


def test_window_title_includes_page_and_notebook(win):
    win.load_page("Home")
    t = win.windowTitle()
    assert "Home" in t
    assert "qnotebook" in t
    assert "nb" in t  # tmp_notebook dir name


def test_status_shows_page_count_and_notebook(win):
    win.load_page("Home")
    s = win._status_label.text()
    assert "nb" in s  # notebook name
    assert "page" in s  # total page count label


def test_recent_notebooks_tracked(qapp, tmp_notebook, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    recents = w._recent_notebooks()
    assert str(tmp_notebook.resolve()) in [r for r in recents]


def test_page_properties_returns_metadata(win):
    info = win.page_properties("Home")
    assert info["path"] == "Home"
    assert info["size"] > 0
    assert info["words"] > 0
    assert info["chars"] > 0
    assert "ctime" in info and info["ctime"]


def test_page_properties_inbound_links(win):
    # Home is linked from Sub:Child
    info = win.page_properties("Home")
    assert info["inbound"] >= 1


def test_page_properties_tags_extracted(win, tmp_notebook: Path):
    (tmp_notebook / "Tagged.md").write_text("# T\n\n#alpha and #beta\n", encoding="utf-8")
    win.index.update_page("Tagged")
    info = win.page_properties("Tagged")
    assert "alpha" in info["tags"]
    assert "beta" in info["tags"]


def test_outline_mode_switches_to_recent(win, qapp):
    win._set_outline_mode("recent")
    assert win.outline_mode() == "recent"
    m = win.tree.model()
    assert m is not win.model
    assert m.rowCount() >= 1


def test_outline_mode_switch_back_to_tree(win, qapp):
    win._set_outline_mode("recent")
    win._set_outline_mode("tree")
    assert win.outline_mode() == "tree"
    assert win.tree.model() is win.model


def test_set_action_shortcut_persists(win, qapp):
    win.set_action_shortcut("Save", "Ctrl+Shift+S")
    s = QSettings("qnotebook", "qnotebook")
    raw = s.value("shortcuts", {}, type=dict) or {}
    assert raw.get("Save") == "Ctrl+Shift+S"
    assert win.act_save.shortcut().toString() == "Ctrl+Shift+S"


def test_custom_shortcut_loads_on_startup(qapp, tmp_notebook, qtbot):
    s = QSettings("qnotebook", "qnotebook")
    s.setValue("shortcuts", {"Save": "Ctrl+Alt+S"})
    s.sync()
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    assert w.act_save.shortcut().toString() == "Ctrl+Alt+S"


def test_quick_note_appends_to_scratch(win, tmp_notebook: Path):
    win.append_to_scratch("Hello world")
    assert win.notebook.exists("Scratch")
    text = win.notebook.get_page("Scratch")
    assert "Hello world" in text


def test_quick_note_appends_with_timestamp(win):
    page = win.append_to_scratch("note one")
    assert page == "Scratch"
    win.append_to_scratch("note two")
    text = win.notebook.get_page("Scratch")
    # Both appended; two timestamp headers
    assert text.count("## ") >= 2
    assert "note one" in text and "note two" in text


def test_dark_mode_toggle_applies_palette(win, qapp):
    win.act_dark.setChecked(True)
    qapp.processEvents()
    s = QSettings("qnotebook", "qnotebook")
    assert bool(s.value("dark_mode", False, type=bool))
    sheet = win.editor.styleSheet()
    assert "#1e1e1e" in sheet
    win.act_dark.setChecked(False)
    qapp.processEvents()
    assert win.editor.styleSheet() == ""


def test_dark_mode_persists_across_open(qapp, tmp_notebook, qtbot):
    s = QSettings("qnotebook", "qnotebook")
    s.setValue("dark_mode", True)
    s.sync()
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    qapp.processEvents()
    assert w.act_dark.isChecked()
    assert "#1e1e1e" in w.editor.styleSheet()


def test_save_atomic_write(win, tmp_notebook):
    win.load_page("Home")
    from PyQt6.QtGui import QTextCursor
    cur = win.editor.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.End)
    cur.insertText("\n\nAtomic write test.\n")
    win._save_current()
    # No leftover .tmp file
    assert not (tmp_notebook / "Home.md.tmp").exists()
    body = (tmp_notebook / "Home.md").read_text(encoding="utf-8")
    assert "Atomic write test." in body


def test_heading_wikilink_navigates_and_scrolls(win, tmp_notebook: Path):
    # Put a page with multiple headings
    (tmp_notebook / "Long.md").write_text(
        "# Long\n\ntext\n\n## Section A\n\na\n\n## Section B\n\nb\n",
        encoding="utf-8",
    )
    win.index.rebuild()
    win._on_link_activated("Long#Section B")
    assert win._current_page == "Long"
    cur = win.editor.textCursor()
    assert cur.block().text().strip() == "Section B"


def test_same_page_anchor_stays_on_page(win, tmp_notebook: Path):
    (tmp_notebook / "A.md").write_text(
        "# A\n\n## Intro\n\ntxt\n\n## End\n\nz\n", encoding="utf-8"
    )
    win.index.rebuild()
    win.load_page("A")
    win._on_link_activated("#End")
    assert win._current_page == "A"
    cur = win.editor.textCursor()
    assert cur.block().text().strip() == "End"


def test_alias_wikilink_resolves(win, tmp_notebook: Path):
    (tmp_notebook / "Real.md").write_text(
        "---\naliases: [MyAlias]\n---\n# Real\n", encoding="utf-8"
    )
    win.index.rebuild()
    win._on_link_activated("MyAlias")
    assert win._current_page == "Real"


def test_split_editor_horizontal_creates_secondary(win):
    assert not win.is_split()
    win.split_editor("horizontal")
    assert win.is_split()
    assert win._secondary_editor is not None


def test_split_editor_vertical_and_close(win):
    win.split_editor("vertical")
    assert win.is_split()
    win.close_split()
    assert not win.is_split()


def test_split_editor_mirrors_current_page(win):
    win.load_page("Home")
    win.split_editor("horizontal")
    assert win._secondary_editor is not None
    assert "Home" in win._secondary_editor.markdown() or win._secondary_editor.markdown().strip() != ""


def test_session_save_and_restore_current_page(win, tmp_notebook, qapp, qtbot):
    from qnotebook import session
    win.load_page("Other")
    data = session.capture(win)
    assert data["current_page"] == "Other"
    session.save(tmp_notebook, data)
    assert (tmp_notebook / ".qnotebook" / "session.json").is_file()
    loaded = session.load(tmp_notebook)
    assert loaded["current_page"] == "Other"


def test_session_restore_applies_cursor(win, tmp_notebook, qapp):
    from qnotebook import session
    win.load_page("Home")
    cur = win.editor.textCursor()
    cur.setPosition(3)
    win.editor.setTextCursor(cur)
    data = session.capture(win)
    assert data["primary_cursor"] == 3
    win.load_page("Other")
    session.restore(win, data)
    assert win._current_page == "Home"
    assert win.editor.textCursor().position() == 3


def test_session_restore_restores_split(win, tmp_notebook, qapp):
    from qnotebook import session
    win.load_page("Home")
    win.split_editor("horizontal")
    data = session.capture(win)
    assert data["split"] is not None
    win.close_split()
    assert not win.is_split()
    session.restore(win, data)
    assert win.is_split()


def test_reveal_in_file_manager_invokes_xdg_open(win, tmp_notebook, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, cmd, *a, **kw):
            calls.append(cmd)

    monkeypatch.setattr("subprocess.Popen", FakePopen)
    win.reveal_in_file_manager("Home")
    assert calls and calls[0][0] == "xdg-open"
    assert str(tmp_notebook) in calls[0][1]


def test_open_terminal_here_respects_env(win, tmp_notebook, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, cmd, *a, **kw):
            calls.append((cmd, kw.get("cwd")))

    monkeypatch.setattr("subprocess.Popen", FakePopen)
    monkeypatch.setenv("TERMINAL", "my-term")
    win.open_terminal_here(None)
    assert calls
    assert calls[0][0] == ["my-term"]
    assert str(tmp_notebook) in (calls[0][1] or "")


def test_tree_nav_down_moves_selection(win):
    # Start at root's first child
    idx0 = win.model.index(0, 0)
    win.tree.setCurrentIndex(idx0)
    assert win.tree.currentIndex().row() == 0
    win.tree_nav_down()
    assert win.tree.currentIndex().row() == 1


def test_tree_nav_up_moves_selection(win):
    idx = win.model.index(1, 0)
    win.tree.setCurrentIndex(idx)
    win.tree_nav_up()
    assert win.tree.currentIndex().row() == 0


def test_tree_nav_open_keeps_editor_focus(win):
    idx = win.model.index(0, 0)
    win.tree.setCurrentIndex(idx)
    ref = win.model.page_for_index(idx)
    win.tree_nav_open()
    assert win._current_page == ref.path
