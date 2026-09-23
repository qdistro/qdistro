"""Tests for the agent_event_channel plugin.

Layers:
  - Pure AgentEventChannel service.
  - Event publishing.
  - Plugin lifecycle.
  - Integration with shell_integration and triggers.
"""

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.agent_event_channel import (
    AgentEventChannel,
    AgentEventChannelPlugin,
    EventTypes,
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
# EventTypes tests
# ---------------------------------------------------------------------------

def test_event_types_constants():
    """Test event type constants are defined."""
    assert EventTypes.COMMAND_FINISHED == "command_finished"
    assert EventTypes.TRIGGER_MATCH == "trigger_match"
    assert EventTypes.CWD_CHANGED == "cwd_changed"
    # "data" is reserved by agent_control's raw PTY stream and must
    # not appear here; broadcast_event also rejects it.
    assert not hasattr(EventTypes, "DATA")


# ---------------------------------------------------------------------------
# AgentEventChannel service tests
# ---------------------------------------------------------------------------

class FakeAgentControl:
    """Fake agent_control for testing."""

    def __init__(self):
        self.broadcast_calls = []

    def broadcast_event(self, tab_id, event_type, payload):
        self.broadcast_calls.append({
            "tab_id": tab_id,
            "event_type": event_type,
            "payload": payload,
        })


class FakeWindow:
    """Fake window for testing."""


def test_service_set_enabled_events():
    """Test setting enabled events."""
    win = FakeWindow()
    fake_agent = FakeAgentControl()
    service = AgentEventChannel(win, fake_agent)

    events = ["command_finished", "trigger_match"]
    service.set_enabled_events(events)

    assert service.is_event_enabled("command_finished")
    assert service.is_event_enabled("trigger_match")
    assert not service.is_event_enabled("cwd_changed")


def test_service_publish_when_enabled():
    """Test publishing when event is enabled."""
    win = FakeWindow()
    fake_agent = FakeAgentControl()
    service = AgentEventChannel(win, fake_agent)

    service.set_enabled_events(["command_finished"])

    service.publish(123, "command_finished", {"text": "make"})

    assert len(fake_agent.broadcast_calls) == 1
    assert fake_agent.broadcast_calls[0]["tab_id"] == 123
    assert fake_agent.broadcast_calls[0]["event_type"] == "command_finished"


def test_service_ignores_disabled_events():
    """Test that disabled events are not published."""
    win = FakeWindow()
    fake_agent = FakeAgentControl()
    service = AgentEventChannel(win, fake_agent)

    service.set_enabled_events(["command_finished"])

    # Try to publish a disabled event
    service.publish(123, "trigger_match", {"text": "error"})

    assert len(fake_agent.broadcast_calls) == 0


def test_service_handles_no_agent_control():
    """Test graceful handling when agent_control is None."""
    win = FakeWindow()
    service = AgentEventChannel(win, None)

    service.set_enabled_events(["command_finished"])

    # Should not crash
    service.publish(123, "command_finished", {"text": "make"})


def test_service_default_events():
    """Test default events can be configured."""
    win = FakeWindow()
    fake_agent = FakeAgentControl()
    service = AgentEventChannel(win, fake_agent)

    # Set up default events (as the plugin does)
    default_events = ["command_finished", "trigger_match", "cwd_changed"]
    service.set_enabled_events(default_events)

    assert service.is_event_enabled("command_finished")
    assert service.is_event_enabled("trigger_match")
    assert service.is_event_enabled("cwd_changed")
    assert not service.is_event_enabled("data")  # not enabled by default


# ---------------------------------------------------------------------------
# Plugin tests
# ---------------------------------------------------------------------------

def test_plugin_activates_when_enabled():
    """Test plugin activates when enabled."""
    cfg = Config()
    cfg.set("plugins", "agent_event_channel", "enabled", True)

    # Create a fake window with agent_control
    class FakeWin:
        _tabs = None
        agent_control = FakeAgentControl()

    win = FakeWin()
    plugin = AgentEventChannelPlugin()
    plugin.activate(win)

    assert plugin._service is not None

    plugin.deactivate()


def test_plugin_no_agent_control_does_not_install():
    """Test plugin doesn't install when agent_control is missing."""
    cfg = Config()
    cfg.set("plugins", "agent_event_channel", "enabled", True)

    class FakeWin:
        _tabs = None
        # No agent_control

    win = FakeWin()
    plugin = AgentEventChannelPlugin()
    plugin.activate(win)

    # Should not install service
    assert plugin._service is None


def test_plugin_disabled_does_not_install():
    """Test plugin doesn't install when disabled."""
    cfg = Config()
    cfg.set("plugins", "agent_event_channel", "enabled", False)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = AgentEventChannelPlugin()
    plugin.activate(win)

    assert plugin._window is None


def test_plugin_custom_events():
    """Test plugin respects custom event config."""
    cfg = Config()
    cfg.set("plugins", "agent_event_channel", "enabled", True)
    cfg.set("plugins", "agent_event_channel", "events", ["command_finished"])

    class FakeWin:
        _tabs = None
        agent_control = FakeAgentControl()

    win = FakeWin()
    plugin = AgentEventChannelPlugin()
    plugin.activate(win)

    assert plugin._service is not None
    assert plugin._service.is_event_enabled("command_finished")
    assert not plugin._service.is_event_enabled("trigger_match")

    plugin.deactivate()


def test_plugin_installs_service_on_window():
    """Test plugin installs service on window."""
    cfg = Config()
    cfg.set("plugins", "agent_event_channel", "enabled", True)

    class FakeWin:
        _tabs = None
        agent_control = FakeAgentControl()

    win = FakeWin()
    plugin = AgentEventChannelPlugin()
    plugin.activate(win)

    assert hasattr(win, "agent_events")

    plugin.deactivate()


def test_plugin_deactivate_cleans_up():
    """Test deactivate cleans up properly."""
    cfg = Config()
    cfg.set("plugins", "agent_event_channel", "enabled", True)

    class FakeWin:
        _tabs = None
        agent_control = FakeAgentControl()

    win = FakeWin()
    plugin = AgentEventChannelPlugin()
    plugin.activate(win)

    plugin.deactivate()

    assert plugin._window is None
    assert plugin._service is None


def test_plugin_subscribes_to_shell_integration():
    """Test plugin subscribes to shell integration via the documented
    ``subscribe_command_finished`` service API (not the per-history
    ``add_subscriber`` — that's a different layer)."""
    cfg = Config()
    cfg.set("plugins", "agent_event_channel", "enabled", True)

    class FakeShellIntegration:
        def __init__(self):
            self._subscribers = []

        def subscribe_command_finished(self, cb):
            self._subscribers.append(cb)

        def unsubscribe_command_finished(self, cb):
            try:
                self._subscribers.remove(cb)
            except ValueError:
                pass

    class FakeWin:
        _tabs = None
        shell_integration = FakeShellIntegration()
        agent_control = FakeAgentControl()

    win = FakeWin()
    plugin = AgentEventChannelPlugin()
    plugin.activate(win)

    assert len(win.shell_integration._subscribers) == 1

    plugin.deactivate()
    assert win.shell_integration._subscribers == []


def test_plugin_subscribes_to_triggers():
    """Test plugin subscribes to triggers."""
    cfg = Config()
    cfg.set("plugins", "agent_event_channel", "enabled", True)

    class FakeTrigger:
        def __init__(self):
            self._subscribers = []

        def subscribe(self, cb):
            self._subscribers.append(cb)

        def unsubscribe(self, cb):
            self._subscribers.remove(cb)

    class FakeWin:
        _tabs = None
        triggers = FakeTrigger()
        agent_control = FakeAgentControl()

    win = FakeWin()
    plugin = AgentEventChannelPlugin()
    plugin.activate(win)

    # Should have subscribed
    assert len(win.triggers._subscribers) == 1

    plugin.deactivate()


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_plugin_loads_on_real_window(qtbot):
    """Test plugin loads on real window."""
    pytest.skip("Requires full MainWindow integration - run manually")


def test_service_publish_command_finished():
    """Test publishing command_finished event."""
    win = FakeWindow()
    fake_agent = FakeAgentControl()
    service = AgentEventChannel(win, fake_agent)
    service.set_enabled_events(["command_finished"])

    # Fake command record
    class FakeRecord:
        text = "make"
        exit_status = 0
        started_at = 1234567890.0
        finished_at = 1234567902.4
        cwd = "/home/user/project"

    payload = {
        "command": {
            "text": FakeRecord.text,
            "exit_status": FakeRecord.exit_status,
            "started_at": FakeRecord.started_at,
            "finished_at": FakeRecord.finished_at,
        },
        "cwd": FakeRecord.cwd,
    }

    service.publish(123, "command_finished", payload)

    assert len(fake_agent.broadcast_calls) == 1
    assert fake_agent.broadcast_calls[0]["event_type"] == "command_finished"


def test_service_publish_trigger_match():
    """Test publishing trigger_match event."""
    win = FakeWindow()
    fake_agent = FakeAgentControl()
    service = AgentEventChannel(win, fake_agent)
    service.set_enabled_events(["trigger_match"])

    payload = {
        "rule_id": "errors",
        "pattern": "ERROR:",
        "line": "ERROR: build failed",
        "match": "ERROR:",
        "captured": {"file": "src/x.c", "lineno": 42},
    }

    service.publish(123, "trigger_match", payload)

    assert len(fake_agent.broadcast_calls) == 1
    assert fake_agent.broadcast_calls[0]["event_type"] == "trigger_match"
    assert fake_agent.broadcast_calls[0]["payload"]["captured"]["file"] == "src/x.c"
