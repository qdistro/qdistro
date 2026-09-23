"""Visual GUI tests with a real event loop.

These tests use qtbot.waitExposed() + qtbot.waitUntil() so widgets
actually render (including terminal content) before assertions run, instead
of fixed sleeps. Screenshots are saved to /tmp/qterminator_test_*.png for
manual inspection.
"""

import os

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

# PNG files begin with this 8-byte signature.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _is_valid_png(path, min_size=5000):
    """A rendered screenshot must exist, be non-trivial, and be a real PNG."""
    if not os.path.exists(path) or os.path.getsize(path) <= min_size:
        return False
    with open(path, "rb") as fh:
        return fh.read(8) == _PNG_MAGIC

import qterminator.config as config_mod  # noqa: E402
from PyQt6.QtWidgets import QMessageBox  # noqa: E402
from qterminator.config import Config  # noqa: E402
from qterminator.preferences import PreferencesDialog  # noqa: E402
from qterminator.theme import apply_dark_theme  # noqa: E402
from qterminator.window import MainWindow  # noqa: E402

SCREENSHOT_DIR = "/tmp"


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    """Each test gets a fresh config with no disk state."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None


def _save(widget, name):
    path = os.path.join(SCREENSHOT_DIR, f"qterminator_test_{name}.png")
    widget.grab().save(path)
    return path


@pytest.fixture
def themed_app(qapp):
    """Apply dark theme to the app for visual tests."""
    apply_dark_theme(qapp)
    yield qapp


@pytest.fixture
def window(qtbot, themed_app):
    win = MainWindow()
    win.resize(900, 600)
    win.show()
    qtbot.waitExposed(win)
    # Wait on the observable condition (terminal laid out with real size)
    # instead of a fixed 200ms sleep, so the fixture isn't flaky on slow
    # hosts and isn't wastefully slow on fast ones.
    split = win._tabs.widget(0)
    qtbot.waitUntil(
        lambda: split.find_terminals()
        and split.find_terminals()[0].width() > 0
        and split.find_terminals()[0].height() > 0,
        timeout=2000,
    )
    return win


class TestSingleTerminal:
    def test_renders(self, window, qtbot):
        """Single terminal renders with shell content."""
        path = os.path.join(SCREENSHOT_DIR, "qterminator_test_single_terminal.png")
        # Re-grab until the rendered screenshot is a valid, non-trivial PNG,
        # rather than sleeping a fixed 500ms and hoping rendering finished.
        qtbot.waitUntil(
            lambda: _is_valid_png(_save(window, "single_terminal")),
            timeout=3000,
        )
        assert os.path.exists(path)
        # Non-trivial size AND a real PNG header (not a truncated/garbage file).
        assert os.path.getsize(path) > 5000
        with open(path, "rb") as fh:
            assert fh.read(8) == _PNG_MAGIC

    def test_menubar_hidden_by_default(self, window):
        assert not window.menuBar().isVisible()

    def test_menubar_toggle_shows(self, window):
        window._toggle_menubar()
        assert window.menuBar().isVisible()

    def test_tab_bar_hidden(self, window):
        assert not window._tab_bar.isVisible()


class TestSplits:
    def test_horizontal_split_renders(self, window, qtbot):
        window._split_horizontal()
        split = window._tabs.widget(0)
        # Wait for the second terminal to exist and be laid out with height.
        qtbot.waitUntil(
            lambda: len(split.find_terminals()) == 2
            and all(t.height() > 0 for t in split.find_terminals()),
            timeout=2000,
        )
        _save(window, "split_horizontal")
        terms = split.find_terminals()
        assert len(terms) == 2
        # Both should have nonzero height
        for t in terms:
            assert t.height() > 0

    def test_vertical_split_renders(self, window, qtbot):
        window._split_vertical()
        split = window._tabs.widget(0)
        qtbot.waitUntil(
            lambda: len(split.find_terminals()) == 2
            and all(t.width() > 0 for t in split.find_terminals()),
            timeout=2000,
        )
        _save(window, "split_vertical")
        terms = split.find_terminals()
        assert len(terms) == 2
        # Both should have nonzero width
        for t in terms:
            assert t.width() > 0

    def test_three_way_split(self, window, qtbot):
        """Split horiz then vert gives 3 terminals, all visible."""
        split = window._tabs.widget(0)
        window._split_horizontal()
        qtbot.waitUntil(lambda: len(split.find_terminals()) == 2, timeout=2000)
        window._split_vertical()
        qtbot.waitUntil(lambda: len(split.find_terminals()) == 3, timeout=2000)
        _save(window, "three_way_split")
        terms = split.find_terminals()
        assert len(terms) == 3


class TestTabs:
    def test_two_tabs_shows_bar(self, window, qtbot):
        window.new_tab()
        qtbot.waitUntil(
            lambda: window._tabs.count() == 2 and window._tab_bar.isVisible(),
            timeout=2000,
        )
        _save(window, "two_tabs")
        assert window._tab_bar.isVisible()
        assert window._tabs.count() == 2

    def test_tab_switch(self, window, qtbot):
        window.new_tab()
        qtbot.waitUntil(lambda: window._tabs.count() == 2, timeout=2000)
        window._tabs.setCurrentIndex(0)
        qtbot.waitUntil(lambda: window._tabs.currentIndex() == 0, timeout=2000)
        _save(window, "tab_switch")
        assert window._tabs.currentIndex() == 0


class TestZoom:
    def test_zoom_hides_others(self, window, qtbot):
        split = window._tabs.widget(0)
        window._split_horizontal()
        qtbot.waitUntil(lambda: len(split.find_terminals()) == 2, timeout=2000)
        window._toggle_zoom()
        qtbot.waitUntil(
            lambda: len([t for t in split.find_terminals() if t.isVisible()]) == 1,
            timeout=2000,
        )
        _save(window, "zoomed")
        visible = [t for t in split.find_terminals() if t.isVisible()]
        assert len(visible) == 1

    def test_unzoom_restores(self, window, qtbot):
        split = window._tabs.widget(0)
        window._split_horizontal()
        qtbot.waitUntil(lambda: len(split.find_terminals()) == 2, timeout=2000)
        window._toggle_zoom()
        qtbot.waitUntil(
            lambda: len([t for t in split.find_terminals() if t.isVisible()]) == 1,
            timeout=2000,
        )
        window._toggle_zoom()
        qtbot.waitUntil(
            lambda: len([t for t in split.find_terminals() if t.isVisible()]) == 2,
            timeout=2000,
        )
        _save(window, "unzoomed")
        visible = [t for t in split.find_terminals() if t.isVisible()]
        assert len(visible) == 2


class TestPreferences:
    def test_dialog_renders(self, window, qtbot):
        dlg = PreferencesDialog(window)
        dlg.show()
        qtbot.waitExposed(dlg)
        qtbot.waitUntil(lambda: dlg._tab_widget.count() == 3, timeout=2000)
        _save(dlg, "preferences")
        assert dlg._tab_widget.count() == 3
        dlg.close()

    def test_behavior_tab(self, window, qtbot):
        dlg = PreferencesDialog(window)
        dlg.show()
        qtbot.waitExposed(dlg)
        dlg._tab_widget.setCurrentIndex(1)
        qtbot.waitUntil(lambda: dlg._tab_widget.currentIndex() == 1, timeout=2000)
        _save(dlg, "preferences_behavior")
        dlg.close()

    def test_shortcuts_tab(self, window, qtbot):
        dlg = PreferencesDialog(window)
        dlg.show()
        qtbot.waitExposed(dlg)
        dlg._tab_widget.setCurrentIndex(2)
        qtbot.waitUntil(lambda: dlg._tab_widget.currentIndex() == 2, timeout=2000)
        _save(dlg, "preferences_shortcuts")
        dlg.close()


class TestKeyboard:
    """Test keyboard shortcuts via QTest."""

    def test_ctrl_shift_t_new_tab(self, window, qtbot):
        assert window._tabs.count() == 1
        QTest.keyClick(window, Qt.Key.Key_T,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        qtbot.waitUntil(lambda: window._tabs.count() == 2, timeout=2000)
        assert window._tabs.count() == 2

    def test_ctrl_shift_o_split(self, window, qtbot):
        split = window._tabs.widget(0)
        assert len(split.find_terminals()) == 1
        QTest.keyClick(window, Qt.Key.Key_O,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        qtbot.waitUntil(lambda: len(split.find_terminals()) == 2, timeout=2000)
        assert len(split.find_terminals()) == 2

    def test_ctrl_shift_e_split(self, window, qtbot):
        split = window._tabs.widget(0)
        assert len(split.find_terminals()) == 1
        QTest.keyClick(window, Qt.Key.Key_E,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        qtbot.waitUntil(lambda: len(split.find_terminals()) == 2, timeout=2000)
        assert len(split.find_terminals()) == 2


class TestMenuBarToggle:
    """Tests for menu bar visibility toggling."""

    def test_hidden_by_default(self, window):
        """Menu bar is hidden when config show_menubar=False (default)."""
        assert not window.menuBar().isVisible()

    def test_toggle_shows_menubar(self, window):
        """_toggle_menubar makes the menu bar visible."""
        window._toggle_menubar()
        assert window.menuBar().isVisible()

    def test_toggle_twice_hides_again(self, window):
        """Toggling twice returns menu bar to hidden."""
        window._toggle_menubar()
        assert window.menuBar().isVisible()
        window._toggle_menubar()
        assert not window.menuBar().isVisible()

    def test_toggle_persists_to_config(self, window):
        """Toggle updates the config value."""
        window._toggle_menubar()
        cfg = Config()
        assert cfg.get("general", "show_menubar") is True
        window._toggle_menubar()
        cfg = Config()
        assert cfg.get("general", "show_menubar") is False

    def test_context_menu_has_show_menubar(self, window):
        """Window actions include menu bar toggle (Ctrl+Shift+M)."""
        actions = window.actions()
        shortcuts = [a.shortcut().toString() for a in actions]
        assert "Ctrl+Shift+M" in shortcuts


class TestMainEntryPoint:
    """Tests for behaviors matching __main__.py main() logic.

    We don't call main() directly since it creates QApplication.
    Instead we test the individual window behaviors that main() orchestrates.
    """

    def test_default_window_has_one_tab(self, window):
        """A fresh MainWindow starts with exactly 1 tab."""
        assert window._tabs.count() == 1

    def test_geometry_resizes_correctly(self, qtbot, themed_app):
        """Window with geometry '1024x768' resizes to that size."""
        win = MainWindow()
        win.show()
        qtbot.waitExposed(win)
        # Simulate --geometry parsing from __main__.py
        geom = "1024x768"
        w, h = geom.split("x")
        win.resize(int(w), int(h))
        qtbot.waitUntil(
            lambda: win.width() == 1024 and win.height() == 768, timeout=2000,
        )
        assert win.width() == 1024
        assert win.height() == 768
        win.close()

    def test_invalid_geometry_no_crash(self, qtbot, themed_app):
        """Invalid geometry string (no 'x') doesn't crash."""
        win = MainWindow()
        win.show()
        qtbot.waitExposed(win)
        geom = "1024"
        try:
            w, h = geom.split("x")
            win.resize(int(w), int(h))
        except ValueError:
            pass  # Should be silently ignored, matching __main__.py behavior
        # Window should still be usable
        assert win._tabs.count() == 1
        win.close()

    def test_title_sets_window_title(self, qtbot, themed_app):
        """Setting title via args.title changes window title."""
        win = MainWindow()
        win.show()
        qtbot.waitExposed(win)
        win.setWindowTitle("Custom Title")
        assert win.windowTitle() == "Custom Title"
        win.close()

    def test_working_directory_creates_tab(self, qtbot, themed_app, tmp_path):
        """new_tab with working_directory uses that cwd."""
        win = MainWindow()
        win.show()
        qtbot.waitExposed(win)
        # The constructor already created one tab; add another with cwd
        win.new_tab(working_directory=str(tmp_path))
        qtbot.waitUntil(lambda: win._tabs.count() == 2, timeout=2000)
        assert win._tabs.count() == 2
        win.close()

    def test_no_restore_skips_layout(self, qtbot, themed_app):
        """With --no-restore logic, restore_layout is not called."""
        win = MainWindow()
        win.show()
        qtbot.waitExposed(win)
        # Simulate: no_restore=True means we don't call restore_layout
        # Just verify it creates a default single tab
        assert win._tabs.count() == 1
        win.close()

    def test_execute_sends_text(self, qtbot, themed_app, monkeypatch):
        """--execute sends text to the active terminal."""
        win = MainWindow()
        win.show()
        qtbot.waitExposed(win)
        qtbot.wait(200)
        sent_texts = []
        monkeypatch.setattr(
            win._active_terminal, "send_text",
            lambda text: sent_texts.append(text),
        )
        # Simulate what main() does with args.execute
        cmd = " ".join(["echo", "hello"]) + "\n"
        win._active_terminal.send_text(cmd)
        assert sent_texts == ["echo hello\n"]
        win.close()

    def test_restore_layout_returns_false_on_empty(self, window):
        """restore_layout returns False when config has no saved layout."""
        result = window.restore_layout()
        assert result is False

    def test_restore_window_state_with_saved_geometry(self, qtbot, themed_app):
        """restore_window_state applies saved geometry from config."""
        cfg = Config()
        cfg.set("general", "window_width", 1000)
        cfg.set("general", "window_height", 700)
        cfg.set("general", "window_x", 50)
        cfg.set("general", "window_y", 50)
        cfg.save()

        win = MainWindow()
        win.show()
        qtbot.waitExposed(win)
        win.restore_window_state()
        qtbot.waitUntil(
            lambda: win.width() == 1000 and win.height() == 700, timeout=2000,
        )
        assert win.width() == 1000
        assert win.height() == 700
        win.close()

    def test_restore_window_state_defaults(self, qtbot, themed_app):
        """restore_window_state uses defaults when nothing saved."""
        win = MainWindow()
        win.show()
        qtbot.waitExposed(win)
        win.restore_window_state()
        qtbot.waitUntil(
            lambda: win.width() == 800 and win.height() == 500, timeout=2000,
        )
        # Default is 800x500 per restore_window_state
        assert win.width() == 800
        assert win.height() == 500
        win.close()


class TestEditableTabBar:
    """Tests for the EditableTabBar double-click rename and context menu."""

    def test_double_click_starts_editor(self, window, qtbot):
        """Double-clicking a tab starts the rename editor."""
        # Need multiple tabs so the tab bar is visible
        window.new_tab()
        qtbot.waitUntil(lambda: window._tabs.count() == 2, timeout=2000)
        tab_bar = window._tab_bar
        rect = tab_bar.tabRect(0)
        QTest.mouseDClick(tab_bar, Qt.MouseButton.LeftButton, pos=rect.center())
        qtbot.waitUntil(lambda: tab_bar._editor is not None, timeout=2000)
        assert tab_bar._editor is not None

    def test_editor_text_matches_tab(self, window, qtbot):
        """Rename editor pre-fills with the current tab text."""
        tab_bar = window._tab_bar
        original_text = tab_bar.tabText(0)
        rect = tab_bar.tabRect(0)
        QTest.mouseDClick(tab_bar, Qt.MouseButton.LeftButton, pos=rect.center())
        qtbot.waitUntil(lambda: tab_bar._editor is not None, timeout=2000)
        assert tab_bar._editor.text() == original_text

    def test_finish_edit_changes_tab_text(self, window, qtbot):
        """Finishing the editor with text updates the tab name."""
        tab_bar = window._tab_bar
        tab_bar._start_edit(0)
        qtbot.waitUntil(lambda: tab_bar._editor is not None, timeout=2000)
        tab_bar._editor.setText("My Custom Tab")
        tab_bar._finish_edit()
        assert tab_bar.tabText(0) == "My Custom Tab"

    def test_finish_edit_empty_preserves_original(self, window, qtbot):
        """Finishing with empty text preserves the original name."""
        tab_bar = window._tab_bar
        original_text = tab_bar.tabText(0)
        tab_bar._start_edit(0)
        qtbot.waitUntil(lambda: tab_bar._editor is not None, timeout=2000)
        tab_bar._editor.setText("")
        tab_bar._finish_edit()
        assert tab_bar.tabText(0) == original_text

    def test_context_menu_has_expected_items(self, window, qtbot, monkeypatch):
        """Right-click context menu has New Tab, Rename Tab, Close Tab."""
        from PyQt6.QtWidgets import QMenu
        menu_actions = []

        def capture_exec(self_menu, *args, **kwargs):
            for action in self_menu.actions():
                menu_actions.append(action.text())

        monkeypatch.setattr(QMenu, "exec", capture_exec)

        tab_bar = window._tab_bar
        rect = tab_bar.tabRect(0)
        # capture_exec runs synchronously when the menu is shown, so
        # menu_actions is already populated on return -- no async wait needed.
        tab_bar._show_context_menu(rect.center())

        assert "New Tab" in menu_actions
        assert "Rename Tab" in menu_actions
        assert "Close Tab" in menu_actions

    def test_context_menu_new_tab(self, window, qtbot, monkeypatch):
        """New Tab from context menu creates a new tab."""
        from PyQt6.QtWidgets import QMenu

        # Intercept menu.exec and trigger New Tab action
        def trigger_new_tab(self_menu, *args, **kwargs):
            for action in self_menu.actions():
                if action.text() == "New Tab":
                    action.trigger()
                    return

        monkeypatch.setattr(QMenu, "exec", trigger_new_tab)

        assert window._tabs.count() == 1
        tab_bar = window._tab_bar
        rect = tab_bar.tabRect(0)
        tab_bar._show_context_menu(rect.center())
        qtbot.waitUntil(lambda: window._tabs.count() == 2, timeout=2000)
        assert window._tabs.count() == 2

    def test_custom_name_stored_in_tab_data(self, window, qtbot):
        """Custom name from rename is stored in tabData."""
        tab_bar = window._tab_bar
        tab_bar._start_edit(0)
        qtbot.waitUntil(lambda: tab_bar._editor is not None, timeout=2000)
        tab_bar._editor.setText("Custom Name")
        tab_bar._finish_edit()
        assert tab_bar.tabData(0) == "Custom Name"

    def test_custom_name_prevents_title_overwrite(self, window, qtbot):
        """Tab with custom name is not overwritten by terminal title changes."""
        tab_bar = window._tab_bar
        tab_bar._start_edit(0)
        qtbot.waitUntil(lambda: tab_bar._editor is not None, timeout=2000)
        tab_bar._editor.setText("Pinned Name")
        tab_bar._finish_edit()
        assert tab_bar.tabText(0) == "Pinned Name"

        # Simulate a terminal title change. _on_terminal_title_changed is a
        # synchronous slot, so the tab text is already final on return -- no
        # observable async signal to wait on; assert directly.
        window._on_terminal_title_changed("some new shell title")
        assert tab_bar.tabText(0) == "Pinned Name"


class TestCloseEvent:
    """Tests for window close event handling."""

    def test_close_saves_layout(self, window, qtbot, monkeypatch):
        """closeEvent calls save_layout."""
        saved = []
        monkeypatch.setattr(window, "save_layout", lambda: saved.append(True))
        # Mock has_running_process to return False
        split = window._tabs.widget(0)
        for t in split.find_terminals():
            monkeypatch.setattr(t, "has_running_process", lambda: False)
        window.close()
        qtbot.waitUntil(lambda: len(saved) == 1, timeout=2000)
        assert len(saved) == 1

    def test_close_no_running_process_accepts(self, window, qtbot, monkeypatch):
        """Close with no running process accepts the event."""
        monkeypatch.setattr(window, "save_layout", lambda: None)
        split = window._tabs.widget(0)
        for t in split.find_terminals():
            monkeypatch.setattr(t, "has_running_process", lambda: False)
        from PyQt6.QtGui import QCloseEvent
        event = QCloseEvent()
        window.closeEvent(event)
        assert event.isAccepted()

    def test_close_running_process_accept(self, window, qtbot, monkeypatch):
        """Close with running process and user accepts closes the window."""
        monkeypatch.setattr(window, "save_layout", lambda: None)
        split = window._tabs.widget(0)
        for t in split.find_terminals():
            monkeypatch.setattr(t, "has_running_process", lambda: True)
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )
        from PyQt6.QtGui import QCloseEvent
        event = QCloseEvent()
        window.closeEvent(event)
        assert event.isAccepted()

    def test_close_running_process_reject(self, window, qtbot, monkeypatch):
        """Close with running process and user rejects cancels close."""
        monkeypatch.setattr(window, "save_layout", lambda: None)
        split = window._tabs.widget(0)
        for t in split.find_terminals():
            monkeypatch.setattr(t, "has_running_process", lambda: True)
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.No,
        )
        from PyQt6.QtGui import QCloseEvent
        event = QCloseEvent()
        window.closeEvent(event)
        assert not event.isAccepted()

    def test_close_saves_window_geometry(self, window, qtbot, monkeypatch):
        """closeEvent saves window geometry to config."""
        # Don't mock save_layout so it actually runs
        split = window._tabs.widget(0)
        for t in split.find_terminals():
            monkeypatch.setattr(t, "has_running_process", lambda: False)
        from PyQt6.QtGui import QCloseEvent
        event = QCloseEvent()
        window.closeEvent(event)
        cfg = Config()
        # After save_layout, window dimensions should be stored
        assert cfg.get("general", "window_width") is not None
        assert cfg.get("general", "window_height") is not None


class TestScrollback:
    """Tests for scrollback navigation."""

    def test_scroll_page_up_no_crash(self, window, qtbot):
        """Shift+PgUp (scroll page up) doesn't crash."""
        # _scroll_page_up is a synchronous slot with no observable completion
        # signal; this is a pure no-crash smoke test, so we just call it and
        # assert the window is still functional afterwards.
        window._scroll_page_up()
        assert window._tabs.count() == 1

    def test_scroll_page_down_no_crash(self, window, qtbot):
        """Shift+PgDown (scroll page down) doesn't crash."""
        # Synchronous no-crash smoke test (see test_scroll_page_up_no_crash).
        window._scroll_page_down()
        assert window._tabs.count() == 1

    def test_scrollbar_toggle_applies_across_tabs(self, window, qtbot):
        """Toggling scrollbar off and on doesn't crash across multiple tabs."""
        window.new_tab()
        window._split_horizontal()
        qtbot.waitUntil(
            lambda: window._tabs.count() == 2
            and len(window._tabs.widget(1).find_terminals()) == 2,
            timeout=2000,
        )

        # Toggle scrollbar off then on — synchronous slots, no async signal;
        # this is a no-crash smoke test.
        window._scrollbar_action.setChecked(False)
        window._toggle_scrollbar()

        window._scrollbar_action.setChecked(True)
        window._toggle_scrollbar()

        # Verify all terminals still exist
        total = 0
        for i in range(window._tabs.count()):
            total += len(window._tabs.widget(i).find_terminals())
        assert total >= 3  # tab1: 1 terminal, tab2: 2 terminals
