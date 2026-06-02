"""Finding #7 — inbound RequestTabs caller-auth + op-allowlist.

The session-bus ``RequestTabs`` surface forwards inbound ops to the
browser extension. Before this hardening it had NO caller authentication
and NO op allowlist: any same-session process that reached the per-bridge
bus name could open/close tabs or extract page content.

These tests pin:
  * the op allowlist (fail closed; page-extraction opt-in only),
  * the caller authorization (same-uid AND trusted; fail closed for
    unresolved/cross-uid callers),
  * the full ``inbound_dbus_serve`` dispatch denying an unauthorized
    caller and an unknown op before any forward to the extension.

Each test fails against the old behavior (no gate) and passes after the
fix.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

jeepney = pytest.importorskip("jeepney")
from jeepney import new_method_call, DBusAddress, HeaderFields  # noqa: E402

_MOD = (Path(__file__).resolve().parent.parent.parent
        / "browser_bridge" / "qdistro_browser_bridge.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge", _MOD)
bb = importlib.util.module_from_spec(spec)
sys.modules.setdefault("qdistro_browser_bridge", bb)
spec.loader.exec_module(bb)


# ---- op allowlist ----------------------------------------------------

class TestInboundOpAllowlist:
    @pytest.mark.parametrize("op", [
        "tabs.list", "tabs.open", "tabs.close", "mpris.publish",
        "notifications.show", "containers.list",
    ])
    def test_allowlisted_ops_allowed(self, op):
        ok, _ = bb._inbound_op_allowed(op)
        assert ok is True

    @pytest.mark.parametrize("op", [
        "", "qdistro.handshake", "pwd.fill", "clipboard.set",
        "totally.unknown", "tabs.list.reply",
    ])
    def test_non_allowlisted_ops_denied(self, op):
        ok, reason = bb._inbound_op_allowed(op)
        assert ok is False
        assert reason in ("op-not-allowlisted",)

    @pytest.mark.parametrize("op", [
        "page.extract", "page.extract.request", "cookies.export",
    ])
    def test_extract_ops_denied_by_default(self, op, monkeypatch):
        monkeypatch.delenv("QDISTRO_BRIDGE_INBOUND_ALLOW_EXTRACT",
                           raising=False)
        ok, reason = bb._inbound_op_allowed(op)
        assert ok is False
        assert reason == "op-extract-disabled"

    @pytest.mark.parametrize("op", [
        "page.extract", "page.extract.request", "cookies.export",
    ])
    def test_extract_ops_allowed_only_with_optin(self, op, monkeypatch):
        monkeypatch.setenv("QDISTRO_BRIDGE_INBOUND_ALLOW_EXTRACT", "1")
        ok, reason = bb._inbound_op_allowed(op)
        assert ok is True
        assert reason == "op-extract-enabled"


# ---- caller authorization -------------------------------------------

class _AuthConn:
    """Fake jeepney conn answering GetConnectionUnix{User,ProcessID}."""

    def __init__(self, uid, pid, *, error=False):
        self._uid = uid
        self._pid = pid
        self._error = error

    def send_and_get_reply(self, call, timeout=None):
        member = call.header.fields.get(HeaderFields.member)

        class _R:
            pass

        r = _R()
        if self._error:
            r.header = type("H", (), {
                "message_type": jeepney.MessageType.error})()
            r.body = ("err",)
            return r
        r.header = type("H", (), {
            "message_type": jeepney.MessageType.method_return})()
        r.body = ((self._uid,) if member == "GetConnectionUnixUser"
                  else (self._pid,))
        return r


class TestInboundCallerAuth:
    def test_no_sender_denied(self):
        ok, reason = bb._inbound_caller_authorized(_AuthConn(0, 0), "")
        assert ok is False and reason == "no-sender"

    def test_same_uid_trusted_allowed(self):
        my = os.getuid()
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my, 4242), ":1.55")
        assert ok is True
        assert "caller-ok" in reason

    def test_cross_uid_denied(self):
        my = os.getuid()
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my + 1, 4242), ":1.55")
        assert ok is False
        assert reason.startswith("cross-uid")

    def test_unresolved_uid_denied(self):
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(None, None, error=True), ":1.55")
        assert ok is False
        assert reason == "uid-unresolved"

    def test_trusted_uids_env_cannot_escalate_cross_uid(self, monkeypatch):
        """Adding a foreign uid to the trusted env var must NOT let a
        cross-uid caller through — the same-uid gate still applies."""
        my = os.getuid()
        monkeypatch.setenv("QDISTRO_BRIDGE_INBOUND_TRUSTED_UIDS",
                           str(my + 7))
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my + 7, 4242), ":1.55")
        assert ok is False
        assert reason.startswith("cross-uid")


# ---- full dispatch integration --------------------------------------

def _request_tabs_msg(op, sender):
    addr = DBusAddress(
        "/org/qdistro/BrowserBridge",
        bus_name="org.qdistro.BrowserBridge",
        interface="org.qdistro.BrowserBridge")
    msg = new_method_call(addr, "RequestTabs", "ss", (op, "{}"))
    msg.header.fields[HeaderFields.sender] = sender
    return msg


class _DispatchConn:
    """Drives inbound_dbus_serve with one queued RequestTabs message.

    RequestName replies PRIMARY_OWNER (1). The caller-auth path issues
    GetConnectionUnix{User,ProcessID} via send_and_get_reply; we answer
    those with the configured uid/pid. ``receive`` yields the one queued
    method call, then sets stop. ``send`` captures the error/return name.
    """

    def __init__(self, uid, pid, msg, stop):
        self._uid = uid
        self._pid = pid
        self._msg = msg
        self._stop = stop
        self._delivered = False
        self.closed = False
        self.sent = []  # list of (kind, detail)

    def send_and_get_reply(self, call, timeout=None):
        member = call.header.fields.get(HeaderFields.member)
        r = type("R", (), {})()
        if member == "RequestName":
            r.header = type("H", (), {
                "message_type": jeepney.MessageType.method_return})()
            r.body = (1,)  # PRIMARY_OWNER
            return r
        r.header = type("H", (), {
            "message_type": jeepney.MessageType.method_return})()
        r.body = ((self._uid,) if member == "GetConnectionUnixUser"
                  else (self._pid,))
        return r

    def receive(self, timeout=None):
        if not self._delivered:
            self._delivered = True
            return self._msg
        self._stop.set()
        return None

    def send(self, msg):
        # Distinguish error vs method_return by the ERROR_NAME header.
        err = msg.header.fields.get(HeaderFields.error_name)
        if err:
            self.sent.append(("error", err))
        else:
            self.sent.append(("return", None))

    def close(self):
        self.closed = True


def _run_serve(monkeypatch, conn, stop):
    import jeepney.io.blocking as blocking
    import jeepney.bus_messages as bus_messages
    monkeypatch.setattr(blocking, "open_dbus_connection", lambda bus: conn)
    monkeypatch.setattr(
        bus_messages.message_bus, "RequestName",
        lambda name, flags: new_method_call(
            DBusAddress("/", bus_name="org.freedesktop.DBus",
                        interface="org.freedesktop.DBus"),
            "RequestName", "su", (name, flags)))
    bb.inbound_dbus_serve(ppid=1, out_stream=None, stop_event=stop,
                          request_timeout_s=0.5)


class TestInboundDispatchGate:
    def test_cross_uid_caller_denied_no_forward(self, monkeypatch):
        """A cross-uid caller is denied with AccessDenied and the op is
        never forwarded to the extension."""
        forwarded = []
        monkeypatch.setattr(
            bb, "enqueue_inbound_request",
            lambda *a, **k: forwarded.append(a) or {"ok": True})
        stop = threading.Event()
        my = os.getuid()
        conn = _DispatchConn(my + 1, 4242,
                             _request_tabs_msg("tabs.list", ":1.7"), stop)
        _run_serve(monkeypatch, conn, stop)
        assert forwarded == []
        assert ("error", "org.freedesktop.DBus.Error.AccessDenied") \
            in conn.sent

    def test_unknown_op_denied_no_forward(self, monkeypatch):
        """An authorized same-uid caller asking for a non-allowlisted op
        is still denied before any forward."""
        forwarded = []
        monkeypatch.setattr(
            bb, "enqueue_inbound_request",
            lambda *a, **k: forwarded.append(a) or {"ok": True})
        stop = threading.Event()
        my = os.getuid()
        conn = _DispatchConn(
            my, 4242, _request_tabs_msg("pwd.fill", ":1.7"), stop)
        _run_serve(monkeypatch, conn, stop)
        assert forwarded == []
        assert ("error", "org.freedesktop.DBus.Error.AccessDenied") \
            in conn.sent

    def test_extract_op_denied_by_default(self, monkeypatch):
        monkeypatch.delenv("QDISTRO_BRIDGE_INBOUND_ALLOW_EXTRACT",
                           raising=False)
        forwarded = []
        monkeypatch.setattr(
            bb, "enqueue_inbound_request",
            lambda *a, **k: forwarded.append(a) or {"ok": True})
        stop = threading.Event()
        my = os.getuid()
        conn = _DispatchConn(
            my, 4242, _request_tabs_msg("page.extract", ":1.7"), stop)
        _run_serve(monkeypatch, conn, stop)
        assert forwarded == []

    def test_authorized_allowlisted_op_forwards(self, monkeypatch):
        """Positive control: same-uid trusted caller + allowlisted op is
        forwarded to the extension and returns a method_return."""
        forwarded = []
        monkeypatch.setattr(
            bb, "enqueue_inbound_request",
            lambda op, args, out, **k: forwarded.append(op)
            or {"ok": True, "op": op})
        stop = threading.Event()
        my = os.getuid()
        conn = _DispatchConn(
            my, 4242, _request_tabs_msg("tabs.list", ":1.7"), stop)
        _run_serve(monkeypatch, conn, stop)
        assert forwarded == ["tabs.list"]
        assert ("return", None) in conn.sent
