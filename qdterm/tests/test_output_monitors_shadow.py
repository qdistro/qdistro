"""Regression tests for output_monitors using ShadowScreenRegistry."""

import pytest

pytest.importorskip("pyte")

import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.output_monitors import (
    BuildProgressMonitor,
    ErrorDetector,
    LogLevelColorizer,
    LongCommandNotifier,
    SensitiveDataWarner,
    _install_connect_hook,
    _remove_connect_hook,
    _snapshot_text,
)
from qterminator.shadow_screen import ShadowScreenRegistry


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


@pytest.fixture
def terminal(qtbot):
    from qterminator.terminal import TerminalWidget
    t = TerminalWidget()
    qtbot.addWidget(t)
    t.resize(800, 400)
    t.show()
    qtbot.waitExposed(t)
    yield t


class _Split:
    def __init__(self, terminal):
        self._terminal = terminal

    def find_terminals(self):
        return [self._terminal]


class _Tabs:
    def __init__(self, terminal):
        self._split = _Split(terminal)

    def count(self):
        return 1

    def widget(self, _index):
        return self._split


class _Window:
    def __init__(self, terminal):
        self.shadow_screens = ShadowScreenRegistry()
        self._tabs = _Tabs(terminal)

    def _connect_terminal(self, _terminal):
        pass


def test_build_progress_uses_final_rendered_carriage_return(qtbot, terminal):
    win = _Window(terminal)
    plugin = BuildProgressMonitor()
    plugin.activate(win)
    try:
        handle, _listener = plugin._handles[id(terminal)]
        handle.shadow.feed("\r[1/100]\r[2/100]\r[3/100]\n")
        qtbot.wait(plugin.DEBOUNCE_MS + 40)
        assert terminal._titlebar._activity_label.toolTip() == "Build progress: 3%"
    finally:
        plugin.deactivate()


def test_error_detector_snapshot_flags_rendered_screen(terminal):
    plugin = ErrorDetector()
    plugin.on_snapshot(terminal, {"lines": ["", "BUILD ERROR", ""]})
    assert terminal._titlebar._activity_label.toolTip() == "Error detected in output"
    assert terminal._titlebar._title_label.text().startswith("\u26a0 ")


def test_log_level_snapshot_replaces_raw_history(terminal):
    plugin = LogLevelColorizer()
    plugin.on_output(terminal, "ERROR one\nERROR two\n")
    plugin.on_snapshot(terminal, {"lines": ["INFO ok", "DEBUG detail"]})
    tooltip = terminal._titlebar._activity_label.toolTip()
    assert "ERROR=0" in tooltip
    assert "INFO=1" in tooltip
    assert "DEBUG=1" in tooltip


def test_sensitive_data_snapshot_detects_overwritten_secret(terminal):
    plugin = SensitiveDataWarner()
    plugin.on_snapshot(
        terminal,
        {"lines": ["token = abcdefghijklmnopqrstuvwxyz123456"]},
    )
    assert "Possible secret" in terminal._titlebar._activity_label.toolTip()


# ---------------------------------------------------------------------------
# New tests appended below
# ---------------------------------------------------------------------------


def test_two_watchers_both_see_data(qtbot, terminal):
    """Multiple watchers activated on the same terminal don't conflict;
    both see shadow data and update the titlebar independently."""
    win = _Window(terminal)
    err = ErrorDetector()
    build = BuildProgressMonitor()
    err.activate(win)
    build.activate(win)
    try:
        # Both should have a handle for the same terminal
        assert id(terminal) in err._handles
        assert id(terminal) in build._handles

        # Feed data that triggers ErrorDetector
        err_handle, _ = err._handles[id(terminal)]
        err_handle.shadow.feed("FATAL error occurred\n")
        qtbot.wait(err.DEBOUNCE_MS + 40)
        assert "Error detected" in terminal._titlebar._activity_label.toolTip()

        # Now feed data that triggers BuildProgressMonitor
        build_handle, _ = build._handles[id(terminal)]
        build_handle.shadow.feed("[50/100]\n")
        qtbot.wait(build.DEBOUNCE_MS + 40)
        assert "Build progress: 50%" in terminal._titlebar._activity_label.toolTip()
    finally:
        err.deactivate()
        build.deactivate()


def test_deactivate_releases_handles_and_removes_hook(terminal):
    """After deactivate(), shadow handles are released and the connect hook
    is removed from the app controller."""
    win = _Window(terminal)
    plugin = ErrorDetector()
    plugin.activate(win)
    assert id(terminal) in plugin._handles
    # Hook list should exist
    assert hasattr(win, "_shadow_connect_hooks")

    plugin.deactivate()

    assert len(plugin._handles) == 0
    assert plugin._window is None
    # With no hooks remaining, the hook list should be cleaned up
    assert not hasattr(win, "_shadow_connect_hooks")


def test_after_deactivate_new_terminals_not_tracked(terminal):
    """After deactivate, calling _connect_terminal for a new terminal
    should NOT trigger the watcher to attach."""
    win = _Window(terminal)
    plugin = ErrorDetector()
    plugin.activate(win)
    plugin.deactivate()

    # Simulate connecting a new terminal -- the original connect function
    # should be restored, and the plugin hook should no longer fire.
    win._connect_terminal(terminal)
    assert id(terminal) not in plugin._handles


def test_long_command_notifier_uses_raw_output(qtbot, terminal):
    """LongCommandNotifier has USE_RAW_OUTPUT=True and receives raw text
    directly through on_output()."""
    win = _Window(terminal)
    plugin = LongCommandNotifier()
    assert plugin.USE_RAW_OUTPUT is True
    plugin.activate(win)
    try:
        handle, _ = plugin._handles[id(terminal)]
        handle.shadow.feed("compiling...\n")
        # The raw text should be recorded in _state
        qtbot.wait(20)
        state = plugin._state.get(id(terminal))
        assert state is not None
        assert state["total_bytes"] > 0
    finally:
        plugin.deactivate()


def test_long_command_notifier_no_notify_when_focused(qtbot, terminal, monkeypatch):
    """LongCommandNotifier doesn't send notification if terminal is focused."""
    import time as time_mod

    plugin = LongCommandNotifier()
    tid = id(terminal)

    # Pretend the terminal is focused
    monkeypatch.setattr(terminal, "is_active", lambda: True)

    # Simulate: past output with enough bytes, then a long silence,
    # then new output arrives.
    fake_now = [100.0]
    monkeypatch.setattr(time_mod, "monotonic", lambda: fake_now[0])

    plugin._state[tid] = {
        "last_time": fake_now[0],
        "total_bytes": 600,
        "notified": False,
    }

    # Advance time past silence threshold
    fake_now[0] = 100.0 + plugin.SILENCE_THRESHOLD + 1

    notify_called = []
    import subprocess as sp_mod

    def mock_popen(*args, **kwargs):
        notify_called.append(args)

    monkeypatch.setattr(sp_mod, "Popen", mock_popen)

    plugin.on_output(terminal, "new output\n")

    # Since terminal is focused, notify-send should NOT be called
    assert len(notify_called) == 0


def test_build_progress_fraction_pattern(terminal):
    """BuildProgressMonitor with fraction pattern [42/100] sets 42%."""
    plugin = BuildProgressMonitor()
    plugin.on_snapshot(terminal, {"lines": ["[42/100] Compiling..."]})
    assert terminal._titlebar._activity_label.toolTip() == "Build progress: 42%"


def test_build_progress_percent_pattern(terminal):
    """BuildProgressMonitor with percent pattern 75% sets correct tooltip."""
    plugin = BuildProgressMonitor()
    plugin.on_snapshot(terminal, {"lines": ["Progress: 75%"]})
    assert terminal._titlebar._activity_label.toolTip() == "Build progress: 75%"


def test_build_progress_auto_clears_at_100(terminal):
    """BuildProgressMonitor auto-clears activity when reaching 100%."""
    plugin = BuildProgressMonitor()
    plugin.on_snapshot(terminal, {"lines": ["[100/100] Done"]})
    # At 100% the plugin should auto-clear and reset the tooltip
    assert terminal._titlebar._activity_label.toolTip() == "Activity detected"


def test_build_progress_clears_on_build_success(terminal):
    """BuildProgressMonitor clears progress on BUILD SUCCESS pattern."""
    plugin = BuildProgressMonitor()
    # First set some progress
    plugin.on_snapshot(terminal, {"lines": ["[50/100] Compiling"]})
    assert "Build progress: 50%" in terminal._titlebar._activity_label.toolTip()

    # Then complete
    plugin.on_snapshot(terminal, {"lines": ["BUILD SUCCESS"]})
    assert terminal._titlebar._activity_label.toolTip() == "Activity detected"


def test_error_detector_segfault_trigger(terminal):
    """ErrorDetector triggers on 'segfault' in snapshot."""
    plugin = ErrorDetector()
    plugin.on_snapshot(terminal, {"lines": [
        "segfault at 0x00007fff",
        "core dumped",
    ]})
    assert terminal._titlebar._activity_label.toolTip() == "Error detected in output"
    assert terminal._titlebar._title_label.text().startswith("⚠ ")


def test_error_detector_idempotent(terminal):
    """ErrorDetector only prefixes the title once per terminal."""
    plugin = ErrorDetector()

    plugin.on_snapshot(terminal, {"lines": ["ERROR: first"]})
    title_after_first = terminal._titlebar._title_label.text()
    assert title_after_first.startswith("⚠ ")

    # Trigger again -- title should not double-prefix
    plugin.on_snapshot(terminal, {"lines": ["ERROR: second"]})
    title_after_second = terminal._titlebar._title_label.text()
    assert title_after_second == title_after_first


def test_sensitive_data_warner_aws_key(terminal):
    """SensitiveDataWarner triggers on AWS key pattern AKIA..."""
    plugin = SensitiveDataWarner()
    plugin.on_snapshot(terminal, {"lines": [
        "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
    ]})
    tooltip = terminal._titlebar._activity_label.toolTip()
    assert "Possible secret" in tooltip


def test_sensitive_data_warner_private_key_header(terminal):
    """SensitiveDataWarner triggers on private key header."""
    plugin = SensitiveDataWarner()
    plugin.on_snapshot(terminal, {"lines": [
        "-----BEGIN RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA2zGhg...",
        "-----END RSA PRIVATE KEY-----",
    ]})
    tooltip = terminal._titlebar._activity_label.toolTip()
    assert "Possible secret" in tooltip


def test_log_level_colorizer_dominant_level(terminal):
    """LogLevelColorizer with mixed levels picks ERROR as dominant when present."""
    plugin = LogLevelColorizer()
    plugin.on_snapshot(terminal, {"lines": [
        "INFO startup complete",
        "DEBUG loading module",
        "INFO connected",
        "ERROR connection refused",
        "WARN timeout approaching",
    ]})
    tooltip = terminal._titlebar._activity_label.toolTip()
    assert "ERROR=1" in tooltip
    assert "WARN=1" in tooltip
    assert "INFO=2" in tooltip
    assert "DEBUG=1" in tooltip
    # ERROR > WARN > INFO > DEBUG, so dominant color should be error red
    style = terminal._titlebar._activity_label.styleSheet()
    assert "#e74c3c" in style  # red (error color)


def test_log_level_colorizer_warn_dominant(terminal):
    """LogLevelColorizer picks WARN as dominant when no ERRORs present."""
    plugin = LogLevelColorizer()
    plugin.on_snapshot(terminal, {"lines": [
        "INFO request handled",
        "WARNING slow query detected",
        "DEBUG cache miss",
    ]})
    tooltip = terminal._titlebar._activity_label.toolTip()
    assert "ERROR=0" in tooltip
    assert "WARN=1" in tooltip
    style = terminal._titlebar._activity_label.styleSheet()
    assert "#f39c12" in style  # yellow/orange (warn color)


def test_snapshot_text_empty_lines():
    """_snapshot_text with empty lines produces newlines between blanks."""
    result = _snapshot_text({"lines": ["", "", ""]})
    assert result == "\n\n"


def test_snapshot_text_strips_trailing_whitespace():
    """_snapshot_text strips trailing whitespace from each line."""
    result = _snapshot_text({"lines": ["hello   ", "world  "]})
    assert result == "hello\nworld"


def test_snapshot_text_missing_lines_key():
    """_snapshot_text with no 'lines' key returns empty string."""
    result = _snapshot_text({})
    assert result == ""


def test_nested_hook_install_remove(terminal):
    """Install 3 hooks, remove the middle one, remaining two still fire."""
    win = _Window(terminal)
    calls = {"a": 0, "b": 0, "c": 0}

    def hook_a(_t):
        calls["a"] += 1

    def hook_b(_t):
        calls["b"] += 1

    def hook_c(_t):
        calls["c"] += 1

    _install_connect_hook(win, hook_a)
    _install_connect_hook(win, hook_b)
    _install_connect_hook(win, hook_c)

    # All three should be in the hooks list
    assert len(win._shadow_connect_hooks) == 3

    # Simulate connecting a terminal -- all three fire
    win._connect_terminal(terminal)
    assert calls == {"a": 1, "b": 1, "c": 1}

    # Remove the middle hook
    _remove_connect_hook(win, hook_b)
    assert len(win._shadow_connect_hooks) == 2

    # Connect again -- only a and c fire
    win._connect_terminal(terminal)
    assert calls == {"a": 2, "b": 1, "c": 2}


def test_hook_cleanup_restores_original_connect(terminal):
    """After removing all hooks, _connect_terminal is restored to the original
    and the hook infrastructure attributes are cleaned up."""
    win = _Window(terminal)

    calls = []

    def hook_x(_t):
        calls.append("x")

    _install_connect_hook(win, hook_x)
    assert hasattr(win, "_shadow_connect_hooks")

    _remove_connect_hook(win, hook_x)
    # Hook infrastructure attributes should be cleaned up
    assert not hasattr(win, "_shadow_connect_hooks")
    assert not hasattr(win, "_shadow_connect_orig")

    # The restored _connect_terminal should be the original method (no hooks fire)
    win._connect_terminal(terminal)
    assert calls == []  # hook_x was removed, should not fire


def test_two_watchers_deactivate_independently(qtbot, terminal):
    """Deactivating one watcher does not affect the other's shadow handle."""
    win = _Window(terminal)
    err = ErrorDetector()
    build = BuildProgressMonitor()
    err.activate(win)
    build.activate(win)

    # Deactivate only the error detector
    err.deactivate()
    assert len(err._handles) == 0

    # Build monitor should still have its handle
    assert id(terminal) in build._handles
    build_handle, _ = build._handles[id(terminal)]
    assert not build_handle.released

    # Build monitor can still receive data
    build_handle.shadow.feed("[25/50]\n")
    qtbot.wait(build.DEBOUNCE_MS + 40)
    assert "Build progress: 50%" in terminal._titlebar._activity_label.toolTip()

    build.deactivate()


def test_long_command_notifier_tracks_bytes(terminal, monkeypatch):
    """LongCommandNotifier accumulates total_bytes from on_output calls."""
    import time as time_mod

    plugin = LongCommandNotifier()
    fake_now = [1000.0]
    monkeypatch.setattr(time_mod, "monotonic", lambda: fake_now[0])

    plugin.on_output(terminal, "short\n")
    state = plugin._state[id(terminal)]
    assert state["total_bytes"] == len("short\n")

    fake_now[0] = 1000.1  # small time advance (no silence trigger)
    plugin.on_output(terminal, "more data here\n")
    assert state["total_bytes"] == len("short\n") + len("more data here\n")
