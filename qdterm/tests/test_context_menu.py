"""Tests for context menu structure."""

from unittest.mock import patch

import pytest
import qterminator.config as config_mod
from PyQt6.QtGui import QIcon
from qterminator.config import Config
from qterminator.context_menu import build_context_menu
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


# ---------------------------------------------------------------------------
# Original 5 tests (unchanged)
# ---------------------------------------------------------------------------

def test_context_menu_has_expected_items(window):
    """Context menu contains all expected action names."""
    menu = build_context_menu(window._active_terminal)
    names = [a.text() for a in menu.actions() if a.text()]
    assert "Copy" in names
    assert "Paste" in names
    assert "Split Horizontally" in names
    assert "Split Vertically" in names
    assert "New Tab" in names
    assert "Color Scheme" in names
    assert "Read-Only" in names
    assert "Group" in names
    assert "Search..." in names
    assert "Reset" in names
    assert "Reset && Clear" in names
    assert "Zoom In" in names
    assert "Zoom Out" in names
    assert "Preferences..." in names
    assert "Close Terminal" in names


def test_context_menu_read_only_toggle(window):
    """Read-Only checkbox reflects terminal state."""
    term = window._active_terminal
    menu = build_context_menu(term)
    ro = next(a for a in menu.actions() if a.text() == "Read-Only")
    assert not ro.isChecked()

    term.toggle_read_only()
    menu2 = build_context_menu(term)
    ro2 = next(a for a in menu2.actions() if a.text() == "Read-Only")
    assert ro2.isChecked()


def test_context_menu_group_submenu(window):
    """Group submenu has predefined groups and custom option."""
    menu = build_context_menu(window._active_terminal)
    group_menu = None
    for action in menu.actions():
        if action.text() == "Group" and action.menu():
            group_menu = action.menu()
            break
    assert group_menu is not None
    names = [a.text() for a in group_menu.actions() if a.text()]
    assert "None" in names
    assert "Alpha" in names
    assert "Custom..." in names


def test_context_menu_has_color_scheme_submenu(window):
    """Color Scheme entry is a submenu with available schemes."""
    menu = build_context_menu(window._active_terminal)
    # Find the Color Scheme submenu
    scheme_menu = None
    for action in menu.actions():
        if action.text() == "Color Scheme" and action.menu():
            scheme_menu = action.menu()
            break
    assert scheme_menu is not None
    assert scheme_menu.actions()  # has at least one scheme


def test_context_menu_shortcuts_shown(window):
    """Key shortcuts are displayed on menu items."""
    menu = build_context_menu(window._active_terminal)
    shortcuts = {}
    for action in menu.actions():
        if action.shortcut().toString():
            shortcuts[action.text()] = action.shortcut().toString()
    assert shortcuts.get("Copy") == "Ctrl+Shift+C"
    assert shortcuts.get("Paste") == "Ctrl+Shift+V"


# ---------------------------------------------------------------------------
# Individual action presence tests
# ---------------------------------------------------------------------------

def _find_action(menu, text):
    """Return the first action with the given text, or None."""
    for a in menu.actions():
        if a.text() == text:
            return a
    return None


def _find_submenu(menu, text):
    """Return the QMenu for a submenu action with the given text, or None."""
    for a in menu.actions():
        if a.text() == text and a.menu():
            return a.menu()
    return None


def test_menu_has_copy_action(window):
    """Menu contains a Copy action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Copy") is not None


def test_menu_has_paste_action(window):
    """Menu contains a Paste action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Paste") is not None


def test_menu_has_split_horizontally(window):
    """Menu contains a Split Horizontally action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Split Horizontally") is not None


def test_menu_has_split_vertically(window):
    """Menu contains a Split Vertically action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Split Vertically") is not None


def test_menu_has_new_tab(window):
    """Menu contains a New Tab action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "New Tab") is not None


def test_menu_has_read_only_checkable(window):
    """Read-Only action exists and is checkable."""
    menu = build_context_menu(window._active_terminal)
    action = _find_action(menu, "Read-Only")
    assert action is not None
    assert action.isCheckable()


def test_menu_has_search_action(window):
    """Menu contains a Search action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Search...") is not None


def test_menu_has_reset_action(window):
    """Menu contains a Reset action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Reset") is not None


def test_menu_has_reset_and_clear_action(window):
    """Menu contains a Reset && Clear action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Reset && Clear") is not None


def test_menu_has_zoom_in(window):
    """Menu contains a Zoom In action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Zoom In") is not None


def test_menu_has_zoom_out(window):
    """Menu contains a Zoom Out action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Zoom Out") is not None


def test_menu_has_preferences(window):
    """Menu contains a Preferences action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Preferences...") is not None


def test_menu_has_close_terminal(window):
    """Menu contains a Close Terminal action."""
    menu = build_context_menu(window._active_terminal)
    assert _find_action(menu, "Close Terminal") is not None


# ---------------------------------------------------------------------------
# Monitor submenu tests
# ---------------------------------------------------------------------------

def test_menu_has_monitor_submenu(window):
    """Menu contains a Monitor submenu."""
    menu = build_context_menu(window._active_terminal)
    assert _find_submenu(menu, "Monitor") is not None


def test_monitor_has_watch_for_activity(window):
    """Monitor submenu contains a checkable Watch for Activity action."""
    menu = build_context_menu(window._active_terminal)
    monitor = _find_submenu(menu, "Monitor")
    action = _find_action(monitor, "Watch for Activity")
    assert action is not None
    assert action.isCheckable()


def test_monitor_has_watch_for_silence(window):
    """Monitor submenu contains a checkable Watch for Silence action."""
    menu = build_context_menu(window._active_terminal)
    monitor = _find_submenu(menu, "Monitor")
    action = _find_action(monitor, "Watch for Silence")
    assert action is not None
    assert action.isCheckable()


# ---------------------------------------------------------------------------
# Group submenu detailed tests
# ---------------------------------------------------------------------------

def test_menu_has_group_submenu(window):
    """Menu contains a Group submenu."""
    menu = build_context_menu(window._active_terminal)
    assert _find_submenu(menu, "Group") is not None


def test_group_submenu_has_none_option(window):
    """Group submenu has a None option."""
    menu = build_context_menu(window._active_terminal)
    group_menu = _find_submenu(menu, "Group")
    assert _find_action(group_menu, "None") is not None


def test_group_submenu_has_predefined_groups(window):
    """Group submenu has Alpha, Beta, Gamma, and Delta groups."""
    menu = build_context_menu(window._active_terminal)
    group_menu = _find_submenu(menu, "Group")
    names = [a.text() for a in group_menu.actions() if a.text()]
    for expected in ["Alpha", "Beta", "Gamma", "Delta"]:
        assert expected in names


def test_group_submenu_has_custom_option(window):
    """Group submenu has a Custom... option."""
    menu = build_context_menu(window._active_terminal)
    group_menu = _find_submenu(menu, "Group")
    assert _find_action(group_menu, "Custom...") is not None


def test_group_none_checked_when_no_group(window):
    """Group None is checked when terminal has no group."""
    term = window._active_terminal
    assert term.group is None
    menu = build_context_menu(term)
    group_menu = _find_submenu(menu, "Group")
    none_action = _find_action(group_menu, "None")
    assert none_action.isChecked()


def test_group_option_checked_matches_terminal_group(window):
    """When terminal is in a group, that group option is checked."""
    term = window._active_terminal
    term.group = "Beta"
    menu = build_context_menu(term)
    group_menu = _find_submenu(menu, "Group")
    beta = _find_action(group_menu, "Beta")
    assert beta.isChecked()
    none_action = _find_action(group_menu, "None")
    assert not none_action.isChecked()


# ---------------------------------------------------------------------------
# Profiles submenu tests
# ---------------------------------------------------------------------------

def test_menu_has_profiles_submenu(window):
    """Menu contains a Profiles submenu."""
    menu = build_context_menu(window._active_terminal)
    assert _find_submenu(menu, "Profiles") is not None


def test_profiles_submenu_lists_configured_profiles(window):
    """Profiles submenu lists profiles from config."""
    config = Config()
    profile_names = config.list_profiles()
    menu = build_context_menu(window._active_terminal)
    profiles_menu = _find_submenu(menu, "Profiles")
    submenu_names = [a.text() for a in profiles_menu.actions() if a.text()]
    for pname in profile_names:
        assert pname in submenu_names


def test_current_profile_is_checked(window):
    """The terminal's current profile is checked in Profiles submenu."""
    term = window._active_terminal
    menu = build_context_menu(term)
    profiles_menu = _find_submenu(menu, "Profiles")
    current = term._profile_name
    for action in profiles_menu.actions():
        if action.text() == current:
            assert action.isChecked()
        elif action.text():
            assert not action.isChecked()


# ---------------------------------------------------------------------------
# Color scheme submenu tests
# ---------------------------------------------------------------------------

def test_color_scheme_submenu_populated(window):
    """Color Scheme submenu has at least one entry."""
    menu = build_context_menu(window._active_terminal)
    scheme_menu = _find_submenu(menu, "Color Scheme")
    assert scheme_menu is not None
    assert len(scheme_menu.actions()) > 0


def test_current_color_scheme_is_checked(window):
    """The terminal's current color scheme is checked in the submenu."""
    term = window._active_terminal
    current_scheme = term._config.get_profile().get("color_scheme", "Linux")
    menu = build_context_menu(term)
    scheme_menu = _find_submenu(menu, "Color Scheme")
    checked = [a.text() for a in scheme_menu.actions() if a.isChecked()]
    assert current_scheme in checked


# ---------------------------------------------------------------------------
# Read-Only state reflection tests
# ---------------------------------------------------------------------------

def test_read_only_unchecked_when_not_read_only(window):
    """Read-Only is unchecked when terminal is not read-only."""
    term = window._active_terminal
    menu = build_context_menu(term)
    ro = _find_action(menu, "Read-Only")
    assert not ro.isChecked()


def test_read_only_checked_when_read_only(window):
    """Read-Only is checked when terminal is in read-only mode."""
    term = window._active_terminal
    term.toggle_read_only()
    menu = build_context_menu(term)
    ro = _find_action(menu, "Read-Only")
    assert ro.isChecked()


# ---------------------------------------------------------------------------
# Shortcut tests
# ---------------------------------------------------------------------------

def test_search_shortcut_shown(window):
    """Search action has its keyboard shortcut set."""
    menu = build_context_menu(window._active_terminal)
    action = _find_action(menu, "Search...")
    assert action.shortcut().toString() == "Ctrl+Shift+F"


# ---------------------------------------------------------------------------
# Icon tests
# ---------------------------------------------------------------------------

def test_icons_set_on_actions(window):
    """Key actions request theme icons via _icon()."""
    requested = []
    original_from_theme = QIcon.fromTheme

    def tracking_from_theme(name):
        requested.append(name)
        return original_from_theme(name)

    with patch("qterminator.context_menu.QIcon") as mock_icon:
        mock_icon.fromTheme = tracking_from_theme
        build_context_menu(window._active_terminal)

    icon_actions = [
        "edit-copy", "edit-paste", "window-close", "view-split-left-right",
        "view-split-top-bottom", "tab-new", "zoom-in", "zoom-out",
        "edit-find", "view-refresh", "edit-clear", "object-locked",
        "preferences-system",
    ]
    for name in icon_actions:
        assert name in requested, f"Icon {name!r} was not requested"


def test_actions_have_correct_icon_theme_names(window):
    """Actions use the expected FreeDesktop icon theme names."""
    original_from_theme = QIcon.fromTheme
    call_order = []

    def tracking_from_theme(name):
        call_order.append(name)
        return original_from_theme(name)

    with patch("qterminator.context_menu.QIcon") as mock_icon:
        mock_icon.fromTheme = tracking_from_theme
        build_context_menu(window._active_terminal)

    # Verify specific icon names were requested during menu construction
    expected_names = {
        "edit-copy", "edit-paste", "window-close", "edit-find",
        "view-refresh", "edit-clear", "zoom-in", "zoom-out",
        "preferences-system", "object-locked", "view-split-left-right",
        "view-split-top-bottom", "tab-new", "utilities-system-monitor",
        "object-group", "user-identity", "preferences-desktop-color",
    }
    for icon_name in expected_names:
        assert icon_name in call_order, (
            f"Expected icon theme name {icon_name!r} not found in requests"
        )


# ---------------------------------------------------------------------------
# Menu Bar toggle in context menu
# ---------------------------------------------------------------------------


class TestMenuBarToggleInContext:
    """Tests for the Show Menu Bar action in the context menu."""

    def test_context_menu_has_show_menu_bar(self, window):
        """Context menu contains a Show Menu Bar action."""
        menu = build_context_menu(window._active_terminal)
        assert _find_action(menu, "Show Menu Bar") is not None

    def test_show_menu_bar_is_checkable(self, window):
        """Show Menu Bar action is checkable."""
        menu = build_context_menu(window._active_terminal)
        action = _find_action(menu, "Show Menu Bar")
        assert action.isCheckable()

    def test_show_menu_bar_unchecked_when_hidden(self, window):
        """Show Menu Bar is unchecked when the menu bar is hidden."""
        window.menuBar().setVisible(False)
        menu = build_context_menu(window._active_terminal)
        action = _find_action(menu, "Show Menu Bar")
        assert not action.isChecked()

    def test_show_menu_bar_shortcut(self, window):
        """Show Menu Bar has Ctrl+Shift+M shortcut."""
        menu = build_context_menu(window._active_terminal)
        action = _find_action(menu, "Show Menu Bar")
        assert action.shortcut().toString() == "Ctrl+Shift+M"


# ---------------------------------------------------------------------------
# Context menu icon tests
# ---------------------------------------------------------------------------


class TestContextMenuIcons:
    """Verify key actions have icons set."""

    def test_copy_has_icon(self, window):
        """Copy action has an icon (edit-copy)."""
        # Icon may be null if theme doesn't have it, but it should be set
        # We verify via the fromTheme tracking approach
        requested = []
        original_from_theme = QIcon.fromTheme

        def tracking_from_theme(name):
            requested.append(name)
            return original_from_theme(name)

        with patch("qterminator.context_menu.QIcon") as mock_icon:
            mock_icon.fromTheme = tracking_from_theme
            build_context_menu(window._active_terminal)
        assert "edit-copy" in requested

    def test_paste_has_icon(self, window):
        """Paste action has an icon (edit-paste)."""
        requested = []
        original_from_theme = QIcon.fromTheme

        def tracking_from_theme(name):
            requested.append(name)
            return original_from_theme(name)

        with patch("qterminator.context_menu.QIcon") as mock_icon:
            mock_icon.fromTheme = tracking_from_theme
            build_context_menu(window._active_terminal)
        assert "edit-paste" in requested

    def test_close_terminal_has_icon(self, window):
        """Close Terminal action has an icon (window-close)."""
        requested = []
        original_from_theme = QIcon.fromTheme

        def tracking_from_theme(name):
            requested.append(name)
            return original_from_theme(name)

        with patch("qterminator.context_menu.QIcon") as mock_icon:
            mock_icon.fromTheme = tracking_from_theme
            build_context_menu(window._active_terminal)
        assert "window-close" in requested

    def test_preferences_has_icon(self, window):
        """Preferences action has an icon (preferences-system)."""
        requested = []
        original_from_theme = QIcon.fromTheme

        def tracking_from_theme(name):
            requested.append(name)
            return original_from_theme(name)

        with patch("qterminator.context_menu.QIcon") as mock_icon:
            mock_icon.fromTheme = tracking_from_theme
            build_context_menu(window._active_terminal)
        assert "preferences-system" in requested

    def test_split_actions_have_icons(self, window):
        """Split actions have icons set."""
        requested = []
        original_from_theme = QIcon.fromTheme

        def tracking_from_theme(name):
            requested.append(name)
            return original_from_theme(name)

        with patch("qterminator.context_menu.QIcon") as mock_icon:
            mock_icon.fromTheme = tracking_from_theme
            build_context_menu(window._active_terminal)
        assert "view-split-left-right" in requested
        assert "view-split-top-bottom" in requested
