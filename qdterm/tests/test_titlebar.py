"""Tests for TerminalTitlebar widget."""

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QLabel, QToolButton
from qterminator.titlebar import (
    ACTIVE_BG,
    GROUP_COLORS,
    INACTIVE_BG,
    TITLE_HEIGHT,
    TerminalTitlebar,
)


@pytest.fixture
def titlebar(qtbot):
    tb = TerminalTitlebar()
    qtbot.addWidget(tb)
    return tb


# --- Creation and defaults ---

def test_creation_does_not_crash(titlebar):
    """Widget can be instantiated without errors."""
    assert titlebar is not None


def test_default_title(titlebar):
    """Default title label text is 'Terminal'."""
    assert titlebar._title_label.text() == "Terminal"


def test_default_not_active(titlebar):
    """Titlebar starts inactive."""
    assert titlebar._active is False


def test_default_group_hidden(titlebar):
    """Group indicator is hidden by default."""
    assert titlebar._group_label.isHidden()


def test_default_readonly_hidden(titlebar):
    """Read-only label is hidden by default."""
    assert titlebar._readonly_label.isHidden()


def test_default_activity_hidden(titlebar):
    """Activity indicator is hidden by default."""
    assert titlebar._activity_label.isHidden()


def test_fixed_height(titlebar):
    """Titlebar has the expected fixed height."""
    assert titlebar.maximumHeight() == TITLE_HEIGHT
    assert titlebar.minimumHeight() == TITLE_HEIGHT
    assert TITLE_HEIGHT == 20


# --- set_title ---

def test_set_title_normal(titlebar):
    """Setting a normal-length title updates the label."""
    titlebar.set_title("my shell")
    assert titlebar._title_label.text() == "my shell"


def test_set_title_long_text_truncated(titlebar):
    """Titles longer than 60 characters are truncated with ellipsis."""
    long_title = "a" * 80
    titlebar.set_title(long_title)
    text = titlebar._title_label.text()
    assert len(text) == 60
    assert text.endswith("...")
    assert text == "a" * 57 + "..."


def test_set_title_exactly_60(titlebar):
    """A title of exactly 60 chars is not truncated."""
    title = "b" * 60
    titlebar.set_title(title)
    assert titlebar._title_label.text() == title


def test_set_title_61_chars_truncated(titlebar):
    """A title of 61 chars is truncated."""
    title = "c" * 61
    titlebar.set_title(title)
    assert titlebar._title_label.text() == "c" * 57 + "..."


def test_set_title_empty_string(titlebar):
    """Setting an empty title is allowed."""
    titlebar.set_title("")
    assert titlebar._title_label.text() == ""


def test_set_title_special_characters(titlebar):
    """Special characters in title are preserved."""
    titlebar.set_title("user@host: ~/dir & <stuff>")
    assert titlebar._title_label.text() == "user@host: ~/dir & <stuff>"


# --- set_active ---

def test_set_active_true(titlebar):
    """Activating sets _active flag and changes stylesheet."""
    titlebar.set_active(True)
    assert titlebar._active is True
    assert ACTIVE_BG in titlebar.styleSheet()


def test_set_active_false(titlebar):
    """Deactivating sets _active flag and changes stylesheet."""
    titlebar.set_active(True)
    titlebar.set_active(False)
    assert titlebar._active is False
    assert INACTIVE_BG in titlebar.styleSheet()


def test_set_active_then_inactive(titlebar):
    """Toggling active state updates background each time."""
    titlebar.set_active(True)
    assert ACTIVE_BG in titlebar.styleSheet()
    titlebar.set_active(False)
    assert INACTIVE_BG in titlebar.styleSheet()
    assert ACTIVE_BG not in titlebar.styleSheet()


# --- set_group ---

def test_set_group_none_hides(titlebar):
    """Setting group to None hides the group indicator."""
    titlebar.set_group(None)
    assert titlebar._group_label.isHidden()


def test_set_group_named_shows(titlebar):
    """Setting a group name shows the group indicator."""
    titlebar.set_group("alpha")
    assert not titlebar._group_label.isHidden()


def test_set_group_sets_tooltip(titlebar):
    """Group tooltip contains the group name."""
    titlebar.set_group("beta")
    assert "beta" in titlebar._group_label.toolTip()


def test_set_group_color_from_hash(titlebar):
    """Group indicator color is derived from hash of group name."""
    titlebar.set_group("gamma")
    expected_idx = hash("gamma") % len(GROUP_COLORS)
    expected_color = GROUP_COLORS[expected_idx]
    assert expected_color in titlebar._group_label.styleSheet()


def test_set_group_different_names_different_colors(titlebar):
    """Different group names can produce different colors."""
    colors = set()
    # Try several names; at least two should differ
    for name in ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]:
        idx = hash(name) % len(GROUP_COLORS)
        colors.add(GROUP_COLORS[idx])
    assert len(colors) > 1


def test_set_group_then_clear(titlebar):
    """Setting a group then clearing it hides the indicator."""
    titlebar.set_group("delta")
    assert not titlebar._group_label.isHidden()
    titlebar.set_group(None)
    assert titlebar._group_label.isHidden()


def test_set_group_empty_string_hides(titlebar):
    """An empty string group name hides the indicator (falsy)."""
    titlebar.set_group("")
    assert titlebar._group_label.isHidden()


# --- set_read_only ---

def test_set_read_only_true(titlebar):
    """Setting read-only True shows the [RO] label."""
    titlebar.set_read_only(True)
    assert not titlebar._readonly_label.isHidden()


def test_set_read_only_false(titlebar):
    """Setting read-only False hides the [RO] label."""
    titlebar.set_read_only(True)
    titlebar.set_read_only(False)
    assert titlebar._readonly_label.isHidden()


def test_read_only_label_text(titlebar):
    """The read-only label displays [RO]."""
    assert titlebar._readonly_label.text() == "[RO]"


# --- set_activity ---

def test_set_activity_true(titlebar):
    """Setting activity True shows the activity dot."""
    titlebar.set_activity(True)
    assert not titlebar._activity_label.isHidden()


def test_set_activity_false(titlebar):
    """Setting activity False hides the activity dot."""
    titlebar.set_activity(True)
    titlebar.set_activity(False)
    assert titlebar._activity_label.isHidden()


def test_activity_label_tooltip(titlebar):
    """Activity label has an informative tooltip."""
    assert "ctivit" in titlebar._activity_label.toolTip()


# --- Close button ---

def test_close_button_exists(titlebar):
    """Close button is present in the titlebar."""
    assert titlebar._close_btn is not None


def test_close_button_emits_signal(qtbot, titlebar):
    """Clicking close button emits close_clicked signal."""
    with qtbot.waitSignal(titlebar.close_clicked, timeout=1000):
        titlebar._close_btn.click()


# --- Mouse click / clicked signal ---

def test_left_click_emits_clicked(qtbot, titlebar):
    """Left-clicking the titlebar emits the clicked signal."""
    titlebar.show()
    with qtbot.waitSignal(titlebar.clicked, timeout=1000):
        QTest.mouseClick(titlebar, Qt.MouseButton.LeftButton)


# --- Multiple state changes ---

def test_multiple_state_changes(titlebar):
    """Multiple state changes in sequence all take effect."""
    titlebar.set_title("first")
    titlebar.set_active(True)
    titlebar.set_group("grp1")
    titlebar.set_read_only(True)
    titlebar.set_activity(True)

    assert titlebar._title_label.text() == "first"
    assert titlebar._active is True
    assert not titlebar._group_label.isHidden()
    assert not titlebar._readonly_label.isHidden()
    assert not titlebar._activity_label.isHidden()

    # Now reverse everything
    titlebar.set_title("second")
    titlebar.set_active(False)
    titlebar.set_group(None)
    titlebar.set_read_only(False)
    titlebar.set_activity(False)

    assert titlebar._title_label.text() == "second"
    assert titlebar._active is False
    assert titlebar._group_label.isHidden()
    assert titlebar._readonly_label.isHidden()
    assert titlebar._activity_label.isHidden()


def test_close_btn_is_flat(titlebar):
    """Close button is flat (no border)."""
    assert titlebar._close_btn.isFlat()


def test_group_indicator_fixed_size(titlebar):
    """Group indicator dot has a fixed 12x12 size."""
    assert titlebar._group_label.minimumWidth() == 12
    assert titlebar._group_label.maximumWidth() == 12
    assert titlebar._group_label.minimumHeight() == 12
    assert titlebar._group_label.maximumHeight() == 12


# --- Extension widgets ---

def test_add_titlebar_widget_right_inserts_before_close(titlebar):
    """Extra right-side widgets appear before the close button."""
    label = QLabel("VM")
    returned = titlebar.add_titlebar_widget("vm", label)

    assert returned is label
    assert titlebar.titlebar_widget("vm") is label
    assert titlebar.layout().indexOf(label) < titlebar.layout().indexOf(titlebar._close_btn)


def test_add_titlebar_widget_left_inserts_before_title(titlebar):
    """Extra left-side widgets appear between indicators and title."""
    label = QLabel("L")
    titlebar.add_titlebar_widget("left", label, side="left")

    assert titlebar.layout().indexOf(label) < titlebar.layout().indexOf(titlebar._title_label)


def test_add_titlebar_widget_replaces_existing(titlebar):
    """Adding the same name replaces the previous widget."""
    first = QLabel("1")
    second = QLabel("2")

    titlebar.add_titlebar_widget("slot", first)
    titlebar.add_titlebar_widget("slot", second)

    assert titlebar.titlebar_widget("slot") is second
    assert first.parent() is None
    assert titlebar.layout().indexOf(first) == -1


def test_remove_titlebar_widget(titlebar):
    """Named extra widgets can be removed."""
    label = QLabel("VM")
    titlebar.add_titlebar_widget("vm", label)

    assert titlebar.remove_titlebar_widget("vm") is True
    assert titlebar.titlebar_widget("vm") is None
    assert label.parent() is None
    assert titlebar.remove_titlebar_widget("vm") is False


def test_add_titlebar_button_emits_callback(titlebar):
    """Convenience API creates a native Qt tool button."""
    calls = []
    button = titlebar.add_titlebar_button(
        "pin", "\u25ce", "Pin terminal", lambda: calls.append("clicked")
    )

    assert isinstance(button, QToolButton)
    assert titlebar.titlebar_widget("pin") is button
    assert button.toolTip() == "Pin terminal"
    button.click()
    assert calls == ["clicked"]


def test_invalid_titlebar_widget_args(titlebar):
    """Extension API rejects ambiguous slot definitions."""
    with pytest.raises(ValueError):
        titlebar.add_titlebar_widget("", QLabel("bad"))
    with pytest.raises(ValueError):
        titlebar.add_titlebar_widget("bad", QLabel("bad"), side="middle")


def test_vm_indicator_show_and_hide(titlebar):
    """VM convenience indicator is a removable titlebar widget."""
    titlebar.set_vm_indicator("work")
    label = titlebar.titlebar_widget("vm-indicator")

    assert isinstance(label, QLabel)
    assert label.text() == "VM: work"
    assert "work" in label.toolTip()

    titlebar.set_vm_indicator(None)
    assert titlebar.titlebar_widget("vm-indicator") is None
