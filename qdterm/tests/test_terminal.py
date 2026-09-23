"""Tests for terminal widget and split container (requires display)."""


import pytest
import qterminator.config as config_mod
from PyQt6.QtCore import Qt, pyqtSignal
from qterminator.config import Config
from qterminator.splitter import SplitContainer
from qterminator.terminal import TerminalWidget


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_terminal(qtbot, **kwargs):
    """Create a TerminalWidget and register it with qtbot."""
    t = TerminalWidget(**kwargs)
    qtbot.addWidget(t)
    return t


def _make_split(qtbot, orientation=Qt.Orientation.Horizontal):
    """Create a SplitContainer and register it with qtbot."""
    s = SplitContainer(orientation=orientation)
    qtbot.addWidget(s)
    return s


# ===================================================================
# TerminalWidget tests
# ===================================================================


class TestTerminalCreation:
    """Tests for TerminalWidget construction."""

    def test_creates_with_defaults(self, qtbot):
        t = _make_terminal(qtbot)
        assert t.shell_pid() > 0

    def test_creates_with_custom_working_directory(self, qtbot, tmp_path):
        t = _make_terminal(qtbot, working_directory=str(tmp_path))
        wd = t.working_directory()
        assert isinstance(wd, str)

    def test_creates_with_custom_profile(self, qtbot):
        t = _make_terminal(qtbot, profile="default")
        assert t._profile_name == "default"

    def test_creates_with_nonexistent_profile(self, qtbot):
        # Should fall back gracefully (Config.get_profile returns defaults)
        t = _make_terminal(qtbot, profile="does_not_exist")
        assert t._profile_name == "does_not_exist"
        assert t.shell_pid() > 0


class TestTerminalTitle:
    """Tests for the title property."""

    def test_title_returns_string(self, qtbot):
        t = _make_terminal(qtbot)
        assert isinstance(t.title(), str)

    def test_title_default_nonempty(self, qtbot):
        t = _make_terminal(qtbot)
        assert len(t.title()) > 0


class TestTerminalActive:
    """Tests for is_active / set_active."""

    def test_default_not_active(self, qtbot):
        t = _make_terminal(qtbot)
        assert t.is_active() is False

    def test_set_active_true(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_active(True)
        assert t.is_active() is True

    def test_set_active_toggle(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_active(True)
        assert t.is_active() is True
        t.set_active(False)
        assert t.is_active() is False


class TestTerminalReadOnly:
    """Tests for read-only mode."""

    def test_default_not_read_only(self, qtbot):
        t = _make_terminal(qtbot)
        assert t.is_read_only() is False

    def test_set_read_only(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        assert t.is_read_only() is True

    def test_toggle_read_only(self, qtbot):
        t = _make_terminal(qtbot)
        assert t.is_read_only() is False
        t.toggle_read_only()
        assert t.is_read_only() is True
        t.toggle_read_only()
        assert t.is_read_only() is False

    # --- Enforcement: the flag must actually block direct input ---

    def _key_event(self, text="a", key=Qt.Key.Key_A):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        return QKeyEvent(QEvent.Type.KeyPress, key,
                         Qt.KeyboardModifier.NoModifier, text)

    def test_event_filter_installed(self, qtbot):
        t = _make_terminal(qtbot)
        assert t._read_only_filter is not None

    def test_key_delivery_to_focus_proxy_blocked_when_read_only(self, qtbot):
        """End-to-end: the filter must intercept events delivered to the
        widget that actually receives keystrokes -- QTermWidget's focus proxy
        (the inner TerminalDisplay), not the outer QTermWidget. A filter on
        the wrong widget would be silently bypassed by real typing.
        """
        from PyQt6.QtWidgets import QApplication
        t = _make_terminal(qtbot)
        target = t._term.focusProxy() or t._term

        # Writable: event is not accepted by our filter (propagates normally).
        ev = self._key_event()
        t.set_read_only(False)
        QApplication.sendEvent(target, ev)
        # Read-only: sending to the real input target is fully consumed.
        ev2 = self._key_event()
        t.set_read_only(True)
        handled = QApplication.sendEvent(target, ev2)
        assert handled is True and ev2.isAccepted()

    def test_keypress_passes_through_when_writable(self, qtbot):
        t = _make_terminal(qtbot)
        # Not read-only -> filter must not swallow (returns False).
        ev = self._key_event()
        assert t._read_only_filter.eventFilter(t._term, ev) is False

    def test_keypress_swallowed_when_read_only(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        ev = self._key_event()
        # True == swallowed: the key never reaches the terminal/pty.
        assert t._read_only_filter.eventFilter(t._term, ev) is True

    def test_keyrelease_swallowed_when_read_only(self, qtbot):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        ev = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_A,
                       Qt.KeyboardModifier.NoModifier, "a")
        assert t._read_only_filter.eventFilter(t._term, ev) is True

    def test_input_method_swallowed_when_read_only(self, qtbot):
        from PyQt6.QtGui import QInputMethodEvent
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        ev = QInputMethodEvent("commit", [])
        assert t._read_only_filter.eventFilter(t._term, ev) is True

    def test_input_method_passes_when_writable(self, qtbot):
        from PyQt6.QtGui import QInputMethodEvent
        t = _make_terminal(qtbot)
        ev = QInputMethodEvent("commit", [])
        assert t._read_only_filter.eventFilter(t._term, ev) is False

    def test_non_input_event_not_swallowed_when_read_only(self, qtbot):
        from PyQt6.QtCore import QEvent
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        # A paint event must always pass through, even in read-only mode.
        ev = QEvent(QEvent.Type.Paint)
        assert t._read_only_filter.eventFilter(t._term, ev) is False

    def test_paste_clipboard_blocked_when_read_only(self, qtbot, monkeypatch):
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        pasted = []
        monkeypatch.setattr(t._term, "pasteClipboard",
                            lambda: pasted.append(True))
        t.paste_clipboard()
        assert pasted == []

    def test_paste_selection_blocked_when_read_only(self, qtbot, monkeypatch):
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        pasted = []
        monkeypatch.setattr(t._term, "pasteSelection",
                            lambda: pasted.append(True))
        t.paste_selection()
        assert pasted == []

    def test_paste_allowed_when_writable(self, qtbot, monkeypatch):
        t = _make_terminal(qtbot)
        # Writable + non-dangerous clipboard -> paste proceeds.
        monkeypatch.setattr(t, "_confirm_dangerous_paste", lambda: True)
        pasted = []
        monkeypatch.setattr(t._term, "pasteClipboard",
                            lambda: pasted.append(True))
        t.paste_clipboard()
        assert pasted == [True]

    # --- send_text central gate (broadcast / MCP / snippets / triggers etc.) ---

    def test_send_text_blocked_when_read_only(self, qtbot, monkeypatch):
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        sent = []
        monkeypatch.setattr(t._term, "sendText", lambda s: sent.append(s))
        assert t.send_text("rm -rf /\n") is False
        assert sent == []

    def test_send_text_allowed_when_writable(self, qtbot, monkeypatch):
        t = _make_terminal(qtbot)
        sent = []
        monkeypatch.setattr(t._term, "sendText", lambda s: sent.append(s))
        assert t.send_text("ls\n") is True
        assert sent == ["ls\n"]

    def test_send_text_force_bypasses_read_only(self, qtbot, monkeypatch):
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        sent = []
        monkeypatch.setattr(t._term, "sendText", lambda s: sent.append(s))
        assert t.send_text("x", force=True) is True
        assert sent == ["x"]

    def test_reset_suppressed_when_read_only(self, qtbot, monkeypatch):
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        sent = []
        monkeypatch.setattr(t._term, "sendText", lambda s: sent.append(s))
        t.reset()
        assert sent == []

    # --- Middle-click paste (primary-selection paste into the pty) ---

    def _mouse_event(self, button):
        from PyQt6.QtCore import QEvent, QPointF
        from PyQt6.QtGui import QMouseEvent
        return QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(1, 1),
                           button, button, Qt.KeyboardModifier.NoModifier)

    def test_middle_click_blocked_when_read_only(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        ev = self._mouse_event(Qt.MouseButton.MiddleButton)
        assert t._read_only_filter.eventFilter(t._term, ev) is True

    def test_middle_click_passes_when_writable(self, qtbot):
        t = _make_terminal(qtbot)
        ev = self._mouse_event(Qt.MouseButton.MiddleButton)
        assert t._read_only_filter.eventFilter(t._term, ev) is False

    def test_left_click_not_blocked_when_read_only(self, qtbot):
        # Left-button selection is a non-mutating read-only operation.
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        ev = self._mouse_event(Qt.MouseButton.LeftButton)
        assert t._read_only_filter.eventFilter(t._term, ev) is False

    def test_right_click_not_blocked_when_read_only(self, qtbot):
        # Right-button drives the context menu; must keep working read-only.
        t = _make_terminal(qtbot)
        t.set_read_only(True)
        ev = self._mouse_event(Qt.MouseButton.RightButton)
        assert t._read_only_filter.eventFilter(t._term, ev) is False


class TestTerminalGroup:
    """Tests for the group property (broadcast input)."""

    def test_default_group_none(self, qtbot):
        t = _make_terminal(qtbot)
        assert t.group is None

    def test_set_group_string(self, qtbot):
        t = _make_terminal(qtbot)
        t.group = "alpha"
        assert t.group == "alpha"

    def test_change_group(self, qtbot):
        t = _make_terminal(qtbot)
        t.group = "alpha"
        t.group = "beta"
        assert t.group == "beta"

    def test_set_group_back_to_none(self, qtbot):
        t = _make_terminal(qtbot)
        t.group = "alpha"
        t.group = None
        assert t.group is None


class TestTerminalClipboard:
    """Tests that clipboard operations don't crash."""

    def test_copy_clipboard(self, qtbot):
        t = _make_terminal(qtbot)
        t.copy_clipboard()  # no crash

    def test_paste_clipboard(self, qtbot):
        t = _make_terminal(qtbot)
        t.paste_clipboard()  # no crash


class TestTerminalZoom:
    """Tests that zoom operations don't crash."""

    def test_zoom_in(self, qtbot):
        t = _make_terminal(qtbot)
        t.zoom_in()

    def test_zoom_out(self, qtbot):
        t = _make_terminal(qtbot)
        t.zoom_out()

    def test_zoom_in_multiple(self, qtbot):
        t = _make_terminal(qtbot)
        for _ in range(5):
            t.zoom_in()

    def test_zoom_out_multiple(self, qtbot):
        t = _make_terminal(qtbot)
        for _ in range(5):
            t.zoom_out()


class TestTerminalSendText:
    """Tests that send_text doesn't crash."""

    def test_send_empty_string(self, qtbot):
        t = _make_terminal(qtbot)
        t.send_text("")

    def test_send_text(self, qtbot):
        t = _make_terminal(qtbot)
        t.send_text("echo hello\n")


class TestTerminalReset:
    """Tests that reset / reset_clear don't crash."""

    def test_reset(self, qtbot):
        t = _make_terminal(qtbot)
        t.reset()

    def test_reset_clear(self, qtbot):
        t = _make_terminal(qtbot)
        t.reset_clear()


class TestTerminalSearch:
    """Tests for search bar toggle."""

    def test_toggle_search(self, qtbot):
        t = _make_terminal(qtbot)
        t.toggle_search()  # no crash


class TestTerminalFont:
    """Tests for set_font."""

    def test_set_font_valid(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_font("Monospace", 12)

    def test_set_font_different_size(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_font("Monospace", 8)
        t.set_font("Monospace", 24)


class TestTerminalColorScheme:
    """Tests for set_color_scheme."""

    def test_set_color_scheme(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_color_scheme("Linux")


class TestTerminalScrollback:
    """Tests for set_scrollback with various values."""

    def test_scrollback_zero(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_scrollback(0)

    def test_scrollback_100(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_scrollback(100)

    def test_scrollback_10000(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_scrollback(10000)

    def test_scrollback_infinite(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_scrollback(-1)


class TestTerminalApplyProfile:
    """Tests for apply_profile."""

    def test_apply_default_profile(self, qtbot):
        t = _make_terminal(qtbot)
        t.apply_profile("default")
        assert t._profile_name == "default"

    def test_apply_nonexistent_profile_falls_back(self, qtbot):
        t = _make_terminal(qtbot)
        t.apply_profile("nonexistent_profile_xyz")
        # Should not crash; falls back to defaults
        assert t._profile_name == "nonexistent_profile_xyz"


class TestTerminalMonitoring:
    """Tests for activity/silence monitoring."""

    def test_monitor_activity_enable(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_monitor_activity(True)
        assert t._monitor_activity is True

    def test_monitor_activity_disable(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_monitor_activity(True)
        t.set_monitor_activity(False)
        assert t._monitor_activity is False

    def test_monitor_silence_enable(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_monitor_silence(True)
        assert t._monitor_silence is True

    def test_monitor_silence_disable(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_monitor_silence(True)
        t.set_monitor_silence(False)
        assert t._monitor_silence is False


class TestTerminalSignals:
    """Tests that expected signals exist on TerminalWidget."""

    def test_signal_title_changed(self, qtbot):
        t = _make_terminal(qtbot)
        assert hasattr(t, "title_changed")
        assert isinstance(TerminalWidget.title_changed, pyqtSignal)

    def test_signal_finished(self, qtbot):
        t = _make_terminal(qtbot)
        assert hasattr(t, "finished")
        assert isinstance(TerminalWidget.finished, pyqtSignal)

    def test_signal_focus_gained(self, qtbot):
        t = _make_terminal(qtbot)
        assert hasattr(t, "focus_gained")
        assert isinstance(TerminalWidget.focus_gained, pyqtSignal)

    def test_signal_close_request(self, qtbot):
        t = _make_terminal(qtbot)
        assert hasattr(t, "close_request")
        assert isinstance(TerminalWidget.close_request, pyqtSignal)

    def test_signal_split_horizontal_request(self, qtbot):
        t = _make_terminal(qtbot)
        assert hasattr(t, "split_horizontal_request")
        assert isinstance(TerminalWidget.split_horizontal_request, pyqtSignal)

    def test_signal_split_vertical_request(self, qtbot):
        t = _make_terminal(qtbot)
        assert hasattr(t, "split_vertical_request")
        assert isinstance(TerminalWidget.split_vertical_request, pyqtSignal)

    def test_signal_new_tab_request(self, qtbot):
        t = _make_terminal(qtbot)
        assert hasattr(t, "new_tab_request")
        assert isinstance(TerminalWidget.new_tab_request, pyqtSignal)

    def test_signal_activity_detected(self, qtbot):
        t = _make_terminal(qtbot)
        assert hasattr(t, "activity_detected")
        assert isinstance(TerminalWidget.activity_detected, pyqtSignal)

    def test_signal_silence_detected(self, qtbot):
        t = _make_terminal(qtbot)
        assert hasattr(t, "silence_detected")
        assert isinstance(TerminalWidget.silence_detected, pyqtSignal)


class TestTerminalMisc:
    """Miscellaneous terminal tests."""

    def test_shell_pid_positive(self, qtbot):
        t = _make_terminal(qtbot)
        assert t.shell_pid() > 0

    def test_working_directory_returns_string(self, qtbot):
        t = _make_terminal(qtbot)
        wd = t.working_directory()
        assert isinstance(wd, str)

    def test_has_running_process(self, qtbot):
        t = _make_terminal(qtbot)
        # Just after creation, the shell is the foreground process
        result = t.has_running_process()
        assert isinstance(result, bool)

    def test_clear_no_crash(self, qtbot):
        t = _make_terminal(qtbot)
        t.clear()

    def test_term_property(self, qtbot):
        t = _make_terminal(qtbot)
        assert t.term is not None


# ===================================================================
# SplitContainer tests
# ===================================================================


class TestSplitContainerCreation:
    """Tests for SplitContainer construction."""

    def test_create_horizontal(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        assert s.orientation() == Qt.Orientation.Horizontal

    def test_create_vertical(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Vertical)
        assert s.orientation() == Qt.Orientation.Vertical

    def test_orientation_property(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        assert s.orientation() == Qt.Orientation.Horizontal
        s.setOrientation(Qt.Orientation.Vertical)
        assert s.orientation() == Qt.Orientation.Vertical


class TestSplitContainerAddTerminal:
    """Tests for add_terminal."""

    def test_add_terminal_creates_new(self, qtbot):
        s = _make_split(qtbot)
        t = s.add_terminal()
        assert isinstance(t, TerminalWidget)
        assert s.count() == 1

    def test_add_terminal_with_existing(self, qtbot):
        s = _make_split(qtbot)
        # Don't register `t` with qtbot: add_terminal() reparents it into `s`,
        # so qtbot would otherwise try to close an already-deleted widget at
        # teardown ("wrapped C/C++ object ... has been deleted").
        t = TerminalWidget()
        result = s.add_terminal(terminal=t)
        assert result is t
        assert s.count() == 1

    def test_add_terminal_with_working_directory(self, qtbot, tmp_path):
        s = _make_split(qtbot)
        t = s.add_terminal(working_directory=str(tmp_path))
        assert isinstance(t, TerminalWidget)
        assert s.count() == 1

    def test_add_multiple_terminals(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        t2 = s.add_terminal()
        assert s.count() == 2
        assert t1 is not t2


class TestSplitContainerSplit:
    """Tests for the split method."""

    def test_split_horizontal_same_orientation(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Horizontal)
        assert t2 is not None
        assert s.count() == 2

    def test_split_vertical_same_orientation(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Vertical)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Vertical)
        assert t2 is not None
        assert s.count() == 2

    def test_split_single_child_changes_orientation(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Vertical)
        assert t2 is not None
        # With only 1 child, orientation is changed directly
        assert s.orientation() == Qt.Orientation.Vertical
        assert s.count() == 2

    def test_split_different_orientation_creates_nested(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        t1 = s.add_terminal()
        _t2 = s.add_terminal()
        # Now split t1 vertically -- different orientation with 2 children
        t3 = s.split(t1, Qt.Orientation.Vertical)
        assert t3 is not None
        # Should have created a nested SplitContainer
        terminals = s.find_terminals()
        assert len(terminals) == 3

    def test_split_not_found_returns_none(self, qtbot):
        s = _make_split(qtbot)
        _t1 = s.add_terminal()
        orphan = _make_terminal(qtbot)
        result = s.split(orphan, Qt.Orientation.Horizontal)
        assert result is None

    def test_multiple_sequential_splits(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Horizontal)
        _t3 = s.split(t2, Qt.Orientation.Horizontal)
        terminals = s.find_terminals()
        assert len(terminals) == 3


class TestSplitContainerRemoveTerminal:
    """Tests for remove_terminal."""

    def test_remove_single_child(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        result = s.remove_terminal(t1)
        assert result is True  # container now empty

    def test_remove_with_multiple_children(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Horizontal)
        s.remove_terminal(t2)
        terminals = s.find_terminals()
        assert len(terminals) == 1
        assert terminals[0] is t1

    def test_remove_not_found(self, qtbot):
        s = _make_split(qtbot)
        _t1 = s.add_terminal()
        orphan = _make_terminal(qtbot)
        result = s.remove_terminal(orphan)
        assert result is False
        assert s.count() == 1


class TestSplitContainerFindTerminals:
    """Tests for find_terminals."""

    def test_find_terminals_empty(self, qtbot):
        s = _make_split(qtbot)
        assert s.find_terminals() == []

    def test_find_terminals_single(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        found = s.find_terminals()
        assert found == [t1]

    def test_find_terminals_multiple(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        t2 = s.add_terminal()
        found = s.find_terminals()
        assert len(found) == 2
        assert t1 in found
        assert t2 in found

    def test_find_terminals_nested_splits(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        t1 = s.add_terminal()
        _t2 = s.add_terminal()
        # Create nested split (different orientation with >1 children)
        _t3 = s.split(t1, Qt.Orientation.Vertical)
        found = s.find_terminals()
        assert len(found) == 3


class TestSplitContainerFindNextTerminal:
    """Tests for find_next_terminal."""

    def test_find_next_right(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Horizontal)
        assert s.find_next_terminal(t1, "right") is t2

    def test_find_next_down(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Horizontal)
        assert s.find_next_terminal(t1, "down") is t2

    def test_find_next_left(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Horizontal)
        assert s.find_next_terminal(t2, "left") is t1

    def test_find_next_up(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Horizontal)
        assert s.find_next_terminal(t2, "up") is t1

    def test_find_next_wraps_around_right(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Horizontal)
        # From last terminal, right wraps to first
        assert s.find_next_terminal(t2, "right") is t1

    def test_find_next_wraps_around_left(self, qtbot):
        s = _make_split(qtbot)
        t1 = s.add_terminal()
        t2 = s.split(t1, Qt.Orientation.Horizontal)
        # From first terminal, left wraps to last
        assert s.find_next_terminal(t1, "left") is t2

    def test_find_next_unknown_terminal(self, qtbot):
        s = _make_split(qtbot)
        _t1 = s.add_terminal()
        orphan = _make_terminal(qtbot)
        result = s.find_next_terminal(orphan, "right")
        assert result is None

    def test_find_next_empty_container(self, qtbot):
        s = _make_split(qtbot)
        orphan = _make_terminal(qtbot)
        result = s.find_next_terminal(orphan, "right")
        assert result is None


# ===================================================================
# Exit action tests
# ===================================================================


class TestExitAction:
    """Tests for exit_action behavior on shell termination."""

    def test_default_exit_action_is_close(self, qtbot):
        t = _make_terminal(qtbot)
        assert t._exit_action == "close"

    def test_exit_action_hold_from_profile(self, qtbot, fresh_config):
        cfg = Config()
        cfg.set("profiles", "default", "exit_action", "hold")
        t = _make_terminal(qtbot, profile="default")
        assert t._exit_action == "hold"

    def test_exit_action_restart_from_profile(self, qtbot, fresh_config):
        cfg = Config()
        cfg.set("profiles", "default", "exit_action", "restart")
        t = _make_terminal(qtbot, profile="default")
        assert t._exit_action == "restart"

    def test_on_finished_close_emits(self, qtbot, fresh_config):
        t = TerminalWidget()
        qtbot.addWidget(t)
        with qtbot.waitSignal(t.close_request, timeout=1000):
            t._on_finished()

    def test_on_finished_hold_no_close(self, qtbot, fresh_config):
        t = _make_terminal(qtbot)
        t._exit_action = "hold"
        emitted = []
        t.close_request.connect(lambda obj: emitted.append(obj))
        t._on_finished()
        assert emitted == []

    def test_on_finished_restart_calls_start(self, qtbot, monkeypatch, fresh_config):
        t = _make_terminal(qtbot)
        t._exit_action = "restart"
        calls = []
        monkeypatch.setattr(t._term, "startShellProgram", lambda: calls.append(True))
        t._on_finished()
        assert len(calls) == 1


# ===================================================================
# Key bindings tests
# ===================================================================


class TestKeyBindings:
    """Tests for terminal key bindings."""

    def test_default_key_bindings_linux(self, qtbot):
        t = _make_terminal(qtbot)
        assert t._term.keyBindings() == "linux"

    def test_key_bindings_returns_linux(self, qtbot):
        t = _make_terminal(qtbot)
        bindings = t._term.keyBindings()
        assert bindings == "linux"

    def test_key_bindings_is_string(self, qtbot):
        t = _make_terminal(qtbot)
        assert isinstance(t._term.keyBindings(), str)


# ===================================================================
# Bell behavior tests
# ===================================================================


class TestBellBehavior:
    """Tests for bell handling."""

    def test_on_bell_no_crash(self, qtbot):
        t = _make_terminal(qtbot)
        t._on_bell("bell")  # should not crash

    def test_flash_bell_no_crash(self, qtbot):
        t = _make_terminal(qtbot)
        t._flash_bell()  # should not crash

    def test_visible_bell_triggers_flash(self, qtbot, monkeypatch, fresh_config):
        cfg = Config()
        cfg.set("profiles", "default", "visible_bell", True)
        t = _make_terminal(qtbot, profile="default")
        flashed = []
        monkeypatch.setattr(t, "_flash_bell", lambda: flashed.append(True))
        t._on_bell("bell")
        assert len(flashed) == 1

    def test_no_flash_when_visible_bell_off(self, qtbot, monkeypatch, fresh_config):
        cfg = Config()
        cfg.set("profiles", "default", "visible_bell", False)
        t = _make_terminal(qtbot, profile="default")
        flashed = []
        monkeypatch.setattr(t, "_flash_bell", lambda: flashed.append(True))
        t._on_bell("bell")
        assert len(flashed) == 0


# ===================================================================
# Monitor signal emission tests
# ===================================================================


class TestMonitorSignals:
    """Tests for activity/silence signal emission."""

    def test_activity_detected_emits_when_enabled(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_monitor_activity(True)
        with qtbot.waitSignal(t.activity_detected, timeout=1000):
            t._on_activity()

    def test_silence_detected_emits_when_enabled(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_monitor_silence(True)
        with qtbot.waitSignal(t.silence_detected, timeout=1000):
            t._on_silence()

    def test_on_activity_no_emit_when_disabled(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_monitor_activity(False)
        emitted = []
        t.activity_detected.connect(lambda obj: emitted.append(obj))
        t._on_activity()
        assert emitted == []

    def test_on_silence_no_emit_when_disabled(self, qtbot):
        t = _make_terminal(qtbot)
        t.set_monitor_silence(False)
        emitted = []
        t.silence_detected.connect(lambda obj: emitted.append(obj))
        t._on_silence()
        assert emitted == []


class TestSplitContainerEqualize:
    """Tests for _equalize."""

    def test_equalize_distributes_sizes(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        s.resize(600, 400)
        s.add_terminal()
        s.add_terminal()
        s._equalize()
        sizes = s.sizes()
        assert len(sizes) == 2
        # All sizes should be equal
        assert sizes[0] == sizes[1]


class TestSplitContainerDeepNesting:
    """Tests for deep nesting (3+ levels)."""

    def test_three_level_nesting(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        t1 = s.add_terminal()
        _t2 = s.add_terminal()
        # First nested level: split t1 vertically
        _t3 = s.split(t1, Qt.Orientation.Vertical)
        # Now find the nested container and split within it
        terminals = s.find_terminals()
        assert len(terminals) == 3

    def test_deep_find_terminals(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        t1 = s.add_terminal()
        t2 = s.add_terminal()
        _t3 = s.split(t1, Qt.Orientation.Vertical)
        _t4 = s.split(t2, Qt.Orientation.Vertical)
        terminals = s.find_terminals()
        assert len(terminals) == 4

    def test_remove_from_nested(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        t1 = s.add_terminal()
        _t2 = s.add_terminal()
        t3 = s.split(t1, Qt.Orientation.Vertical)
        # Remove the nested terminal
        s.remove_terminal(t3)
        terminals = s.find_terminals()
        assert len(terminals) == 2

    def test_navigation_through_nested(self, qtbot):
        s = _make_split(qtbot, Qt.Orientation.Horizontal)
        t1 = s.add_terminal()
        _t2 = s.add_terminal()
        _t3 = s.split(t1, Qt.Orientation.Vertical)
        terminals = s.find_terminals()
        # Navigation should traverse all terminals in order
        for i, t in enumerate(terminals):
            next_t = s.find_next_terminal(t, "right")
            expected = terminals[(i + 1) % len(terminals)]
            assert next_t is expected
