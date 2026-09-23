"""agent_event_channel — push notifications to attached agents.

Extends agent_control with structured event push instead of polling.
Agents get notified in real-time when:
- A shell command finishes
- A trigger pattern matches
- The working directory changes

No more polling needed - agents receive events as they happen.

Configuration (config.toml):
    [plugins.agent_event_channel]
    enabled = false                   # default false; opt-in
    events = ["command_finished", "trigger_match", "cwd_changed"]
                                      # default: all event types

This plugin works with agent_control. Events are only sent to agents
that have attached to a tab. Agents can filter which events they receive.

Event formats:
    {"event": "command_finished", "tab_id": 1234,
     "command": {"text": "make", "exit_status": 0, "duration": 12.4,
                 "cwd": "/home/user/project"}}

    {"event": "trigger_match", "tab_id": 1234,
     "rule_id": "errors", "line": "ERROR: build failed",
     "captured": {"file": "src/x.c", "lineno": 42}}

    {"event": "cwd_changed", "tab_id": 1234, "cwd": "/new/path"}
"""

from collections.abc import Callable

from qterminator.config import Config
from qterminator.plugin import Plugin

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

class EventTypes:
    """Available event types for agent_event_channel.

    ``"data"`` is intentionally NOT included here: agent_control reserves
    that name for its raw PTY byte-stream frames, and broadcasting under
    the same event name would corrupt clients that decode by ``event``.
    """
    COMMAND_FINISHED = "command_finished"
    TRIGGER_MATCH = "trigger_match"
    CWD_CHANGED = "cwd_changed"


# ---------------------------------------------------------------------------
# Event channel service
# ---------------------------------------------------------------------------

class AgentEventChannel:
    """Service that publishes events to attached agents."""

    def __init__(self, window, agent_control):
        self._window = window
        self._agent_control = agent_control
        self._enabled_events: set[str] = set()

    def set_enabled_events(self, events: list[str]) -> None:
        """Set which event types to publish."""
        self._enabled_events = set(events)

    def is_event_enabled(self, event_type: str) -> bool:
        """Check if an event type is enabled."""
        return event_type in self._enabled_events

    def publish(self, tab_id: int, event_type: str, payload: dict) -> None:
        """Publish an event to all agents attached to this tab.

        Args:
            tab_id: The terminal/tab ID
            event_type: Event type (command_finished, trigger_match, cwd_changed)
            payload: Event data
        """
        if not self.is_event_enabled(event_type):
            return

        if self._agent_control is None:
            return

        try:
            self._agent_control.broadcast_event(tab_id, event_type, payload)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class AgentEventChannelPlugin(Plugin):
    name = "agent_event_channel"
    description = (
        "Push structured events to attached agents. "
        "Eliminates polling for command_finished, trigger_match, etc."
    )
    version = "0.1"
    capabilities = ["agent_control"]  # extends agent_control

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: AgentEventChannel | None = None
        self._shell_int_sub: Callable | None = None
        self._trigger_sub: Callable | None = None

    def activate(self, app_controller):
        cfg = Config()
        enabled = cfg.get("plugins", "agent_event_channel", "enabled", default=False)
        if not enabled:
            return

        self._window = app_controller

        # Get agent_control plugin
        agent_control = getattr(app_controller, "agent_control", None)
        if agent_control is None:
            # agent_control not enabled, can't publish events
            return

        # Create service
        self._service = AgentEventChannel(app_controller, agent_control)

        # Load enabled events from config
        default_events = [
            EventTypes.COMMAND_FINISHED,
            EventTypes.TRIGGER_MATCH,
            EventTypes.CWD_CHANGED,
        ]
        events = cfg.get(
            "plugins", "agent_event_channel", "events", default=default_events
        )
        self._service.set_enabled_events(events)

        # Expose service
        if not hasattr(app_controller, "agent_events"):
            app_controller.agent_events = self._service

        # Subscribe to shell_integration events
        self._subscribe_shell_integration()

        # Subscribe to trigger events
        self._subscribe_triggers()

    def _subscribe_shell_integration(self) -> None:
        """Subscribe to command_finished from shell_integration.

        Uses the service-level ``subscribe_command_finished`` API, which
        fans out global callbacks of shape ``cb(terminal, record)``.
        That gives us the originating terminal — required to route the
        event to the correct attached agent rather than broadcasting
        across every tab.
        """
        shell_int = getattr(self._window, "shell_integration", None)
        if shell_int is None:
            return

        def on_command_finished(terminal, record):
            self._on_command_finished(terminal, record)

        try:
            shell_int.subscribe_command_finished(on_command_finished)
            self._shell_int_sub = on_command_finished
        except Exception:
            pass

    def _subscribe_triggers(self) -> None:
        """Subscribe to trigger match events."""
        triggers = getattr(self._window, "triggers", None)
        if triggers is None:
            return

        def on_trigger_match(event):
            self._on_trigger_match(event)

        try:
            triggers.subscribe(on_trigger_match)
            self._trigger_sub = on_trigger_match
        except Exception:
            pass

    def _on_command_finished(self, terminal, record) -> None:
        """Handle command_finished from shell_integration.

        Routes the event to the originating terminal's attached agents
        only — broadcasting to every tab would falsely tell an agent
        attached to tab A that a command finished in tab B.

        When ``command_telemetry`` is loaded, it broadcasts its own
        enriched ``command_finished`` event with telemetry attached
        and annotates the record. To avoid duplicate frames on the
        wire, we skip publishing here when ``record.telemetry`` is
        already populated — telemetry's broadcast is the canonical
        one and carries strictly more information.
        """
        if self._service is None or terminal is None:
            return
        if getattr(record, "telemetry", None) is not None:
            return

        payload = {
            "command": {
                "text": record.text,
                "exit_status": record.exit_status,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
            },
            "cwd": record.cwd or "",
        }

        self._service.publish(
            id(terminal),
            EventTypes.COMMAND_FINISHED,
            payload,
        )

    def _on_trigger_match(self, event) -> None:
        """Handle trigger match from triggers plugin.

        Reads only the documented ``TriggerEvent`` fields
        (rule_name, action, text, groups, terminal) — earlier code
        looked up ``event.rule.pattern`` and ``event.match`` which
        don't exist on the dataclass, so the payload was always
        blank ``rule_id``/``match``.
        """
        if self._service is None:
            return

        terminal = getattr(event, "terminal", None)
        if terminal is None:
            return

        payload = {
            "rule_id": getattr(event, "rule_name", "") or "",
            "action": getattr(event, "action", "") or "",
            "match": getattr(event, "text", "") or "",
        }
        groups = getattr(event, "groups", None)
        if groups:
            payload["captured"] = groups

        self._service.publish(
            id(terminal),
            EventTypes.TRIGGER_MATCH,
            payload,
        )

    def deactivate(self):
        # Unsubscribe from shell_integration
        if self._shell_int_sub is not None:
            shell_int = getattr(self._window, "shell_integration", None)
            if shell_int is not None:
                try:
                    shell_int.unsubscribe_command_finished(self._shell_int_sub)
                except Exception:
                    pass
            self._shell_int_sub = None

        # Unsubscribe from triggers
        if self._trigger_sub is not None:
            triggers = getattr(self._window, "triggers", None)
            if triggers is not None:
                try:
                    triggers.unsubscribe(self._trigger_sub)
                except Exception:
                    pass
            self._trigger_sub = None

        # Remove service from window
        if (self._window is not None
                and getattr(self._window, "agent_events", None) is self._service):
            try:
                del self._window.agent_events
            except AttributeError:
                pass

        self._service = None
        self._window = None
