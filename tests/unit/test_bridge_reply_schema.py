"""Outbound reply allowlist (response-schema) tests for the bridge.

Source of truth: ``todo/security-hardening-carryforward.md`` §"Clipboard":
*"Forwarded daemon replies should be allowlisted by response schema, not
passed through after stripping identity fields by denylist."*

The bridge used to strip a fixed denylist of identity keys off each
daemon reply and forward everything else — a fail-OPEN posture: any
new/unexpected field a (more-trusted) daemon returns leaks to the
(less-trusted) extension. :func:`qdistro_browser_bridge._project_reply`
flips this to a per-op ALLOWLIST (fail CLOSED).

These tests assert:
  (a) the legitimate per-op fields survive projection;
  (b) an injected unexpected / sensitive daemon field is DROPPED (the
      key behavior change vs the old denylist);
  (c) an op with no registered schema forwards ONLY the envelope.

Every test mocks the D-Bus client; no session bus is required.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest


_MOD = (Path(__file__).resolve().parent.parent.parent
        / "browser_bridge" / "qdistro_browser_bridge.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge", _MOD)
bb = importlib.util.module_from_spec(spec)
sys.modules["qdistro_browser_bridge"] = bb
spec.loader.exec_module(bb)


ALLOWED = {
    "ppid": 100,
    "parent_exe": "/usr/lib64/firefox/firefox",
    "parent_selinux": "user_u:user_r:user_t:s0",
    "extension_id": "qdistro@qdistro.local",
    "allowed": True,
}


class FakeDBus(bb._BaseDBusClient):
    """Records every call; returns a canned reply."""

    def __init__(self, reply=None):
        self.calls = []
        self._reply = reply or {"ok": True}

    def call(self, bus, service, object_path, interface, method,
             signature, body):
        self.calls.append({"method": method, "body": body})
        return dict(self._reply)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    # These tests dispatch token-gated ops with a master-minted token and
    # no prior qdistro.handshake. Production rejects that no-handshake
    # master path (intent_token_no_handshake); it is permitted only under
    # QDISTRO_TEST_MODE=1.
    monkeypatch.setenv("QDISTRO_TEST_MODE", "1")
    bb.reset_session_secret()
    bb._pending.clear()
    yield
    bb._pending.clear()
    bb._dbus_client = None


def _mint_token(op, request_id="ti-1", at=None):
    ts = time.time() if at is None else at
    mac = bb._compute_token_hmac(request_id, ts, op)
    return {"request_id": request_id, "ts": ts, "op": op, "hmac": mac}


# A sensitive/unexpected field that no schema lists. Under the old
# denylist this would be forwarded verbatim to the extension; under the
# allowlist it must be dropped.
LEAK = "internal_audit_secret"


# =====================================================================
# (a) Legitimate fields survive  +  (b) injected leak is dropped
# =====================================================================

class TestProjectReplyUnit:
    """Direct unit tests of the projection function."""

    def test_envelope_always_survives(self):
        out = bb._project_reply(
            "pwd.save",
            {"ok": True, "op": "pwd.save", "error": None,
             "detail": "x", "request_id": "r1"})
        assert out == {"ok": True, "op": "pwd.save", "error": None,
                       "detail": "x", "request_id": "r1"}

    def test_schema_fields_survive(self):
        out = bb._project_reply(
            "pwd.fill",
            {"ok": True, "credentials": [{"username": "a"}],
             "fill_token": "tok"})
        assert out == {"ok": True,
                       "credentials": [{"username": "a"}],
                       "fill_token": "tok"}

    def test_unexpected_field_dropped(self):
        out = bb._project_reply(
            "pwd.fill",
            {"ok": True, "credentials": [], LEAK: "leaked",
             "vault_path": "/var/lib/qdistro/pwd.kdbx"})
        assert out == {"ok": True, "credentials": []}
        assert LEAK not in out
        assert "vault_path" not in out

    def test_no_schema_forwards_envelope_only(self):
        out = bb._project_reply(
            "totally.unknown.op",
            {"ok": True, "op": "totally.unknown.op",
             "secret_payload": "boom", "rows": [1, 2, 3]})
        assert out == {"ok": True, "op": "totally.unknown.op"}

    def test_identity_fields_not_in_any_schema(self):
        # Identity fields must never be allowlisted on the handler body;
        # dispatch re-stamps them separately for reflecting ops.
        for op, fields in bb._REPLY_SCHEMA.items():
            assert not (fields & bb._IDENTITY_FIELDS), op


# =====================================================================
# (a)+(b) via the real dispatch path, per op
# =====================================================================

class TestDispatchProjection:
    def test_pwd_fill_keeps_creds_drops_leak(self):
        bb._dbus_client = FakeDBus(reply={
            "ok": True,
            "credentials": [{"username": "alice", "password": "redacted"}],
            "fill_token": "ft-1",
            # daemon-internal fields the extension must NOT see:
            LEAK: "leaked", "vault": "passwords", "caller_pid": 4242})
        resp = bb.dispatch(
            {"op": "pwd.fill", "url": "https://example.com/",
             "intent_token": _mint_token("pwd.fill")}, ALLOWED)
        assert resp["ok"] is True
        assert resp["credentials"][0]["username"] == "alice"
        assert resp["fill_token"] == "ft-1"
        assert LEAK not in resp
        assert "vault" not in resp
        assert "caller_pid" not in resp

    def test_pwd_fill_confirm_keeps_creds(self):
        bb._dbus_client = FakeDBus(reply={
            "ok": True,
            "credentials": [{"username": "bob", "password": "s3cret",
                             "url": "https://x"}],
            LEAK: "leaked"})
        resp = bb.dispatch(
            {"op": "pwd.fill_confirm", "url": "https://x",
             "username": "bob", "fill_token": "ft",
             "intent_token": _mint_token("pwd.fill_confirm")}, ALLOWED)
        assert resp["credentials"][0]["password"] == "s3cret"
        assert LEAK not in resp

    def test_pwd_save_envelope_only(self):
        bb._dbus_client = FakeDBus(reply={
            "ok": True, "stored_path": "/var/lib/qdistro/pwd.kdbx",
            LEAK: "leaked"})
        resp = bb.dispatch(
            {"op": "pwd.save", "url": "https://x", "username": "u",
             "password": "p",
             "intent_token": _mint_token("pwd.save")}, ALLOWED)
        assert resp["ok"] is True
        assert resp["op"] == "pwd.save"
        assert "stored_path" not in resp
        assert LEAK not in resp

    def test_cookies_export_keeps_count_drops_payload(self):
        bb._dbus_client = FakeDBus(reply={
            "ok": True, "exported": 3,
            # A buggy/hostile daemon echoing cookie values back must not
            # leak them to the extension via the reply path.
            "cookies": [{"name": "sid", "value": "SECRET"}],
            LEAK: "leaked"})
        resp = bb.dispatch(
            {"op": "cookies.export", "domain": "example.com",
             "cookies": [{"name": "sid", "value": "SECRET"}],
             "intent_token": _mint_token("cookies.export")}, ALLOWED)
        assert resp["exported"] == 3
        assert "cookies" not in resp
        assert LEAK not in resp

    def test_page_extract_envelope_only(self):
        bb._dbus_client = FakeDBus(reply={
            "ok": True, "dest_uid": "1001",
            "audit_row": 99, LEAK: "leaked"})
        resp = bb.dispatch(
            {"op": "page.extract", "url": "https://x", "dest_uid": "u",
             "intent_token": _mint_token("page.extract")}, ALLOWED)
        assert resp["ok"] is True
        assert "dest_uid" not in resp
        assert "audit_row" not in resp
        assert LEAK not in resp

    def test_handshake_keeps_secret_drops_leak(self):
        resp = bb.dispatch({"op": "qdistro.handshake"}, ALLOWED)
        assert resp["ok"] is True
        assert len(resp["session_secret_hex"]) == 64
        assert resp["hmac_algo"] == "sha256"
        assert resp["token_ttl_s"] == bb.INTENT_TOKEN_TTL_S
        # extension_id is re-stamped by dispatch, not from the body.
        assert resp["extension_id"] == ALLOWED["extension_id"]
        # No identity leak.
        for k in bb._IDENTITY_FIELDS:
            if k == "extension_id":
                continue
            assert k not in resp

    def test_ping_reflects_identity_drops_leak(self):
        resp = bb.dispatch(
            {"op": "qdistro.ping", "echo": "hi"}, ALLOWED)
        assert resp["pong"] is True
        assert resp["echo"] == "hi"
        # Re-stamped from the bridge's view, not the stdio payload.
        assert resp["parent_exe"] == ALLOWED["parent_exe"]
        assert resp["extension_id"] == ALLOWED["extension_id"]

    def test_heartbeat_ack_keeps_matched(self):
        resp = bb.dispatch(
            {"op": "qdistro.heartbeat.ack", "request_id": "x"}, ALLOWED)
        assert resp["ok"] is True
        assert resp["matched"] is False

    def test_desktop_op_envelope_plus_stub(self):
        bb._dbus_client = FakeDBus(reply={
            "ok": True, "stub": True, "daemon_pid": 1234, LEAK: "leaked"})
        resp = bb.dispatch(
            {"op": "mpris.publish", "title": "T"}, ALLOWED)
        assert resp["ok"] is True
        assert resp.get("stub") is True
        assert "daemon_pid" not in resp
        assert LEAK not in resp

    def test_tabs_reply_keeps_delivered(self):
        resp = bb.dispatch(
            {"op": "tabs.list.reply", "request_id": "none",
             "tabs": [{"id": 1}], LEAK: "leaked"}, ALLOWED)
        assert resp["ok"] is True
        assert "delivered" in resp
        # The *.reply landing must not echo arbitrary ext-supplied data.
        assert "tabs" not in resp
        assert LEAK not in resp


# =====================================================================
# (c) Error replies still pass through (envelope is universal)
# =====================================================================

class TestErrorEnvelopeSurvives:
    def test_handler_error_passes(self):
        bb._dbus_client = FakeDBus(reply={
            "ok": False, "error": "vault_locked",
            # daemon detail that should NOT be forwarded:
            "internal_trace": "stack...", LEAK: "leaked"})
        resp = bb.dispatch(
            {"op": "pwd.fill", "url": "https://x",
             "intent_token": _mint_token("pwd.fill")}, ALLOWED)
        assert resp["ok"] is False
        assert resp["error"] == "vault_locked"
        assert "internal_trace" not in resp
        assert LEAK not in resp

    def test_unknown_op_envelope(self):
        resp = bb.dispatch({"op": "qdistro.fake"}, ALLOWED)
        assert resp["ok"] is False
        assert resp["error"] == "unknown_op"
        assert resp["op"] == "qdistro.fake"
