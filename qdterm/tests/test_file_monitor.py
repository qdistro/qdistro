"""Tests for file_monitor plugin."""

import os

import pytest
from qterminator import config as config_mod
from qterminator.config import Config
from qterminator.plugins.file_monitor import (
    DEFAULT_CHECK_INTERVAL,
    DEFAULT_IGNORE_PATTERNS,
    DEFAULT_INACTIVITY_TIMEOUT,
    DEFAULT_RECURSIVE,
    FileMonitorPlugin,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "cfg" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


def _touch(path, mtime):
    """Create empty file at path and set its mtime."""
    with open(path, "w"):
        pass
    os.utime(path, (mtime, mtime))


# ---------------------------------------------------------------------------
# _scan_directory tests
# ---------------------------------------------------------------------------

def test_scan_directory_returns_max_mtime(tmp_path):
    plugin = FileMonitorPlugin()
    _touch(tmp_path / "a.txt", 1000.0)
    _touch(tmp_path / "b.txt", 2000.0)
    _touch(tmp_path / "c.txt", 1500.0)

    result = plugin._scan_directory(str(tmp_path))
    assert result == 2000.0


def test_scan_directory_empty(tmp_path):
    plugin = FileMonitorPlugin()
    assert plugin._scan_directory(str(tmp_path)) is None


def test_scan_directory_recursive(tmp_path):
    plugin = FileMonitorPlugin()
    _touch(tmp_path / "top.txt", 1000.0)
    sub = tmp_path / "sub"
    sub.mkdir()
    _touch(sub / "deep.txt", 5000.0)

    result = plugin._scan_directory(str(tmp_path), recursive=True)
    assert result == 5000.0


def test_scan_directory_non_recursive(tmp_path):
    plugin = FileMonitorPlugin()
    _touch(tmp_path / "top.txt", 1000.0)
    sub = tmp_path / "sub"
    sub.mkdir()
    _touch(sub / "deep.txt", 5000.0)

    result = plugin._scan_directory(str(tmp_path), recursive=False)
    assert result == 1000.0


def test_scan_directory_ignores_patterns(tmp_path):
    plugin = FileMonitorPlugin()
    _touch(tmp_path / "real.log", 1000.0)
    _touch(tmp_path / "junk.tmp", 9999.0)
    _touch(tmp_path / "vim.swp", 9999.0)

    result = plugin._scan_directory(
        str(tmp_path), ignore_patterns=["*.tmp", "*.swp"],
    )
    assert result == 1000.0


def test_scan_directory_ignores_subdirs_recursive(tmp_path):
    plugin = FileMonitorPlugin()
    _touch(tmp_path / "real.log", 1000.0)
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    _touch(gitdir / "HEAD", 9999.0)

    result = plugin._scan_directory(
        str(tmp_path), recursive=True, ignore_patterns=[".git"],
    )
    assert result == 1000.0


def test_scan_nonexistent_directory(tmp_path):
    plugin = FileMonitorPlugin()
    missing = tmp_path / "nope"
    assert plugin._scan_directory(str(missing)) is None
    assert plugin._scan_directory(None) is None
    assert plugin._scan_directory("") is None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_plugin_default_config(fresh_config):
    plugin = FileMonitorPlugin()
    plugin.activate(app_controller=None)
    try:
        assert plugin._inactivity_timeout == DEFAULT_INACTIVITY_TIMEOUT
        assert plugin._check_interval == DEFAULT_CHECK_INTERVAL
        assert plugin._recursive == DEFAULT_RECURSIVE
        assert plugin._ignore_patterns == DEFAULT_IGNORE_PATTERNS
    finally:
        plugin.deactivate()


def test_plugin_reads_config(fresh_config, tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.toml"
    cfg_file.write_text(
        "[plugins.file_monitor]\n"
        "inactivity_timeout_s = 90\n"
        "check_interval_s = 10\n"
        "recursive = true\n"
        'ignore_patterns = ["*.bak"]\n'
    )
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(cfg_file))
    Config._instance = None

    plugin = FileMonitorPlugin()
    plugin.activate(app_controller=None)
    try:
        assert plugin._inactivity_timeout == 90
        assert plugin._check_interval == 10
        assert plugin._recursive is True
        assert plugin._ignore_patterns == ["*.bak"]
    finally:
        plugin.deactivate()


# ---------------------------------------------------------------------------
# Timer lifecycle
# ---------------------------------------------------------------------------

def test_plugin_activate_starts_timer(fresh_config, qtbot):
    plugin = FileMonitorPlugin()
    plugin.activate(app_controller=None)
    try:
        assert plugin._timer is not None
        assert plugin._timer.isActive()
    finally:
        plugin.deactivate()


def test_plugin_deactivate_stops_timer(fresh_config, qtbot):
    plugin = FileMonitorPlugin()
    plugin.activate(app_controller=None)
    timer = plugin._timer
    assert timer is not None
    plugin.deactivate()
    assert plugin._timer is None
    # Old timer should no longer be active
    assert not timer.isActive()


# ---------------------------------------------------------------------------
# Per-terminal notification logic
# ---------------------------------------------------------------------------

class FakeTerminal:
    def __init__(self, cwd):
        self._cwd = cwd
        self.term = None  # _flash_terminal bails out cleanly

    def working_directory(self):
        return self._cwd


def test_check_terminal_notifies_after_idle(tmp_path, fresh_config, monkeypatch):
    # Seed one file
    _touch(tmp_path / "build.log", 1000.0)

    plugin = FileMonitorPlugin()
    plugin._inactivity_timeout = 30

    notifications = []
    monkeypatch.setattr(
        plugin, "_notify",
        lambda term, path, idle_for: notifications.append((path, idle_for)),
    )

    term = FakeTerminal(str(tmp_path))

    # First check: records baseline mtime.
    plugin._check_terminal(term, now=100.0)
    assert notifications == []

    # Another check soon after, mtime unchanged, but under threshold.
    plugin._check_terminal(term, now=110.0)
    assert notifications == []

    # Check after timeout elapsed with no new activity: should notify.
    plugin._check_terminal(term, now=200.0)
    assert len(notifications) == 1
    assert notifications[0][0] == str(tmp_path)

    # A further tick while still idle should not re-notify.
    plugin._check_terminal(term, now=300.0)
    assert len(notifications) == 1


def test_check_terminal_resets_notify_on_new_activity(
    tmp_path, fresh_config, monkeypatch,
):
    _touch(tmp_path / "build.log", 1000.0)

    plugin = FileMonitorPlugin()
    plugin._inactivity_timeout = 30

    notifications = []
    monkeypatch.setattr(
        plugin, "_notify",
        lambda term, path, idle_for: notifications.append(path),
    )

    term = FakeTerminal(str(tmp_path))

    plugin._check_terminal(term, now=100.0)
    plugin._check_terminal(term, now=200.0)
    assert len(notifications) == 1

    # New activity: bump mtime on the file.
    os.utime(tmp_path / "build.log", (2000.0, 2000.0))
    plugin._check_terminal(term, now=210.0)
    # Shouldn't re-notify yet; should have reset.
    assert len(notifications) == 1

    # Now idle again past threshold -> another notification.
    plugin._check_terminal(term, now=300.0)
    assert len(notifications) == 2


def test_check_terminal_no_notify_on_empty_dir(
    tmp_path, fresh_config, monkeypatch,
):
    plugin = FileMonitorPlugin()
    plugin._inactivity_timeout = 30

    notifications = []
    monkeypatch.setattr(
        plugin, "_notify",
        lambda term, path, idle_for: notifications.append(path),
    )

    term = FakeTerminal(str(tmp_path))
    plugin._check_terminal(term, now=100.0)
    plugin._check_terminal(term, now=500.0)
    # Empty directory: max_mtime is None, never fire.
    assert notifications == []
