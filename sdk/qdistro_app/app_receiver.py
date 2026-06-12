"""qdistro_app.app_receiver — high-level helper that wires an app
into the ``org.qdistro.App1`` contract with one call.

Apps that want to participate in the P03 launcher / send-to flow
import ``register_app`` from here rather than instantiating
:class:`qdistro_app.AppReceiver` directly. The helper:

- ensures the GLib mainloop is the default for dbus-python so the
  receiver actually wakes up on incoming method calls (Qt's own
  event loop coexists — they wake on fds via the qt-platform
  plugin's internal integration).
- builds the canonical service name ``org.qdistro.<Name>.uid<NNNN>``
  unless the caller hands in a fully-qualified one. The ``uid``
  suffix is what UserRelay.ListLocalReceivers uses to disambiguate
  one user's instances from another's in the broker's ListReceivers
  return.
- swallows the "no session bus available" / "bus name already
  claimed" failure cases into a structured log line + ``None``
  return, so a misconfigured launcher unit doesn't crash an
  otherwise-working app. The caller is expected to treat the return
  as opaque — store it for the lifetime of the app to keep the bus
  name claimed; let it be GC'd to release the registration.

Senders use :func:`send_to_menu_targets` to build a "Send To…" menu
without needing to know the broker's bus path. The helper filters
the broker's ``ListReceivers`` output down to peers (excludes self),
groups by silo, and returns the same row shape qdshell's QML uses.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable

from . import (
    APP1_IFACE,
    APP1_OBJ_PATH,
    DEFAULT_KIND,
    AppReceiver,
    list_receivers,
    send_to,
)

__all__ = [
    "APP1_IFACE",
    "APP1_OBJ_PATH",
    "DEFAULT_KIND",
    "register_app",
    "send_to_menu_targets",
    "is_session_bus_available",
]


def _canonical_service_name(name: str) -> str:
    """Return ``org.qdistro.<Name>.uid<NNNN>`` from a short app name.

    Accepts a bare app name (``"QTerminator"``) or a partial path
    (``"org.qdistro.QTerminator"``) or a fully qualified name; the
    last form passes through unchanged.
    """
    s = str(name)
    uid = os.geteuid()
    if s.startswith("org.qdistro.") and ".uid" in s:
        return s
    if s.startswith("org.qdistro."):
        return f"{s}.uid{uid}"
    return f"org.qdistro.{s}.uid{uid}"


def is_session_bus_available() -> bool:
    """Cheap check: does this process have a session bus address?

    Returns ``False`` when ``$DBUS_SESSION_BUS_ADDRESS`` is unset AND
    the well-known socket ``$XDG_RUNTIME_DIR/bus`` is missing. Used by
    register_app to skip wiring with a clear log line rather than
    blowing up on import-of-dbus-python in environments where the
    session bus genuinely isn't there (CI, --no-display test runs).
    """
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").strip():
        return True
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime and os.path.exists(os.path.join(runtime, "bus")):
        return True
    return False


def register_app(
    name: str,
    *,
    on_receive: Callable[[str, str], None] | None = None,
    friendly_name: str | None = None,
    silo: str | None = None,
    supported_kinds: Iterable[str] | None = None,
    install_glib_mainloop: bool = True,
    log: Callable[[str], None] | None = None,
) -> AppReceiver | None:
    """Claim ``org.qdistro.<name>.uid<NNNN>`` on the session bus and
    return the registered :class:`AppReceiver`.

    ``on_receive`` is called once per delivery (both ``Receive`` and
    ``ReceivePayload`` entry points). Defaults to a no-op so apps that
    only care about "I'm registered so I show up in PodApps" can pass
    nothing.

    ``install_glib_mainloop`` is True by default — dbus-python's GLib
    loop dispatches incoming methods, and Qt's loop coexists. Set
    False when the host process already installed it (re-installation
    is idempotent but the log line is noisy).

    Returns ``None`` and logs a single structured line when the bus
    isn't reachable or the name can't be claimed. Calling code must
    handle ``None`` (i.e. "the app still runs, just isn't on the
    bus") rather than treating registration as required.
    """
    _log = log or _stderr_log
    if not is_session_bus_available():
        _log(f"qdistro_app: no session bus for {name!r} "
             f"(DBUS_SESSION_BUS_ADDRESS unset, no $XDG_RUNTIME_DIR/bus); "
             f"skipping App1 registration")
        return None
    try:
        import dbus
        import dbus.service
    except ImportError as e:
        _log(f"qdistro_app: dbus-python missing ({e}); "
             f"App1 registration skipped for {name!r}")
        return None
    if install_glib_mainloop:
        try:
            import dbus.mainloop.glib
            dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        except ImportError as e:
            # PyGObject is the usual missing piece on minimal images;
            # fall through and let dbus.SessionBus() pick whatever
            # mainloop is current. The receiver still claims the name;
            # it just won't dispatch method calls until something else
            # spins a loop.
            _log(f"qdistro_app: dbus.mainloop.glib unavailable ({e}); "
                 f"App1 receiver will register without GLib dispatch")
    service_name = _canonical_service_name(name)
    callback = on_receive or (lambda kind, payload: None)
    try:
        return AppReceiver(
            service_name=service_name,
            on_receive=callback,
            friendly_name=friendly_name,
            silo=silo,
            supported_kinds=supported_kinds,
        )
    except Exception as e:  # noqa: BLE001
        _log(f"qdistro_app: failed to claim {service_name!r}: {e}")
        return None


def send_to_menu_targets(*, self_service: str | None = None,
                         kind: str | None = None) -> list[dict]:
    """Build the Send-To menu rows for the current process.

    Asks the broker for every registered ``org.qdistro.App1`` receiver,
    excludes ``self_service`` (so an app doesn't list itself), and —
    when ``kind`` is given — drops rows whose receiver returns
    ``CanReceive(kind) == False``. The kind probe is best-effort:
    a receiver that doesn't implement ``CanReceive`` (or whose probe
    raises) stays in the list so the user can still try.

    Each returned dict has keys:
      - ``uid`` (int)
      - ``service`` (str, the bus name to pass to ``send_to``)
      - ``name`` (str, friendly label)
      - ``silo`` (str, best-effort from GetSilo or "")
    """
    try:
        rows = list_receivers()
    except Exception as e:  # noqa: BLE001
        _stderr_log(f"qdistro_app.send_to_menu_targets: ListReceivers "
                    f"failed: {e}")
        return []
    out: list[dict] = []
    try:
        import dbus
    except ImportError:
        dbus = None  # type: ignore[assignment]
    for uid, service, friendly in rows:
        if self_service and service == self_service:
            continue
        silo_label = ""
        accept = True
        if dbus is not None:
            silo_label = _probe_silo(uid, service)
            if kind is not None:
                accept = _probe_can_receive(uid, service, kind)
        if not accept:
            continue
        out.append({
            "uid": uid,
            "service": service,
            "name": friendly,
            "silo": silo_label,
        })
    return out


def _probe_silo(uid: int, service: str) -> str:
    """Best-effort GetSilo probe. Returns ``""`` on any failure so the
    Send-To UI degrades gracefully rather than dropping the row.

    Crosses uid by going through the broker's ListReceivers shape —
    we only have direct session-bus access to our own uid. For peers
    we fall through to ``""`` and let qdshell show no badge. Same-uid
    receivers we can probe directly.
    """
    try:
        import dbus
    except ImportError:
        return ""
    if int(uid) != os.geteuid():
        # Cross-uid GetSilo isn't reachable from the SDK side without
        # bouncing through UserRelay; the qdshell PodApps panel does
        # that lookup itself via the SessionManager registry, so the
        # SDK can stay simple here.
        return ""
    try:
        bus = dbus.SessionBus()
        obj = bus.get_object(service, APP1_OBJ_PATH)
        return str(obj.GetSilo(dbus_interface=APP1_IFACE, timeout=1.0))
    except Exception:  # noqa: BLE001
        return ""


def _probe_can_receive(uid: int, service: str, kind: str) -> bool:
    try:
        import dbus
    except ImportError:
        return True
    if int(uid) != os.geteuid():
        # Same reasoning as _probe_silo — cross-uid probe isn't
        # available; assume the receiver will accept rather than
        # silently dropping the menu entry.
        return True
    try:
        bus = dbus.SessionBus()
        obj = bus.get_object(service, APP1_OBJ_PATH)
        return bool(obj.CanReceive(str(kind),
                                    dbus_interface=APP1_IFACE, timeout=1.0))
    except Exception:  # noqa: BLE001
        return True


def _stderr_log(msg: str) -> None:
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


# Backwards-compat: tests / docs may import these names from
# ``qdistro_app.app_receiver`` directly.
__all__ += ["AppReceiver", "list_receivers", "send_to"]
