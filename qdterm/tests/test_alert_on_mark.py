"""Tests for the alert_on_mark plugin.

Layers:
  - Pure AlertOnMarkService (no Qt).
  - Plugin lifecycle tests.
"""

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.alert_on_mark import (
    AlertOnMarkPlugin,
    AlertOnMarkService,
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
# Pure service tests
# ---------------------------------------------------------------------------

class FakeTerminal:
    """Fake terminal for testing service."""
    pass


class FakeShellIntegration:
    """Fake shell integration with correct global subscribe API."""

    def __init__(self):
        self._global_subs = []  # list of callbacks (terminal, record)

    def subscribe_command_finished(self, callback):
        """Global subscription - callback receives (terminal, record)."""
        if callback not in self._global_subs:
            self._global_subs.append(callback)

    def unsubscribe_command_finished(self, callback):
        try:
            self._global_subs.remove(callback)
        except ValueError:
            pass

    def get_history(self, terminal):
        return None


class FakeWindow:
    """Fake window for testing service."""

    def __init__(self):
        self.shell_integration = FakeShellIntegration()


class FakeRecord:
    """Fake command record for testing."""

    def __init__(self, exit_status=0, text="test"):
        self.exit_status = exit_status
        self.text = text


def test_service_arm_sets_armed_flag():
    win = FakeWindow()
    service = AlertOnMarkService(win)
    term = FakeTerminal()

    result = service.arm(term)
    assert result is True
    assert service.is_armed(term) is True


def test_service_disarm_clears_armed_flag():
    win = FakeWindow()
    service = AlertOnMarkService(win)
    term = FakeTerminal()

    service.arm(term)
    assert service.is_armed(term) is True

    result = service.disarm(term)
    assert result is True
    assert service.is_armed(term) is False


def test_service_is_armed_false_for_unknown_terminal():
    win = FakeWindow()
    service = AlertOnMarkService(win)
    term = FakeTerminal()

    assert service.is_armed(term) is False


def test_service_fires_notification_when_armed(monkeypatch):
    win = FakeWindow()
    service = AlertOnMarkService(win)
    term = FakeTerminal()

    # Mock subprocess.Popen to track notification call
    called = []
    def fake_popen(cmd, **kwargs):
        called.append(cmd)

    # Patch in the correct module
    monkeypatch.setattr("qterminator.plugins.alert_on_mark.subprocess.Popen", fake_popen)

    # Arm the service
    service.arm(term)

    # Fire command finished via the service
    record = FakeRecord(exit_status=0, text="make")
    service._on_command_finished(term, record)

    # Should have called notification
    assert len(called) == 1
    # Should be disarmed after firing (one-shot)
    assert service.is_armed(term) is False


def test_service_does_not_fire_when_not_armed(monkeypatch):
    win = FakeWindow()
    service = AlertOnMarkService(win)
    term = FakeTerminal()

    called = []
    def fake_popen(cmd, **kwargs):
        called.append(cmd)

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    record = FakeRecord(exit_status=0)
    service._on_command_finished(term, record)

    # Should NOT have called notification
    assert len(called) == 0


def test_service_notify_includes_exit_status(monkeypatch):
    win = FakeWindow()
    service = AlertOnMarkService(win)
    term = FakeTerminal()

    called = []
    def fake_popen(cmd, **kwargs):
        called.append(cmd)

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    # Test exit status 0 (success)
    record = FakeRecord(exit_status=0)
    service._notify(term, record)
    assert "completed successfully" in called[0][2]

    called.clear()

    # Test non-zero exit status
    record = FakeRecord(exit_status=1)
    service._notify(term, record)
    assert "exit code: 1" in called[0][2]


def test_service_notify_includes_command_text(monkeypatch):
    win = FakeWindow()
    service = AlertOnMarkService(win)
    term = FakeTerminal()

    called = []
    def fake_popen(cmd, **kwargs):
        called.append(cmd)

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    record = FakeRecord(text="make -j8 build")
    service._notify(term, record)

    assert "make -j8 build" in called[0][2]


# ---------------------------------------------------------------------------
# Plugin tests
# ---------------------------------------------------------------------------

def test_plugin_disabled_does_not_install_service():
    cfg = Config()
    cfg.set("plugins", "alert_on_mark", "enabled", False)

    class FakeWindow:
        pass

    win = FakeWindow()
    plugin = AlertOnMarkPlugin()
    plugin.activate(win)

    assert not hasattr(win, "alert_on_mark")


def test_plugin_enabled_registers_service():
    cfg = Config()
    cfg.set("plugins", "alert_on_mark", "enabled", True)

    class FakeWin:
        shell_integration = FakeShellIntegration()

    win = FakeWin()
    plugin = AlertOnMarkPlugin()
    plugin.activate(win)

    assert hasattr(win, "alert_on_mark")

    plugin.deactivate()


def test_plugin_subscribes_to_shell_integration():
    """Test that plugin subscribes to global shell_integration events."""
    cfg = Config()
    cfg.set("plugins", "alert_on_mark", "enabled", True)

    shell_int = FakeShellIntegration()

    class FakeWin:
        shell_integration = shell_int

    win = FakeWin()
    plugin = AlertOnMarkPlugin()
    plugin.activate(win)

    # Should have subscribed
    assert len(shell_int._global_subs) == 1

    plugin.deactivate()


def test_plugin_deactivate_removes_service():
    """Test that deactivate cleans up properly."""
    cfg = Config()
    cfg.set("plugins", "alert_on_mark", "enabled", True)

    shell_int = FakeShellIntegration()

    class FakeWin:
        shell_integration = shell_int

    win = FakeWin()
    plugin = AlertOnMarkPlugin()
    plugin.activate(win)

    assert hasattr(win, "alert_on_mark")

    plugin.deactivate()

    assert not hasattr(win, "alert_on_mark")
    # Should have unsubscribed
    assert len(shell_int._global_subs) == 0


def test_plugin_default_disabled():
    """Test that plugin is disabled by default."""
    _cfg = Config()
    # Don't set enabled - should default to False

    class FakeWin:
        pass

    win = FakeWin()
    plugin = AlertOnMarkPlugin()
    plugin.activate(win)

    # Should not install service when disabled
    assert not hasattr(win, "alert_on_mark")
