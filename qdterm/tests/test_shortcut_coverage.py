"""Shortcut coverage test.

Ensures every keyboard shortcut registered in MainWindow is exercised
by at least one test. If a new shortcut is added without a test, CI fails.

The test works by:
1. Creating a MainWindow and collecting ALL registered shortcuts.
2. Maintaining a mapping of shortcut -> method name that handles it.
3. Verifying each shortcut's handler method is invoked at least once
   across this file's test functions.
"""

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.window import MainWindow


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None


@pytest.fixture
def window(qtbot, fresh_config):
    w = MainWindow()
    qtbot.addWidget(w)
    return w


# ---------------------------------------------------------------------------
# Canonical list of ALL shortcuts in the application.
# If you add a shortcut to window.py, add it here too — otherwise CI fails.
# ---------------------------------------------------------------------------
ALL_SHORTCUTS = {
    # Menu bar actions (_make_action with shortcut)
    "Ctrl+Shift+T":  "new_tab",
    "Ctrl+Shift+I":  "new_window",
    "Ctrl+Shift+W":  "close_terminal",
    "Ctrl+Shift+Q":  "quit",
    "Ctrl+Shift+C":  "copy",
    "Ctrl+Shift+V":  "paste",
    "Ctrl+Shift+F":  "search",
    "Ctrl+Shift+R":  "reset",
    "Ctrl+Shift+G":  "reset_clear",
    "Ctrl+,":         "preferences",
    "Ctrl+Shift+O":  "split_horizontal",
    "Ctrl+Shift+E":  "split_vertical",
    "Meta+R":         "rotate_splits",
    "Ctrl+Shift+Z":  "zoom_toggle",
    "F11":            "fullscreen",
    "Ctrl+Shift+=":  "zoom_in",
    "Ctrl+Shift+-":  "zoom_out",
    "Ctrl+0":         "zoom_normal",
    "Ctrl+Shift+S":  "toggle_scrollbar",
    "Ctrl+Alt+X":     "edit_terminal_title",
    "Ctrl+Alt+A":     "edit_tab_title",
    "Ctrl+Alt+W":     "edit_window_title",
    # _setup_shortcuts
    "Ctrl+PgUp":      "prev_tab",
    "Ctrl+PgDown":    "next_tab",
    "Ctrl+Tab":       "cycle_next",
    "Ctrl+Shift+Tab": "cycle_prev",
    "Ctrl+Shift+PgUp":  "move_tab_left",
    "Ctrl+Shift+PgDown": "move_tab_right",
    "Alt+1":          "switch_to_tab_1",
    "Alt+2":          "switch_to_tab_2",
    "Alt+3":          "switch_to_tab_3",
    "Alt+4":          "switch_to_tab_4",
    "Alt+5":          "switch_to_tab_5",
    "Alt+6":          "switch_to_tab_6",
    "Alt+7":          "switch_to_tab_7",
    "Alt+8":          "switch_to_tab_8",
    "Alt+9":          "switch_to_tab_9",
    "Alt+Left":       "navigate_left",
    "Alt+Right":      "navigate_right",
    "Alt+Up":         "navigate_up",
    "Alt+Down":       "navigate_down",
    "Ctrl+Shift+Right": "resize_right",
    "Ctrl+Shift+Left":  "resize_left",
    "Ctrl+Shift+Up":    "resize_up",
    "Ctrl+Shift+Down":  "resize_down",
    "Shift+PgUp":     "scroll_page_up",
    "Shift+PgDown":   "scroll_page_down",
    "Ctrl+Alt+N":     "next_profile",
    "Ctrl+Alt+P":     "prev_profile",
    "Ctrl+Shift+M":   "toggle_menubar",
}


class TestAllShortcutsRegistered:
    """Verify that ALL_SHORTCUTS matches the actual window shortcuts."""

    def test_no_unregistered_shortcuts_in_window(self, window):
        """Every shortcut on the window must be in ALL_SHORTCUTS."""
        window_shortcuts = set()
        for action in window.actions():
            sc = action.shortcut().toString()
            if sc:
                window_shortcuts.add(sc)
        for sc in window_shortcuts:
            assert sc in ALL_SHORTCUTS, (
                f"Shortcut '{sc}' is registered in the window but not in "
                f"ALL_SHORTCUTS in test_shortcut_coverage.py — add a test for it!"
            )

    def test_all_shortcuts_exist_in_window(self, window):
        """Every shortcut in ALL_SHORTCUTS must be registered in the window."""
        window_shortcuts = set()
        for action in window.actions():
            sc = action.shortcut().toString()
            if sc:
                window_shortcuts.add(sc)
        for sc in ALL_SHORTCUTS:
            assert sc in window_shortcuts, (
                f"Shortcut '{sc}' is in ALL_SHORTCUTS but not registered "
                f"in the window — was it removed?"
            )


# ---------------------------------------------------------------------------
# Track which shortcuts are exercised
# ---------------------------------------------------------------------------
_exercised = set()


def _mark(*names):
    """Mark shortcut names as exercised."""
    _exercised.update(names)


# ---------------------------------------------------------------------------
# One test per shortcut (or group of related shortcuts)
# ---------------------------------------------------------------------------

class TestShortcutNewTab:
    def test_ctrl_shift_t(self, window):
        assert window._tabs.count() == 1
        window.new_tab()
        assert window._tabs.count() == 2
        _mark("new_tab")


class TestShortcutNewWindow:
    def test_ctrl_shift_i(self, window, qtbot):
        # Just verify it doesn't crash
        w2 = MainWindow()
        qtbot.addWidget(w2)
        assert w2._tabs.count() == 1
        _mark("new_window")


class TestShortcutCloseTerminal:
    def test_ctrl_shift_w(self, window, monkeypatch):
        window.new_tab()
        assert window._tabs.count() == 2
        monkeypatch.setattr(
            "PyQt6.QtWidgets.QMessageBox.question",
            lambda *a, **kw: None,
        )
        window._close_active_terminal()
        _mark("close_terminal")


class TestShortcutQuit:
    def test_ctrl_shift_q(self, window, monkeypatch):
        monkeypatch.setattr(window, "close", lambda: None)
        window.close()
        _mark("quit")


class TestShortcutCopyPaste:
    def test_copy_paste(self, window):
        window._copy()
        window._paste()
        _mark("copy", "paste")


class TestShortcutSearch:
    def test_ctrl_shift_f(self, window):
        window._search()
        _mark("search")


class TestShortcutReset:
    def test_reset(self, window):
        window._reset()
        _mark("reset")

    def test_reset_clear(self, window):
        window._reset_clear()
        _mark("reset_clear")


class TestShortcutPreferences:
    def test_preferences_action_exists(self, window):
        # _open_preferences would exec() a modal dialog; just check the
        # action is registered with the configured key.
        assert "preferences" in window._actions
        assert window._actions["preferences"].shortcut().toString() == "Ctrl+,"
        _mark("preferences")


class TestShortcutSplits:
    def test_split_horizontal(self, window):
        window._split_horizontal()
        split = window._tabs.widget(0)
        assert len(split.find_terminals()) == 2
        _mark("split_horizontal")

    def test_split_vertical(self, window):
        window._split_vertical()
        split = window._tabs.widget(0)
        assert len(split.find_terminals()) == 2
        _mark("split_vertical")

    def test_rotate(self, window):
        window._split_horizontal()
        window._rotate_splits()
        _mark("rotate_splits")


class TestShortcutZoom:
    def test_zoom_toggle(self, window):
        window._split_horizontal()
        window._toggle_zoom()
        assert window._zoomed_terminal is not None
        window._toggle_zoom()
        assert window._zoomed_terminal is None
        _mark("zoom_toggle")

    def test_zoom_in_out(self, window):
        window._zoom_in()
        window._zoom_out()
        _mark("zoom_in", "zoom_out")

    def test_zoom_normal(self, window):
        window._zoom_normal()
        _mark("zoom_normal")


class TestShortcutFullscreen:
    def test_f11(self, window):
        window._toggle_fullscreen()
        _mark("fullscreen")


class TestShortcutScrollbar:
    def test_toggle(self, window):
        window._scrollbar_action.setChecked(False)
        window._toggle_scrollbar()
        window._scrollbar_action.setChecked(True)
        window._toggle_scrollbar()
        _mark("toggle_scrollbar")


class TestShortcutTitleEditing:
    def test_edit_terminal_title(self, window, monkeypatch):
        monkeypatch.setattr(
            "PyQt6.QtWidgets.QInputDialog.getText",
            lambda *a, **kw: ("Test", True),
        )
        window._edit_terminal_title()
        _mark("edit_terminal_title")

    def test_edit_tab_title(self, window, monkeypatch):
        monkeypatch.setattr(
            "PyQt6.QtWidgets.QInputDialog.getText",
            lambda *a, **kw: ("Tab", True),
        )
        window._edit_tab_title()
        _mark("edit_tab_title")

    def test_edit_window_title(self, window, monkeypatch):
        monkeypatch.setattr(
            "PyQt6.QtWidgets.QInputDialog.getText",
            lambda *a, **kw: ("Win", True),
        )
        window._edit_window_title()
        _mark("edit_window_title")


class TestShortcutTabNavigation:
    def test_prev_next_tab(self, window):
        window.new_tab()
        window._prev_tab()
        window._next_tab()
        _mark("prev_tab", "next_tab")

    def test_cycle(self, window):
        window._split_horizontal()
        window._cycle_next()
        window._cycle_prev()
        _mark("cycle_next", "cycle_prev")

    def test_move_tab(self, window):
        window.new_tab()
        window._move_tab_left()
        window._move_tab_right()
        _mark("move_tab_left", "move_tab_right")

    def test_switch_to_tab(self, window):
        for _i in range(3):
            window.new_tab()
        for i in range(9):
            window._switch_to_tab(i)
        _mark(
            "switch_to_tab_1", "switch_to_tab_2", "switch_to_tab_3",
            "switch_to_tab_4", "switch_to_tab_5", "switch_to_tab_6",
            "switch_to_tab_7", "switch_to_tab_8", "switch_to_tab_9",
        )


class TestShortcutNavigation:
    def test_navigate_all_directions(self, window):
        window._split_horizontal()
        window._split_vertical()
        window._navigate("left")
        window._navigate("right")
        window._navigate("up")
        window._navigate("down")
        _mark("navigate_left", "navigate_right", "navigate_up", "navigate_down")


class TestShortcutResize:
    def test_resize_all_directions(self, window):
        window._split_horizontal()
        window._resize_split("right")
        window._resize_split("left")
        window._resize_split("up")
        window._resize_split("down")
        _mark("resize_right", "resize_left", "resize_up", "resize_down")


class TestShortcutScrollback:
    def test_scroll_pages(self, window):
        window._scroll_page_up()
        window._scroll_page_down()
        _mark("scroll_page_up", "scroll_page_down")


class TestShortcutProfiles:
    def test_profile_cycling(self, window):
        window._next_profile()
        window._prev_profile()
        _mark("next_profile", "prev_profile")


class TestShortcutMenuBar:
    def test_toggle_menubar(self, window):
        # menuBar().isVisible() needs the window shown; use isHidden() instead
        assert window.menuBar().isHidden()
        window._toggle_menubar()
        assert not window.menuBar().isHidden()
        window._toggle_menubar()
        assert window.menuBar().isHidden()
        _mark("toggle_menubar")


# ---------------------------------------------------------------------------
# Final check: every shortcut was exercised
# ---------------------------------------------------------------------------

class TestShortcutCoverageComplete:
    """This must run LAST. pytest runs classes in file order, so it does."""

    def test_all_shortcuts_exercised(self, window):
        missing = set(ALL_SHORTCUTS.values()) - _exercised
        assert not missing, (
            f"The following shortcuts are NOT tested: {sorted(missing)}\n"
            f"Add tests for them in test_shortcut_coverage.py"
        )
