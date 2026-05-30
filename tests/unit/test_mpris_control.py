"""Unit tests for the MPRIS daemon control path + qdbrowser allowance.

Covers the two residue items from
``todo/open-followups.md`` §"Browser Phase-9e + qdbrowser unification":

  1. The MPRIS ``mpris.control`` method bodies — the exported
     ``org.mpris.MediaPlayer2.Player`` Play/Pause/etc dispatch into the
     pure ``handle_control`` core, which routes the verb back to the
     originating bridge connection through ``_BridgeClient``.
  2. The explicit, narrow daemon-side qdbrowser forward allowance
     (``qdbrowser_forwarder_allowed`` / ``daemon_forward_allowed``) so a
     qdbrowser→daemon forward is accepted for the real qdbrowser process
     and rejected ``parent_not_allowed`` for anything else.

All dispatch logic is pure: the bridge D-Bus ``call`` and the /proc
readers are injected so nothing here touches a real session bus or
/proc layout. Tests are mutation-sensitive — a bad verb, a wrong
script, or a mis-routed op all flip an assertion.
"""
from __future__ import annotations

import json

import qdistro_browser_daemon_identity as ident
import qdistro_mpris_daemon as mpris


# --------------------------------------------------------------------- #
# _BridgeClient — routes the verb to the bound bridge bus name
# --------------------------------------------------------------------- #

class _RecordingCall:
    """Records every ``(bus_name, op, args_json)`` and returns a reply."""

    def __init__(self, reply=None, raise_exc=None):
        self.calls = []
        self._reply = reply if reply is not None else {"ok": True}
        self._raise = raise_exc

    def __call__(self, bus_name, op, args_json):
        if self._raise is not None:
            raise self._raise
        self.calls.append({"bus_name": bus_name, "op": op,
                           "args": json.loads(args_json)})
        return dict(self._reply)


class TestBridgeClient:
    def test_control_forwards_mpris_control_op(self):
        call = _RecordingCall({"ok": True, "action": "playpause"})
        bc = mpris._BridgeClient("org.qdistro.BrowserBridge.42", call=call)
        reply = bc.control(1000, "firefox", "playpause", "7")
        assert reply["ok"] is True
        assert len(call.calls) == 1
        c = call.calls[0]
        # Addressed at the bound bridge connection, not anywhere else.
        assert c["bus_name"] == "org.qdistro.BrowserBridge.42"
        # The bridge inbound op the extension routes to the tab.
        assert c["op"] == "mpris.control"
        # The extension's handleMprisControlInbound keys off these.
        assert c["args"]["action"] == "playpause"
        assert c["args"]["tab_id"] == "7"
        assert c["args"]["uid"] == 1000
        assert c["args"]["browser"] == "firefox"

    def test_control_no_bridge_fails_closed(self):
        # Empty bus name (unresolvable PPid at publish time) -> no route.
        call = _RecordingCall()
        bc = mpris._BridgeClient("", call=call)
        reply = bc.control(0, "firefox", "play", "1")
        assert reply["ok"] is False
        assert reply["error"] == "no_bridge"
        assert call.calls == []  # nothing dispatched


# --------------------------------------------------------------------- #
# handle_control — pure verb validation + routing
# --------------------------------------------------------------------- #

class TestHandleControlRouting:
    def test_each_valid_verb_routes(self):
        for verb in ("play", "pause", "playpause", "next", "previous",
                     "stop"):
            call = _RecordingCall()
            bc = mpris._BridgeClient("org.qdistro.BrowserBridge.5", call=call)
            reply = mpris.handle_control(
                verb, uid=0, browser="chromium", tab_id="3",
                bridge_client=bc)
            assert reply["ok"] is True, verb
            assert call.calls[0]["args"]["action"] == verb

    def test_verb_is_normalised_before_routing(self):
        call = _RecordingCall()
        bc = mpris._BridgeClient("org.qdistro.BrowserBridge.5", call=call)
        mpris.handle_control("  PlayPause  ", uid=0, browser="x", tab_id="1",
                             bridge_client=bc)
        assert call.calls[0]["args"]["action"] == "playpause"

    def test_bad_verb_rejected_without_routing(self):
        call = _RecordingCall()
        bc = mpris._BridgeClient("org.qdistro.BrowserBridge.5", call=call)
        reply = mpris.handle_control(
            "format_disk", uid=0, browser="x", tab_id="1", bridge_client=bc)
        assert reply["ok"] is False
        assert reply["error"] == "bad_action"
        assert call.calls == []  # never reached the bridge

    def test_bridge_transport_error_is_envelope(self):
        call = _RecordingCall(raise_exc=RuntimeError("bus gone"))
        bc = mpris._BridgeClient("org.qdistro.BrowserBridge.5", call=call)
        reply = mpris.handle_control(
            "play", uid=0, browser="x", tab_id="1", bridge_client=bc)
        assert reply["ok"] is False
        assert reply["error"] == "control_failed"


# --------------------------------------------------------------------- #
# bridge_bus_name_for_pid — derive the inbound name from the attested pid
# --------------------------------------------------------------------- #

class TestBridgeBusNameDerivation:
    def test_uses_ppid_of_caller(self):
        # The bridge owns BrowserBridge.<its own ppid>; we read that PPid
        # from the attested bridge pid.
        name = mpris.bridge_bus_name_for_pid(
            999, ppid_reader=lambda pid: 555 if pid == 999 else None)
        assert name == "org.qdistro.BrowserBridge.555"

    def test_unreadable_ppid_fails_closed(self):
        name = mpris.bridge_bus_name_for_pid(
            999, ppid_reader=lambda pid: None)
        assert name == ""


# --------------------------------------------------------------------- #
# qdbrowser forward allowance — explicit + narrow
# --------------------------------------------------------------------- #

class TestQdbrowserForwarderAllowed:
    QDB = "/usr/bin/qdbrowser"

    def _gate(self, *, cmdline, scripts=(QDB,)):
        return ident.qdbrowser_forwarder_allowed(
            4321, scripts=scripts,
            cmdline_reader=lambda _pid: cmdline)

    def test_real_qdbrowser_allowed(self):
        ok, reason = self._gate(
            cmdline=["python3", self.QDB, "--new-window"])
        assert ok is True
        assert reason == "qdbrowser"

    def test_valueless_flags_tolerated(self):
        ok, _ = self._gate(cmdline=["python3", "-I", "-S", self.QDB])
        assert ok is True

    def test_operand_flag_fails_closed(self):
        # -W consumes the next token; the qdbrowser path could be smuggled
        # as the flag operand while python runs a different file.
        ok, reason = self._gate(
            cmdline=["python3", "-W", self.QDB, "evil.py"])
        assert ok is False
        assert reason == "not-qdbrowser"

    def test_wrong_script_denied(self):
        ok, reason = self._gate(
            cmdline=["python3", "/tmp/evil.py", self.QDB])
        assert ok is False
        assert reason == "not-qdbrowser"

    def test_empty_allowlist_fails_closed(self):
        ok, reason = self._gate(
            cmdline=["python3", self.QDB], scripts=())
        assert ok is False
        assert reason == "qdbrowser-not-configured"


# --------------------------------------------------------------------- #
# daemon_forward_allowed — accept bridge OR qdbrowser, else fail closed
# --------------------------------------------------------------------- #

class TestDaemonForwardAllowed:
    def test_bridge_caller_allowed(self):
        ok, reason = ident.daemon_forward_allowed(
            7,
            bridge_gate=lambda pid: (True, "browser-bridge"),
            qdbrowser_gate=lambda pid: (False, "not-qdbrowser"))
        assert ok is True
        assert reason == "browser-bridge"

    def test_qdbrowser_caller_allowed(self):
        ok, reason = ident.daemon_forward_allowed(
            7,
            bridge_gate=lambda pid: (False, "not-browser-bridge"),
            qdbrowser_gate=lambda pid: (True, "qdbrowser"))
        assert ok is True
        assert reason == "qdbrowser"

    def test_neither_fails_closed(self):
        ok, reason = ident.daemon_forward_allowed(
            7,
            bridge_gate=lambda pid: (False, "not-browser-bridge"),
            qdbrowser_gate=lambda pid: (False, "not-qdbrowser"))
        assert ok is False
        # Surfaces the bridge-gate reason (primary path) for audit parity.
        assert reason == "not-browser-bridge"


# --------------------------------------------------------------------- #
# End-to-end: a qdbrowser forward is accepted; a stranger is rejected
# --------------------------------------------------------------------- #

class _Pub(mpris._BasePublisher):
    def __init__(self):
        self.calls = []

    def publish(self, player_name, playback_status, metadata, *,
                bridge_name="", uid=-1, browser=""):
        self.calls.append((player_name, bridge_name, uid, browser))


class TestPublishForwardAllowanceEndToEnd:
    QDB = "/usr/bin/qdbrowser"

    def _gate(self, cmdline):
        # Real combined gate, driven through injected /proc readers: the
        # bridge gate denies (no native-host script), the qdbrowser gate
        # decides on the executed script.
        def bridge(pid):
            return ident.browser_bridge_allowed(
                pid, bridge_script="/usr/libexec/qdistro/x.py",
                parent_exes=("/usr/bin/firefox",),
                cmdline_reader=lambda _p: cmdline,
                ppid_reader=lambda _p: None,
                exe_reader=lambda _p: "")

        def qdb(pid):
            return ident.qdbrowser_forwarder_allowed(
                pid, scripts=(self.QDB,),
                cmdline_reader=lambda _p: cmdline)

        return lambda pid: ident.daemon_forward_allowed(
            pid, bridge_gate=bridge, qdbrowser_gate=qdb)

    def test_qdbrowser_publish_accepted(self):
        pub = _Pub()
        reply = mpris.handle_publish(
            {"title": "Song", "parent_exe": "qdbrowser", "tab_id": 9},
            caller_uid=1000, caller_pid=4321, publisher=pub,
            bridge_gate=self._gate(["python3", self.QDB]),
            bridge_name_fn=lambda pid: "org.qdistro.BrowserBridge.0")
        assert reply["ok"] is True
        assert pub.calls and pub.calls[0][2] == 1000

    def test_stranger_publish_rejected(self):
        pub = _Pub()
        reply = mpris.handle_publish(
            {"title": "Song", "parent_exe": "qdbrowser"},
            caller_uid=1000, caller_pid=4321, publisher=pub,
            bridge_gate=self._gate(["python3", "/tmp/evil.py"]),
            bridge_name_fn=lambda pid: "org.qdistro.BrowserBridge.0")
        assert reply["ok"] is False
        assert reply["error"] == "parent_not_allowed"
        assert pub.calls == []
