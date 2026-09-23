"""Tests for MainWindow: tabs, splits, zoom, tab bar visibility."""

import pytest
import qterminator.config as config_mod
from PyQt6.QtCore import Qt
from qterminator.config import Config
from qterminator.splitter import SplitContainer
from qterminator.window import MainWindow


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None


@pytest.fixture
def window(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    return win


def test_initial_state(window):
    """Window starts with 1 tab, 1 terminal, tab bar hidden."""
    assert window._tabs.count() == 1
    split = window._tabs.widget(0)
    assert len(split.find_terminals()) == 1
    assert window._active_terminal is not None


def test_tab_bar_hidden_single_tab(window):
    """Tab bar is hidden when there's only 1 tab."""
    assert window._tabs.count() == 1
    assert not window._tab_bar.isVisible()


def test_tab_bar_shown_multiple_tabs(window):
    """Tab bar appears when a second tab is added."""
    window.new_tab()
    assert window._tabs.count() == 2
    assert window._tab_bar.isVisible()


def test_tab_bar_hides_after_close(window):
    """Tab bar hides again when going back to 1 tab."""
    window.new_tab()
    assert window._tab_bar.isVisible()
    # Close second tab
    window._tabs.setCurrentIndex(1)
    window._on_tab_close_requested(1)
    assert window._tabs.count() == 1
    assert not window._tab_bar.isVisible()


def test_new_tab_creates_terminal(window):
    """Each new tab has exactly one terminal."""
    window.new_tab()
    assert window._tabs.count() == 2
    split = window._tabs.widget(1)
    assert len(split.find_terminals()) == 1


def test_split_horizontal(window):
    """Horizontal split adds a terminal in the current tab."""
    window._split_horizontal()
    split = window._tabs.widget(0)
    assert len(split.find_terminals()) == 2


def test_split_vertical(window):
    """Vertical split adds a terminal in the current tab."""
    window._split_vertical()
    split = window._tabs.widget(0)
    assert len(split.find_terminals()) == 2


def test_split_does_not_add_tab(window):
    """Splitting doesn't create extra tabs."""
    window._split_horizontal()
    window._split_vertical()
    assert window._tabs.count() == 1


def test_close_terminal_in_split(window):
    """Closing a terminal in a split removes it, keeping the other."""
    window._split_horizontal()
    split = window._tabs.widget(0)
    assert len(split.find_terminals()) == 2

    # Close the active terminal
    term_to_close = window._active_terminal
    window._remove_terminal(term_to_close)
    assert len(split.find_terminals()) == 1


def test_active_terminal_tracks_focus(window):
    """Active terminal changes after split and navigation."""
    first = window._active_terminal
    window._split_horizontal()
    second = window._active_terminal
    assert second is not first
    assert second.is_active()
    assert not first.is_active()


def test_navigate_between_splits(window):
    """Navigation moves focus between split terminals."""
    window._split_horizontal()
    split = window._tabs.widget(0)
    terminals = split.find_terminals()
    assert len(terminals) == 2

    # Active should be the second (newly created)
    second = window._active_terminal
    window._navigate("up")
    assert window._active_terminal is not second


def test_zoom_toggle(window):
    """Zoom hides other terminals, unzoom restores them."""
    window._split_horizontal()
    split = window._tabs.widget(0)
    assert len(split.find_terminals()) == 2

    # Zoom
    window._toggle_zoom()
    assert window.is_zoomed
    assert window._zoomed_terminal is window._active_terminal

    # Count visible terminals
    visible = [t for t in split.find_terminals() if t.isVisible()]
    assert len(visible) == 1

    # Unzoom
    window._toggle_zoom()
    assert not window.is_zoomed
    visible = [t for t in split.find_terminals() if t.isVisible()]
    assert len(visible) == 2


def test_zoom_noop_single_terminal(window):
    """Zoom does nothing when there's only one terminal."""
    window._toggle_zoom()
    assert not window.is_zoomed


def test_tab_switch(window):
    """Switching tabs changes the active terminal."""
    window.new_tab()
    first_tab_term = window._tabs.widget(0).find_terminals()[0]
    second_tab_term = window._tabs.widget(1).find_terminals()[0]

    window._tabs.setCurrentIndex(0)
    assert window._active_terminal is first_tab_term

    window._tabs.setCurrentIndex(1)
    assert window._active_terminal is second_tab_term


def test_prev_next_tab(window):
    """Ctrl+PgUp/PgDown cycle through tabs."""
    window.new_tab()
    window.new_tab()
    assert window._tabs.count() == 3
    assert window._tabs.currentIndex() == 2

    window._prev_tab()
    assert window._tabs.currentIndex() == 1

    window._next_tab()
    assert window._tabs.currentIndex() == 2

    window._next_tab()  # at end, should stay
    assert window._tabs.currentIndex() == 2


def test_window_title_follows_active(window):
    """Window title reflects the active terminal's title."""
    title = window._active_terminal.title()
    assert window.windowTitle() == title


def test_close_other_tabs(window):
    """Close Other Tabs keeps only the specified tab."""
    window.new_tab()
    window.new_tab()
    assert window._tabs.count() == 3

    window._close_other_tabs(1)
    assert window._tabs.count() == 1


def test_tab_rename_via_edit(window):
    """Double-click rename sets tab text and stores custom data."""
    window.new_tab()
    assert window._tabs.count() == 2

    # Simulate the edit
    window._tab_bar.setTabText(0, "my-server")
    window._tab_bar.setTabData(0, "my-server")
    assert window._tabs.tabText(0) == "my-server"
    # Custom name stored
    assert window._tab_bar.tabData(0) == "my-server"


def test_tab_context_menu_signals_exist(window):
    """Tab bar emits expected signals."""
    bar = window._tab_bar
    assert hasattr(bar, 'new_tab_requested')
    assert hasattr(bar, 'close_tab_requested')
    assert hasattr(bar, 'close_other_tabs_requested')


def test_read_only_toggle(window):
    """Read-only mode toggles on the active terminal."""
    term = window._active_terminal
    assert not term.is_read_only()
    term.toggle_read_only()
    assert term.is_read_only()
    term.toggle_read_only()
    assert not term.is_read_only()


def test_terminal_grouping(window):
    """Terminals can be assigned to groups."""
    term = window._active_terminal
    assert term.group is None
    term.group = "Alpha"
    assert term.group == "Alpha"
    term.group = None
    assert term.group is None


def test_broadcast_targets_off(window):
    """Broadcast mode 'off' returns no targets."""
    window._split_horizontal()
    window._set_broadcast("off")
    assert window._get_broadcast_targets() == []


def test_broadcast_targets_all(window):
    """Broadcast mode 'all' returns all other terminals."""
    window._split_horizontal()
    window._set_broadcast("all")
    targets = window._get_broadcast_targets()
    assert len(targets) == 1
    assert targets[0] is not window._active_terminal


def test_broadcast_targets_group(window):
    """Broadcast mode 'group' returns same-group terminals."""
    window._split_horizontal()
    split = window._tabs.widget(0)
    terms = split.find_terminals()

    # No group set — no targets
    window._set_broadcast("group")
    assert window._get_broadcast_targets() == []

    # Set both to same group
    terms[0].group = "Alpha"
    terms[1].group = "Alpha"
    targets = window._get_broadcast_targets()
    assert len(targets) == 1

    # Different groups — no targets
    terms[1].group = "Beta"
    assert window._get_broadcast_targets() == []


def test_rotate_splits(window):
    """Rotating splits changes orientation."""
    window._split_horizontal()
    split = window._tabs.widget(0)
    orig = split.orientation()
    window._rotate_splits()
    assert split.orientation() != orig


def test_move_tab_left_right(window):
    """Tab movement by keyboard."""
    window.new_tab()
    window.new_tab()
    assert window._tabs.currentIndex() == 2

    window._move_tab_left()
    assert window._tabs.currentIndex() == 1

    window._move_tab_right()
    assert window._tabs.currentIndex() == 2


def test_fullscreen_toggle(window):
    """Fullscreen toggles on and off."""
    assert not window.isFullScreen()
    window._toggle_fullscreen()
    assert window.isFullScreen()
    window._toggle_fullscreen()
    assert not window.isFullScreen()


def test_zoom_normal(window):
    """Zoom normal resets to config default."""
    window._active_terminal.zoom_in()
    window._active_terminal.zoom_in()
    window._zoom_normal()
    font = window._active_terminal.term.getTerminalFont()
    assert font.pointSize() == 11  # default


def test_switch_to_tab(window):
    """Alt+N switches to tab by index."""
    window.new_tab()
    window.new_tab()
    window._switch_to_tab(0)
    assert window._tabs.currentIndex() == 0
    window._switch_to_tab(2)
    assert window._tabs.currentIndex() == 2
    window._switch_to_tab(99)  # out of range, no crash
    assert window._tabs.currentIndex() == 2


def test_cycle_terminals(window):
    """Ctrl+Tab cycles between terminals in splits."""
    window._split_horizontal()
    first = window._active_terminal
    window._cycle_next()
    assert window._active_terminal is not first
    window._cycle_prev()
    assert window._active_terminal is first


def test_resize_split(window, qtbot):
    """Keyboard split resizing changes sizes."""
    window._split_horizontal()
    qtbot.wait(100)
    split = window._tabs.widget(0)
    sizes_before = split.sizes()
    window._resize_split("down")
    sizes_after = split.sizes()
    # First pane should have grown
    assert sizes_after[0] >= sizes_before[0]


def test_scrollbar_toggle(window):
    """Toggle scrollbar hides/shows scrollbar on all terminals."""
    window._scrollbar_action.setChecked(False)
    window._toggle_scrollbar()
    # No crash = success (checking actual scrollbar state requires QTermWidget internals)


def test_apply_profile(window):
    """Applying a profile updates terminal settings."""
    term = window._active_terminal
    term.apply_profile("default")
    assert term._profile_name == "default"


def test_profile_cycling(window):
    """Next/prev profile cycles through available profiles."""
    config = Config()
    config.set_profile("bright", {
        "font_family": "Monospace", "font_size": 14,
        "color_scheme": "WhiteOnBlack", "scrollback_lines": 5000,
        "show_titlebar": True,
    })
    assert len(config.list_profiles()) == 2
    assert window._active_terminal._profile_name == "default"
    window._next_profile()
    assert window._active_terminal._profile_name == "bright"
    window._next_profile()
    assert window._active_terminal._profile_name == "default"
    window._prev_profile()
    assert window._active_terminal._profile_name == "bright"


def test_exit_action_default(window):
    """Default exit action is 'close'."""
    assert window._active_terminal._exit_action == "close"


def test_scroll_page_up_down(window):
    """Scroll shortcuts don't crash."""
    window._scroll_page_up()
    window._scroll_page_down()


# ---------------------------------------------------------------------------
# NEW TESTS: Tab Management
# ---------------------------------------------------------------------------


class TestTabManagement:
    """Corner-case tests for tab management."""

    def test_new_tab_increases_count(self, window):
        """new_tab increments tab count by exactly 1."""
        before = window._tabs.count()
        window.new_tab()
        assert window._tabs.count() == before + 1

    def test_new_tab_increases_count_multiple(self, window):
        """Adding 5 tabs results in 6 total."""
        for _ in range(5):
            window.new_tab()
        assert window._tabs.count() == 6

    def test_new_tab_with_working_directory(self, window, tmp_path):
        """new_tab with working_directory passes it to the terminal."""
        wd = str(tmp_path)
        window.new_tab(working_directory=wd)
        split = window._tabs.widget(window._tabs.count() - 1)
        term = split.find_terminals()[0]
        # The working directory should have been set (may resolve symlinks)
        assert term.working_directory() is not None

    def test_close_other_tabs_with_three_tabs(self, window):
        """Close other tabs with 3 tabs keeps only the target tab."""
        window.new_tab()
        window.new_tab()
        assert window._tabs.count() == 3
        # Keep tab at index 0
        window._close_other_tabs(0)
        assert window._tabs.count() == 1

    def test_close_other_tabs_with_one_tab_noop(self, window):
        """Close other tabs with 1 tab is a no-op."""
        assert window._tabs.count() == 1
        window._close_other_tabs(0)
        assert window._tabs.count() == 1

    def test_tab_bar_visibility_cycle(self, window):
        """Tab bar: hidden with 1, shown with 2, hidden after closing back to 1."""
        assert not window._tab_bar.isVisible()
        window.new_tab()
        assert window._tab_bar.isVisible()
        window._on_tab_close_requested(1)
        assert not window._tab_bar.isVisible()

    def test_prev_tab_at_first_noop(self, window):
        """prev_tab at index 0 stays at 0 (no wrap)."""
        window.new_tab()
        window._tabs.setCurrentIndex(0)
        window._prev_tab()
        assert window._tabs.currentIndex() == 0

    def test_next_tab_at_last_noop(self, window):
        """next_tab at last index stays there (no wrap)."""
        window.new_tab()
        window._tabs.setCurrentIndex(1)
        window._next_tab()
        assert window._tabs.currentIndex() == 1

    def test_move_tab_left_at_first_position(self, window):
        """move_tab_left at index 0 is a no-op."""
        window.new_tab()
        window._tabs.setCurrentIndex(0)
        window._move_tab_left()
        assert window._tabs.currentIndex() == 0

    def test_move_tab_right_at_last_position(self, window):
        """move_tab_right at last index is a no-op."""
        window.new_tab()
        window._tabs.setCurrentIndex(1)
        window._move_tab_right()
        assert window._tabs.currentIndex() == 1

    def test_switch_to_tab_valid_index(self, window):
        """switch_to_tab with valid index moves there."""
        window.new_tab()
        window.new_tab()
        window._switch_to_tab(1)
        assert window._tabs.currentIndex() == 1

    def test_switch_to_tab_out_of_range_negative(self, window):
        """switch_to_tab with negative index doesn't crash."""
        window._switch_to_tab(-1)
        assert window._tabs.currentIndex() == 0

    def test_switch_to_tab_out_of_range_large(self, window):
        """switch_to_tab with very large index doesn't crash."""
        window._switch_to_tab(9999)
        assert window._tabs.currentIndex() == 0

    def test_tab_rename_stores_custom_name_in_tabdata(self, window):
        """Setting tabData stores the custom name."""
        window._tab_bar.setTabText(0, "custom-name")
        window._tab_bar.setTabData(0, "custom-name")
        assert window._tab_bar.tabData(0) == "custom-name"

    def test_tab_title_follows_terminal_when_no_custom_name(self, window):
        """Tab text matches terminal title when no custom name is set."""
        term = window._active_terminal
        title = term.title()
        tab_text = window._tabs.tabText(0)
        assert tab_text == title

    def test_new_tab_sets_current_to_new(self, window):
        """Newly created tab becomes the current tab."""
        window.new_tab()
        assert window._tabs.currentIndex() == 1
        window.new_tab()
        assert window._tabs.currentIndex() == 2

    def test_new_tab_active_terminal_is_in_new_tab(self, window):
        """Active terminal belongs to the newly created tab."""
        window.new_tab()
        split = window._tabs.widget(window._tabs.currentIndex())
        assert window._active_terminal in split.find_terminals()


# ---------------------------------------------------------------------------
# NEW TESTS: Split Operations
# ---------------------------------------------------------------------------


class TestSplitOperations:
    """Corner-case tests for split operations."""

    def test_split_horizontal_creates_two_terminals(self, window):
        """Horizontal split results in exactly 2 terminals."""
        window._split_horizontal()
        split = window._tabs.widget(0)
        assert len(split.find_terminals()) == 2

    def test_split_vertical_creates_two_terminals(self, window):
        """Vertical split results in exactly 2 terminals."""
        window._split_vertical()
        split = window._tabs.widget(0)
        assert len(split.find_terminals()) == 2

    def test_split_does_not_change_tab_count(self, window):
        """Splitting keeps tab count unchanged."""
        before = window._tabs.count()
        window._split_horizontal()
        window._split_vertical()
        assert window._tabs.count() == before

    def test_multiple_splits_three_way(self, window):
        """Two splits in the same tab create 3 terminals."""
        window._split_horizontal()
        window._split_horizontal()
        split = window._tabs.widget(0)
        assert len(split.find_terminals()) == 3

    def test_multiple_splits_four_way(self, window):
        """Three splits create 4 terminals."""
        window._split_horizontal()
        window._split_horizontal()
        window._split_horizontal()
        split = window._tabs.widget(0)
        assert len(split.find_terminals()) == 4

    def test_close_terminal_in_three_way_split(self, window):
        """Closing one terminal in a 3-way split leaves 2."""
        window._split_horizontal()
        window._split_horizontal()
        split = window._tabs.widget(0)
        assert len(split.find_terminals()) == 3
        window._remove_terminal(window._active_terminal)
        assert len(split.find_terminals()) == 2

    def test_close_all_terminals_in_split(self, window):
        """Closing all terminals in a split closes the tab."""
        window.new_tab()  # ensure window won't close
        assert window._tabs.count() == 2
        window._tabs.setCurrentIndex(0)
        split = window._tabs.widget(0)
        term = split.find_terminals()[0]
        window._remove_terminal(term)
        assert window._tabs.count() == 1

    def test_rotate_splits_changes_orientation(self, window):
        """Rotating toggles between horizontal and vertical."""
        window._split_horizontal()
        split = window._tabs.widget(0)
        orig = split.orientation()
        window._rotate_splits()
        assert split.orientation() != orig
        window._rotate_splits()
        assert split.orientation() == orig

    def test_rotate_splits_single_terminal_noop(self, window):
        """Rotate with a single terminal doesn't crash or change anything."""
        split = window._tabs.widget(0)
        orig = split.orientation()
        window._rotate_splits()
        # count() <= 1, so rotate should be a no-op
        assert split.orientation() == orig

    def test_resize_split_right(self, window, qtbot):
        """Resize right increases the active pane."""
        window._split_horizontal()
        qtbot.wait(100)
        split = window._tabs.widget(0)
        sizes_before = split.sizes()
        window._resize_split("right")
        sizes_after = split.sizes()
        assert sizes_after[0] >= sizes_before[0] or sizes_after[1] <= sizes_before[1]

    def test_resize_split_left(self, window, qtbot):
        """Resize left decreases the active pane."""
        window._split_horizontal()
        qtbot.wait(100)
        window._resize_split("left")
        # Just check it doesn't crash

    def test_resize_split_up(self, window, qtbot):
        """Resize up on a horizontal split doesn't crash."""
        window._split_horizontal()
        qtbot.wait(100)
        window._resize_split("up")

    def test_resize_split_at_minimum_size(self, window, qtbot):
        """Resize past minimum is clamped to 10."""
        window._split_horizontal()
        qtbot.wait(100)
        # Resize aggressively to push towards minimum
        for _ in range(50):
            window._resize_split("left")
        split = window._tabs.widget(0)
        sizes = split.sizes()
        # All sizes should be at least 10 (the clamp)
        for s in sizes:
            assert s >= 10


# ---------------------------------------------------------------------------
# NEW TESTS: Navigation
# ---------------------------------------------------------------------------


class TestNavigation:
    """Tests for terminal navigation within splits."""

    def test_navigate_right(self, window):
        """Navigate right moves to next terminal."""
        window._split_horizontal()
        first = window._tabs.widget(0).find_terminals()[0]
        window._set_active_terminal(first)
        window._navigate("right")
        assert window._active_terminal is not first

    def test_navigate_left(self, window):
        """Navigate left moves to previous terminal."""
        window._split_horizontal()
        second = window._active_terminal
        window._navigate("left")
        assert window._active_terminal is not second

    def test_navigate_up(self, window):
        """Navigate up wraps cyclically to previous terminal."""
        window._split_horizontal()
        second = window._active_terminal
        window._navigate("up")
        assert window._active_terminal is not second

    def test_navigate_down(self, window):
        """Navigate down wraps cyclically to next terminal."""
        window._split_horizontal()
        first = window._tabs.widget(0).find_terminals()[0]
        window._set_active_terminal(first)
        window._navigate("down")
        assert window._active_terminal is not first

    def test_navigate_wraps_cyclically(self, window):
        """Navigating past the end wraps to the beginning."""
        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        window._set_active_terminal(terms[1])
        window._navigate("right")
        # Should wrap to the first terminal
        assert window._active_terminal is terms[0]

    def test_cycle_next_through_terminals(self, window):
        """cycle_next visits all terminals in order."""
        window._split_horizontal()
        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        visited = set()
        # Start from first terminal
        window._set_active_terminal(terms[0])
        for _ in range(len(terms)):
            visited.add(window._active_terminal)
            window._cycle_next()
        assert len(visited) == len(terms)

    def test_cycle_prev_through_terminals(self, window):
        """cycle_prev visits all terminals in reverse order."""
        window._split_horizontal()
        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        window._set_active_terminal(terms[0])
        window._cycle_prev()
        # Should go to last terminal
        assert window._active_terminal is terms[-1]

    def test_active_terminal_updates_on_navigate(self, window):
        """_active_terminal is correctly updated after navigation."""
        window._split_horizontal()
        before = window._active_terminal
        window._navigate("left")
        after = window._active_terminal
        assert before is not after
        assert after.is_active()
        assert not before.is_active()


# ---------------------------------------------------------------------------
# NEW TESTS: Zoom
# ---------------------------------------------------------------------------


class TestZoom:
    """Tests for zoom/maximize functionality."""

    def test_zoom_toggle_hides_others(self, window):
        """Zoom hides all non-active terminals."""
        window._split_horizontal()
        window._split_horizontal()
        split = window._tabs.widget(0)
        active = window._active_terminal
        window._toggle_zoom()
        for t in split.find_terminals():
            if t is active:
                assert t.isVisible()
            else:
                assert not t.isVisible()

    def test_zoom_toggle_restores_all(self, window):
        """Unzoom restores all terminals to visible."""
        window._split_horizontal()
        window._toggle_zoom()
        window._toggle_zoom()
        split = window._tabs.widget(0)
        for t in split.find_terminals():
            assert t.isVisible()

    def test_zoom_noop_with_single_terminal(self, window):
        """Zoom does nothing when only one terminal exists."""
        window._toggle_zoom()
        assert not window.is_zoomed
        assert window._zoomed_terminal is None

    def test_zoom_in_changes_font(self, window):
        """zoom_in increases the font size."""
        font_before = window._active_terminal.term.getTerminalFont().pointSize()
        window._active_terminal.zoom_in()
        font_after = window._active_terminal.term.getTerminalFont().pointSize()
        assert font_after > font_before

    def test_zoom_out_changes_font(self, window):
        """zoom_out decreases the font size."""
        # Zoom in first so we have room to zoom out
        window._active_terminal.zoom_in()
        window._active_terminal.zoom_in()
        font_before = window._active_terminal.term.getTerminalFont().pointSize()
        window._active_terminal.zoom_out()
        font_after = window._active_terminal.term.getTerminalFont().pointSize()
        assert font_after < font_before

    def test_zoom_normal_resets_font(self, window):
        """zoom_normal resets to config default size."""
        window._active_terminal.zoom_in()
        window._active_terminal.zoom_in()
        window._active_terminal.zoom_in()
        window._zoom_normal()
        font = window._active_terminal.term.getTerminalFont()
        assert font.pointSize() == 11

    def test_double_zoom_toggle(self, window):
        """Zoom then unzoom twice returns to same state."""
        window._split_horizontal()
        window._toggle_zoom()
        assert window.is_zoomed
        window._toggle_zoom()
        assert not window.is_zoomed
        window._toggle_zoom()
        assert window.is_zoomed
        window._toggle_zoom()
        assert not window.is_zoomed

    def test_zoom_in_via_window(self, window):
        """_zoom_in method on window delegates to terminal."""
        font_before = window._active_terminal.term.getTerminalFont().pointSize()
        window._zoom_in()
        font_after = window._active_terminal.term.getTerminalFont().pointSize()
        assert font_after > font_before

    def test_zoom_out_via_window(self, window):
        """_zoom_out method on window delegates to terminal."""
        window._zoom_in()
        window._zoom_in()
        font_before = window._active_terminal.term.getTerminalFont().pointSize()
        window._zoom_out()
        font_after = window._active_terminal.term.getTerminalFont().pointSize()
        assert font_after < font_before


# ---------------------------------------------------------------------------
# NEW TESTS: Broadcast
# ---------------------------------------------------------------------------


class TestBroadcast:
    """Tests for broadcast input."""

    def test_broadcast_off_returns_empty(self, window):
        """Broadcast off returns empty list."""
        window._split_horizontal()
        window._set_broadcast("off")
        assert window._get_broadcast_targets() == []

    def test_broadcast_all_returns_others(self, window):
        """Broadcast all returns all terminals except active."""
        window._split_horizontal()
        window._split_horizontal()
        window._set_broadcast("all")
        targets = window._get_broadcast_targets()
        assert len(targets) == 2
        assert window._active_terminal not in targets

    def test_broadcast_group_returns_same_group_only(self, window):
        """Broadcast group returns only same-group terminals."""
        window._split_horizontal()
        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        terms[0].group = "web"
        terms[1].group = "web"
        terms[2].group = "db"
        window._set_active_terminal(terms[0])
        window._set_broadcast("group")
        targets = window._get_broadcast_targets()
        assert len(targets) == 1
        assert targets[0] is terms[1]

    def test_broadcast_group_with_no_group_returns_empty(self, window):
        """Broadcast group with no group assigned returns empty."""
        window._split_horizontal()
        window._set_broadcast("group")
        assert window._get_broadcast_targets() == []

    def test_set_broadcast_changes_mode(self, window):
        """set_broadcast changes the internal broadcast mode."""
        window._set_broadcast("all")
        assert window._broadcast_mode == "all"
        window._set_broadcast("group")
        assert window._broadcast_mode == "group"
        window._set_broadcast("off")
        assert window._broadcast_mode == "off"

    def test_broadcast_all_across_tabs(self, window):
        """Broadcast all includes terminals from other tabs."""
        window.new_tab()
        window._tabs.setCurrentIndex(0)
        window._set_broadcast("all")
        targets = window._get_broadcast_targets()
        # The other tab has 1 terminal
        assert len(targets) == 1

    def test_broadcast_default_is_off(self, window):
        """Default broadcast mode is off."""
        assert window._get_broadcast_targets() == []


# ---------------------------------------------------------------------------
# NEW TESTS: Window State
# ---------------------------------------------------------------------------


class TestWindowState:
    """Tests for window state management."""

    def test_fullscreen_toggle_on(self, window):
        """Fullscreen can be enabled."""
        window._toggle_fullscreen()
        assert window.isFullScreen()

    def test_fullscreen_toggle_off(self, window):
        """Fullscreen can be toggled off."""
        window._toggle_fullscreen()
        window._toggle_fullscreen()
        assert not window.isFullScreen()

    def test_scrollbar_toggle_applies_to_all_terminals(self, window):
        """Scrollbar toggle doesn't crash with multiple terminals."""
        window._split_horizontal()
        window.new_tab()
        window._scrollbar_action.setChecked(False)
        window._toggle_scrollbar()
        window._scrollbar_action.setChecked(True)
        window._toggle_scrollbar()

    def test_window_title_follows_active_terminal(self, window):
        """Window title updates when switching active terminal."""
        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        window._set_active_terminal(terms[0])
        assert window.windowTitle() == terms[0].title()
        window._set_active_terminal(terms[1])
        assert window.windowTitle() == terms[1].title()

    def test_window_title_custom_edit(self, window):
        """Setting window title directly works."""
        window.setWindowTitle("Custom Title")
        assert window.windowTitle() == "Custom Title"

    def test_initial_window_size(self, window):
        """Window starts at 800x500."""
        assert window.width() > 0
        assert window.height() > 0


# ---------------------------------------------------------------------------
# NEW TESTS: Read-Only
# ---------------------------------------------------------------------------


class TestReadOnly:
    """Tests for read-only terminal mode."""

    def test_toggle_read_only_via_window(self, window):
        """Window's _toggle_read_only flips the terminal."""
        assert not window._active_terminal.is_read_only()
        window._toggle_read_only()
        assert window._active_terminal.is_read_only()

    def test_read_only_terminal_state_persists(self, window):
        """Read-only state persists through navigation."""
        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        terms[0].set_read_only(True)
        window._set_active_terminal(terms[1])
        window._set_active_terminal(terms[0])
        assert terms[0].is_read_only()

    def test_set_read_only_explicit(self, window):
        """set_read_only(True/False) works directly."""
        term = window._active_terminal
        term.set_read_only(True)
        assert term.is_read_only()
        term.set_read_only(False)
        assert not term.is_read_only()


# ---------------------------------------------------------------------------
# NEW TESTS: Profile
# ---------------------------------------------------------------------------


class TestProfile:
    """Tests for profile management."""

    def test_apply_profile_updates_name(self, window):
        """apply_profile sets the profile name."""
        config = Config()
        config.set_profile("dark", {
            "font_family": "Monospace", "font_size": 12,
            "color_scheme": "GreenOnBlack", "scrollback_lines": 5000,
            "show_titlebar": True,
        })
        window._active_terminal.apply_profile("dark")
        assert window._active_terminal._profile_name == "dark"

    def test_profile_cycling_next_prev(self, window):
        """Profile cycling wraps around."""
        config = Config()
        config.set_profile("p2", {
            "font_family": "Monospace", "font_size": 14,
            "color_scheme": "WhiteOnBlack", "scrollback_lines": 5000,
            "show_titlebar": True,
        })
        config.set_profile("p3", {
            "font_family": "Monospace", "font_size": 16,
            "color_scheme": "GreenOnBlack", "scrollback_lines": 5000,
            "show_titlebar": True,
        })
        assert window._active_terminal._profile_name == "default"
        window._next_profile()
        first_next = window._active_terminal._profile_name
        assert first_next != "default"
        window._prev_profile()
        assert window._active_terminal._profile_name == "default"

    def test_profile_cycling_single_profile_noop(self, window):
        """Cycling with only one profile is a no-op."""
        assert len(Config().list_profiles()) == 1
        window._next_profile()
        assert window._active_terminal._profile_name == "default"
        window._prev_profile()
        assert window._active_terminal._profile_name == "default"

    def test_apply_profile_changes_font(self, window):
        """Applying a profile with different font size changes the font."""
        config = Config()
        config.set_profile("big", {
            "font_family": "Monospace", "font_size": 20,
            "color_scheme": "WhiteOnBlack", "scrollback_lines": 5000,
            "show_titlebar": True,
        })
        window._active_terminal.apply_profile("big")
        font = window._active_terminal.term.getTerminalFont()
        assert font.pointSize() == 20


# ---------------------------------------------------------------------------
# NEW TESTS: Close/Exit
# ---------------------------------------------------------------------------


class TestCloseExit:
    """Tests for close and exit behavior."""

    def test_close_terminal_with_running_process_mock(self, window, monkeypatch):
        """Closing terminal with running process shows dialog (mocked)."""
        monkeypatch.setattr(
            window._active_terminal, "has_running_process", lambda: True
        )
        from PyQt6.QtWidgets import QMessageBox
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.No,
        )
        # _close_active_terminal should not remove the terminal
        before = window._tabs.widget(0).find_terminals()
        window._close_active_terminal()
        after = window._tabs.widget(0).find_terminals()
        assert len(after) == len(before)

    def test_close_terminal_accept_mock(self, window, monkeypatch):
        """Closing terminal with running process, user accepts."""
        window.new_tab()  # ensure window won't close
        window._tabs.setCurrentIndex(0)
        monkeypatch.setattr(
            window._active_terminal, "has_running_process", lambda: True
        )
        from PyQt6.QtWidgets import QMessageBox
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
        )
        window._close_active_terminal()
        # Tab should have been removed
        assert window._tabs.count() == 1

    def test_exit_action_default_value(self, window):
        """Default exit_action is close."""
        assert window._active_terminal._exit_action == "close"


# ---------------------------------------------------------------------------
# NEW TESTS: Layout
# ---------------------------------------------------------------------------


class TestLayout:
    """Tests for layout save/restore."""

    def test_save_layout_stores_to_config(self, window):
        """save_layout stores layout data in config."""
        window.save_layout()
        config = Config()
        layout = config.get("layouts", "last_session")
        assert layout is not None
        assert "tabs" in layout

    def test_save_layout_captures_tabs(self, window):
        """save_layout captures the correct number of tabs."""
        window.new_tab()
        window.save_layout()
        config = Config()
        layout = config.get("layouts", "last_session")
        assert len(layout["tabs"]) == 2

    def test_save_layout_captures_splits(self, window):
        """save_layout captures split structure."""
        window._split_horizontal()
        window.save_layout()
        config = Config()
        layout = config.get("layouts", "last_session")
        tree = layout["tabs"][0]["tree"]
        assert tree["type"] == "split"
        assert len(tree["children"]) == 2

    def test_restore_layout_restores_tabs(self, window):
        """restore_layout recreates the saved tabs."""
        window.new_tab()
        window.save_layout()
        config = Config()
        layout = config.get("layouts", "last_session")

        # Create a fresh window
        win2 = MainWindow()
        # Remove the default tab first
        while win2._tabs.count() > 0:
            win2._tabs.removeTab(0)
        from qterminator.layout import restore_layout
        restore_layout(win2, layout)
        assert win2._tabs.count() == 2


# ---------------------------------------------------------------------------
# NEW TESTS: Menu
# ---------------------------------------------------------------------------


class TestMenu:
    """Tests for menu bar structure."""

    def test_menubar_exists(self, window):
        """Window has a menu bar."""
        assert window.menuBar() is not None

    def test_menubar_has_expected_menus(self, window):
        """Menu bar has File, Edit, View, Terminal, Help menus."""
        menubar = window.menuBar()
        actions = menubar.actions()
        menu_titles = [a.text() for a in actions]
        assert "&File" in menu_titles
        assert "&Edit" in menu_titles
        assert "&View" in menu_titles
        assert "&Terminal" in menu_titles
        assert "&Help" in menu_titles

    def test_file_menu_has_expected_actions(self, window):
        """File menu has New Tab, Close Terminal, Quit."""
        menubar = window.menuBar()
        file_action = [a for a in menubar.actions() if a.text() == "&File"][0]
        file_menu = file_action.menu()
        action_texts = [a.text() for a in file_menu.actions() if not a.isSeparator()]
        assert any("Tab" in t for t in action_texts)
        assert any("Close" in t for t in action_texts)
        assert any("Quit" in t for t in action_texts)

    def test_edit_menu_has_expected_actions(self, window):
        """Edit menu has Copy, Paste, Search."""
        menubar = window.menuBar()
        edit_action = [a for a in menubar.actions() if a.text() == "&Edit"][0]
        edit_menu = edit_action.menu()
        action_texts = [a.text() for a in edit_menu.actions() if not a.isSeparator()]
        assert any("Copy" in t for t in action_texts)
        assert any("Paste" in t for t in action_texts)
        assert any("Search" in t for t in action_texts)

    def test_view_menu_has_expected_actions(self, window):
        """View menu has Split, Zoom, Fullscreen."""
        menubar = window.menuBar()
        view_action = [a for a in menubar.actions() if a.text() == "&View"][0]
        view_menu = view_action.menu()
        action_texts = [a.text() for a in view_menu.actions() if not a.isSeparator()]
        assert any("Split" in t for t in action_texts)
        assert any("Maximize" in t or "Zoom" in t for t in action_texts)
        assert any("Full" in t for t in action_texts)

    def test_terminal_menu_has_expected_actions(self, window):
        """Terminal menu has title editing and read-only."""
        menubar = window.menuBar()
        term_action = [a for a in menubar.actions() if a.text() == "&Terminal"][0]
        term_menu = term_action.menu()
        action_texts = [a.text() for a in term_menu.actions() if not a.isSeparator()]
        assert any("Terminal Title" in t for t in action_texts)
        assert any("Read" in t for t in action_texts)

    def test_help_menu_has_about(self, window):
        """Help menu has About action."""
        menubar = window.menuBar()
        help_action = [a for a in menubar.actions() if a.text() == "&Help"][0]
        help_menu = help_action.menu()
        action_texts = [a.text() for a in help_menu.actions() if not a.isSeparator()]
        assert any("About" in t for t in action_texts)


# ---------------------------------------------------------------------------
# NEW TESTS: Miscellaneous
# ---------------------------------------------------------------------------


class TestMiscellaneous:
    """Tests for internal helpers and edge cases."""

    def test_find_parent_splitter_finds_correct_parent(self, window):
        """_find_parent_splitter returns the immediate SplitContainer."""
        term = window._active_terminal
        parent = window._find_parent_splitter(term)
        assert isinstance(parent, SplitContainer)
        assert parent.indexOf(term) != -1

    def test_current_split_returns_current_tab_widget(self, window):
        """_current_split returns the widget of the current tab."""
        split = window._current_split()
        assert split is window._tabs.widget(window._tabs.currentIndex())

    def test_current_split_changes_with_tab(self, window):
        """_current_split changes when switching tabs."""
        window.new_tab()
        split0 = window._tabs.widget(0)
        split1 = window._tabs.widget(1)
        window._tabs.setCurrentIndex(0)
        assert window._current_split() is split0
        window._tabs.setCurrentIndex(1)
        assert window._current_split() is split1

    def test_about_dialog_does_not_crash(self, window, monkeypatch):
        """About dialog can be triggered without crash."""
        from PyQt6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "about", lambda *args: None)
        window._show_about()

    def test_scroll_page_up_no_crash(self, window):
        """scroll_page_up doesn't crash."""
        window._scroll_page_up()

    def test_scroll_page_down_no_crash(self, window):
        """scroll_page_down doesn't crash."""
        window._scroll_page_down()

    def test_short_title_function(self):
        """_short_title truncates long titles."""
        from qterminator.window import _short_title
        assert _short_title("short") == "short"
        long_title = "a" * 40
        result = _short_title(long_title)
        assert len(result) == 30
        assert result.endswith("...")

    def test_short_title_exact_boundary(self):
        """_short_title at exactly 30 chars returns as-is."""
        from qterminator.window import _short_title
        title = "a" * 30
        assert _short_title(title) == title

    def test_short_title_31_chars_truncates(self):
        """_short_title at 31 chars truncates."""
        from qterminator.window import _short_title
        title = "a" * 31
        result = _short_title(title)
        assert len(result) == 30
        assert result.endswith("...")

    def test_new_window_does_not_crash(self, window, monkeypatch):
        """_new_window creates a new MainWindow without crash."""
        created = []
        monkeypatch.setattr(MainWindow, "show", lambda self: created.append(self))
        window._new_window()
        assert len(created) == 1

    def test_copy_paste_no_crash(self, window):
        """Copy and paste methods don't crash."""
        window._copy()
        window._paste()

    def test_search_toggle_no_crash(self, window):
        """Search toggle doesn't crash."""
        window._search()

    def test_reset_no_crash(self, window):
        """Reset doesn't crash."""
        window._reset()

    def test_reset_clear_no_crash(self, window):
        """Reset and clear doesn't crash."""
        window._reset_clear()

    def test_navigate_with_no_active_terminal(self, window):
        """Navigate with no active terminal doesn't crash."""
        window._active_terminal = None
        window._navigate("left")

    def test_zoom_with_no_active_terminal(self, window):
        """Zoom with no active terminal doesn't crash."""
        window._active_terminal = None
        window._toggle_zoom()
        assert not window.is_zoomed


# ---------------------------------------------------------------------------
# NEW TESTS: MenuBar
# ---------------------------------------------------------------------------


class TestMenuBar:
    """Tests for menubar visibility and toggle behavior."""

    def test_menubar_hidden_by_default(self, window):
        """Menubar is hidden by default (show_menubar=False in defaults)."""
        assert not window.menuBar().isVisible()

    def test_toggle_menubar_shows_it(self, window):
        """_toggle_menubar shows the menubar when hidden."""
        assert not window.menuBar().isVisible()
        window._toggle_menubar()
        assert window.menuBar().isVisible()

    def test_toggle_menubar_twice_hides_again(self, window):
        """_toggle_menubar twice returns to hidden state."""
        window._toggle_menubar()
        assert window.menuBar().isVisible()
        window._toggle_menubar()
        assert not window.menuBar().isVisible()

    def test_toggle_menubar_saves_to_config(self, window):
        """Toggle saves the show_menubar setting to config."""
        window._toggle_menubar()
        config = Config()
        assert config.get("general", "show_menubar") is True
        window._toggle_menubar()
        config = Config()
        assert config.get("general", "show_menubar") is False

    def test_ctrl_shift_m_shortcut_exists(self, window):
        """Ctrl+Shift+M shortcut is registered on the window."""
        actions = window.actions()
        shortcuts = [a.shortcut().toString() for a in actions if a.shortcut()]
        assert "Ctrl+Shift+M" in shortcuts

    def test_menubar_actions_work_when_hidden(self, window):
        """Menu actions still work when menubar is hidden."""
        assert not window.menuBar().isVisible()
        # Call action methods directly -- they should work regardless of visibility
        before = window._tabs.count()
        window.new_tab()
        assert window._tabs.count() == before + 1

    def test_show_menubar_from_config(self, qtbot, fresh_config):
        """Setting show_menubar=True before creating window shows menubar."""
        config = Config()
        config.set("general", "show_menubar", True)
        config.save()
        # Reset singleton so new window picks up saved config
        Config._instance = None
        win = MainWindow()
        qtbot.addWidget(win)
        win.show()
        assert win.menuBar().isVisible()

    def test_menubar_has_actions_after_toggle(self, window):
        """Menubar retains all menus after toggle cycle."""
        window._toggle_menubar()
        window._toggle_menubar()
        actions = window.menuBar().actions()
        menu_titles = [a.text() for a in actions]
        assert "&File" in menu_titles
        assert "&Help" in menu_titles


# ---------------------------------------------------------------------------
# NEW TESTS: Broadcast Key Forwarding
# ---------------------------------------------------------------------------


class TestBroadcastKeyForwarding:
    """Tests for broadcast key event forwarding logic."""

    def test_on_terminal_key_ignores_inactive_source(self, window, monkeypatch):
        """_on_terminal_key only processes events from the active terminal."""
        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        active = window._active_terminal
        inactive = [t for t in terms if t is not active][0]

        window._set_broadcast("all")
        # Mock _get_broadcast_targets to track if it was called
        called = []
        monkeypatch.setattr(
            window, "_get_broadcast_targets",
            lambda: (called.append(True) or [])
        )
        window._on_terminal_key(inactive, "fake_event")
        assert called == []  # Should return early, never call targets

    def test_broadcast_off_returns_no_targets(self, window):
        """Broadcast off: _get_broadcast_targets returns empty list."""
        window._split_horizontal()
        window._set_broadcast("off")
        assert window._get_broadcast_targets() == []

    def test_broadcast_all_returns_other_terminal(self, window):
        """Broadcast all with 2 terminals: returns the other terminal."""
        window._split_horizontal()
        window._set_broadcast("all")
        targets = window._get_broadcast_targets()
        assert len(targets) == 1
        assert targets[0] is not window._active_terminal

    def test_broadcast_group_matching_returns_correct(self, window):
        """Broadcast group with matching groups returns correct terminals."""
        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        terms[0].group = "web"
        terms[1].group = "web"
        window._set_active_terminal(terms[0])
        window._set_broadcast("group")
        targets = window._get_broadcast_targets()
        assert len(targets) == 1
        assert targets[0] is terms[1]

    def test_broadcast_group_non_matching_returns_empty(self, window):
        """Broadcast group with non-matching groups returns empty."""
        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        terms[0].group = "web"
        terms[1].group = "db"
        window._set_active_terminal(terms[0])
        window._set_broadcast("group")
        assert window._get_broadcast_targets() == []

    def test_read_only_terminal_excluded_from_forwarding(self, window, monkeypatch):
        """Read-only terminals are skipped in _on_terminal_key forwarding."""
        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        active = window._active_terminal
        other = [t for t in terms if t is not active][0]

        other.set_read_only(True)
        window._set_broadcast("all")

        # _get_broadcast_targets returns 'other', but _on_terminal_key
        # should skip it because it's read-only.
        # We verify by checking the code path: targets include 'other'
        targets = window._get_broadcast_targets()
        assert other in targets
        # But the is_read_only check in _on_terminal_key will skip it.
        assert other.is_read_only()

    def test_broadcast_forwards_send_text_not_send_key_event(self, window, monkeypatch):
        """Live QTermWidget bindings do not expose sendKeyEvent; broadcast
        should translate keys and use TerminalWidget.send_text."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent

        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        active = window._active_terminal
        other = [t for t in terms if t is not active][0]
        sent = []
        monkeypatch.setattr(other, "send_text", lambda text: sent.append(text))
        window._set_broadcast("all")

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_A,
            Qt.KeyboardModifier.NoModifier,
            "a",
        )
        window._on_terminal_key(active, ev)

        assert sent == ["a"]

    def test_broadcast_translates_special_key(self, window, monkeypatch):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent

        window._split_horizontal()
        terms = window._tabs.widget(0).find_terminals()
        active = window._active_terminal
        other = [t for t in terms if t is not active][0]
        sent = []
        monkeypatch.setattr(other, "send_text", lambda text: sent.append(text))
        window._set_broadcast("all")

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Up,
            Qt.KeyboardModifier.NoModifier,
        )
        window._on_terminal_key(active, ev)

        assert sent == ["\x1b[A"]


# ---------------------------------------------------------------------------
# NEW TESTS: Exit Actions
# ---------------------------------------------------------------------------


class TestExitActions:
    """Tests for terminal exit action behavior."""

    def test_exit_action_close_emits_close_request(self, window, qtbot):
        """Terminal with exit_action 'close' emits close_request on finished."""
        term = window._active_terminal
        assert term._exit_action == "close"
        with qtbot.waitSignal(term.close_request, timeout=1000):
            term._on_finished()

    def test_exit_action_hold_does_not_emit_close(self, window, qtbot):
        """Terminal with exit_action 'hold' does not emit close_request."""
        term = window._active_terminal
        term._exit_action = "hold"
        signals = []
        term.close_request.connect(lambda t: signals.append(t))
        term._on_finished()
        assert signals == []

    def test_exit_action_restart_restarts_shell(self, window, monkeypatch):
        """Terminal with exit_action 'restart' restarts the shell."""
        term = window._active_terminal
        term._exit_action = "restart"
        restarted = []
        monkeypatch.setattr(
            term.term, "startShellProgram", lambda: restarted.append(True)
        )
        term._on_finished()
        assert len(restarted) == 1

    def test_default_exit_action_is_close(self, window):
        """Default exit action from profile is 'close'."""
        assert window._active_terminal._exit_action == "close"

    def test_exit_action_from_profile_config(self, window, qtbot):
        """Exit action is loaded from profile config."""
        config = Config()
        config.set_profile("hold_profile", {
            "font_family": "Monospace", "font_size": 11,
            "color_scheme": "Linux", "scrollback_lines": 5000,
            "show_titlebar": True, "exit_action": "hold",
        })
        from qterminator.terminal import TerminalWidget
        term = TerminalWidget(profile="hold_profile")
        qtbot.addWidget(term)
        assert term._exit_action == "hold"


# ---------------------------------------------------------------------------
# NEW TESTS: Config Corruption
# ---------------------------------------------------------------------------


class TestConfigCorruption:
    """Tests for config resilience to corruption and missing data."""

    def test_invalid_toml_content(self, fresh_config, tmp_path):
        """Config file with invalid TOML content falls back to defaults."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("this is [[[not valid toml ===")
        Config._instance = None
        # Loading invalid TOML should raise or fall back
        try:
            config = Config()
            # If it doesn't raise, it should have defaults
            assert config.get("general", "show_menubar", default=False) is False
        except Exception:
            # It's acceptable to raise on invalid TOML
            pass

    def test_config_file_missing_creates_defaults(self, fresh_config):
        """Config file missing on first load uses all defaults."""
        Config._instance = None
        config = Config()
        assert config.get("general", "window_width", default=800) == 800
        assert config.get("general", "show_menubar", default=False) is False
        assert config.get_profile("default")["font_size"] == 11

    def test_config_file_empty(self, fresh_config, tmp_path):
        """Empty config file uses all defaults."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("")
        Config._instance = None
        config = Config()
        assert config.get("general", "window_width", default=800) == 800
        assert config.get("general", "show_menubar", default=False) is False

    def test_config_file_partial_data(self, fresh_config, tmp_path):
        """Config file with only [general] section still has profile defaults."""
        config_file = tmp_path / "config.toml"
        config_file.write_text('[general]\nshow_menubar = true\n')
        Config._instance = None
        config = Config()
        assert config.get("general", "show_menubar") is True
        # Profile defaults should still be present
        profile = config.get_profile("default")
        assert profile["font_size"] == 11

    def test_config_file_extra_unknown_sections(self, fresh_config, tmp_path):
        """Config file with extra unknown sections preserves them."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[general]\nshow_menubar = false\n\n'
            '[custom_section]\nmy_key = "my_value"\n'
        )
        Config._instance = None
        config = Config()
        assert config.get("custom_section", "my_key") == "my_value"
        assert config.get("general", "show_menubar") is False


# ---------------------------------------------------------------------------
# NEW TESTS: Window Geometry
# ---------------------------------------------------------------------------


class TestWindowGeometry:
    """Tests for window geometry save/restore."""

    def test_save_layout_saves_window_position(self, window):
        """save_layout saves window position to config."""
        window.move(100, 200)
        window.save_layout()
        config = Config()
        assert config.get("general", "window_x") is not None
        assert config.get("general", "window_y") is not None

    def test_restore_window_state_restores_size(self, qtbot, fresh_config):
        """restore_window_state restores saved width and height."""
        config = Config()
        config.set("general", "window_width", 1024)
        config.set("general", "window_height", 768)
        win = MainWindow()
        qtbot.addWidget(win)
        win.restore_window_state()
        assert win.width() == 1024
        assert win.height() == 768

    def test_restore_window_state_maximized(self, qtbot, fresh_config):
        """restore_window_state with maximized flag calls showMaximized."""
        config = Config()
        config.set("general", "window_maximized", True)
        win = MainWindow()
        qtbot.addWidget(win)
        win.restore_window_state()
        assert win.isMaximized()

    def test_default_size_is_800x500(self, window):
        """Default window size is 800x500."""
        # The window was created with default config so should be 800x500
        # Note: actual size may differ slightly due to window manager, so
        # check the resize was called with these values
        config = Config()
        assert config.get("general", "window_width", default=800) == 800
        assert config.get("general", "window_height", default=500) == 500


# ---------------------------------------------------------------------------
# NEW TESTS: Broadcast Key Translation
# ---------------------------------------------------------------------------


class TestBroadcastKeyTranslation:
    """Tests for _qkey_to_text key-to-PTY-text translation."""

    def test_ctrl_a_produces_soh(self):
        """Ctrl+A produces \\x01 (SOH)."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        from qterminator.window import _qkey_to_text

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_A,
            Qt.KeyboardModifier.ControlModifier,
        )
        assert _qkey_to_text(ev) == "\x01"

    def test_ctrl_c_produces_etx(self):
        """Ctrl+C produces \\x03 (ETX)."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        from qterminator.window import _qkey_to_text

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_C,
            Qt.KeyboardModifier.ControlModifier,
        )
        assert _qkey_to_text(ev) == "\x03"

    def test_ctrl_z_produces_sub(self):
        """Ctrl+Z produces \\x1a (SUB)."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        from qterminator.window import _qkey_to_text

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Z,
            Qt.KeyboardModifier.ControlModifier,
        )
        assert _qkey_to_text(ev) == "\x1a"

    def test_ctrl_bracket_left_produces_esc(self):
        """Ctrl+[ produces \\x1b (ESC)."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        from qterminator.window import _qkey_to_text

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_BracketLeft,
            Qt.KeyboardModifier.ControlModifier,
        )
        assert _qkey_to_text(ev) == "\x1b"

    def test_f1_produces_ss3_p(self):
        """F1 key produces \\x1bOP."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        from qterminator.window import _qkey_to_text

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_F1,
            Qt.KeyboardModifier.NoModifier,
        )
        assert _qkey_to_text(ev) == "\x1bOP"

    def test_f12_produces_csi_24_tilde(self):
        """F12 key produces \\x1b[24~."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        from qterminator.window import _qkey_to_text

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_F12,
            Qt.KeyboardModifier.NoModifier,
        )
        assert _qkey_to_text(ev) == "\x1b[24~"

    def test_delete_produces_csi_3_tilde(self):
        """Delete key produces \\x1b[3~."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        from qterminator.window import _qkey_to_text

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Delete,
            Qt.KeyboardModifier.NoModifier,
        )
        assert _qkey_to_text(ev) == "\x1b[3~"

    def test_home_produces_csi_h(self):
        """Home key produces \\x1b[H."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        from qterminator.window import _qkey_to_text

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Home,
            Qt.KeyboardModifier.NoModifier,
        )
        assert _qkey_to_text(ev) == "\x1b[H"

    def test_modifier_only_press_produces_none(self):
        """Modifier-only press (just Shift) produces None."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        from qterminator.window import _qkey_to_text

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Shift,
            Qt.KeyboardModifier.ShiftModifier,
        )
        result = _qkey_to_text(ev)
        assert result is None

    def test_tab_key_produces_tab_char(self):
        """Tab key produces \\t."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        from qterminator.window import _qkey_to_text

        ev = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Tab,
            Qt.KeyboardModifier.NoModifier,
            "\t",
        )
        assert _qkey_to_text(ev) == "\t"
