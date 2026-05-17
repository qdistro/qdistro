#!/usr/bin/env python3
"""qdistro-user-relay — per-user session-bus daemon.

One instance runs as each user with `org.qdistro.UserRelay` on the
session bus. Lets the broker (running as root on the system bus)
reach receivers in that user's session without the broker having
to open session-bus connections as arbitrary uids itself.

Why the indirection? The broker is root; it CAN open any user's
session-bus socket via DAC_OVERRIDE. But the call surface it would
need (arbitrary `org.qdistro.*.Receive` invocations) is wide. With
a relay in between, root only ever calls `UserRelay.Forward(...)`
and `UserRelay.ListLocalReceivers()` — two narrow methods. Easier
to audit, easier to lock down with a session-bus policy later.

Phase-3 scope: Forward() blindly invokes `<service>.Receive(kind,
payload)`. No ACL (the admin approval gate is the broker's job);
no retry; no payload size cap beyond broker's detail sanitiser.
"""
from __future__ import annotations

import json
import os
import sys

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

BUS_NAME = "org.qdistro.UserRelay"
OBJ_PATH = "/org/qdistro/UserRelay"
APP1_IFACE = "org.qdistro.App1"
APP1_OBJ_PATH = "/org/qdistro/App1"

# Browser-bridge surface (see qdistro/doc/firefox-containers.md).
# A bridge claims org.qdistro.BrowserBridge.<ppid> on each user's
# session bus and exposes RequestTabs(s op, s args_json) -> s reply_json.
# ForwardBrowserBridgeOp lets the broker reach those bridges without
# crossing into a non-owning uid's session bus directly.
BRIDGE_NAME_PREFIX = "org.qdistro.BrowserBridge."
BRIDGE_OBJ_PATH = "/org/qdistro/BrowserBridge"
BRIDGE_IFACE = "org.qdistro.BrowserBridge"

# System-bus name per uid. Broker (running as root on the system bus)
# talks to the relay here so it doesn't have to open cross-uid session
# buses — dbus-broker's session instances only accept the session
# owner's uid as a connection peer, so root-to-user-session is a
# non-starter on modern openSUSE.
SYSTEM_BUS_NAME_FMT = "org.qdistro.UserRelay.uid{uid}"

# Receivers claim bus names starting with this prefix; Forward and
# ListLocalReceivers both filter on it. Keeps the relay from being
# tricked into dispatching to arbitrary org.freedesktop.* services.
RECEIVER_PREFIX = "org.qdistro."

# Names to never treat as receivers even if they match the prefix.
EXCLUDED_NAMES = frozenset({
    BUS_NAME,
    "org.qdistro.AdminBroker1",   # system-bus name; wouldn't appear here, but be defensive
    "org.qdistro.StubSender",     # senders aren't receivers
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

    @dbus.service.method(BUS_NAME, in_signature="sss", out_signature="s",
                         sender_keyword="sender")
    def ForwardBrowserBridgeOp(self, op: str, args_json: str,
                               selector_json: str,
                               sender: str | None = None) -> str:
        """Forward a bridge op to an `org.qdistro.BrowserBridge.<ppid>`
        on this user's session bus and return the bridge's JSON reply.

        Wire shape mirrors the bridge's `RequestTabs(s, s) -> s`. The
        relay is a uid-crossing wrapper: cross-uid callers reach this
        method on the *system* bus, the relay forwards to the bridge
        on its own *session* bus.

        ``selector_json`` chooses which bridge instance receives the
        op. The user may have multiple browsers running, each with its
        own bridge:

        - ``{"ppid": <int>}`` — exact match on
          ``org.qdistro.BrowserBridge.<ppid>``. Use when the caller
          already knows the bridge's ppid (e.g. via a prior probe).
        - ``{"any": true}`` — pick the first bridge found.
          Adequate when only one browser is running, or when the op
          doesn't care which browser (e.g. ``qdistro.ping``).

        Returns a JSON-encoded reply dict. Failures inside the relay
        are surfaced as ``{"ok": false, "error": "<code>", ...}``
        rather than D-Bus exceptions so callers handle them
        identically to bridge-side ``ok:false`` replies. Authorization
        is the system-bus peer-uid policy on
        ``org.qdistro.UserRelay.uid<NNNN>`` — same model as
        :meth:`Forward`.
        """
        op_s = str(op)
        if not op_s:
            reply = {"ok": False, "error": "missing_op"}
            _audit("forward_bridge_op", sender, op_s, "-", reply)
            return json.dumps(reply)
        try:
            selector = json.loads(str(selector_json) or "{}")
            if not isinstance(selector, dict):
                raise ValueError("selector must be a JSON object")
        except (ValueError, json.JSONDecodeError) as e:
            reply = {"ok": False, "error": "bad_selector",
                     "detail": str(e)[:200]}
            _audit("forward_bridge_op", sender, op_s, "-", reply)
            return json.dumps(reply)
        # Refuse selectors that mix `ppid` and `any` rather than
        # silently letting one win — the caller's intent is ambiguous.
        if "ppid" in selector and selector.get("any") is True:
            reply = {"ok": False, "error": "bad_selector",
                     "detail": "selector cannot set both 'ppid' and 'any'"}
            _audit("forward_bridge_op", sender, op_s, "-", reply)
            return json.dumps(reply)
        # Tighten `ppid` typing: JSON ints only. A quoted "1234"
        # likely indicates a caller bug worth surfacing rather than
        # silently coercing.
        if "ppid" in selector and not isinstance(selector["ppid"], int):
            reply = {"ok": False, "error": "bad_selector",
                     "detail": "'ppid' must be a JSON integer"}
            _audit("forward_bridge_op", sender, op_s, "-", reply)
            return json.dumps(reply)
        bridge_name = self._select_bridge(selector)
        if bridge_name is None:
            reply = {"ok": False, "error": "no_bridge_found",
                     "selector": selector}
            _audit("forward_bridge_op", sender, op_s, "-", reply)
            return json.dumps(reply)
        try:
            obj = self._bus.get_object(bridge_name, BRIDGE_OBJ_PATH)
            # args_json is opaque pass-through; default to "{}" so the
            # bridge always receives JSON-parseable args even when the
            # caller passes "" or None.
            reply_str = obj.RequestTabs(
                op_s, str(args_json) or "{}",
                dbus_interface=BRIDGE_IFACE)
        except dbus.DBusException as e:
            reply = {"ok": False, "error": "bridge_call_failed",
                     "bridge": bridge_name,
                     "dbus_name": e.get_dbus_name(),
                     "detail": str(e)[:200]}
            _audit("forward_bridge_op", sender, op_s, bridge_name, reply)
            return json.dumps(reply)
        except Exception as e:  # noqa: BLE001
            reply = {"ok": False, "error": "bridge_call_failed",
                     "bridge": bridge_name,
                     "detail": f"{type(e).__name__}: {e}"[:200]}
            _audit("forward_bridge_op", sender, op_s, bridge_name, reply)
            return json.dumps(reply)
        # Parse the bridge's reply purely so the audit line can record
        # ok/error; fall back to {} if it isn't JSON.
        reply_str_s = str(reply_str)
        try:
            audit_reply = json.loads(reply_str_s)
            if not isinstance(audit_reply, dict):
                audit_reply = {}
        except (ValueError, json.JSONDecodeError):
            audit_reply = {}
        _audit("forward_bridge_op", sender, op_s, bridge_name, audit_reply)
        return reply_str_s

    def _select_bridge(self, selector: dict) -> str | None:
        """Return the chosen org.qdistro.BrowserBridge.<ppid> name
        on the session bus, or None if no bridge matches.

        Only names whose suffix after :data:`BRIDGE_NAME_PREFIX` is
        all-digits count — a same-uid attacker can claim
        ``org.qdistro.BrowserBridge.impostor`` on the session bus, and
        without the digit check ``{"any": true}`` could route admin
        calls there. The bridge itself always uses ``<ppid>`` (an int),
        so the gate is lossless for legitimate names.
        """
        proxy = self._bus.get_object("org.freedesktop.DBus",
                                      "/org/freedesktop/DBus")
        iface = dbus.Interface(proxy, "org.freedesktop.DBus")
        bridges_with_ppid: list[tuple[int, str]] = []
        for n in iface.ListNames():
            name = str(n)
            if not name.startswith(BRIDGE_NAME_PREFIX):
                continue
            if name.startswith(":"):
                continue
            suffix = name[len(BRIDGE_NAME_PREFIX):]
            if not suffix.isdigit():
                continue
            bridges_with_ppid.append((int(suffix), name))
        if not bridges_with_ppid:
            return None
        if "ppid" in selector:
            want = int(selector["ppid"])  # already validated above
            target = f"{BRIDGE_NAME_PREFIX}{want}"
            for _, name in bridges_with_ppid:
                if name == target:
                    return name
            return None
        if selector.get("any") is True:
            # Sort numerically by ppid so the choice is stable AND
            # matches operator intuition (lexicographic on the full
            # name would put "BrowserBridge.10000" before
            # "BrowserBridge.9999").
            bridges_with_ppid.sort(key=lambda t: t[0])
            return bridges_with_ppid[0][1]
        return None

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


def _audit(kind: str, sender: str | None, op: str,
           bridge: str, reply: dict) -> None:
    """Write one journal line summarising a relay operation.

    Goes to stderr; systemd routes that to the journal under the
    relay's unit. Fields are space-separated key=value so journalctl
    grep over the relay log is trivial. ``reply`` is parsed for
    ``ok`` and ``error`` only — full reply bodies could contain
    container metadata (names, icons) which we don't want to mirror
    into a second log site.

    Example::

        [qdistro-user-relay/audit] kind=forward_bridge_op
            sender=:1.42 op=containers.list
            bridge=org.qdistro.BrowserBridge.1234 ok=true error=
    """
    ok = bool(reply.get("ok", False)) if isinstance(reply, dict) else False
    err = str(reply.get("error", "")) if isinstance(reply, dict) else ""
    print(
        f"[qdistro-user-relay/audit] kind={kind} "
        f"sender={sender or '-'} op={op} bridge={bridge} "
        f"ok={'true' if ok else 'false'} error={err}",
        file=sys.stderr, flush=True,
    )


def _friendly_name(service: str) -> str:
    """Best-effort human-readable name derived from the service name.

    Phase 3 doesn't have an attestation channel for a receiver to
    declare a friendly name — the service name itself is the only
    thing we know. Strip the common prefix and the uid suffix:
    `org.qdistro.StubNotepad.uid3000` -> `StubNotepad`.
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
