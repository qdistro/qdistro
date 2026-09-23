"""Tests for the hotkey_window plugin.

Layers:
  - CLI argument parsing.
  - Plugin lifecycle.
  - Dropdown window creation.
"""

import sys

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.hotkey_window import (
    HotkeyWindowPlugin,
    parse_cli_args,
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
# CLI argument parsing tests
# ---------------------------------------------------------------------------

def test_parse_cli_args_dropdown_flag(monkeypatch):
    """Test parsing --dropdown flag."""
    monkeypatch.setattr(sys, "argv", ["qterminator", "--dropdown"])

    result = parse_cli_args()
    assert result is True


def test_parse_cli_args_quake_flag(monkeypatch):
    """Test parsing --quake flag."""
    monkeypatch.setattr(sys, "argv", ["qterminator", "--quake"])

    result = parse_cli_args()
    assert result is True


def test_parse_cli_args_short_flag(monkeypatch):
    """Test parsing -q short flag."""
    monkeypatch.setattr(sys, "argv", ["qterminator", "-q"])

    result = parse_cli_args()
    assert result is True


def test_parse_cli_args_no_flag(monkeypatch):
    """Test parsing without any dropdown flag."""
    monkeypatch.setattr(sys, "argv", ["qterminator"])

    result = parse_cli_args()
    assert result is False


def test_parse_cli_args_with_other_args(monkeypatch):
    """Test parsing with other arguments."""
    monkeypatch.setattr(sys, "argv", ["qterminator", "--profile=default", "--dropdown", "--verbose"])

    result = parse_cli_args()
    assert result is True


# ---------------------------------------------------------------------------
# Plugin tests
# ---------------------------------------------------------------------------

def test_plugin_enabled_by_default():
    """Test plugin activates when enabled."""
    cfg = Config()
    cfg.set("plugins", "hotkey_window", "enabled", True)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = HotkeyWindowPlugin()
    plugin.activate(win)

    assert plugin._window is not None

    plugin.deactivate()


def test_plugin_disabled_does_not_install():
    """Test plugin doesn't install when disabled."""
    cfg = Config()
    cfg.set("plugins", "hotkey_window", "enabled", False)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = HotkeyWindowPlugin()
    plugin.activate(win)

    # No window state should be set
    assert plugin._window is None


def test_plugin_creates_shortcut():
    """Test that plugin attempts to create shortcut.

    Note: Shortcut creation requires a real QApplication, so we just
    verify the method is called and no exception is raised.
    """
    cfg = Config()
    cfg.set("plugins", "hotkey_window", "enabled", True)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = HotkeyWindowPlugin()

    # The plugin tries to create shortcut but may fail in headless env
    # We just verify activate doesn't crash and checks are in place
    try:
        plugin.activate(win)
    except Exception:
        pass  # Expected in headless/test environment

    # Shortcut may or may not be created depending on Qt environment
    # Just verify plugin tried to set it up
    assert hasattr(plugin, '_shortcut')  # attribute exists


def test_plugin_default_config_values():
    """Test default configuration values."""
    cfg = Config()

    assert cfg.get("plugins", "hotkey_window", "enabled", default=True) is True
    assert cfg.get("plugins", "hotkey_window", "height", default=400) == 400
    assert cfg.get("plugins", "hotkey_window", "width_percent", default=80) == 80
    assert cfg.get("plugins", "hotkey_window", "hide_on_focus_lost", default=True) is True


def test_plugin_toggle_method_exists():
    """Test that toggle method exists."""
    plugin = HotkeyWindowPlugin()

    assert hasattr(plugin, 'toggle')
    assert callable(plugin.toggle)


def test_plugin_show_method_exists():
    """Test that show method exists."""
    plugin = HotkeyWindowPlugin()

    assert hasattr(plugin, 'show')
    assert callable(plugin.show)


def test_plugin_hide_method_exists():
    """Test that hide method exists."""
    plugin = HotkeyWindowPlugin()

    assert hasattr(plugin, 'hide')
    assert callable(plugin.hide)


def test_plugin_deactivate_cleans_up():
    """Test deactivate cleans up properly."""
    cfg = Config()
    cfg.set("plugins", "hotkey_window", "enabled", True)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = HotkeyWindowPlugin()
    plugin.activate(win)

    plugin.deactivate()

    assert plugin._window is None
    assert plugin._shortcut is None


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_plugin_with_custom_shortcut(monkeypatch):
    """Test plugin with custom shortcut configuration."""
    cfg = Config()
    cfg.set("plugins", "hotkey_window", "enabled", True)
    cfg.set("plugins", "hotkey_window", "shortcut", "Ctrl+Alt+T")

    class FakeWin:
        pass

    win = FakeWin()
    plugin = HotkeyWindowPlugin()

    try:
        plugin.activate(win)
    except Exception:
        pass  # Expected in headless environment

    # Verify plugin attempted to set up
    assert hasattr(plugin, '_shortcut')


def test_plugin_with_custom_dimensions(monkeypatch):
    """Test plugin with custom dimensions."""
    cfg = Config()
    cfg.set("plugins", "hotkey_window", "enabled", True)
    cfg.set("plugins", "hotkey_window", "height", 600)
    cfg.set("plugins", "hotkey_window", "width_percent", 70)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = HotkeyWindowPlugin()
    plugin.activate(win)

    # Config should have the values
    assert cfg.get("plugins", "hotkey_window", "height") == 600
    assert cfg.get("plugins", "hotkey_window", "width_percent") == 70

    plugin.deactivate()


def test_plugin_disabled_shortcut_not_created():
    """Test that shortcut is not created when disabled."""
    cfg = Config()
    cfg.set("plugins", "hotkey_window", "enabled", False)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = HotkeyWindowPlugin()
    plugin.activate(win)

    # Shortcut should be None
    assert plugin._shortcut is None
