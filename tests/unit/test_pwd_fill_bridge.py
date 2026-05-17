"""P04 Phase-C unit tests: pwd.fill end-to-end (intent token → daemon).

Drives :func:`qdistro_browser_bridge._handle_pwd_fill` from valid
intent token mint through the (mocked) ``com.qdistro.Pwd1.Fill``
daemon call and asserts:

  * intent-token replay is rejected on second use,
  * future-dated tokens are rejected,
  * the daemon receives the URL + extension_id + parent_exe in the
    forwarded body,
  * the bridge strips identity fields from the reply that flows back
    to the extension (kernel-attested fields must NOT leak),
  * a daemon-side ``vault_locked`` reply makes its way through
    unmodified so the qdbrowser orchestrator can surface the right
    error.

Mocks the D-Bus client via the bridge's ``_dbus_client`` injection
point; no session bus is required.
"""
from __future__ import annotations

import importlib.util
import json
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALLOWED = {
    "ppid": 100,
    "parent_exe": "/usr/lib64/firefox/firefox",
    "parent_selinux": "user_u:user_r:user_t:s0",
    "extension_id": "qdistro@qdistro.local",
    "allowed": True,
}


class _FakeDBus(bb._BaseDBusClient):
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    def call(self, bus, service, object_path, interface, method,
             signature, body):
        self.calls.append({
            "bus": bus, "service": service,
            "object_path": object_path,
            "interface": interface, "method": method,
            "signature": signature, "body": body,
        })
        if callable(self._reply):
            return self._reply(body)
        return dict(self._reply)


@pytest.fixture(autouse=True)
def _fresh_session_secret():
    """Rotate the bridge's HMAC secret per test so intent tokens
    minted in one test can't accidentally validate against the next
    test's secret. Also clears the per-extension secret registry."""
    bb.reset_session_secret()
    bb._EXT_SECRETS.clear()
    yield
    bb._dbus_client = None
    bb._EXT_SECRETS.clear()


def _mint_token(op: str, *, ts_offset: float = 0.0) -> dict:
    """Mint a valid intent token against the live session secret.

    The bridge's :func:`verify_intent_token` recomputes the HMAC
    against ``_SESSION_SECRET`` — we just call the bridge's own
    :func:`_compute_token_hmac` so the wire format always matches.
    """
    import secrets
    req_id = secrets.token_hex(16)
    ts = time.time() + ts_offset
    mac = bb._compute_token_hmac(req_id, ts, op)
    return {"request_id": req_id, "ts": ts, "op": op, "hmac": mac}


# ---------------------------------------------------------------------------
# pwd.fill — happy path + identity forwarding
# ---------------------------------------------------------------------------

class TestPwdFillHappyPath:
    def test_credentials_returned(self):
        bb._dbus_client = _FakeDBus(reply={
            "ok": True,
            "credentials": [{"username": "alice", "password": "s3cret"}],
        })
        out = bb._handle_pwd_fill(
            {"url": "https://example.com/",
             "intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        assert out["ok"] is True
        assert out["credentials"][0]["username"] == "alice"

    def test_daemon_receives_url_and_identity(self):
        fake = _FakeDBus(reply={"ok": True, "credentials": []})
        bb._dbus_client = fake
        bb._handle_pwd_fill(
            {"url": "https://example.com/login",
             "intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        assert len(fake.calls) == 1
        body_json = fake.calls[0]["body"][0]
        body = json.loads(body_json)
        assert body["url"] == "https://example.com/login"
        assert body["extension_id"] == ALLOWED["extension_id"]
        assert body["parent_exe"] == ALLOWED["parent_exe"]

    def test_dbus_target_is_pwd_daemon(self):
        fake = _FakeDBus(reply={"ok": True})
        bb._dbus_client = fake
        bb._handle_pwd_fill(
            {"url": "https://example.com/",
             "intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        c = fake.calls[0]
        # SYSTEM bus is load-bearing — the pwd daemon ships on SYSTEM.
        # A regression that routes pwd ops onto SESSION lets any
        # same-uid process claim the name and serve forged Fill replies
        # (P04 H3 / HIGH-1 review). Pin all four bus-coordinates here.
        assert c["bus"] == "SYSTEM"
        assert c["service"] == "com.qdistro.Pwd1"
        assert c["object_path"] == "/com/qdistro/Pwd1"
        assert c["interface"] == "com.qdistro.Pwd1"
        assert c["method"] == "Fill"


# ---------------------------------------------------------------------------
# pwd.fill — gating / failure modes
# ---------------------------------------------------------------------------

class TestPwdFillGates:
    def test_parent_not_allowed(self):
        denied = dict(ALLOWED, allowed=False)
        out = bb._handle_pwd_fill(
            {"url": "https://example.com/",
             "intent_token": _mint_token("pwd.fill")},
            denied)
        assert out["ok"] is False
        assert out["error"] == "parent_not_allowed"

    def test_missing_url(self):
        bb._dbus_client = _FakeDBus(reply={"ok": True})
        out = bb._handle_pwd_fill(
            {"intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        assert out["ok"] is False
        assert out["error"] == "missing_url"

    def test_intent_token_missing(self):
        out = bb._handle_pwd_fill(
            {"url": "https://example.com/"}, ALLOWED)
        assert out["ok"] is False
        assert out["error"] == "missing_intent_token"

    def test_intent_token_wrong_op(self):
        tok = _mint_token("pwd.save")  # wrong op
        out = bb._handle_pwd_fill(
            {"url": "https://example.com/", "intent_token": tok},
            ALLOWED)
        assert out["ok"] is False
        assert out["error"] == "intent_token_op_mismatch"

    def test_intent_token_expired(self):
        tok = _mint_token("pwd.fill", ts_offset=-60.0)
        out = bb._handle_pwd_fill(
            {"url": "https://example.com/", "intent_token": tok},
            ALLOWED)
        assert out["ok"] is False
        assert out["error"] == "intent_token_expired"

    def test_intent_token_future(self):
        tok = _mint_token("pwd.fill", ts_offset=60.0)
        out = bb._handle_pwd_fill(
            {"url": "https://example.com/", "intent_token": tok},
            ALLOWED)
        assert out["ok"] is False
        assert out["error"] == "intent_token_future"

    def test_intent_token_bad_hmac(self):
        tok = _mint_token("pwd.fill")
        tok["hmac"] = "deadbeef" * 8
        out = bb._handle_pwd_fill(
            {"url": "https://example.com/", "intent_token": tok},
            ALLOWED)
        assert out["ok"] is False
        assert out["error"] == "intent_token_bad_hmac"

    def test_intent_token_replay_rejected(self):
        bb._dbus_client = _FakeDBus(reply={"ok": True, "credentials": []})
        tok = _mint_token("pwd.fill")
        # First call: token consumed.
        first = bb._handle_pwd_fill(
            {"url": "https://example.com/", "intent_token": dict(tok)},
            ALLOWED)
        assert first["ok"] is True
        # Replay: same request_id, same body → replay reject.
        second = bb._handle_pwd_fill(
            {"url": "https://example.com/", "intent_token": dict(tok)},
            ALLOWED)
        assert second["ok"] is False
        assert second["error"] == "intent_token_replay"


# ---------------------------------------------------------------------------
# pwd.fill — daemon failure pass-through
# ---------------------------------------------------------------------------

class TestPwdFillDaemonFailures:
    def test_vault_locked_passes_through(self):
        bb._dbus_client = _FakeDBus(
            reply={"ok": False, "error": "vault_locked"})
        out = bb._handle_pwd_fill(
            {"url": "https://example.com/",
             "intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        assert out["ok"] is False
        assert out["error"] == "vault_locked"

    def test_identity_stripped_from_reply(self):
        # A misbehaving daemon includes kernel-attested fields. The
        # bridge must NOT leak them back to the extension.
        # Strip is centralised in dispatch() (per P04 fix-pass L4) so
        # we drive through dispatch to assert the invariant.
        bb._dbus_client = _FakeDBus(reply={
            "ok": True,
            "credentials": [],
            "ppid": 999,
            "parent_exe": "spoofed",
            "extension_id": "evil",
        })
        out = bb.dispatch(
            {"op": "pwd.fill",
             "url": "https://example.com/",
             "intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        assert "ppid" not in out
        assert "parent_exe" not in out
        assert "extension_id" not in out


# ---------------------------------------------------------------------------
# Handshake — per-extension secret binding (P04 fix-pass H2)
# ---------------------------------------------------------------------------

class TestHandshakePerExtensionBinding:
    """The handshake reply must bind the returned secret to the
    argv-attested ``extension_id``. Two extensions sharing the same
    bridge process get distinct secrets; a token minted against
    extension A's secret is rejected when verified against B's.
    """

    def test_two_extensions_get_distinct_secrets(self):
        id_a = "a" * 32
        id_b = "b" * 32
        reply_a = bb._handle_handshake({}, dict(ALLOWED, extension_id=id_a))
        reply_b = bb._handle_handshake({}, dict(ALLOWED, extension_id=id_b))
        assert reply_a["ok"] is True and reply_b["ok"] is True
        assert (reply_a["session_secret_hex"]
                != reply_b["session_secret_hex"])
        # The reply binds the id explicitly so the orchestrator can
        # detect a misrouted handshake.
        assert reply_a["extension_id"] == id_a
        assert reply_b["extension_id"] == id_b

    def test_token_minted_against_other_extension_secret_is_rejected(self):
        id_a = "a" * 32
        id_b = "b" * 32
        # Handshake both extensions so the bridge has secrets on
        # record for both.
        reply_a = bb._handle_handshake({}, dict(ALLOWED, extension_id=id_a))
        reply_b = bb._handle_handshake({}, dict(ALLOWED, extension_id=id_b))
        assert reply_a["session_secret_hex"] != reply_b["session_secret_hex"]
        # Mint a token using extension A's secret.
        secret_a = bytes.fromhex(reply_a["session_secret_hex"])
        import secrets as _s
        req = _s.token_hex(16)
        ts = time.time()
        mac = bb._compute_token_hmac(req, ts, "pwd.fill",
                                     secret=secret_a)
        tok = {"request_id": req, "ts": ts, "op": "pwd.fill",
               "hmac": mac}
        # Verify as extension B → bad HMAC, not a replay.
        ok, err = bb.verify_intent_token(tok, "pwd.fill",
                                         extension_id=id_b)
        assert ok is False
        assert err == "intent_token_bad_hmac"

    def test_token_minted_against_own_secret_verifies(self):
        ext_id = "c" * 32
        reply = bb._handle_handshake({}, dict(ALLOWED, extension_id=ext_id))
        secret = bytes.fromhex(reply["session_secret_hex"])
        import secrets as _s
        req = _s.token_hex(16)
        ts = time.time()
        mac = bb._compute_token_hmac(req, ts, "pwd.fill", secret=secret)
        tok = {"request_id": req, "ts": ts, "op": "pwd.fill",
               "hmac": mac}
        ok, err = bb.verify_intent_token(tok, "pwd.fill",
                                         extension_id=ext_id)
        assert ok is True
        assert err == ""

    def test_strict_strip_in_dispatch_for_pwd_fill(self):
        # A daemon that accidentally leaks identity fields gets
        # caught by the centralised _strip_identity in dispatch.
        # pwd.fill is NOT in _IDENTITY_REFLECTING_OPS so all fields
        # must be stripped.
        bb._dbus_client = _FakeDBus(reply={
            "ok": True,
            "credentials": [],
            # Daemon shouldn't be returning these; bridge scrubs.
            "ppid": 999,
            "parent_exe": "leak",
            "parent_selinux": "leak",
            "extension_id": "leak",
            "allowed": True,
        })
        out = bb.dispatch(
            {"op": "pwd.fill",
             "url": "https://x.com",
             "intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        for f in bb._IDENTITY_FIELDS:
            assert f not in out, f"{f} leaked into pwd.fill reply"
