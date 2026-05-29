"""Regression tests pinning two browser-bridge identity invariants.

Source: ``todo/security-hardening-carryforward.md`` §"Clipboard".

Invariant 1 — daemon bus targeting (fail on session-bus drift)
    ``qdistro-pwd`` is ``org.qdistro.Pwd1`` on the *SYSTEM* bus, and the
    admin broker lives on the SYSTEM bus too. Browser-bridge *requests*
    arrive on the user *session* bus, but the outbound daemon calls must
    target the daemon's owning (SYSTEM) bus. If someone flips the
    pwd/broker bus kind to ``"SESSION"``, a same-uid process could claim
    the well-known name and serve forged replies. These tests fail if the
    bus kind drifts to session — both at the module-constant level and at
    the actual ``_dbus_client.call`` boundary.

Invariant 2 — inbound name ownership fails closed
    The inbound D-Bus surface (``inbound_dbus_serve``) must own its
    well-known name (``org.qdistro.BrowserBridge.<ppid>``) as PRIMARY
    OWNER with ``DO_NOT_QUEUE``. If ``RequestName`` returns anything other
    than ``1`` (PRIMARY_OWNER) — IN_QUEUE / EXISTS / ALREADY_OWNER — the
    bridge must refuse to serve (return without entering the dispatch
    loop) rather than queueing behind / coexisting with a same-uid
    impostor. These tests fail if the fail-closed guard is removed or
    relaxed.

Both surfaces are exercised against fakes; no real D-Bus is required.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest


_MOD = (Path(__file__).resolve().parent.parent.parent
        / "browser_bridge" / "qdistro_browser_bridge.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge", _MOD)
bb = importlib.util.module_from_spec(spec)
sys.modules["qdistro_browser_bridge"] = bb
spec.loader.exec_module(bb)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

ALLOWED = {
    "allowed": True,
    "extension_id": "ext-allowed",
    "parent_exe": "/usr/bin/qdbrowser",
}


def _mint_token(op: str) -> dict:
    """Mint a valid single-use intent token for ``op``.

    Computed via the bridge's own :func:`_compute_token_hmac` against the
    master session secret. ``ALLOWED`` has no handshake on record, so
    :func:`verify_intent_token` falls back to the master secret — matching
    the wire format produced here. Mirrors the helper in
    ``test_pwd_fill_bridge.py``.
    """
    import secrets
    import time
    req_id = secrets.token_hex(16)
    ts = time.time()
    mac = bb._compute_token_hmac(req_id, ts, op)
    return {"request_id": req_id, "ts": ts, "op": op, "hmac": mac}


class _RecordingDBus(bb._BaseDBusClient):
    """Records every outbound ``call`` and returns a canned reply."""

    def __init__(self, reply=None):
        self.reply = reply if reply is not None else {"ok": True}
        self.calls = []

    def call(self, bus, service, object_path, interface, method,
             signature, body):
        self.calls.append({
            "bus": bus, "service": service, "object_path": object_path,
            "interface": interface, "method": method,
            "signature": signature, "body": body,
        })
        return dict(self.reply)


@pytest.fixture(autouse=True)
def _reset_client():
    bb._dbus_client = None
    bb.reset_session_secret()
    yield
    bb._dbus_client = None


# ===========================================================================
# Invariant 1 — daemon bus targeting must be SYSTEM, never session
# ===========================================================================

class TestDaemonBusIsSystem:
    """The pwd + broker daemons live on the SYSTEM bus. A regression that
    routes those ops onto SESSION is what these tests are built to catch.
    """

    def test_pwd_bus_constant_is_system(self):
        # Module-level guard: flipping this to "SESSION" trips here even
        # if a handler refactor stops threading the constant through.
        assert bb._PWD_BUS_KIND == "SYSTEM"

    def test_broker_bus_constant_is_system(self):
        assert bb._BROKER_BUS_KIND == "SYSTEM"

    def test_pwd_fill_call_targets_system_bus(self):
        """End-to-end at the client boundary: pwd.fill must hand the
        outbound call ``bus="SYSTEM"``. If the handler is rewired to pass
        SESSION (or _PWD_BUS_KIND flips), this assertion fails.
        """
        fake = _RecordingDBus(reply={"ok": True, "credentials": []})
        bb._dbus_client = fake
        out = bb._handle_pwd_fill(
            {"url": "https://example.com/login",
             "intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        assert out["ok"] is True
        assert len(fake.calls) == 1
        c = fake.calls[0]
        assert c["bus"] == "SYSTEM", (
            "pwd.fill regressed onto a non-SYSTEM bus: "
            f"{c['bus']!r}")
        assert c["service"] == "org.qdistro.Pwd1"
        assert c["method"] == "Fill"

    def test_pwd_fill_confirm_call_targets_system_bus(self):
        fake = _RecordingDBus(reply={"ok": True})
        bb._dbus_client = fake
        bb._handle_pwd_fill_confirm(
            {"url": "https://example.com/login",
             "username": "alice",
             "fill_token": "ft-1",
             "intent_token": _mint_token("pwd.fill_confirm")},
            ALLOWED)
        assert len(fake.calls) == 1
        assert fake.calls[0]["bus"] == "SYSTEM"
        assert fake.calls[0]["method"] == "FillConfirm"

    def test_pwd_save_call_targets_system_bus(self):
        fake = _RecordingDBus(reply={"ok": True})
        bb._dbus_client = fake
        bb._handle_pwd_save(
            {"url": "https://example.com/login",
             "username": "alice",
             "password": "s3cret",
             "intent_token": _mint_token("pwd.save")},
            ALLOWED)
        assert len(fake.calls) == 1
        assert fake.calls[0]["bus"] == "SYSTEM"

    def test_broker_page_extract_call_targets_system_bus(self):
        """Broker boundary: the admin broker (``org.qdistro.AdminBroker1``)
        lives on SYSTEM. Drive a broker op (``page.extract``) through
        ``dispatch`` and pin the outbound call's bus so this file catches
        a broker regression on its own, not just via the constant.
        """
        fake = _RecordingDBus(reply={"ok": True, "delivery_id": "x"})
        bb._dbus_client = fake
        resp = bb.dispatch(
            {"op": "page.extract",
             "url": "https://example.com/article",
             "title": "Hello", "selected_text": "lorem",
             "dest_uid": "dev-user",
             "intent_token": _mint_token("page.extract")},
            ALLOWED)
        assert resp["ok"] is True
        assert len(fake.calls) == 1
        c = fake.calls[0]
        assert c["bus"] == "SYSTEM", (
            "broker op regressed onto a non-SYSTEM bus: "
            f"{c['bus']!r}")
        assert c["service"] == "org.qdistro.AdminBroker1"

    def test_jeepney_client_rejects_unknown_bus_kind(self):
        """Belt-and-suspenders: the production jeepney client only accepts
        SESSION/SYSTEM; a typo'd / unknown bus kind is refused outright
        (no silent default). NOTE: ``"SESSION"`` is *intentionally* valid
        here (other daemons + the inbound surface use it) — this guards
        the validation gate, not the daemon-bus invariant.
        """
        client = bb._JeepneyDBusClient()
        bad = client.call(
            "SYSTEMM", "org.qdistro.Pwd1", "/org/qdistro/Pwd1",
            "org.qdistro.Pwd1", "Fill", "s", ("{}",))
        assert bad["ok"] is False
        assert bad["error"] == "bad_bus"


# ===========================================================================
# Invariant 2 — inbound name ownership must fail closed
# ===========================================================================

class _FakeReply:
    def __init__(self, code):
        self.body = (code,)


class _FakeConn:
    """Stand-in for a jeepney blocking connection.

    ``send_and_get_reply`` is used once for ``RequestName`` and returns a
    reply whose ``body[0]`` is the configured RequestName result code.
    ``receive`` records that the dispatch loop was entered. Individual
    tests replace ``receive`` with a side-effecting version that flips the
    stop event so the loop (if reached) terminates deterministically.
    """

    def __init__(self, request_name_code):
        self.request_name_code = request_name_code
        self.receive_calls = 0
        self.closed = False

    def send_and_get_reply(self, msg, timeout=None):
        return _FakeReply(self.request_name_code)

    def receive(self, timeout=None):
        self.receive_calls += 1
        return None

    def send(self, msg):
        pass

    def close(self):
        self.closed = True


def _patch_jeepney(monkeypatch, conn):
    """Point ``inbound_dbus_serve``'s lazily-imported jeepney symbols at
    fakes so no real bus is touched.
    """
    import jeepney
    import jeepney.bus_messages as bus_messages
    import jeepney.io.blocking as blocking

    monkeypatch.setattr(
        blocking, "open_dbus_connection",
        lambda bus: conn)

    # RequestName just needs to produce *some* message object; the fake
    # conn ignores it and replies with the configured code.
    monkeypatch.setattr(
        bus_messages.message_bus, "RequestName",
        lambda name, flags: ("RequestName", name, flags))
    # MessageType / new_* are imported but only used inside the loop;
    # leave the real ones in place.


class TestInboundNameOwnershipFailsClosed:
    """RequestName result handling for ``inbound_dbus_serve``.

    Only ``1`` (PRIMARY_OWNER) is acceptable. Everything else must abort
    before the dispatch loop. These tests fail if the
    ``if request_name_result != 1: return`` guard is removed/relaxed.
    """

    # D-Bus RequestName reply codes:
    #   1 = PRIMARY_OWNER (good), 2 = IN_QUEUE, 3 = EXISTS,
    #   4 = ALREADY_OWNER.
    @pytest.mark.parametrize("code", [2, 3, 4, 0])
    def test_non_primary_owner_refuses_to_serve(self, monkeypatch, code):
        conn = _FakeConn(request_name_code=code)
        _patch_jeepney(monkeypatch, conn)
        stop = threading.Event()

        # IMPORTANT: do NOT pre-set the stop event. If we did, a *broken*
        # guard that falls into the dispatch loop would still see
        # ``stop_event.is_set()`` true on entry and skip the loop body,
        # leaving ``receive_calls == 0`` and making this test vacuous.
        # Instead we leave the event clear and have ``receive`` set it on
        # its first call. With the guard intact the loop is never entered
        # (receive_calls stays 0); with the guard broken the loop runs
        # once, receive fires (receive_calls == 1) and sets stop so the
        # loop then exits — and the assertion below catches the breach.
        def _receive_then_stop(timeout=None):
            conn.receive_calls += 1
            stop.set()
            return None

        conn.receive = _receive_then_stop

        bb.inbound_dbus_serve(
            ppid=4242, out_stream=None, stop_event=stop,
            request_timeout_s=1.0)

        # Fail-closed: dispatch loop was never entered.
        assert conn.receive_calls == 0, (
            f"bridge served requests despite RequestName={code}; "
            "fail-closed guard is broken")
        assert conn.closed is True

    def test_primary_owner_enters_dispatch_loop(self, monkeypatch):
        """Positive control: code 1 (PRIMARY_OWNER) proceeds into the
        receive loop. This pins that the guard isn't simply rejecting
        everything (which would make the negative test vacuous).
        """
        conn = _FakeConn(request_name_code=1)
        _patch_jeepney(monkeypatch, conn)
        stop = threading.Event()

        # Stop the loop on the first poll so the test terminates, but
        # only *after* the loop body has run at least once. We flip the
        # event from inside receive via a side-effecting fake.
        original_receive = conn.receive

        def _receive_then_stop(timeout=None):
            stop.set()
            return original_receive(timeout=timeout)

        conn.receive = _receive_then_stop

        bb.inbound_dbus_serve(
            ppid=4242, out_stream=None, stop_event=stop,
            request_timeout_s=1.0)

        assert conn.receive_calls >= 1, (
            "PRIMARY_OWNER (1) should proceed into the dispatch loop")
        assert conn.closed is True

    def test_inbound_surface_uses_session_bus(self, monkeypatch):
        """The inbound *request* surface (browser → bridge) lives on the
        SESSION bus — that's where the extension/browser can reach it.
        (Contrast with the SYSTEM-bus *daemon* calls in Invariant 1.)
        Pin the bus kind so the two surfaces don't get swapped.
        """
        seen = {}
        import jeepney.io.blocking as blocking
        import jeepney.bus_messages as bus_messages

        conn = _FakeConn(request_name_code=1)

        def _capture_open(bus):
            seen["bus"] = bus
            return conn

        monkeypatch.setattr(blocking, "open_dbus_connection", _capture_open)
        monkeypatch.setattr(
            bus_messages.message_bus, "RequestName",
            lambda name, flags: ("RequestName", name, flags))

        stop = threading.Event()

        def _receive_then_stop(timeout=None):
            stop.set()
            return None

        conn.receive = _receive_then_stop
        bb.inbound_dbus_serve(
            ppid=99, out_stream=None, stop_event=stop,
            request_timeout_s=1.0)

        assert seen.get("bus") == "SESSION"

    def test_requestname_uses_do_not_queue_flag(self, monkeypatch):
        """The DO_NOT_QUEUE flag (0x04) is what makes a same-uid
        impostor's prior ownership surface as a non-primary result
        instead of silently queueing. Pin the flag value.
        """
        captured = {}
        import jeepney.io.blocking as blocking
        import jeepney.bus_messages as bus_messages

        conn = _FakeConn(request_name_code=1)
        monkeypatch.setattr(
            blocking, "open_dbus_connection", lambda bus: conn)

        def _capture_requestname(name, flags):
            captured["name"] = name
            captured["flags"] = flags
            return ("RequestName", name, flags)

        monkeypatch.setattr(
            bus_messages.message_bus, "RequestName", _capture_requestname)

        stop = threading.Event()

        def _receive_then_stop(timeout=None):
            stop.set()
            return None

        conn.receive = _receive_then_stop
        bb.inbound_dbus_serve(
            ppid=4242, out_stream=None, stop_event=stop,
            request_timeout_s=1.0)

        assert captured.get("flags") == 4, (
            "RequestName must use DO_NOT_QUEUE (0x04); without it the "
            "bridge silently queues behind a same-uid impostor")
        assert captured.get("name") == "org.qdistro.BrowserBridge.4242"
