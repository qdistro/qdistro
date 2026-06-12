"""Broker client abstraction for the TUI.

Production: DBusBrokerClient — dbus-python + GLib mainloop in a thread,
bridged to Textual's event loop via App.call_from_thread.

Tests: FakeBrokerClient — in-memory, synchronous; tests can prod the
state and have signals fire via direct callable invocation. No D-Bus,
no thread, no display.

Contract (both clients): decide_request() is **fire-and-forget**. The
queue row only disappears when the broker emits RequestDecided and the
on_decided callback handles it. The Fake mirrors this via the
`auto_emit_on_decide` flag (default True for ergonomic tests; flip to
False in tests that verify the async contract).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

BUS_NAME = "org.qdistro.AdminBroker1"
OBJ_PATH = "/org/qdistro/AdminBroker1"

_log = logging.getLogger("qdistro.tui.broker")


# ---------------------------------------------------------------------------
# Bounded broker-error labels for the (shoulder-visible) TUI surface
# ---------------------------------------------------------------------------
#
# Security hardening (todo/security-hardening-carryforward.md §"Broker and
# rules"): the admin UI must render *bounded, known* broker error labels.
# Raw D-Bus exception strings — which can embed paths, argv fragments,
# uids, or internal detail — belong in the logs, NOT in TUI text that a
# bystander can read over the admin's shoulder.
#
# Keys are the broker's typed D-Bus error names (see
# broker/qdistro_admin_broker.py `raise dbus.DBusException(name=BUS_NAME +
# ".<Name>")`). Bare-name keys (the suffix after the last `.`) are also
# accepted so the mapping survives a BUS_NAME change. The label is a short
# human phrase with no embedded request detail.
_BROKER_ERROR_LABELS: dict[str, str] = {
    "AccessDenied":            "Access denied by broker policy",
    "AuditUnavailable":        "Audit log unavailable — decision refused",
    "BadArgument":             "Broker rejected the request (bad argument)",
    "CacheGcFailed":           "Cache cleanup failed",
    "CallerGone":              "The requesting process is no longer present",
    "CallerIdentityMismatch":  "Caller identity check failed",
    "Denied":                  "Request denied by broker",
    "Internal":                "Broker internal error",
    "InvalidArgument":         "Broker rejected the request (invalid argument)",
    "PrintAuditError":         "Print audit failed",
    "RateLimited":             "Too many requests — rate limited",
    "RelayFailed":             "Relay to target failed",
    "RulesEngineRefused":      "Rules engine refused this rule",
    "ScopeNotPermitted":       "Selected scope is not permitted for this request",
    "SiloManagerUnreachable":  "Silo manager unreachable",
    "SiloNotActive":           "Target silo is not active",
    "SnapperUnavailable":      "Snapshot service unavailable",
    "SnapshotFailed":          "Snapshot failed",
    "TargetNotReady":          "Target application is not ready",
}

# Bus-level errors that the retry layer surfaces when the broker is down.
_BUS_ERROR_LABELS: dict[str, str] = {
    "org.freedesktop.DBus.Error.ServiceUnknown": "Broker not running",
    "org.freedesktop.DBus.Error.NameHasNoOwner": "Broker not running",
    "org.freedesktop.DBus.Error.NoReply":        "Broker did not respond",
    "org.freedesktop.DBus.Error.AccessDenied":   "Not authorized to talk to the broker",
}

# Fallback label when an exception carries no recognizable D-Bus error
# name. Deliberately generic — no `str(exc)` leakage.
_GENERIC_BROKER_ERROR_LABEL = "Broker error"


def _dbus_error_name(exc: BaseException) -> str | None:
    """Best-effort extraction of a D-Bus error name from an exception.

    dbus-python's DBusException exposes `get_dbus_name()`. We avoid
    importing dbus here (tests + the fake client run without it) and
    duck-type instead.
    """
    getter = getattr(exc, "get_dbus_name", None)
    if callable(getter):
        try:
            name = getter()
        except Exception:  # noqa: BLE001
            return None
        return str(name) if name else None
    return None


def broker_error_label(exc: BaseException) -> str:
    """Map a broker exception to a bounded, shoulder-safe label.

    Returns a short known phrase for recognized D-Bus error names and a
    generic fallback otherwise. NEVER includes the raw exception text;
    callers are responsible for logging the raw detail separately (see
    `log_broker_error`). Pure function — no textual / dbus import — so it
    is unit-testable on a host without those deps.
    """
    name = _dbus_error_name(exc)
    if name is not None:
        if name in _BUS_ERROR_LABELS:
            return _BUS_ERROR_LABELS[name]
        # Match either the fully-qualified name or its bare suffix so a
        # BUS_NAME prefix change doesn't silently fall through to generic.
        suffix = name.rsplit(".", 1)[-1]
        if name in _BROKER_ERROR_LABELS:
            return _BROKER_ERROR_LABELS[name]
        if suffix in _BROKER_ERROR_LABELS:
            return _BROKER_ERROR_LABELS[suffix]
    return _GENERIC_BROKER_ERROR_LABEL


def log_broker_error(context: str, exc: BaseException) -> None:
    """Log the raw broker exception detail (name + full text) so it lands
    in the journal/logs, never on the shoulder-visible TUI surface."""
    name = _dbus_error_name(exc) or type(exc).__name__
    _log.warning("broker error during %s: name=%s detail=%s",
                 context, name, exc)


@dataclass
class Request:
    """One pending broker request, mirrored client-side."""
    id: int
    uid: int
    pid: int
    exe: str
    action: str
    details: dict[str, str] = field(default_factory=dict)


# Callback signatures used to bridge broker signals -> Textual app
PendingCallback = Callable[[int], None]
DecidedCallback = Callable[[int, str], None]


class BrokerClient(Protocol):
    """Minimal interface the TUI needs."""

    def get_pending(self) -> list[Request]: ...

    def decide_request(self, rid: int, decision: str, scope: str) -> None: ...

    def save_rule(self, filename: str, yaml_body: str) -> str: ...

    def start(self, on_pending: PendingCallback,
              on_decided: DecidedCallback) -> None: ...

    def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# Fake (test) implementation
# ---------------------------------------------------------------------------

class FakeBrokerClient:
    """In-memory broker for tests.

    Default `auto_emit_on_decide=True` mimics a fast happy-path: decide
    immediately removes from pending and fires on_decided. Set False to
    exercise the real broker's fire-and-forget contract — the test must
    then call `emit_decided(rid, decision)` explicitly.
    """

    def __init__(self, pending: list[Request] | None = None,
                 *, auto_emit_on_decide: bool = True):
        self._pending: dict[int, Request] = {r.id: r for r in (pending or [])}
        self._auto_emit = auto_emit_on_decide
        self.decided: list[tuple[int, str, str]] = []
        self._on_pending: PendingCallback | None = None
        self._on_decided: DecidedCallback | None = None
        self._started = False

    # -- BrokerClient protocol --
    def get_pending(self) -> list[Request]:
        return list(self._pending.values())

    def decide_request(self, rid: int, decision: str, scope: str) -> None:
        # Fire-and-forget: record the call and return. The real broker
        # commits + emits RequestDecided; we mirror that when auto.
        self.decided.append((rid, decision, scope))
        if self._auto_emit:
            self._pending.pop(rid, None)
            if self._on_decided is not None:
                self._on_decided(rid, decision)

    def save_rule(self, filename: str, yaml_body: str) -> str:
        """Record the save_rule call; return the would-be path."""
        if not hasattr(self, "saved_rules"):
            self.saved_rules = []
        self.saved_rules.append((filename, yaml_body))
        return f"/etc/qdistro/rules.d/{filename}"

    def start(self, on_pending: PendingCallback,
              on_decided: DecidedCallback) -> None:
        self._on_pending = on_pending
        self._on_decided = on_decided
        self._started = True

    def stop(self) -> None:
        self._started = False

    # -- Test affordances --
    def emit_pending(self, req: Request) -> None:
        """Simulate a new RequestPending signal arriving from the broker."""
        self._pending[req.id] = req
        if self._on_pending is not None:
            self._on_pending(req.id)

    def emit_decided(self, rid: int, decision: str) -> None:
        """Simulate the broker's RequestDecided signal arriving."""
        self._pending.pop(rid, None)
        if self._on_decided is not None:
            self._on_decided(rid, decision)


# ---------------------------------------------------------------------------
# Real D-Bus implementation
# ---------------------------------------------------------------------------

class DBusBrokerClient:
    """dbus-python + GLib mainloop in a worker thread.

    The dbus connection is established **in `start()`**, not __init__,
    so a missing/unreachable broker surfaces as an in-app error rather
    than a Python traceback before Textual is up.

    Signals (RequestPending, RequestDecided) fire on the GLib thread; the
    constructor takes an `app` argument so we can route them back into
    Textual's event loop via app.call_from_thread.
    """

    def __init__(self, app=None):
        self._app = app  # Textual App for call_from_thread
        self._on_pending: PendingCallback | None = None
        self._on_decided: DecidedCallback | None = None
        self._started = False

        # Lazy attributes — populated in start()
        self._dbus = None
        self._GLib = None
        self._bus = None
        self._proxy = None
        self._loop = None
        self._thread: threading.Thread | None = None

    def _reconnect(self) -> None:
        """Rebuild the proxy after the broker restarted.

        dbus-python's ProxyObject holds state the bus daemon no longer
        honors once the old owner of BUS_NAME is gone. A fresh
        `bus.get_object(...)` picks up the new owner. Called from the
        method wrappers on DBusException so one retry recovers the
        TUI from a transient broker restart without the user needing
        to quit+relaunch.
        """
        if self._bus is None:
            return
        self._proxy = self._bus.get_object(BUS_NAME, OBJ_PATH)

    # D-Bus error names that indicate the broker restarted or the bus
    # lost the name owner — retrying with a fresh proxy is appropriate.
    # Application-level errors (broker raised a typed DBusException)
    # must NOT be retried: they reflect real business logic rejections.
    _RETRIABLE_DBUS_ERRORS = frozenset((
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.NoReply",
    ))

    def _call(self, name: str, *args):
        if self._proxy is None:
            raise RuntimeError(f"DBusBrokerClient.{name} called before start()")
        import dbus  # already imported in start() but keep local ref
        method = getattr(self._proxy, name)
        try:
            return method(*args, dbus_interface=BUS_NAME)
        except dbus.DBusException as exc:
            # Only retry on bus-level / owner-loss errors that signal a
            # broker restart. Application errors propagate immediately.
            if exc.get_dbus_name() not in self._RETRIABLE_DBUS_ERRORS:
                raise
            self._reconnect()
            method = getattr(self._proxy, name)
            return method(*args, dbus_interface=BUS_NAME)

    # -- BrokerClient protocol --
    def get_pending(self) -> list[Request]:
        raw = self._call("GetPending")
        out: list[Request] = []
        for r in raw:
            out.append(Request(
                id=int(r["id"]),
                uid=int(r["uid"]),
                pid=int(r["pid"]),
                exe=str(r["exe"]),
                action=str(r["action"]),
                details={str(k): str(v) for k, v in dict(r["details"]).items()},
            ))
        return out

    def decide_request(self, rid: int, decision: str, scope: str) -> None:
        self._call("DecideRequest", int(rid), str(decision), str(scope))

    def save_rule(self, filename: str, yaml_body: str) -> str:
        """SaveRule on the broker. Returns the absolute path of the
        saved YAML."""
        return str(self._call("SaveRule", str(filename), str(yaml_body)))

    def start(self, on_pending: PendingCallback,
              on_decided: DecidedCallback) -> None:
        # Take callbacks BEFORE wiring the bus so signals can never
        # arrive into a half-initialized state.
        self._on_pending = on_pending
        self._on_decided = on_decided

        # Lazy imports so tests can run without dbus installed.
        import dbus
        import dbus.mainloop.glib
        from gi.repository import GLib

        self._dbus = dbus
        self._GLib = GLib
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus = dbus.SystemBus()
        self._proxy = self._bus.get_object(BUS_NAME, OBJ_PATH)

        # Subscribe to broker signals
        self._bus.add_signal_receiver(
            self._dispatch_pending,
            signal_name="RequestPending",
            dbus_interface=BUS_NAME,
        )
        self._bus.add_signal_receiver(
            self._dispatch_decided,
            signal_name="RequestDecided",
            dbus_interface=BUS_NAME,
        )

        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(
            target=self._loop.run, name="dbus-glib", daemon=True)
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        if self._loop is not None:
            self._loop.quit()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                # Daemon thread, will die at interpreter shutdown; flag it
                print("[DBusBrokerClient] WARNING: GLib thread did not stop "
                      "within 2s; leaking until process exit", flush=True)
        self._started = False

    # -- Signal handlers (run on GLib thread; must bridge back) --
    def _dispatch_pending(self, rid):
        cb = self._on_pending
        if cb is None:
            return
        if self._app is not None:
            self._app.call_from_thread(cb, int(rid))
        else:
            cb(int(rid))

    def _dispatch_decided(self, rid, decision):
        cb = self._on_decided
        if cb is None:
            return
        if self._app is not None:
            self._app.call_from_thread(cb, int(rid), str(decision))
        else:
            cb(int(rid), str(decision))
