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
import time
import urllib.parse
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

# --------------------------------------------------------------------------
# Broker-module resolution (installed layout FIRST, checkout layout second).
#
# The relay needs broker/qdistro_admin_rules.py for the Firefox-containers
# cross-uid opt-in gate. Getting this wrong is invisible at runtime, so the
# search order is explicit and every candidate is documented:
#
#   1. The relay's own directory. This is the load-bearing one on a real
#      install: the broker and the shared daemon modules are installed
#      FLATTENED into /usr/libexec/qdistro/ (install-broker-for-qdwin.sh drops
#      qdistro_admin_rules.py there; install-user-relay-for-vm.sh drops this
#      file beside it), exactly like the browser bridge and the portal
#      backend resolve qdistro_proc_identity.
#   2. $QDISTRO_LIBEXEC (default /usr/libexec/qdistro) — belt and braces for
#      the case where the relay is executed through a symlink/wrapper from
#      some other prefix, and the seam the installed-layout test drives.
#      Mirrors pwd/qdistro-vault-recovery.py.
#   3. ../broker — the git checkout, where broker/ is a sibling of
#      user_relay/. Dev/test only; it does not exist on an install.
#
# Historical note: this used to be JUST candidate 3, computed as
# __file__/../../broker. Installed that resolved to /usr/local/lib/broker,
# which nothing creates, so the import below always failed, was swallowed,
# and the F4 opt-in could never be enabled in production. See
# tests/unit/test_user_relay_installed_layout.py, which reproduces the
# installed layout rather than the checkout layout.
_HERE = Path(__file__).resolve().parent
_LIBEXEC_DIR = Path(os.environ.get("QDISTRO_LIBEXEC", "/usr/libexec/qdistro"))
_BROKER_DIR_CANDIDATES: tuple[Path, ...] = (
    _HERE,
    _LIBEXEC_DIR,
    _HERE.parent / "broker",
)
for _cand in reversed(_BROKER_DIR_CANDIDATES):
    if _cand.is_dir() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

# Import the rules engine EAGERLY, at module import, so a broken install is
# visible the moment the daemon starts rather than only on the first (and
# every subsequent) denied container op.
_RULES_IMPORT_ERROR: str | None = None
try:
    from qdistro_admin_rules import (  # type: ignore[import-not-found]
        RulesEngine as _RulesEngine,
    )
except Exception as _e:  # noqa: BLE001
    _RulesEngine = None  # type: ignore[assignment]
    _RULES_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"


def _rules_dir() -> str:
    return os.environ.get("QDISTRO_RULES_DIR", "/etc/qdistro/rules.d")


def _log_error(msg: str) -> None:
    """Emit one ERROR line to stderr (systemd routes it to the journal)."""
    print(f"[qdistro-user-relay] ERROR: {msg}", file=sys.stderr, flush=True)


def _loggable(value: str, limit: int = 120) -> str:
    """repr() a caller-supplied string, length-capped, for a journal line.

    ``op`` arrives over D-Bus from a peer we are about to DENY, so it is
    attacker-influenced: repr() neutralises newlines/control characters (no
    forged journal lines) and the cap stops a multi-megabyte op name from
    being an amplification lever against the journal.
    """
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _audit_field(value: str, limit: int = 200) -> str:
    """Bound and neutralise a peer-supplied audit field.

    The audit line is a space-separated ``key=value`` record, so a
    peer-supplied value containing whitespace can forge a *field* and one
    containing a newline can forge a whole *line*. Well-formed values (the
    overwhelming majority: bus names, op names, error codes) pass through
    UNCHANGED so the documented ``journalctl | grep`` contract in
    doc/firefox-containers.md and the existing audit-line assertions still
    hold; a value that is empty, over-long, or carries whitespace or
    non-printables is percent-encoded instead.

    Percent-encoding rather than ``repr()``: repr escapes newlines, tabs and
    control characters but NOT the ordinary space, so a repr'd value still
    contains literal ``ok=true`` tokens that a space/token-based consumer
    would read as authoritative fields. The encoded form contains no
    whitespace at all, so the record always has exactly the six documented
    keys. It is reversible, so nothing is silently dropped.
    """
    text = str(value)
    if text and len(text) <= limit and all(
            ch.isprintable() and not ch.isspace() for ch in text):
        return text
    encoded = urllib.parse.quote(text, safe="")
    if len(encoded) > limit:
        encoded = encoded[:limit] + "...(truncated)"
    return f"%enc%{encoded}" if encoded else "%enc%"


# Rate-limit for the per-call install-defect ERROR. The full diagnostic is
# worth emitting once; after that a peer can call ForwardBrowserBridgeOp in a
# loop, and an unbounded ERROR per call would push the useful first line out
# of the journal's rate-limit window. Emit the full text once, then a terse
# line at most every _RULES_ERROR_REMIND_S seconds carrying the suppressed
# count so the signal never disappears entirely.
_RULES_ERROR_REMIND_S = 300.0
_rules_error_log_state = {"emitted": 0, "last": 0.0, "suppressed": 0}


def _log_rules_engine_defect(op: str, uid: int, status: str) -> None:
    st = _rules_error_log_state
    now = time.monotonic()
    if st["emitted"] == 0:
        st.update(emitted=1, last=now, suppressed=0)
        _log_error(f"containers op {_loggable(op)} for uid={uid} DENIED — "
                   f"{status}")
        return
    if now - float(st["last"]) < _RULES_ERROR_REMIND_S:
        st["suppressed"] = int(st["suppressed"]) + 1
        return
    suppressed = int(st["suppressed"])
    st.update(last=now, suppressed=0)
    _log_error(
        f"containers ops still DENIED (rules engine unavailable); "
        f"{suppressed} identical message(s) suppressed in the last "
        f"{int(_RULES_ERROR_REMIND_S)}s; latest op {_loggable(op)} "
        f"uid={uid}")


def _rules_engine_status() -> str | None:
    """Return None when the rules engine is importable, else a diagnostic.

    Split out so both the daemon's startup preflight and the per-call gate
    report the same thing.
    """
    if _RulesEngine is not None:
        return None
    return (
        "broker rules engine (qdistro_admin_rules) is NOT importable: "
        f"{_RULES_IMPORT_ERROR}; searched "
        + os.pathsep.join(str(p) for p in _BROKER_DIR_CANDIDATES)
        + ". The Firefox-containers cross-uid opt-in "
        "(qdistro.browser.containers.cross_uid:*) CANNOT be enabled while "
        "this is broken — every containers.* relay op fails closed. Install "
        "the broker modules beside this file (install-broker-for-qdwin.sh) "
        "or set QDISTRO_LIBEXEC to the directory holding "
        "qdistro_admin_rules.py."
    )


# Emit the diagnostic at import time too — `python3 -c "import
# qdistro_user_relay"` in a post-install smoke check is then enough to
# catch a mis-laid-out install, and the daemon's very first journal line
# says so on a real boot.
if _RULES_IMPORT_ERROR is not None:  # pragma: no cover - install-defect path
    _log_error(_rules_engine_status() or "")


def _containers_cross_uid_allowed(uid: int, op: str,
                                  rules_dir: str | None = None) -> tuple[bool, str]:
    """Return whether cross-uid Firefox container relay is opted in.

    The opt-in is an ordinary broker rule so it is visible in the admin app's
    Rules tab and survives the same reload/audit workflow. Missing rules,
    malformed rules, and explicit deny all fail closed.

    Fail-closed is preserved, but it is no longer *silent*. A missing rules
    engine is an install defect, not a policy decision, so it is reported
    with its own reason code (``rules-import-error``, distinct from the
    genuine policy outcomes ``no-opt-in-rule`` / ``denied-by-rule``), an
    ERROR journal line on the first such call, and a rate-limited reminder
    (carrying the suppressed count) thereafter. The daemon deliberately does not abort:
    the relay's primary duty — Forward/ListLocalReceivers for cross-silo
    Send-To — has no dependency on the rules engine, so exiting here would
    trade one dead feature for a crash-looping unit that kills Send-To for
    every silo. Loud-and-degraded beats loud-and-broken; the three signals
    (import-time line, startup preflight, the first-call ERROR plus its
    periodic reminder, and the ``feature_not_enabled`` detail returned to the
    D-Bus caller on EVERY call) are what the original bare ``except`` denied
    the operator.
    """
    if not op.startswith("containers."):
        return True, "not-containers-op"
    status = _rules_engine_status()
    if status is not None:
        _log_rules_engine_defect(op, uid, status)
        return False, "rules-import-error"
    try:
        engine = _RulesEngine(rules_dir or _rules_dir())
        if engine.load_errors():
            return False, "rules-load-error"
        action = f"{CONTAINERS_CROSS_UID_ACTION_PREFIX}{op}"
        rule = engine.match(uid=int(uid), action=action,
                            exe=CONTAINERS_CROSS_UID_EXE)
    except Exception as e:  # noqa: BLE001
        _log_error(
            f"containers op {_loggable(op)} for uid={uid} DENIED — rules "
            f"engine raised {type(e).__name__}: {_loggable(str(e), 200)} "
            f"(rules_dir={rules_dir or _rules_dir()!r})")
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
            ) from e

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
    # `op`, `sender` and `bridge` are all peer-supplied. Emitting them raw let
    # a newline forge an extra audit line and an unbounded string amplify log
    # volume; _loggable repr-escapes and caps every one of them.
    print(
        f"[qdistro-user-relay/audit] kind={kind} "
        f"sender={_audit_field(sender or '-')} op={_audit_field(op)} "
        f"bridge={_audit_field(bridge)} "
        f"ok={'true' if ok else 'false'} error={_audit_field(err)}",
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


# Exit status for "the system bus refuses to let this identity own its relay
# name". Distinct from 1 so the unit's RestartPreventExitStatus can stop the
# restart loop for a refusal that is a static-policy fact, while every other
# failure keeps Restart=on-failure. EX_CONFIG from sysexits.h.
EXIT_POLICY_DENIED = 78


def _is_permanent_name_denial(exc: Exception) -> bool:
    """True for a D-Bus name request refused by static bus policy.

    Anything else (bus not up yet, socket error, name already owned by a
    live peer) may clear on its own and must keep the unit restarting, so
    this deliberately matches only the policy-refusal signatures.
    """
    name = getattr(exc, "get_dbus_name", lambda: "")() or ""
    if name in ("org.freedesktop.DBus.Error.AccessDenied",
                "org.freedesktop.DBus.Error.NotSupported"):
        return True
    return "refused by policy" in str(exc).lower()


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    uid = os.geteuid()
    # Startup preflight: surface a broken broker-module layout in the very
    # first lines of `journalctl -u qdistro-user-relay@<uid>`. Non-fatal by
    # design — see _containers_cross_uid_allowed's docstring.
    _preflight = _rules_engine_status()
    if _preflight is not None:  # pragma: no cover - install-defect path
        _log_error(f"startup preflight: {_preflight}")
    session_bus = dbus.SessionBus()
    system_bus = dbus.SystemBus()
    system_name = SYSTEM_BUS_NAME_FMT.format(uid=uid)
    # Anchors: dbus-python releases a BusName as soon as it's GC'd.
    # Named locals keep it alive for the lifetime of main().
    try:
        system_bus_name = dbus.service.BusName(
            system_name, system_bus, do_not_queue=True)
    except dbus.DBusException as e:
        permanent = _is_permanent_name_denial(e)
        _log_error(
            f"could not claim system-bus {system_name}: {e}. This name is "
            f"granted by a per-silo policy fragment that "
            f"qdistro-session-manager writes to "
            f"/etc/dbus-1/system.d/org.qdistro.UserRelay.silo-<name>.conf "
            f"when the silo is created, and re-issues for every silo at its "
            f"own startup. A refusal here means that fragment is missing, "
            f"names a different user than the one running this relay, or the "
            f"bus has not re-read its config since it was written. Check: "
            f"(1) `grep -l 'user=\"{uid}\"' "
            f"/etc/dbus-1/system.d/org.qdistro.UserRelay.silo-*` — the "
            f"fragment keys the grant on the NUMERIC uid, so there must be "
            f"one naming {uid}; "
            f"(2) `systemctl status qdistro-session-manager` — if it is not "
            f"installed, nothing issues the grant at all; "
            f"(3) `systemctl reload dbus.service`. Neither cross-silo "
            f"Send-To nor the Firefox-containers cross-uid opt-in can work "
            f"for uid={uid} until the grant is live. See "
            f"doc/firefox-containers.md 'Reachability'.")
        if permanent:
            _log_error(
                f"exiting {EXIT_POLICY_DENIED} (RestartPreventExitStatus) — "
                "this is a static-policy refusal, not a transient fault; "
                "restarting on a 2s cadence would only churn the unit and "
                "flood the journal. qdshell-session-launcher retries on the "
                "next silo launch, and a corrected policy takes effect then.")
            return EXIT_POLICY_DENIED
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
