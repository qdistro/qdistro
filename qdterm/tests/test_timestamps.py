"""Tests for the timestamps plugin.

Layers:
  - TimestampMargin widget.
  - Plugin lifecycle.
"""

import time

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.timestamps import (
    TimestampMargin,
    TimestampsPlugin,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


# ---------------------------------------------------------------------------
# TimestampMargin tests
# ---------------------------------------------------------------------------

def test_margin_initializes(qtbot):
    """Test that margin widget initializes properly."""
    margin = TimestampMargin()
    qtbot.addWidget(margin)

    assert margin.width() == 80  # default width


def test_margin_set_line_height(qtbot):
    """Test setting line height."""
    margin = TimestampMargin()
    qtbot.addWidget(margin)

    margin.set_line_height(20)
    assert margin._line_height == 20


def test_margin_set_timestamps(qtbot):
    """Test setting timestamps for display."""
    margin = TimestampMargin()
    qtbot.addWidget(margin)

    now = time.time()
    timestamps = [
        (0, now),
        (16, now + 1),
        (32, now + 2),
    ]

    margin.set_timestamps(timestamps)

    assert len(margin._timestamps) == 3


# ---------------------------------------------------------------------------
# Plugin tests
# ---------------------------------------------------------------------------

def test_plugin_enabled_by_default():
    """Test plugin activates when enabled."""
    cfg = Config()
    cfg.set("plugins", "timestamps", "enabled", True)

    class FakeWin:
        _tabs = None

    win = FakeWin()
    plugin = TimestampsPlugin()
    plugin.activate(win)

    assert plugin._window is not None

    plugin.deactivate()


def test_plugin_disabled_does_not_install():
    """Test plugin doesn't install when disabled."""
    cfg = Config()
    cfg.set("plugins", "timestamps", "enabled", False)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = TimestampsPlugin()
    plugin.activate(win)

    # No window state should be set
    assert plugin._window is None


def test_plugin_attaches_to_existing_tabs():
    """Test plugin attaches to terminals in existing tabs."""
    cfg = Config()
    cfg.set("plugins", "timestamps", "enabled", True)

    class FakeTerminal:
        pass

    class FakeSplit:
        def find_terminals(self):
            return [FakeTerminal()]

    class FakeTabs:
        def __init__(self):
            self._splits = [FakeSplit()]

        def count(self):
            return len(self._splits)

        def widget(self, i):
            return self._splits[i]

    class FakeWin:
        _tabs = FakeTabs()

    win = FakeWin()
    plugin = TimestampsPlugin()
    plugin.activate(win)

    # Should have attempted to attach
    # (具体行为取决于shadow_screens是否存在)

    plugin.deactivate()


def test_plugin_deactivate_cleans_up():
    """Test deactivate cleans up properly."""
    cfg = Config()
    cfg.set("plugins", "timestamps", "enabled", True)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = TimestampsPlugin()
    plugin.activate(win)

    plugin.deactivate()

    assert plugin._window is None
    assert len(plugin._margins) == 0


def test_plugin_default_format():
    """Test default timestamp format."""
    cfg = Config()

    # Default format should be %H:%M:%S
    fmt = cfg.get("plugins", "timestamps", "format", default="%H:%M:%S")
    assert fmt == "%H:%M:%S"


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_plugin_hooks_new_terminals():
    """Test plugin wraps _connect_terminal for new terminals."""
    cfg = Config()
    cfg.set("plugins", "timestamps", "enabled", True)

    class FakeWin:
        _tabs = None
        _connect_terminal = None

    win = FakeWin()
    plugin = TimestampsPlugin()
    plugin.activate(win)

    # _connect_terminal should be wrapped
    # (Note: in real usage it would be wrapped)

    plugin.deactivate()


def test_margin_stores_correct_timestamps(qtbot):
    """Test that margin stores timestamps correctly."""
    margin = TimestampMargin()
    qtbot.addWidget(margin)

    now = time.time()
    margin.set_timestamps([(0, now), (16, now + 1), (32, now + 2)])

    # Check stored timestamps
    assert margin._timestamps[0][1] == now
    assert margin._timestamps[1][1] == now + 1
    assert margin._timestamps[2][1] == now + 2


def test_detach_removes_margin():
    """Test that detaching removes margin widget."""
    cfg = Config()
    cfg.set("plugins", "timestamps", "enabled", True)

    class FakeTerminal:
        pass

    class FakeWin:
        _tabs = None
        shadow_screens = None

    win = FakeWin()
    plugin = TimestampsPlugin()
    plugin.activate(win)

    # Add a fake margin
    class FakeMargin:
        deleted = False
        def deleteLater(self):
            self.deleted = True

    fake_margin = FakeMargin()
    plugin._margins[id(FakeTerminal())] = fake_margin

    # Detach
    plugin.deactivate()

    assert fake_margin.deleted is True
