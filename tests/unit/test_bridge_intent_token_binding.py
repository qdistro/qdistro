"""Regression: intent-token / session-secret extension binding.

Source invariant — ``todo/security-hardening-carryforward.md`` §"Clipboard":
*"Intent-token and session-secret handshakes must be extension-bound. Do
not ship a production master/shared-secret fallback."*

This locks two properties of ``browser_bridge/qdistro_browser_bridge.py``:

(a) **Extension binding.** The per-session HMAC secret returned by
    ``qdistro.handshake`` is derived from the bridge's per-launch master
    secret bound to the caller's *kernel-attested* ``extension_id``
    (from argv, never from the stdio payload). A token minted under
    extension A's derived secret does NOT validate when presented as
    extension B, and vice-versa. Tampering with the bound ``extension_id``
    at verify time fails the HMAC.

(b) **No production master/shared-secret fallback.** ``_SESSION_SECRET``
    is minted with ``secrets.token_bytes`` per launch and is NEVER read
    from the environment or any constant. There is no ``*_SECRET`` /
    ``*_TEST`` env escape hatch that would let an arbitrary client
    authenticate. The only ``_TEST``-suffixed override in the module (the
    parent-exe allowlist, P0-2) is gated behind ``QDISTRO_TEST_MODE=1``;
    we assert that gate still holds and that it does not touch the
    secret/token path.

These tests are MUTATION-SENSITIVE: if extension binding is removed
(e.g. ``verify_intent_token`` stops consulting the per-extension secret,
or ``derive_extension_session_secret`` stops mixing in the extension_id),
the cross-extension tests below start passing tokens they must reject and
fail.
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


EXT_A = "alpha@qdistro.local"
EXT_B = "bravo@qdistro.local"


def _identity(extension_id: str) -> dict:
    return {
        "ppid": 100,
        "parent_exe": "/usr/lib64/firefox/firefox",
        "parent_selinux": "user_u:user_r:user_t:s0",
        "extension_id": extension_id,
        "allowed": True,
    }


def _handshake(extension_id: str) -> bytes:
    """Run a real handshake for ``extension_id``; return the derived
    secret bytes the extension would use to mint tokens."""
    resp = bb._handle_handshake({"op": "qdistro.handshake"},
                                _identity(extension_id))
    assert resp["ok"] is True
    assert resp["extension_id"] == extension_id
    return bytes.fromhex(resp["session_secret_hex"])


def _mint(secret: bytes, op: str, request_id: str, at: float | None = None
          ) -> dict:
    ts = time.time() if at is None else at
    mac = bb._compute_token_hmac(request_id, ts, op, secret=secret)
    return {"request_id": request_id, "ts": ts, "op": op, "hmac": mac}


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    # These tests assert PRODUCTION behaviour, so QDISTRO_TEST_MODE must
    # be OFF — the no-handshake master fallback is a test-only path and
    # must stay closed here.
    monkeypatch.delenv("QDISTRO_TEST_MODE", raising=False)
    bb.reset_session_secret()
    yield
    bb.reset_session_secret()


# =====================================================================
# (a) Extension binding
# =====================================================================

class TestDerivationBinding:
    def test_distinct_extensions_get_distinct_secrets(self):
        sec_a = _handshake(EXT_A)
        sec_b = _handshake(EXT_B)
        assert sec_a != sec_b
        # Both are HMAC-SHA256 over the master -> 32 bytes, and neither
        # equals the raw master (the secret is derived, not the master).
        assert len(sec_a) == 32 and len(sec_b) == 32
        assert sec_a != bb._SESSION_SECRET
        assert sec_b != bb._SESSION_SECRET

    def test_derivation_mixes_in_extension_id(self):
        master = bb._SESSION_SECRET
        d_a = bb.derive_extension_session_secret(master, EXT_A)
        d_b = bb.derive_extension_session_secret(master, EXT_B)
        # Mutation sentinel: if derive_extension_session_secret stops
        # mixing the extension_id, these collide and the test fails.
        assert d_a != d_b
        assert d_a == bb.derive_extension_session_secret(master, EXT_A)


class TestCrossExtensionTokenRejection:
    def test_token_minted_for_A_rejected_as_B(self):
        sec_a = _handshake(EXT_A)
        _handshake(EXT_B)
        tok = _mint(sec_a, "pwd.fill", "rid-a-1")
        # Validates as the minting extension...
        ok, err = bb.verify_intent_token(
            tok, "pwd.fill", extension_id=EXT_A)
        assert ok, err
        # ...but a fresh, identical token must NOT validate as B.
        tok2 = _mint(sec_a, "pwd.fill", "rid-a-2")
        ok_b, err_b = bb.verify_intent_token(
            tok2, "pwd.fill", extension_id=EXT_B)
        assert ok_b is False
        assert err_b == "intent_token_bad_hmac"

    def test_token_minted_for_B_rejected_as_A(self):
        _handshake(EXT_A)
        sec_b = _handshake(EXT_B)
        tok = _mint(sec_b, "pwd.fill", "rid-b-1")
        ok, err = bb.verify_intent_token(
            tok, "pwd.fill", extension_id=EXT_B)
        assert ok, err
        tok2 = _mint(sec_b, "pwd.fill", "rid-b-2")
        ok_a, err_a = bb.verify_intent_token(
            tok2, "pwd.fill", extension_id=EXT_A)
        assert ok_a is False
        assert err_a == "intent_token_bad_hmac"

    def test_tampering_with_bound_extension_id_at_verify_fails(self):
        """A correctly-minted token for A, but the verifier is told the
        caller is B (the kernel-attested id the dispatch path passes).
        Binding means the swap is detected via the HMAC."""
        sec_a = _handshake(EXT_A)
        _handshake(EXT_B)
        tok = _mint(sec_a, "cookies.export", "rid-tamper")
        ok, err = bb.verify_intent_token(
            tok, "cookies.export", extension_id=EXT_B)
        assert ok is False
        assert err == "intent_token_bad_hmac"

    def test_dispatch_rejects_cross_extension_token_end_to_end(self):
        """Full dispatch path: token minted under A's secret, presented
        on a request whose kernel-attested identity is B -> rejected."""
        sec_a = _handshake(EXT_A)
        _handshake(EXT_B)
        tok = _mint(sec_a, "pwd.fill", "rid-disp")
        resp = bb.dispatch(
            {"op": "pwd.fill",
             "url": "https://example.com/login",
             "intent_token": tok},
            _identity(EXT_B))
        assert resp.get("ok") is False
        assert resp.get("error") == "intent_token_bad_hmac"

    def test_secret_does_not_survive_master_rotation(self):
        """Master rotation invalidates every previously-handed-out
        derived secret (the per-extension registry is cleared and the
        master changes), so a stale A token no longer validates."""
        sec_a = _handshake(EXT_A)
        tok = _mint(sec_a, "pwd.fill", "rid-rot")
        bb.reset_session_secret()
        # No handshake on record post-rotation -> in production the
        # verifier refuses to fall back to the master at all.
        ok, err = bb.verify_intent_token(
            tok, "pwd.fill", extension_id=EXT_A)
        assert ok is False
        assert err == "intent_token_no_handshake"


# =====================================================================
# (b1) No master fallback for an attested-but-un-handshaked extension
# =====================================================================

class TestNoMasterFallbackWithoutHandshake:
    """The verifier must NOT validate a master-HMAC token just because a
    kernel-attested extension_id is present but never handshaked — that
    would be a production master-secret bypass of the per-extension
    binding. (Closes codex HIGH finding.)"""

    def test_master_minted_token_rejected_without_handshake(self):
        # Token minted directly against the raw master (the bypass an
        # attacker would attempt), presented for an attested extension
        # that has not handshaked.
        master = bb._SESSION_SECRET
        tok = _mint(master, "pwd.fill", "rid-nofb")
        ok, err = bb.verify_intent_token(
            tok, "pwd.fill", extension_id=EXT_A)
        assert ok is False
        assert err == "intent_token_no_handshake"

    def test_dispatch_rejects_master_token_without_handshake(self):
        master = bb._SESSION_SECRET
        tok = _mint(master, "pwd.fill", "rid-nofb-disp")
        resp = bb.dispatch(
            {"op": "pwd.fill",
             "url": "https://example.com/login",
             "intent_token": tok},
            _identity(EXT_A))
        assert resp.get("ok") is False
        assert resp.get("error") == "intent_token_no_handshake"

    def test_after_handshake_derived_token_still_works(self):
        # Sanity: the legitimate flow (handshake, then mint against the
        # derived secret) still validates — the fix only closes the
        # no-handshake master path.
        sec_a = _handshake(EXT_A)
        tok = _mint(sec_a, "pwd.fill", "rid-ok")
        ok, err = bb.verify_intent_token(
            tok, "pwd.fill", extension_id=EXT_A)
        assert ok, err

    def test_master_fallback_unlocked_only_under_test_mode(
            self, monkeypatch):
        """The no-handshake master path is a test-only convenience and
        is unlocked solely by QDISTRO_TEST_MODE=1 (mirrors the P0-2
        parent-exe allowlist _TEST gating)."""
        master = bb._SESSION_SECRET
        # Without test mode: rejected.
        tok = _mint(master, "pwd.fill", "rid-tm-off")
        ok, err = bb.verify_intent_token(
            tok, "pwd.fill", extension_id=EXT_A)
        assert ok is False and err == "intent_token_no_handshake"
        # With test mode: the same master path is permitted.
        monkeypatch.setenv("QDISTRO_TEST_MODE", "1")
        tok2 = _mint(master, "pwd.fill", "rid-tm-on")
        ok2, err2 = bb.verify_intent_token(
            tok2, "pwd.fill", extension_id=EXT_A)
        assert ok2, err2


# =====================================================================
# (b) No production master/shared-secret fallback
# =====================================================================

class TestNoSharedSecretFallback:
    def test_session_secret_is_random_per_launch(self):
        a = bb.reset_session_secret()
        b = bb.reset_session_secret()
        assert a != b
        assert len(a) == 32 and len(b) == 32

    def test_no_secret_env_override(self, monkeypatch):
        """Setting plausible *_SECRET / *_TEST env names must NOT change
        the minted master secret. Guards against a shared-secret escape
        hatch being introduced."""
        for name in (
            "QDISTRO_SESSION_SECRET", "QDISTRO_SESSION_SECRET_TEST",
            "QDISTRO_BRIDGE_SECRET", "QDISTRO_BRIDGE_SECRET_TEST",
            "QDISTRO_MASTER_SECRET", "QDISTRO_INTENT_TOKEN_SECRET",
            "QDISTRO_SHARED_SECRET",
        ):
            monkeypatch.setenv(name, "deadbeef" * 8)
        secrets_seen = {bb.reset_session_secret() for _ in range(5)}
        # If any env name fed the secret, the set would collapse to 1
        # (or equal the supplied constant). It must stay random.
        assert len(secrets_seen) == 5
        attacker = bytes.fromhex("deadbeef" * 8)
        assert attacker not in secrets_seen

    def test_module_source_has_no_secret_env_read(self):
        """Static guard: the module must not read a *secret* env var.
        ``os.environ``/``getenv`` may appear (recall root, allowlist,
        heartbeat/dbus toggles) but none on the same line as 'secret'.
        Mutation sentinel for an env-fed shared secret."""
        src = _MOD.read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if ("os.environ" in line or "getenv" in line) \
                    and "secret" in line.lower():
                pytest.fail(f"secret read from env: {line!r}")

    def test_test_mode_allowlist_override_still_gated(self, monkeypatch):
        """The only _TEST-suffixed override (parent-exe allowlist, P0-2)
        must stay gated behind QDISTRO_TEST_MODE=1 and must not be a
        secret bypass. Without test-mode the override is rejected."""
        monkeypatch.setenv(
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST", "/bin/anything")
        monkeypatch.delenv("QDISTRO_TEST_MODE", raising=False)
        with pytest.raises(RuntimeError):
            bb._resolve_allowlist()
        # Gate satisfied -> override accepted (it controls the parent
        # allowlist, NOT any secret), proving it is not on the token path.
        monkeypatch.setenv("QDISTRO_TEST_MODE", "1")
        allowed = bb._resolve_allowlist()
        assert "/bin/anything" in allowed
        # And toggling it never perturbs the session secret.
        before = bb.reset_session_secret()
        after = bb.reset_session_secret()
        assert before != after
