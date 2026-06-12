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
from pathlib import Path

import dbus
import dbus.mainloop.glib
import dbus.service
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

# Debounce window for coalescing a burst of session-bus
# NameOwnerChanged events into a single LocalReceiversChanged emit. A
# silo coming up registers several receivers back-to-back; without the
# coalesce the broker (and qdshell behind it) would re-run ListReceivers
# once per name. 250ms is short enough to feel instant in the launcher,
# long enough to swallow a typical startup burst.
RECEIVER_CHANGE_DEBOUNCE_MS = 250

CONTAINERS_CROSS_UID_ACTION_PREFIX = "qdistro.browser.containers.cross_uid:"
CONTAINERS_CROSS_UID_EXE = "qdistro-user-relay"

_BROKER_DIR = Path(__file__).resolve().parent.parent / "broker"
if str(_BROKER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROKER_DIR))


def _rules_dir() -> str:
    return os.environ.get("QDISTRO_RULES_DIR", "/etc/qdistro/rules.d")


def _containers_cross_uid_allowed(uid: int, op: str,
                                  rules_dir: str | None = None) -> tuple[bool, str]:
    """Return whether cross-uid Firefox container relay is opted in.

    The opt-in is an ordinary broker rule so it is visible in the admin app's
    Rules tab and survives the same reload/audit workflow. Missing rules,
    malformed rules, and explicit deny all fail closed.
    """
    if not op.startswith("containers."):
        return True, "not-containers-op"
    try:
        from qdistro_admin_rules import RulesEngine  # type: ignore[import-not-found]
        engine = RulesEngine(rules_dir or _rules_dir())
        if engine.load_errors():
            return False, "rules-load-error"
        action = f"{CONTAINERS_CROSS_UID_ACTION_PREFIX}{op}"
        rule = engine.match(uid=int(uid), action=action,
                            exe=CONTAINERS_CROSS_UID_EXE)
    except Exception:  # noqa: BLE001
        return False, "rules-unavailable"
    if rule is None:
        return False, "no-opt-in-rule"
    if rule.decision != "allow":
        return False, "denied-by-rule"
    return True, f"allowed-by:{rule.name}"


def _is_receiver_name_change(name: str, old_owner: str,
                             new_owner: str) -> bool:
    """True iff a session-bus NameOwnerChanged represents a receiver
    appearing or disappearing — i.e. the kind of change qdshell's
    launcher must re-fetch for.

    A change counts only when ``name`` is a real receiver name (matches
    :data:`RECEIVER_PREFIX`, not in :data:`EXCLUDED_NAMES`, not a
    ``:1.N`` unique-connection name) AND it is an acquire or a release —
    exactly one of ``old_owner`` / ``new_owner`` is empty:

    - acquire:  ``old_owner == ""`` and ``new_owner != ""``
    - release:  ``new_owner == ""`` and ``old_owner != ""``

    A pure ownership transfer (both owners present) and a no-op (both
    empty) are *not* receiver changes — the set of registered receivers
    is unchanged — so they return False. Pure function, no bus access,
    unit-tested in tests/unit/test_user_relay.py.
    """
    if not name.startswith(RECEIVER_PREFIX):
        return False
    if name in EXCLUDED_NAMES:
        return False
    if name.startswith(":"):
        return False
    # Exactly one of the two owners empty == acquire xor release.
    return (old_owner == "") != (new_owner == "")


class UserRelay(dbus.service.Object):

    def __init__(self, system_bus, session_bus, uid=None):
        # Expose on the SYSTEM bus so root-broker can call us without
        # crossing uid into this user's session bus (dbus-broker's
        # session instance rejects non-owner-uid peers). All receiver
        # lookups below still happen on the session bus via the
        # second connection we hold here.
        super().__init__(system_bus, OBJ_PATH)
        self._bus = session_bus
        # uid carried in the LocalReceiversChanged payload so the broker
        # can log which session changed. Defaults to this process's
        # euid; main() passes it explicitly.
        self._uid = int(uid if uid is not None else os.geteuid())
        # GLib timeout id of a pending (debounced) emit, 0 when none is
        # armed. Coalesces a burst of receiver changes into one signal.
        self._receivers_changed_timer = 0

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
        allowed, reason = _containers_cross_uid_allowed(self._uid, op_s)
        if not allowed:
            reply = {"ok": False, "error": "feature_not_enabled",
                     "feature": "firefox-containers-cross-user",
                     "detail": reason}
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

    @dbus.service.signal(BUS_NAME, signature="i")
    def LocalReceiversChanged(self, uid):
        """Emitted when a receiver appears or disappears on this user's
        session bus — i.e. the result of :meth:`ListLocalReceivers`
        would now differ. ``uid`` is this relay's uid.

        Emitted on the relay's *system*-bus object (same iface + path
        for every per-uid relay) so the root broker can observe it with
        a single, sender-agnostic match instead of one per uid. The
        broker re-emits its own payload-free ``ReceiversChanged`` for
        qdshell. Emits are coalesced: a burst of session-bus
        NameOwnerChanged events (a silo bringing up several receivers at
        once) collapses to one signal via a short debounce.
        """
        pass

    def _on_session_name_owner_changed(self, name, old_owner, new_owner):
        """Session-bus NameOwnerChanged handler. Arms a debounced
        LocalReceiversChanged emit when the change is a receiver
        acquire/release; ignores everything else (transfers, no-ops,
        non-receiver names)."""
        if not _is_receiver_name_change(str(name), str(old_owner),
                                        str(new_owner)):
            return
        if self._receivers_changed_timer:
            # A burst is already pending — let the armed timer fire once
            # for the whole burst rather than resetting it (resetting on
            # every event could starve the emit under a steady trickle).
            return
        self._receivers_changed_timer = GLib.timeout_add(
            RECEIVER_CHANGE_DEBOUNCE_MS, self._emit_receivers_changed)

    def _emit_receivers_changed(self) -> bool:
        """Debounce-timer callback: emit one LocalReceiversChanged and
        clear the pending-timer id. Returns False so GLib drops the
        one-shot timeout."""
        self._receivers_changed_timer = 0
        print(f"[qdistro-user-relay] receivers changed on session bus; "
              f"emitting LocalReceiversChanged(uid={self._uid})",
              file=sys.stderr, flush=True)
        self.LocalReceiversChanged(self._uid)
        return False


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
    relay = UserRelay(system_bus, session_bus, uid=uid)
    relay._anchor_system = system_bus_name  # noqa: SLF001
    # Watch the session bus for receivers coming and going so we can
    # nudge the broker (and qdshell behind it) to re-fetch. Per-uid
    # NameOwnerChanged is session-local; the broker's system-bus name
    # never reflects it, hence this relay-side bridge into a system-bus
    # LocalReceiversChanged signal.
    session_bus.add_signal_receiver(
        relay._on_session_name_owner_changed,  # noqa: SLF001
        signal_name="NameOwnerChanged",
        dbus_interface="org.freedesktop.DBus",
        bus_name="org.freedesktop.DBus")
    print(f"[qdistro-user-relay] uid={uid} system={system_name} "
          f"session-peer-for-receivers=OK",
          flush=True)
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
