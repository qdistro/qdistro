"""Finding #7 — inbound RequestTabs caller-auth + op-allowlist.

The session-bus ``RequestTabs`` surface forwards inbound ops to the
browser extension. Before this hardening it had NO caller authentication
and NO op allowlist: any same-session process that reached the per-bridge
bus name could open/close tabs or extract page content.

These tests pin:
  * the op allowlist (fail closed; page-extraction opt-in only) derived
    from the extension's REAL inbound switch — including ``mpris.control``,
  * the caller authorization: same-uid is NECESSARY BUT NOT SUFFICIENT —
    an arbitrary same-uid process (random exe / random script) is DENIED,
    and only the specific legitimate qdistro daemon scripts (attested via
    /proc exe + script basename, anchored on starttime) are ALLOWED,
  * the full ``inbound_dbus_serve`` dispatch denying an unauthorized
    caller and an unknown op before any forward to the extension.

The caller-auth tests deliberately FAIL against a same-uid-sufficient
implementation (the broken finding #7 remediation): an arbitrary same-uid
caller running a random exe must be denied.
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
    # The allowlist must equal the extension's REAL inbound switch(op)
    # (handleInboundRequest in background.js): tabs.list/open/close,
    # containers.list/create/remove, notifications.show, mpris.control.
    @pytest.mark.parametrize("op", [
        "tabs.list", "tabs.open", "tabs.close",
        "containers.list", "containers.create", "containers.remove",
        "notifications.show", "mpris.control",
    ])
    def test_allowlisted_ops_allowed(self, op):
        ok, _ = bb._inbound_op_allowed(op)
        assert ok is True

    def test_mpris_control_is_allowed(self):
        """Regression for the broken finding #7 remediation: mpris.control
        is a REAL inbound op (admin media widget -> tab, sent by
        qdistro_mpris_daemon via RequestTabs). It MUST be allowed or
        legitimate media Play/Pause/Next is denied."""
        ok, reason = bb._inbound_op_allowed("mpris.control")
        assert ok is True
        assert reason == "op-allowed"

    def test_bogus_op_is_denied(self):
        ok, reason = bb._inbound_op_allowed("totally.bogus.op")
        assert ok is False
        assert reason == "op-not-allowlisted"

    @pytest.mark.parametrize("op", [
        # Outbound-only ops the extension *initiates*; they are NOT in the
        # inbound switch, so the inbound surface must deny them.
        "mpris.publish", "downloads.notify", "screenlock.inhibit",
        "screenlock.release", "cookies.export",
        # genuinely unknown ops.
        "", "qdistro.handshake", "pwd.fill", "clipboard.set",
        "totally.unknown", "tabs.list.reply",
    ])
    def test_non_allowlisted_ops_denied(self, op):
        ok, reason = bb._inbound_op_allowed(op)
        assert ok is False
        assert reason in ("op-not-allowlisted",)

    @pytest.mark.parametrize("op", [
        "page.extract.request",
    ])
    def test_extract_ops_denied_by_default(self, op, monkeypatch):
        monkeypatch.delenv("QDISTRO_BRIDGE_INBOUND_ALLOW_EXTRACT",
                           raising=False)
        ok, reason = bb._inbound_op_allowed(op)
        assert ok is False
        assert reason == "op-extract-disabled"

    @pytest.mark.parametrize("op", [
        "page.extract.request",
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


def _fake_proc(monkeypatch, *, exe, argv, starttime=999):
    """Make the /proc identity readers describe a single fake caller.

    ``exe`` is the resolved ``/proc/<pid>/exe`` target (the interpreter
    for a python daemon); ``argv`` is the NUL-split cmdline (argv[0] =
    interpreter, then the script path + args).
    """
    assert bb._pi is not None, "proc-identity readers must be importable"
    monkeypatch.setattr(bb._pi, "read_starttime", lambda pid: starttime)
    monkeypatch.setattr(bb._pi, "read_exe", lambda pid: exe)
    monkeypatch.setattr(bb, "_proc_cmdline", lambda pid: list(argv))


# A legitimate caller: systemd launches the daemons as
# `/usr/bin/python3 <install>/qdistro_mpris_daemon.py` (see .service
# ExecStart), so exe is the interpreter and the script identifies the
# daemon.
_LEGIT_MPRIS = dict(
    exe="/usr/bin/python3",
    argv=["/usr/bin/python3", "/usr/libexec/qdistro/qdistro_mpris_daemon.py"])
_LEGIT_RELAY = dict(
    exe="/usr/bin/python3",
    argv=["/usr/bin/python3", "/usr/local/lib/qdistro/qdistro_user_relay.py"])


class TestInboundCallerAuth:
    def test_no_sender_denied(self):
        ok, reason = bb._inbound_caller_authorized(_AuthConn(0, 0), "")
        assert ok is False and reason == "no-sender"

    def test_arbitrary_same_uid_caller_denied(self, monkeypatch):
        """The CORE finding-#7 regression: an arbitrary same-uid process
        (the desktop user running some random binary) must be DENIED.

        This FAILS against the broken same-uid-sufficient version, which
        authorized every same-session process.
        """
        my = os.getuid()
        _fake_proc(monkeypatch,
                   exe="/usr/bin/evil-tool",
                   argv=["/usr/bin/evil-tool", "--pwn"])
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my, 4242), ":1.55")
        assert ok is False
        assert "caller-not-trusted" in reason

    def test_same_uid_random_python_script_denied(self, monkeypatch):
        """Even a same-uid python process is denied unless it is running
        one of the specific trusted daemon scripts."""
        my = os.getuid()
        _fake_proc(monkeypatch,
                   exe="/usr/bin/python3",
                   argv=["/usr/bin/python3", "/home/user/attacker.py"])
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my, 4242), ":1.55")
        assert ok is False
        assert "script-not-trusted" in reason

    def test_legit_mpris_daemon_allowed(self, monkeypatch):
        """The real qdistro_mpris_daemon (python interpreter running the
        known daemon script) is ALLOWED."""
        my = os.getuid()
        _fake_proc(monkeypatch, **_LEGIT_MPRIS)
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my, 4242), ":1.55")
        assert ok is True
        assert "caller-ok" in reason
        assert "qdistro_mpris_daemon.py" in reason

    def test_legit_user_relay_allowed(self, monkeypatch):
        my = os.getuid()
        _fake_proc(monkeypatch, **_LEGIT_RELAY)
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my, 4242), ":1.55")
        assert ok is True
        assert "caller-ok" in reason

    def test_legit_daemon_but_cross_uid_denied(self, monkeypatch):
        """Same-uid is necessary: a cross-uid caller is denied even if it
        would otherwise attest as a trusted daemon."""
        my = os.getuid()
        _fake_proc(monkeypatch, **_LEGIT_MPRIS)
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my + 1, 4242), ":1.55")
        assert ok is False
        assert reason.startswith("cross-uid")

    def test_zero_starttime_denied(self, monkeypatch):
        """Anti-PID-reuse: a zero starttime (process gone / unreadable) is
        the fail-closed signal even for the right exe/script."""
        my = os.getuid()
        _fake_proc(monkeypatch, starttime=0, **_LEGIT_MPRIS)
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my, 4242), ":1.55")
        assert ok is False
        assert "zero-starttime" in reason

    def test_spoofed_script_name_but_non_python_exe_denied(self, monkeypatch):
        """A random ELF named like a daemon script (argv[1] looks right)
        but whose interpreter exe is NOT python is denied — the python
        interpreter pairing anchors the identity."""
        my = os.getuid()
        _fake_proc(monkeypatch,
                   exe="/home/user/qdistro_mpris_daemon.py",
                   argv=["/home/user/qdistro_mpris_daemon.py"])
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my, 4242), ":1.55")
        assert ok is False
        assert "non-python-interp" in reason

    def test_cross_uid_denied(self, monkeypatch):
        my = os.getuid()
        _fake_proc(monkeypatch, **_LEGIT_MPRIS)
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my + 1, 4242), ":1.55")
        assert ok is False
        assert reason.startswith("cross-uid")

    def test_unresolved_uid_denied(self):
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(None, None, error=True), ":1.55")
        assert ok is False
        assert reason == "uid-unresolved"

    def test_trusted_scripts_env_narrows_only(self, monkeypatch):
        """The env override may only NARROW: setting it to exclude the
        mpris daemon denies it; it can never authorize an arbitrary
        script not already built-in."""
        my = os.getuid()
        # Narrow to relay only -> mpris denied.
        monkeypatch.setenv("QDISTRO_BRIDGE_INBOUND_TRUSTED_SCRIPTS",
                           "qdistro_user_relay.py")
        _fake_proc(monkeypatch, **_LEGIT_MPRIS)
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my, 4242), ":1.55")
        assert ok is False
        assert "script-not-trusted" in reason
        # An arbitrary script named in the env var is NOT authorized.
        monkeypatch.setenv("QDISTRO_BRIDGE_INBOUND_TRUSTED_SCRIPTS",
                           "attacker.py")
        _fake_proc(monkeypatch,
                   exe="/usr/bin/python3",
                   argv=["/usr/bin/python3", "/home/user/attacker.py"])
        ok, reason = bb._inbound_caller_authorized(
            _AuthConn(my, 4242), ":1.55")
        assert ok is False
        assert "script-not-trusted" in reason


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


def _trust_caller(monkeypatch):
    """Make /proc attestation describe a legit qdistro daemon so the
    dispatch tests can isolate the op-allowlist / forward behavior."""
    _fake_proc(monkeypatch, **_LEGIT_MPRIS)


class TestInboundDispatchGate:
    def test_cross_uid_caller_denied_no_forward(self, monkeypatch):
        """A cross-uid caller is denied with AccessDenied and the op is
        never forwarded to the extension."""
        forwarded = []
        monkeypatch.setattr(
            bb, "enqueue_inbound_request",
            lambda *a, **k: forwarded.append(a) or {"ok": True})
        _trust_caller(monkeypatch)  # even an attestable daemon, cross-uid
        stop = threading.Event()
        my = os.getuid()
        conn = _DispatchConn(my + 1, 4242,
                             _request_tabs_msg("tabs.list", ":1.7"), stop)
        _run_serve(monkeypatch, conn, stop)
        assert forwarded == []
        assert ("error", "org.freedesktop.DBus.Error.AccessDenied") \
            in conn.sent

    def test_arbitrary_same_uid_caller_denied_no_forward(self, monkeypatch):
        """A same-uid caller that is NOT an attestable trusted daemon is
        denied before any forward — the finding-#7 regression at the
        dispatch layer."""
        forwarded = []
        monkeypatch.setattr(
            bb, "enqueue_inbound_request",
            lambda *a, **k: forwarded.append(a) or {"ok": True})
        _fake_proc(monkeypatch,
                   exe="/usr/bin/evil-tool",
                   argv=["/usr/bin/evil-tool"])
        stop = threading.Event()
        my = os.getuid()
        conn = _DispatchConn(
            my, 4242, _request_tabs_msg("tabs.list", ":1.7"), stop)
        _run_serve(monkeypatch, conn, stop)
        assert forwarded == []
        assert ("error", "org.freedesktop.DBus.Error.AccessDenied") \
            in conn.sent

    def test_unknown_op_denied_no_forward(self, monkeypatch):
        """An authorized (attested) caller asking for a non-allowlisted op
        is still denied before any forward."""
        forwarded = []
        monkeypatch.setattr(
            bb, "enqueue_inbound_request",
            lambda *a, **k: forwarded.append(a) or {"ok": True})
        _trust_caller(monkeypatch)
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
        _trust_caller(monkeypatch)
        stop = threading.Event()
        my = os.getuid()
        conn = _DispatchConn(
            my, 4242, _request_tabs_msg("page.extract", ":1.7"), stop)
        _run_serve(monkeypatch, conn, stop)
        assert forwarded == []

    def test_authorized_allowlisted_op_forwards(self, monkeypatch):
        """Positive control: attested trusted caller + allowlisted op is
        forwarded to the extension and returns a method_return."""
        forwarded = []
        monkeypatch.setattr(
            bb, "enqueue_inbound_request",
            lambda op, args, out, **k: forwarded.append(op)
            or {"ok": True, "op": op})
        _trust_caller(monkeypatch)
        stop = threading.Event()
        my = os.getuid()
        conn = _DispatchConn(
            my, 4242, _request_tabs_msg("tabs.list", ":1.7"), stop)
        _run_serve(monkeypatch, conn, stop)
        assert forwarded == ["tabs.list"]
        assert ("return", None) in conn.sent

    def test_mpris_control_forwards(self, monkeypatch):
        """mpris.control (real inbound op) from the attested mpris daemon
        is forwarded — guards the functional regression where it had been
        dropped from the allowlist."""
        forwarded = []
        monkeypatch.setattr(
            bb, "enqueue_inbound_request",
            lambda op, args, out, **k: forwarded.append(op)
            or {"ok": True, "op": op})
        _trust_caller(monkeypatch)
        stop = threading.Event()
        my = os.getuid()
        conn = _DispatchConn(
            my, 4242, _request_tabs_msg("mpris.control", ":1.7"), stop)
        _run_serve(monkeypatch, conn, stop)
        assert forwarded == ["mpris.control"]
        assert ("return", None) in conn.sent
