"""qdistro_app — Python SDK for user apps to interact with the admin
broker and with peer user silos.

Phase 1 surface was a single `request()` call. Phase 3 adds a minimal
receiver mixin for the `com.qdistro.App1` cross-user contract, plus
helpers for enumerating peer receivers and sending to one. The full
App1 surface (SetReadonly / SetViewer / SetIsolationTier / HandleHandoff)
is deferred to Phase 4; Phase 3 only defines Receive(kind, payload).
"""
from __future__ import annotations

from typing import Callable

import dbus
import dbus.service

_BUS_NAME = "com.qdistro.AdminBroker1"
_OBJ_PATH = "/com/qdistro/AdminBroker1"

APP1_IFACE = "com.qdistro.App1"

DEFAULT_TIMEOUT_S = 600


def request(action: str, details: dict | None = None, *,
            timeout: float = DEFAULT_TIMEOUT_S) -> bool:
    """Request a permission from admin. Blocks until admin decides
    or `timeout` seconds pass.

    timeout is the per-call D-Bus reply timeout on WaitForDecision.
    The default 600s (10 minutes) balances "admin is at lunch" with
    "calling app shouldn't hang forever"; pass a smaller value for
    interactive paths where a stale request is worse than no answer.
    Returns True if approved, False if denied or timed out.
    """
    bus = dbus.SystemBus()
    broker = bus.get_object(_BUS_NAME, _OBJ_PATH)
    rid = int(broker.RequestPermission(str(action), details or {}, dbus_interface=_BUS_NAME))
    allowed = broker.WaitForDecision(rid, dbus_interface=_BUS_NAME, timeout=float(timeout))
    return bool(allowed)


class AppReceiver(dbus.service.Object):
    """Claims a session-bus name and exposes com.qdistro.App1.Receive.

    Phase-3 minimum surface. Subclass and override `on_receive` to
    handle incoming messages; the D-Bus-thread call hops to your
    callback which MUST be thread-safe or marshal onto Qt's main
    thread (`QTimer.singleShot(0, ...)`) itself.

    Phase 4 will add SetReadonly / SetViewer / SetIsolationTier /
    HandleHandoff. Adding them now would require stubs the stub apps
    can't meaningfully implement, so the surface is deliberately
    incomplete.
    """

    OBJ_PATH = "/com/qdistro/App1"

    def __init__(self, service_name: str,
                 on_receive: Callable[[str, str], None],
                 bus: dbus.Bus | None = None):
        if bus is None:
            bus = dbus.SessionBus()
        # Claim the bus name before registering the object — a name
        # collision here means another instance is already running
        # for this uid under this service name, and silently shadowing
        # it would make debugging painful.
        self._bus_name = dbus.service.BusName(
            service_name, bus, do_not_queue=True)
        super().__init__(bus, self.OBJ_PATH)
        self._service_name = str(service_name)
        self._on_receive = on_receive
        # Phase 4 plugins want a cheap "did it arrive?" for headless
        # tests without rigging an app-specific GetDocument each time.
        # Track the most recent (kind, payload) on the receiver and
        # expose it via GetLastReceived — generic enough to be useful
        # for qterminator (PTY text), qnotebook (markdown text), and
        # any future App1 participant. `Receive` updates it before
        # dispatching to the caller's on_receive callback, so even if
        # on_receive raises, the probe still reports what landed.
        self._last_received: tuple[str, str] | None = None

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def last_received(self) -> tuple[str, str] | None:
        return self._last_received

    @dbus.service.method(APP1_IFACE, in_signature="ss", out_signature="")
    def Receive(self, kind: str, payload: str) -> None:
        k, p = str(kind), str(payload)
        self._last_received = (k, p)
        self._on_receive(k, p)

    @dbus.service.method(APP1_IFACE, in_signature="", out_signature="s")
    def GetLastReceived(self) -> str:
        """Return the most recent payload in `[kind] payload` form, or
        empty string if nothing has been received yet. Formatted to
        match qstub-notepad's GetDocument output so assertions can be
        shared across Phase 3 and Phase 4 scenarios."""
        if self._last_received is None:
            return ""
        k, p = self._last_received
        return f"[{k}] {p}"


def list_receivers() -> list[tuple[int, str, str]]:
    """Return (uid, service_name, friendly_name) for every receiver
    currently registered across all running user sessions.

    Calls the broker's ListReceivers (root-only side-channel that
    can peek into every uid's session bus). No admin approval
    required for enumeration — gate is on RelayMessage.
    """
    bus = dbus.SystemBus()
    broker = bus.get_object(_BUS_NAME, _OBJ_PATH)
    rows = broker.ListReceivers(dbus_interface=_BUS_NAME)
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def send_to(target_uid: int, target_service: str,
            kind: str, payload: str, *,
            timeout: float = DEFAULT_TIMEOUT_S) -> bool:
    """Ask admin to relay a message to `target_service` running as
    `target_uid`. Blocks until admin decides or timeout expires.

    Returns True iff the target's Receive(kind, payload) was invoked
    (admin approved and the relay delivered). DBusException bubbles
    up on deny or relay failure.
    """
    bus = dbus.SystemBus()
    broker = bus.get_object(_BUS_NAME, _OBJ_PATH)
    broker.RelayMessage(
        dbus.Int32(int(target_uid)),
        dbus.String(str(target_service)),
        dbus.String(str(kind)),
        dbus.String(str(payload)),
        dbus_interface=_BUS_NAME,
        timeout=float(timeout),
    )
    return True
