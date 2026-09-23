"""Tests for preferences dialog."""

import pytest
import qterminator.config as config_mod
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QSpinBox,
    QTabWidget,
)
from qterminator.config import Config
from qterminator.preferences import PreferencesDialog
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
# Original 8 tests (unchanged)
# ---------------------------------------------------------------------------

def test_preferences_opens(window, qtbot):
    """Preferences dialog opens without error."""
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)
    dlg.show()
    assert dlg._tab_widget.count() == 3


def test_preferences_loads_defaults(window, qtbot):
    """Preferences loads current config values."""
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)
    assert dlg._font_size.value() == 11
    assert dlg._scrollback.value() == 5000
    assert dlg._color_scheme.currentText() == "Linux"


def test_preferences_has_color_schemes(window, qtbot):
    """Color scheme dropdown has available schemes."""
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)
    assert dlg._color_scheme.count() > 0


def test_preferences_apply_font(window, qtbot):
    """Applying a font change updates all terminals."""
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)

    # Change font size
    dlg._font_size.setValue(16)
    dlg._apply()

    term = window._active_terminal
    font = term.term.getTerminalFont()
    assert font.pointSize() == 16


def test_preferences_apply_scrollback(window, qtbot):
    """Applying scrollback change updates terminals."""
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)

    dlg._scrollback.setValue(10000)
    dlg._apply()

    # QTermWidget doesn't expose historySize() easily,
    # but we can verify no crash and the call succeeds
    assert True


def test_preferences_apply_color_scheme(window, qtbot):
    """Applying color scheme change doesn't crash."""
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)

    # Pick a different scheme
    for i in range(dlg._color_scheme.count()):
        if dlg._color_scheme.itemText(i) != "Linux":
            dlg._color_scheme.setCurrentIndex(i)
            break

    dlg._apply()
    # No crash = success


def test_preferences_tab_position(window, qtbot):
    """Changing tab position applies to tab widget."""
    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)

    # Set to bottom
    dlg._tab_position.setCurrentIndex(1)  # Bottom
    dlg._apply()

    from PyQt6.QtWidgets import QTabWidget
    assert window._tabs.tabPosition() == QTabWidget.TabPosition.South


def test_preferences_apply_across_splits(window, qtbot):
    """Font change applies to all terminals including splits."""
    window._split_horizontal()
    split = window._tabs.widget(0)
    assert len(split.find_terminals()) == 2

    dlg = PreferencesDialog(window)
    qtbot.addWidget(dlg)
    dlg._font_size.setValue(14)
    dlg._apply()

    for term in split.find_terminals():
        font = term.term.getTerminalFont()
        assert font.pointSize() == 14


# ---------------------------------------------------------------------------
# Dialog Structure tests
# ---------------------------------------------------------------------------

class TestDialogStructure:
    """Verify dialog widget tree has the expected controls."""

    def test_three_tabs(self, window, qtbot):
        """Dialog has exactly 3 tabs: Appearance, Behavior, Shortcuts."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._tab_widget.count() == 3
        assert dlg._tab_widget.tabText(0) == "Appearance"
        assert dlg._tab_widget.tabText(1) == "Behavior"
        assert dlg._tab_widget.tabText(2) == "Shortcuts"

    def test_appearance_has_font_family_combo(self, window, qtbot):
        """Appearance tab has a QFontComboBox for font family."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert isinstance(dlg._font_combo, QFontComboBox)

    def test_appearance_has_font_size_spinbox(self, window, qtbot):
        """Appearance tab has a QSpinBox for font size."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert isinstance(dlg._font_size, QSpinBox)

    def test_appearance_has_color_scheme_combo(self, window, qtbot):
        """Appearance tab has a QComboBox for color scheme."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert isinstance(dlg._color_scheme, QComboBox)

    def test_appearance_has_cursor_shape_combo(self, window, qtbot):
        """Appearance tab has a QComboBox for cursor shape."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert isinstance(dlg._cursor_shape, QComboBox)
        # Verify items
        items = [dlg._cursor_shape.itemText(i) for i in range(dlg._cursor_shape.count())]
        assert items == ["Block", "Underline", "IBeam"]

    def test_appearance_has_cursor_blink_checkbox(self, window, qtbot):
        """Appearance tab has a QCheckBox for cursor blinking."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert isinstance(dlg._cursor_blink, QCheckBox)

    def test_appearance_has_opacity_spinbox(self, window, qtbot):
        """Appearance tab has a QDoubleSpinBox for opacity."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert isinstance(dlg._opacity, QDoubleSpinBox)

    def test_behavior_has_scrollback_spinbox(self, window, qtbot):
        """Behavior tab has a QSpinBox for scrollback lines."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert isinstance(dlg._scrollback, QSpinBox)

    def test_behavior_has_tab_position_combo(self, window, qtbot):
        """Behavior tab has a QComboBox for tab position."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert isinstance(dlg._tab_position, QComboBox)
        items = [dlg._tab_position.itemText(i) for i in range(dlg._tab_position.count())]
        assert items == ["Top", "Bottom", "Left", "Right"]

    def test_behavior_has_confirm_close_checkbox(self, window, qtbot):
        """Behavior tab has a QCheckBox for confirm close."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert isinstance(dlg._confirm_close, QCheckBox)

    def test_shortcuts_tab_has_keybinding_tree(self, window, qtbot):
        """Shortcuts tab shows keybinding entries grouped in a tree."""
        from PyQt6.QtWidgets import QTreeWidget
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        shortcuts_tab = dlg._tab_widget.widget(2)
        trees = shortcuts_tab.findChildren(QTreeWidget)
        assert len(trees) == 1
        tree = trees[0]
        # One top-level item per category we have data for.
        assert tree.topLevelItemCount() > 1
        assert len(dlg._shortcut_leaves) == len(dlg._shortcut_actions)
        assert len(dlg._shortcut_actions) > 5


# ---------------------------------------------------------------------------
# Loading Settings tests
# ---------------------------------------------------------------------------

class TestLoadingSettings:
    """Verify dialog loads values from config correctly."""

    def test_loads_default_font_size(self, window, qtbot):
        """Default font size of 11 is loaded."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._font_size.value() == 11

    def test_loads_default_color_scheme(self, window, qtbot):
        """Default color scheme 'Linux' is loaded."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._color_scheme.currentText() == "Linux"

    def test_loads_default_scrollback(self, window, qtbot):
        """Default scrollback of 5000 is loaded."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._scrollback.value() == 5000

    def test_loads_default_opacity(self, window, qtbot):
        """Default opacity of 1.0 is loaded."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._opacity.value() == 1.0

    def test_loads_default_cursor_shape(self, window, qtbot):
        """Default cursor shape 'Block' is loaded."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._cursor_shape.currentText() == "Block"

    def test_loads_default_cursor_blink(self, window, qtbot):
        """Default cursor blink is True."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._cursor_blink.isChecked() is True

    def test_loads_default_confirm_close(self, window, qtbot):
        """Default confirm_close is True."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._confirm_close.isChecked() is True

    def test_loads_custom_config_values(self, window, qtbot):
        """If config has custom values, dialog reflects them."""
        cfg = Config()
        cfg.set("profiles", "default", "font_size", 18)
        cfg.set("profiles", "default", "scrollback_lines", 999)
        cfg.set("profiles", "default", "background_opacity", 0.75)
        cfg.set("profiles", "default", "cursor_shape", "underline")
        cfg.set("profiles", "default", "cursor_blink", False)
        cfg.set("general", "confirm_close", False)
        cfg.set("general", "tab_position", "bottom")

        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)

        assert dlg._font_size.value() == 18
        assert dlg._scrollback.value() == 999
        assert dlg._opacity.value() == 0.75
        assert dlg._cursor_shape.currentText() == "Underline"
        assert dlg._cursor_blink.isChecked() is False
        assert dlg._confirm_close.isChecked() is False
        assert dlg._tab_position.currentText() == "Bottom"


# ---------------------------------------------------------------------------
# Applying Settings tests
# ---------------------------------------------------------------------------

class TestApplyingSettings:
    """Verify _apply() saves to config and updates widgets."""

    def test_apply_font_size_changes_config(self, window, qtbot):
        """Applying font size persists to config."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._font_size.setValue(20)
        dlg._apply()

        cfg = Config()
        assert cfg.get("profiles", "default", "font_size") == 20

    def test_apply_color_scheme_changes_config(self, window, qtbot):
        """Applying color scheme persists to config."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)

        # Pick any scheme different from current
        for i in range(dlg._color_scheme.count()):
            text = dlg._color_scheme.itemText(i)
            if text != "Linux":
                dlg._color_scheme.setCurrentIndex(i)
                break

        dlg._apply()
        cfg = Config()
        assert cfg.get("profiles", "default", "color_scheme") == dlg._color_scheme.currentText()

    def test_apply_scrollback_changes_config(self, window, qtbot):
        """Applying scrollback persists to config."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._scrollback.setValue(25000)
        dlg._apply()

        cfg = Config()
        assert cfg.get("profiles", "default", "scrollback_lines") == 25000

    def test_apply_tab_position_changes_tab_widget(self, window, qtbot):
        """Applying tab position updates the QTabWidget position."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)

        dlg._tab_position.setCurrentIndex(2)  # Left
        dlg._apply()
        assert window._tabs.tabPosition() == QTabWidget.TabPosition.West

        dlg._tab_position.setCurrentIndex(3)  # Right
        dlg._apply()
        assert window._tabs.tabPosition() == QTabWidget.TabPosition.East

    def test_apply_cursor_shape_changes_config(self, window, qtbot):
        """Applying cursor shape persists to config."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._cursor_shape.setCurrentIndex(2)  # IBeam
        dlg._apply()

        cfg = Config()
        assert cfg.get("profiles", "default", "cursor_shape") == "ibeam"

    def test_apply_opacity_changes_config(self, window, qtbot):
        """Applying opacity persists to config."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._opacity.setValue(0.50)
        dlg._apply()

        cfg = Config()
        assert cfg.get("profiles", "default", "background_opacity") == 0.50

    def test_apply_cursor_blink_changes_config(self, window, qtbot):
        """Applying cursor blink checkbox persists to config."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._cursor_blink.setChecked(False)
        dlg._apply()

        cfg = Config()
        assert cfg.get("profiles", "default", "cursor_blink") is False

    def test_apply_confirm_close_changes_config(self, window, qtbot):
        """Applying confirm_close checkbox persists to config."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._confirm_close.setChecked(False)
        dlg._apply()

        cfg = Config()
        assert cfg.get("general", "confirm_close") is False

    def test_apply_across_multiple_split_terminals(self, window, qtbot):
        """Font change propagates across multiple splits."""
        window._split_horizontal()
        window._split_vertical()
        split = window._tabs.widget(0)
        terminals = split.find_terminals()
        assert len(terminals) >= 2

        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._font_size.setValue(18)
        dlg._apply()

        for term in terminals:
            font = term.term.getTerminalFont()
            assert font.pointSize() == 18


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_font_size_minimum(self, window, qtbot):
        """Font size spinbox enforces minimum of 6."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._font_size.minimum() == 6
        dlg._font_size.setValue(1)
        assert dlg._font_size.value() == 6

    def test_font_size_maximum(self, window, qtbot):
        """Font size spinbox enforces maximum of 72."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._font_size.maximum() == 72
        dlg._font_size.setValue(200)
        assert dlg._font_size.value() == 72

    def test_scrollback_minimum(self, window, qtbot):
        """Scrollback spinbox minimum is 0 (disabled)."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._scrollback.minimum() == 0
        dlg._scrollback.setValue(0)
        dlg._apply()
        cfg = Config()
        assert cfg.get("profiles", "default", "scrollback_lines") == 0

    def test_scrollback_very_large(self, window, qtbot):
        """Scrollback spinbox accepts very large values up to 1,000,000."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._scrollback.maximum() == 1_000_000
        dlg._scrollback.setValue(1_000_000)
        dlg._apply()
        cfg = Config()
        assert cfg.get("profiles", "default", "scrollback_lines") == 1_000_000

    def test_opacity_minimum(self, window, qtbot):
        """Opacity spinbox enforces minimum of 0.0."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._opacity.minimum() == 0.0
        dlg._opacity.setValue(0.0)
        dlg._apply()
        cfg = Config()
        assert cfg.get("profiles", "default", "background_opacity") == 0.0

    def test_opacity_maximum(self, window, qtbot):
        """Opacity spinbox enforces maximum of 1.0."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._opacity.maximum() == 1.0
        dlg._opacity.setValue(5.0)
        assert dlg._opacity.value() == 1.0

    def test_apply_with_no_parent(self, qtbot):
        """Apply with no parent (None) does not crash."""
        dlg = PreferencesDialog(None)
        qtbot.addWidget(dlg)
        dlg._font_size.setValue(20)
        dlg._apply()  # Should return early, no crash

    def test_dialog_cancel_does_not_apply(self, window, qtbot):
        """Rejecting the dialog does not change config."""
        cfg = Config()
        original_size = cfg.get("profiles", "default", "font_size")

        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._font_size.setValue(42)
        dlg.reject()

        cfg2 = Config()
        assert cfg2.get("profiles", "default", "font_size") == original_size

    def test_multiple_open_close_cycles(self, window, qtbot):
        """Opening and closing the dialog multiple times does not crash."""
        for _ in range(5):
            dlg = PreferencesDialog(window)
            qtbot.addWidget(dlg)
            dlg.show()
            dlg._font_size.setValue(14)
            dlg._apply()
            dlg.close()

        # Final state should reflect last apply
        cfg = Config()
        assert cfg.get("profiles", "default", "font_size") == 14

    def test_apply_and_close_accepts_dialog(self, window, qtbot):
        """_apply_and_close calls apply and accepts the dialog."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._font_size.setValue(22)
        dlg._apply_and_close()

        cfg = Config()
        assert cfg.get("profiles", "default", "font_size") == 22

    def test_scrollback_special_value_text(self, window, qtbot):
        """Scrollback spinbox shows 'Disabled' for value 0."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._scrollback.specialValueText() == "Disabled"


# ---------------------------------------------------------------------------
# MenuBar Preference tests
# ---------------------------------------------------------------------------

class TestMenuBarPreference:
    """Tests for the show_menubar preference."""

    def test_has_show_menubar_checkbox(self, window, qtbot):
        """Preferences dialog has _show_menubar checkbox."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert hasattr(dlg, "_show_menubar")
        assert isinstance(dlg._show_menubar, QCheckBox)

    def test_default_value_unchecked(self, window, qtbot):
        """Default value is unchecked (show_menubar=False)."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._show_menubar.isChecked() is False

    def test_checked_and_apply_saves_to_config(self, window, qtbot):
        """Setting to checked and applying saves to config."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._show_menubar.setChecked(True)
        dlg._apply()

        cfg = Config()
        assert cfg.get("general", "show_menubar") is True

    def test_apply_updates_parent_menubar_visibility(self, window, qtbot):
        """Apply updates parent window menubar visibility."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)

        # Enable menubar
        dlg._show_menubar.setChecked(True)
        dlg._apply()
        assert window.menuBar().isVisible() is True

        # Disable menubar
        dlg._show_menubar.setChecked(False)
        dlg._apply()
        assert window.menuBar().isVisible() is False

    def test_load_reflects_config_value_true(self, window, qtbot):
        """Load reflects config value when show_menubar=True."""
        cfg = Config()
        cfg.set("general", "show_menubar", True)

        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._show_menubar.isChecked() is True


# ---------------------------------------------------------------------------
# Preferences Integration tests
# ---------------------------------------------------------------------------

class TestPreferencesIntegration:
    """Integration tests for preferences roundtripping."""

    def test_apply_changes_persist_through_save_reload(self, window, qtbot):
        """Apply changes persist through config save/reload."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._font_size.setValue(24)
        dlg._scrollback.setValue(8000)
        dlg._apply()

        # Force a fresh config reload
        Config._instance = None
        cfg = Config()
        assert cfg.get("profiles", "default", "font_size") == 24
        assert cfg.get("profiles", "default", "scrollback_lines") == 8000

    def test_all_appearance_settings_roundtrip(self, window, qtbot):
        """All appearance settings roundtrip through apply."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)

        dlg._font_size.setValue(16)
        dlg._opacity.setValue(0.80)
        dlg._cursor_shape.setCurrentIndex(1)  # Underline
        dlg._cursor_blink.setChecked(False)
        dlg._apply()

        cfg = Config()
        assert cfg.get("profiles", "default", "font_size") == 16
        assert cfg.get("profiles", "default", "background_opacity") == 0.80
        assert cfg.get("profiles", "default", "cursor_shape") == "underline"
        assert cfg.get("profiles", "default", "cursor_blink") is False

    def test_tab_position_west_applies(self, window, qtbot):
        """Tab position West applies correctly."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._tab_position.setCurrentIndex(2)  # Left
        dlg._apply()

        assert window._tabs.tabPosition() == QTabWidget.TabPosition.West
        cfg = Config()
        assert cfg.get("general", "tab_position") == "left"

    def test_cursor_shape_ibeam_applies(self, window, qtbot):
        """Cursor shape IBeam applies correctly."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._cursor_shape.setCurrentIndex(2)  # IBeam
        dlg._apply()

        cfg = Config()
        assert cfg.get("profiles", "default", "cursor_shape") == "ibeam"

    def test_opacity_half_applies(self, window, qtbot):
        """Opacity 0.5 applies correctly."""
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._opacity.setValue(0.50)
        dlg._apply()

        cfg = Config()
        assert cfg.get("profiles", "default", "background_opacity") == 0.50


# ---------------------------------------------------------------------------
# Editable keybindings tests
# ---------------------------------------------------------------------------

class TestEditableShortcuts:
    """The Shortcuts page lets users record and persist new key sequences."""

    def test_tree_seeds_from_config(self, window, qtbot):
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._shortcut_leaves["new_tab"].text(1) == "Ctrl+Shift+T"

    def test_apply_persists_shortcut_to_config(self, window, qtbot):
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._shortcut_leaves["new_tab"].setText(1, "Ctrl+Alt+T")
        dlg._apply()
        cfg = Config()
        assert cfg.get_keybinding("new_tab") == "Ctrl+Alt+T"

    def test_apply_rebinds_running_action(self, window, qtbot):
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._shortcut_leaves["new_tab"].setText(1, "Ctrl+Alt+J")
        dlg._apply()
        assert window._actions["new_tab"].shortcut().toString() == "Ctrl+Alt+J"

    def test_clear_shortcut_persists_as_empty(self, window, qtbot):
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._shortcut_leaves["new_tab"].setText(1, "")
        dlg._apply()
        assert window._actions["new_tab"].shortcut().toString() == ""
        cfg = Config()
        assert cfg.get_keybinding("new_tab") == ""

    def test_delegate_is_key_sequence_delegate(self, window, qtbot):
        from PyQt6.QtWidgets import QKeySequenceEdit
        from qterminator.preferences import _KeySequenceDelegate
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        delegate = dlg._shortcut_tree.itemDelegateForColumn(1)
        assert isinstance(delegate, _KeySequenceDelegate)
        # Build an index on a leaf row and exercise the editor factory.
        leaf = dlg._shortcut_leaves["new_tab"]
        index = dlg._shortcut_tree.indexFromItem(leaf, 1)
        editor = delegate.createEditor(dlg._shortcut_tree, None, index)
        qtbot.addWidget(editor)
        assert isinstance(editor, QKeySequenceEdit)


class TestShortcutConflicts:
    """Two actions sharing a non-empty shortcut should be flagged."""

    def test_distinct_bindings_have_no_conflict(self, window, qtbot):
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        assert dlg._shortcut_conflicts["new_tab"] is False
        assert dlg._shortcut_conflicts["copy"] is False

    def test_duplicate_binding_flags_both(self, window, qtbot):
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._shortcut_leaves["new_tab"].setText(1, "Ctrl+Alt+Z")
        dlg._shortcut_leaves["copy"].setText(1, "Ctrl+Alt+Z")
        dlg._refresh_shortcut_conflicts()
        assert dlg._shortcut_conflicts["new_tab"] is True
        assert dlg._shortcut_conflicts["copy"] is True
        # Tooltip names the conflicting action.
        assert "Copy" in dlg._shortcut_leaves["new_tab"].toolTip(1)

    def test_blank_bindings_never_conflict(self, window, qtbot):
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._shortcut_leaves["new_tab"].setText(1, "")
        dlg._shortcut_leaves["copy"].setText(1, "")
        dlg._refresh_shortcut_conflicts()
        assert dlg._shortcut_conflicts["new_tab"] is False
        assert dlg._shortcut_conflicts["copy"] is False

    def test_resolving_duplicate_clears_flag(self, window, qtbot):
        dlg = PreferencesDialog(window)
        qtbot.addWidget(dlg)
        dlg._shortcut_leaves["new_tab"].setText(1, "Ctrl+Alt+Z")
        dlg._shortcut_leaves["copy"].setText(1, "Ctrl+Alt+Z")
        dlg._refresh_shortcut_conflicts()
        assert dlg._shortcut_conflicts["new_tab"] is True
        # Resolve by giving "copy" a binding that no other action uses.
        dlg._shortcut_leaves["copy"].setText(1, "Ctrl+Alt+F12")
        dlg._refresh_shortcut_conflicts()
        assert dlg._shortcut_conflicts["new_tab"] is False
        assert dlg._shortcut_conflicts["copy"] is False
