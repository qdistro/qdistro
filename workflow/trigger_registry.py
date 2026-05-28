"""Trigger registry for the workflow engine.

Maps trigger types to watcher implementations. Each watcher
monitors a system event source (cgroup, cron, D-Bus signal, qbus)
and calls back into the engine when its condition fires.

Production watchers:
  - ProcessSpawnTrigger watches a cgroup v2 subtree (polling
    ``cgroup.procs``) for newly-spawned pids.
  - CronTrigger parses real 5-field cron expressions, with an
    ``interval_seconds`` fallback and a minimum-interval clamp.
  - DBusSignalTrigger subscribes to a real D-Bus signal via
    ``bus.add_signal_receiver`` (runs a GLib main loop unless the
    embedding process already runs one).
  - QbusEventTrigger is a DBusSignalTrigger pre-aimed at the
    qbus/admin broker's signal surface (e.g. RulesReloaded).

Failure isolation: a watcher thread that hits a bad event source logs
and keeps going (or stops itself) without crashing the engine or other
watchers. dbus/GLib are imported lazily so the module loads in
environments without them (the cgroup/cron watchers don't need them).
"""
from __future__ import annotations

import fnmatch
import glob
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable

from workflow_schema import (  # type: ignore[import-not-found]
    TriggerDef,
    TriggerType,
    WorkflowDef,
)
from cron_parser import CronExpr, CronParseError  # type: ignore[import-not-found]

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
        """Called when the trigger condition is met.

        The engine callback is wrapped so a failure starting one run
        never tears down the watcher (which would silently stop all
        future firings).
        """
        ctx = dict(context or {})
        ctx["trigger_type"] = self.trigger_def.type.value
        ctx["fired_at"] = time.time()
        logger.info(
            "trigger fired: workflow=%s type=%s",
            self.workflow_name, self.trigger_def.type.value,
        )
        try:
            self._callback(self.workflow_name, ctx)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "trigger callback failed for workflow %s: %r",
                self.workflow_name, e,
            )


class ProcessSpawnTrigger(BaseTrigger):
    """Fires when a process is spawned into a matching cgroup v2 subtree.

    Polls ``cgroup.procs`` across cgroup directories matching
    ``cgroup_pattern`` (a glob relative to ``cgroup_root``). On each
    scan, any pid not seen on the previous scan for that cgroup fires
    the trigger with ``{"cgroup": <rel-path>, "pid": <pid>}``.

    Polling (rather than inotify) keeps the watcher stdlib-only and
    trivially fail-safe: a bad/disappearing cgroup tree just yields an
    empty scan instead of an exception storm. The poll interval is
    clamped to a floor so a misconfiguration can't spin the CPU.

    Pre-existing pids at start() are baselined (recorded, not fired)
    unless ``fire_on_existing`` is set, so the trigger reacts to
    *new* spawns rather than whatever happened to be running.
    """

    _MIN_POLL_S = 0.25
    # Hard cap on tracked pids to bound memory if something pathological
    # churns a huge number of short-lived processes.
    _MAX_TRACKED_PIDS = 100_000

    def __init__(self, workflow_name: str, trigger_def: TriggerDef,
                 callback: TriggerCallback):
        super().__init__(workflow_name, trigger_def, callback)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # cgroup rel-path -> set of pids seen on the last scan.
        self._seen: dict[str, set[int]] = {}
        self._baselined = False

        cfg = trigger_def.config
        self._cgroup_root = str(cfg.get("cgroup_root", "/sys/fs/cgroup"))
        self._pattern = str(cfg.get("cgroup_pattern", "*"))
        raw_poll = float(cfg.get("poll_interval", 1.0))
        self._poll = max(raw_poll, self._MIN_POLL_S)
        self._fire_on_existing = bool(cfg.get("fire_on_existing", False))

    def start(self) -> None:
        logger.info(
            "ProcessSpawnTrigger: watching cgroup pattern %r under %s "
            "(poll=%ss) for workflow %s",
            self._pattern, self._cgroup_root, self._poll, self.workflow_name,
        )
        self._active = True
        self._stop_event.clear()
        self._baselined = False
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"procspawn-{self.workflow_name}",
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._scan_once()
            except Exception as e:  # noqa: BLE001
                # Never let a transient cgroup-tree error kill the watcher.
                logger.warning(
                    "ProcessSpawnTrigger scan error for workflow %s: %r",
                    self.workflow_name, e,
                )
            self._stop_event.wait(self._poll)

    def _matching_cgroups(self) -> list[str]:
        """Return absolute paths of cgroup dirs matching the pattern."""
        base = os.path.join(self._cgroup_root, self._pattern)
        return [p for p in glob.glob(base) if os.path.isdir(p)]

    @staticmethod
    def _read_procs(cgroup_dir: str) -> set[int]:
        procs_file = os.path.join(cgroup_dir, "cgroup.procs")
        pids: set[int] = set()
        try:
            with open(procs_file, "r", encoding="ascii") as f:
                for line in f:
                    line = line.strip()
                    if line.isdigit():
                        pids.add(int(line))
        except (OSError, ValueError):
            # cgroup vanished mid-scan, or unreadable — treat as empty.
            return set()
        return pids

    def _scan_once(self) -> list[dict[str, Any]]:
        """Run a single scan. Fires for new pids. Returns fired contexts.

        Exposed for tests so they can drive scans deterministically
        without waiting on the poll thread.
        """
        fired: list[dict[str, Any]] = []
        current_dirs = self._matching_cgroups()
        live_rel_paths: set[str] = set()
        baseline = not self._baselined and not self._fire_on_existing

        for cgroup_dir in current_dirs:
            rel = os.path.relpath(cgroup_dir, self._cgroup_root)
            live_rel_paths.add(rel)
            pids = self._read_procs(cgroup_dir)
            previous = self._seen.get(rel, set())
            new_pids = pids - previous
            self._seen[rel] = pids
            if baseline:
                continue
            for pid in sorted(new_pids):
                ctx = {"cgroup": rel, "pid": pid,
                       "cgroup_path": cgroup_dir}
                fired.append(ctx)
                self.fire(ctx)

        # Drop bookkeeping for cgroups that disappeared, and bound memory.
        for rel in list(self._seen.keys()):
            if rel not in live_rel_paths:
                del self._seen[rel]
        if sum(len(s) for s in self._seen.values()) > self._MAX_TRACKED_PIDS:
            logger.warning(
                "ProcessSpawnTrigger: tracked-pid cap exceeded for "
                "workflow %s; resetting baseline", self.workflow_name,
            )
            self._seen.clear()

        self._baselined = True
        return fired

    def stop(self) -> None:
        self._active = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("ProcessSpawnTrigger: stopped for workflow %s",
                    self.workflow_name)


class CronTrigger(BaseTrigger):
    """Fires on a cron schedule, or on a fixed interval as a fallback.

    If ``schedule`` is a valid 5-field cron expression it drives the
    timing; otherwise the watcher falls back to ``interval_seconds``.
    Either way the inter-fire delay is clamped to ``_MIN_INTERVAL_S`` so
    a zero/negative/sub-second value can't spin the thread.
    """

    _MIN_INTERVAL_S = 1.0

    def __init__(self, workflow_name: str, trigger_def: TriggerDef,
                 callback: TriggerCallback):
        super().__init__(workflow_name, trigger_def, callback)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cron: CronExpr | None = None

        cfg = trigger_def.config
        schedule = str(cfg.get("schedule", "")).strip()
        if schedule:
            try:
                self._cron = CronExpr(schedule)
                # Probe satisfiability once: an expression like
                # "0 0 31 2 *" (Feb 31) parses but never matches, and a
                # full 4-year minute search per loop would burn CPU
                # forever. Catch it at construction and fall back.
                self._cron.next_after(datetime.now())
            except CronParseError as e:
                logger.warning(
                    "CronTrigger: unusable schedule %r for workflow %s "
                    "(%s); falling back to interval", schedule,
                    workflow_name, e,
                )
                self._cron = None
        raw_interval = float(cfg.get("interval_seconds", 3600))
        self._interval = max(raw_interval, self._MIN_INTERVAL_S)
        if raw_interval < self._MIN_INTERVAL_S:
            logger.warning(
                "CronTrigger: interval_seconds=%s below minimum %s for "
                "workflow %s; clamped", raw_interval,
                self._MIN_INTERVAL_S, workflow_name,
            )
        self._schedule = schedule

    def next_delay(self, now: float | None = None) -> float:
        """Seconds until the next fire. Cron-driven if a schedule was
        parsed, else the clamped interval. Exposed for tests."""
        if self._cron is None:
            return self._interval
        now = time.time() if now is None else now
        nxt = self._cron.next_after(datetime.fromtimestamp(now))
        delay = nxt.timestamp() - now
        return max(delay, self._MIN_INTERVAL_S)

    def start(self) -> None:
        logger.info(
            "CronTrigger: schedule=%r interval=%ss for workflow %s",
            self._schedule, self._interval, self.workflow_name,
        )
        self._active = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"cron-{self.workflow_name}",
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                delay = self.next_delay()
            except Exception as e:  # noqa: BLE001
                # Disable cron permanently so a degenerate expression
                # can't re-trigger the expensive search every loop.
                logger.warning(
                    "CronTrigger: next_delay failed for workflow %s: %r; "
                    "disabling cron and using interval", self.workflow_name, e,
                )
                self._cron = None
                delay = self._interval
            if self._stop_event.wait(delay):
                return
            self.fire({"schedule": self._schedule})

    def stop(self) -> None:
        self._active = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


class DBusSignalTrigger(BaseTrigger):
    """Subscribes to a D-Bus signal and fires with the signal args.

    Config keys: ``bus`` ("system"|"session", default "system"),
    ``bus_name``, ``interface``, ``member`` (omit ``member`` to match
    all members of the interface), ``object_path``.

    A ``bus`` object may be injected (for tests) — anything exposing
    ``add_signal_receiver``/``remove_signal_receiver``. dbus + GLib are
    imported lazily.

    Signal delivery needs a running GLib main loop. By default the
    trigger runs its own loop in a daemon thread; pass
    ``run_own_loop=False`` (e.g. when embedded in the broker, which
    already runs the process main loop) to reuse the existing loop.
    """

    def __init__(self, workflow_name: str, trigger_def: TriggerDef,
                 callback: TriggerCallback, *,
                 bus: Any | None = None, run_own_loop: bool = True):
        super().__init__(workflow_name, trigger_def, callback)
        self._bus = bus
        self._run_own_loop = run_own_loop
        self._loop: Any | None = None
        self._loop_thread: threading.Thread | None = None
        self._match = None

        cfg = self._resolve_config(trigger_def.config)
        self._bus_kind = str(cfg.get("bus", "system"))
        self._bus_name = cfg.get("bus_name") or None
        self._interface = cfg.get("interface") or None
        self._member = cfg.get("member") or None
        self._object_path = cfg.get("object_path") or None

    def _resolve_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Hook for subclasses to remap config keys. Base passes through."""
        return dict(cfg)

    def _on_signal(self, *args: Any, **_kwargs: Any) -> None:
        # A late delivery after stop()/unregister must not start a run.
        if not self._active:
            return
        self.fire({
            "args": [self._coerce(a) for a in args],
            "interface": self._interface or "",
            "member": self._member or "",
        })

    @staticmethod
    def _coerce(value: Any) -> Any:
        """Best-effort conversion of dbus types to plain Python."""
        try:
            if isinstance(value, (list, tuple)):
                return [DBusSignalTrigger._coerce(v) for v in value]
            if isinstance(value, dict):
                return {DBusSignalTrigger._coerce(k):
                        DBusSignalTrigger._coerce(v)
                        for k, v in value.items()}
            if isinstance(value, bytes):
                return value
            if isinstance(value, bool):
                return bool(value)
            if isinstance(value, int):
                return int(value)
            if isinstance(value, float):
                return float(value)
            return str(value)
        except Exception:  # noqa: BLE001
            return repr(value)

    def _get_bus(self) -> Any:
        if self._bus is not None:
            return self._bus
        import dbus  # lazy
        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)
        if self._bus_kind == "session":
            return dbus.SessionBus()
        return dbus.SystemBus()

    def start(self) -> None:
        try:
            bus = self._get_bus()
            kwargs: dict[str, Any] = {}
            if self._member:
                kwargs["signal_name"] = self._member
            if self._interface:
                kwargs["dbus_interface"] = self._interface
            if self._bus_name:
                kwargs["bus_name"] = self._bus_name
            if self._object_path:
                kwargs["path"] = self._object_path
            self._match = bus.add_signal_receiver(self._on_signal, **kwargs)
            self._bus = bus
        except Exception as e:  # noqa: BLE001
            logger.error(
                "DBusSignalTrigger: failed to subscribe %s.%s on %s for "
                "workflow %s: %r", self._interface, self._member,
                self._bus_name, self.workflow_name, e,
            )
            self._active = False
            return

        logger.info(
            "DBusSignalTrigger: subscribed to %s.%s on %s for workflow %s",
            self._interface, self._member, self._bus_name, self.workflow_name,
        )
        self._active = True
        if self._run_own_loop:
            self._start_loop()

    def _start_loop(self) -> None:
        try:
            from gi.repository import GLib  # lazy
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "DBusSignalTrigger: GLib unavailable, signals will only be "
                "delivered if the host runs a main loop (%r)", e,
            )
            return
        self._loop = GLib.MainLoop()
        self._loop_thread = threading.Thread(
            target=self._loop.run, daemon=True,
            name=f"dbus-{self.workflow_name}",
        )
        self._loop_thread.start()

    def stop(self) -> None:
        self._active = False
        try:
            if self._match is not None:
                # dbus-python's SignalMatch.remove() passes the original
                # member/interface/path filters back to the connection,
                # which is required to actually drop a *filtered*
                # subscription. A bare remove_signal_receiver(match) only
                # clears the unfiltered bucket and silently leaks the rest.
                remover = getattr(self._match, "remove", None)
                if callable(remover):
                    remover()
                elif self._bus is not None:
                    self._bus.remove_signal_receiver(self._match)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "DBusSignalTrigger: remove_signal_receiver failed for "
                "workflow %s: %r", self.workflow_name, e,
            )
        self._match = None
        if self._loop is not None:
            try:
                self._loop.quit()
            except Exception:  # noqa: BLE001
                pass
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=2.0)
            self._loop_thread = None
        self._loop = None
        logger.info("DBusSignalTrigger: stopped for workflow %s",
                    self.workflow_name)


# Default qbus/admin broker endpoint the QbusEventTrigger aims at.
_QBUS_DEFAULT_BUS_NAME = "org.qdistro.AdminBroker1"
_QBUS_DEFAULT_INTERFACE = "org.qdistro.AdminBroker1"
_QBUS_DEFAULT_OBJ_PATH = "/org/qdistro/AdminBroker1"


class QbusEventTrigger(DBusSignalTrigger):
    """Subscribes to a qbus/admin-broker event signal.

    Thin specialisation of DBusSignalTrigger pre-aimed at the broker's
    signal surface. Config: ``event`` (the signal member, default
    ``RulesReloaded``); ``bus_name``/``interface``/``object_path``
    override the broker defaults if needed.
    """

    def _resolve_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        event = cfg.get("event") or cfg.get("member") or "RulesReloaded"
        return {
            "bus": cfg.get("bus", "system"),
            "bus_name": cfg.get("bus_name", _QBUS_DEFAULT_BUS_NAME),
            "interface": cfg.get("interface", _QBUS_DEFAULT_INTERFACE),
            "member": event,
            "object_path": cfg.get("object_path", _QBUS_DEFAULT_OBJ_PATH),
        }


# Map trigger types to their implementation classes.
TRIGGER_CLASSES: dict[TriggerType, type[BaseTrigger]] = {
    TriggerType.PROCESS_SPAWN: ProcessSpawnTrigger,
    TriggerType.CRON: CronTrigger,
    TriggerType.DBUS_SIGNAL: DBusSignalTrigger,
    TriggerType.QBUS_EVENT: QbusEventTrigger,
}


class TriggerRegistry:
    """Registry that creates and manages trigger instances for workflows."""

    def __init__(self, dbus_run_own_loop: bool = True,
                 dbus_bus: Any | None = None) -> None:
        self._triggers: dict[str, BaseTrigger] = {}  # workflow_name -> trigger
        self._lock = threading.Lock()
        # When embedded in a process that already runs a GLib main loop
        # (the broker), D-Bus triggers must reuse it rather than spin
        # their own — two loops on the default context can't both run.
        self._dbus_run_own_loop = dbus_run_own_loop
        self._dbus_bus = dbus_bus

    def register(self, workflow: WorkflowDef,
                 callback: TriggerCallback) -> BaseTrigger:
        """Create and start a trigger for the given workflow."""
        trigger_cls = TRIGGER_CLASSES.get(workflow.trigger.type)
        if trigger_cls is None:
            raise ValueError(
                f"no trigger implementation for type "
                f"{workflow.trigger.type.value!r}"
            )
        if issubclass(trigger_cls, DBusSignalTrigger):
            trigger = trigger_cls(
                workflow.name, workflow.trigger, callback,
                bus=self._dbus_bus, run_own_loop=self._dbus_run_own_loop)
        else:
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
