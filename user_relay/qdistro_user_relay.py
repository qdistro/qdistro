#!/usr/bin/env python3
"""qdistro-user-relay — per-user session-bus daemon.

One instance runs as each user with `com.qdistro.UserRelay` on the
session bus. Lets the broker (running as root on the system bus)
reach receivers in that user's session without the broker having
to open session-bus connections as arbitrary uids itself.

Why the indirection? The broker is root; it CAN open any user's
session-bus socket via DAC_OVERRIDE. But the call surface it would
need (arbitrary `com.qdistro.*.Receive` invocations) is wide. With
a relay in between, root only ever calls `UserRelay.Forward(...)`
and `UserRelay.ListLocalReceivers()` — two narrow methods. Easier
to audit, easier to lock down with a session-bus policy later.

Phase-3 scope: Forward() blindly invokes `<service>.Receive(kind,
payload)`. No ACL (the admin approval gate is the broker's job);
no retry; no payload size cap beyond broker's detail sanitiser.
"""
from __future__ import annotations

import os
import sys

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

BUS_NAME = "com.qdistro.UserRelay"
OBJ_PATH = "/com/qdistro/UserRelay"
APP1_IFACE = "com.qdistro.App1"
APP1_OBJ_PATH = "/com/qdistro/App1"

# System-bus name per uid. Broker (running as root on the system bus)
# talks to the relay here so it doesn't have to open cross-uid session
# buses — dbus-broker's session instances only accept the session
# owner's uid as a connection peer, so root-to-user-session is a
# non-starter on modern openSUSE.
SYSTEM_BUS_NAME_FMT = "com.qdistro.UserRelay.uid{uid}"

# Receivers claim bus names starting with this prefix; Forward and
# ListLocalReceivers both filter on it. Keeps the relay from being
# tricked into dispatching to arbitrary org.freedesktop.* services.
RECEIVER_PREFIX = "com.qdistro."

# Names to never treat as receivers even if they match the prefix.
EXCLUDED_NAMES = frozenset({
    BUS_NAME,
    "com.qdistro.AdminBroker1",   # system-bus name; wouldn't appear here, but be defensive
    "com.qdistro.StubSender",     # senders aren't receivers
})


class UserRelay(dbus.service.Object):

    def __init__(self, system_bus, session_bus):
        # Expose on the SYSTEM bus so root-broker can call us without
        # crossing uid into this user's session bus (dbus-broker's
        # session instance rejects non-owner-uid peers). All receiver
        # lookups below still happen on the session bus via the
        # second connection we hold here.
        super().__init__(system_bus, OBJ_PATH)
        self._bus = session_bus

    @dbus.service.method(BUS_NAME, in_signature="", out_signature="a(ss)",
                         sender_keyword="sender", connection_keyword="conn")
    def ListLocalReceivers(self, sender=None, conn=None):
        """Return [(service_name, friendly_name)] for every receiver
        on this session bus. Authz: any caller on the session bus
        (it's all one uid). The admin approval gate for actually
        sending is on the broker's RelayMessage, not here.
        """
        proxy = self._bus.get_object("org.freedesktop.DBus",
                                      "/org/freedesktop/DBus")
        iface = dbus.Interface(proxy, "org.freedesktop.DBus")
        names = [str(n) for n in iface.ListNames()]
        out = []
        for n in names:
            if not n.startswith(RECEIVER_PREFIX):
                continue
            if n in EXCLUDED_NAMES:
                continue
            if n.startswith(":"):
                continue
            out.append((n, _friendly_name(n)))
        return dbus.Array(out, signature="(ss)")

    @dbus.service.method(BUS_NAME, in_signature="sss", out_signature="")
    def Forward(self, service: str, kind: str, payload: str):
        """Invoke `<service>.Receive(kind, payload)` on this session
        bus. Raises if the service isn't present. Called only by the
        broker (over root's connection to this session bus)."""
        service_s = str(service)
        if not service_s.startswith(RECEIVER_PREFIX):
            raise dbus.DBusException(
                f"refusing to forward to non-qdistro service {service_s!r}",
                name=BUS_NAME + ".BadTarget",
            )
        if service_s in EXCLUDED_NAMES:
            raise dbus.DBusException(
                f"service {service_s!r} is not a receiver",
                name=BUS_NAME + ".BadTarget",
            )
        try:
            obj = self._bus.get_object(service_s, APP1_OBJ_PATH)
            obj.Receive(str(kind), str(payload), dbus_interface=APP1_IFACE)
        except dbus.DBusException:
            raise
        except Exception as e:  # noqa: BLE001
            raise dbus.DBusException(
                f"Forward to {service_s!r} failed: {e}",
                name=BUS_NAME + ".ForwardFailed",
            )


def _friendly_name(service: str) -> str:
    """Best-effort human-readable name derived from the service name.

    Phase 3 doesn't have an attestation channel for a receiver to
    declare a friendly name — the service name itself is the only
    thing we know. Strip the common prefix and the uid suffix:
    `com.qdistro.StubNotepad.uid3000` -> `StubNotepad`.
    """
    name = service
    if name.startswith(RECEIVER_PREFIX):
        name = name[len(RECEIVER_PREFIX):]
    # drop trailing .uidNNNN
    parts = name.rsplit(".", 1)
    if len(parts) == 2 and parts[1].startswith("uid") and parts[1][3:].isdigit():
        name = parts[0]
    return name


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    uid = os.geteuid()
    session_bus = dbus.SessionBus()
    system_bus = dbus.SystemBus()
    system_name = SYSTEM_BUS_NAME_FMT.format(uid=uid)
    # Anchors: dbus-python releases a BusName as soon as it's GC'd.
    # Named locals keep it alive for the lifetime of main().
    try:
        system_bus_name = dbus.service.BusName(
            system_name, system_bus, do_not_queue=True)
    except dbus.DBusException as e:
        print(f"[qdistro-user-relay] could not claim system-bus "
              f"{system_name}: {e} (missing dbus policy for uid={uid}?)",
              file=sys.stderr, flush=True)
        return 1
    relay = UserRelay(system_bus, session_bus)
    relay._anchor_system = system_bus_name  # noqa: SLF001
    print(f"[qdistro-user-relay] uid={uid} system={system_name} "
          f"session-peer-for-receivers=OK",
          flush=True)
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
