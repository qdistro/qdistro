"""Trigger registry for the workflow engine.

Maps trigger types to watcher implementations. Each watcher
monitors a system event source (cgroup, cron, D-Bus signal, qbus)
and calls back into the engine when its condition fires.

Skeleton implementation: watchers log what they would do rather
than actually hooking into system event sources. Production
versions will use the real kernel / D-Bus / timer APIs.
"""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from workflow_schema import (  # type: ignore[import-not-found]
    TriggerDef,
    TriggerType,
    WorkflowDef,
)

logger = logging.getLogger("qdistro.workflow.triggers")

# Type for the callback the engine passes to triggers.
# Called with (workflow_name, trigger_context_dict).
TriggerCallback = Callable[[str, dict[str, Any]], None]


class BaseTrigger(ABC):
    """Abstract base for trigger watchers."""

    def __init__(self, workflow_name: str, trigger_def: TriggerDef,
                 callback: TriggerCallback):
        self.workflow_name = workflow_name
        self.trigger_def = trigger_def
        self._callback = callback
        self._active = False

    @abstractmethod
    def start(self) -> None:
        """Begin watching for the trigger condition."""

    @abstractmethod
    def stop(self) -> None:
        """Stop watching and release resources."""

    @property
    def active(self) -> bool:
        return self._active

    def fire(self, context: dict[str, Any] | None = None) -> None:
        """Called when the trigger condition is met."""
        ctx = dict(context or {})
        ctx["trigger_type"] = self.trigger_def.type.value
        ctx["fired_at"] = time.time()
        logger.info(
            "trigger fired: workflow=%s type=%s",
            self.workflow_name, self.trigger_def.type.value,
        )
        self._callback(self.workflow_name, ctx)


class ProcessSpawnTrigger(BaseTrigger):
    """Watches cgroup events for process spawns.

    Skeleton: logs that it would watch the configured cgroup pattern.
    Production would use cgroup.events / inotify on the cgroup tree.
    """

    def start(self) -> None:
        pattern = self.trigger_def.config.get("cgroup_pattern", "*")
        logger.info(
            "ProcessSpawnTrigger: would watch cgroup pattern %r "
            "for workflow %s",
            pattern, self.workflow_name,
        )
        self._active = True

    def stop(self) -> None:
        self._active = False
        logger.info(
            "ProcessSpawnTrigger: stopped for workflow %s",
            self.workflow_name,
        )


class CronTrigger(BaseTrigger):
    """Fires on a cron-like schedule.

    Skeleton: starts a background thread that fires at the configured
    interval (seconds). Production would parse real cron expressions.
    """

    def __init__(self, workflow_name: str, trigger_def: TriggerDef,
                 callback: TriggerCallback):
        super().__init__(workflow_name, trigger_def, callback)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        interval = float(self.trigger_def.config.get("interval_seconds", 3600))
        schedule = self.trigger_def.config.get("schedule", "")
        logger.info(
            "CronTrigger: schedule=%r interval=%ss for workflow %s",
            schedule, interval, self.workflow_name,
        )
        self._active = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, args=(interval,),
            daemon=True, name=f"cron-{self.workflow_name}",
        )
        self._thread.start()

    def _run(self, interval: float) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(interval)
            if not self._stop_event.is_set():
                self.fire({"schedule": self.trigger_def.config.get("schedule", "")})

    def stop(self) -> None:
        self._active = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


class DBusSignalTrigger(BaseTrigger):
    """Subscribes to a D-Bus signal.

    Skeleton: logs the signal match rule. Production would call
    bus.add_signal_receiver() with the configured interface/member.
    """

    def start(self) -> None:
        bus_name = self.trigger_def.config.get("bus_name", "")
        interface = self.trigger_def.config.get("interface", "")
        member = self.trigger_def.config.get("member", "")
        logger.info(
            "DBusSignalTrigger: would subscribe to %s.%s on %s "
            "for workflow %s",
            interface, member, bus_name, self.workflow_name,
        )
        self._active = True

    def stop(self) -> None:
        self._active = False
        logger.info(
            "DBusSignalTrigger: stopped for workflow %s",
            self.workflow_name,
        )


class QbusEventTrigger(BaseTrigger):
    """Subscribes to a qbus-admin event.

    Skeleton: logs the event name. Production would connect to the
    qbus event bus and subscribe.
    """

    def start(self) -> None:
        event_name = self.trigger_def.config.get("event", "")
        logger.info(
            "QbusEventTrigger: would subscribe to qbus event %r "
            "for workflow %s",
            event_name, self.workflow_name,
        )
        self._active = True

    def stop(self) -> None:
        self._active = False
        logger.info(
            "QbusEventTrigger: stopped for workflow %s",
            self.workflow_name,
        )


# Map trigger types to their implementation classes.
TRIGGER_CLASSES: dict[TriggerType, type[BaseTrigger]] = {
    TriggerType.PROCESS_SPAWN: ProcessSpawnTrigger,
    TriggerType.CRON: CronTrigger,
    TriggerType.DBUS_SIGNAL: DBusSignalTrigger,
    TriggerType.QBUS_EVENT: QbusEventTrigger,
}


class TriggerRegistry:
    """Registry that creates and manages trigger instances for workflows."""

    def __init__(self) -> None:
        self._triggers: dict[str, BaseTrigger] = {}  # workflow_name -> trigger
        self._lock = threading.Lock()

    def register(self, workflow: WorkflowDef,
                 callback: TriggerCallback) -> BaseTrigger:
        """Create and start a trigger for the given workflow."""
        trigger_cls = TRIGGER_CLASSES.get(workflow.trigger.type)
        if trigger_cls is None:
            raise ValueError(
                f"no trigger implementation for type "
                f"{workflow.trigger.type.value!r}"
            )
        trigger = trigger_cls(workflow.name, workflow.trigger, callback)
        with self._lock:
            # Stop any existing trigger for this workflow.
            old = self._triggers.pop(workflow.name, None)
            if old is not None:
                old.stop()
            self._triggers[workflow.name] = trigger
        trigger.start()
        return trigger

    def unregister(self, workflow_name: str) -> None:
        """Stop and remove the trigger for the named workflow."""
        with self._lock:
            trigger = self._triggers.pop(workflow_name, None)
        if trigger is not None:
            trigger.stop()

    def unregister_all(self) -> None:
        """Stop all triggers."""
        with self._lock:
            triggers = list(self._triggers.values())
            self._triggers.clear()
        for t in triggers:
            t.stop()

    def active_triggers(self) -> dict[str, BaseTrigger]:
        """Return a snapshot of currently registered triggers."""
        with self._lock:
            return dict(self._triggers)
