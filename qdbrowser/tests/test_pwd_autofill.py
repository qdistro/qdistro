"""Tests for pwd_autofill — the qdbrowser-side autofill orchestrator.

Mocks the bridge + compositor-prompt clients so the round-trip can
run without real D-Bus. Covers:

  * Intent-token mint shape matches the bridge's verify expectation.
  * Orchestrator surfaces vault_locked / no_match / autofill_denied
    distinctly.
  * Approve-and-fill path returns the credential.
  * to_extension_reply() collapses errors to {ok:False, error:...}.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

import pytest
from qdbrowser import pwd_autofill as pa

# ---------------------------------------------------------------------------
# Intent token primitives
# ---------------------------------------------------------------------------

class TestMintIntentToken:
    def test_basic_shape(self):
        secret = b"x" * 32
        tok = pa.mint_intent_token(secret, "pwd.fill",
                                   now_fn=lambda: 1234567.0)
        assert tok.op == "pwd.fill"
        assert tok.ts == 1234567.0
        assert isinstance(tok.request_id, str) and len(tok.request_id) == 32
        # Re-derive the HMAC and confirm it matches.
        canonical = f"{tok.request_id}|{tok.ts}|{tok.op}".encode()
        expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
        assert tok.hmac_hex == expected

    def test_request_id_unique_across_mints(self):
        secret = b"y" * 32
        ids = {pa.mint_intent_token(secret, "pwd.fill").request_id
               for _ in range(50)}
        assert len(ids) == 50

    def test_to_dict(self):
        secret = b"z" * 32
        tok = pa.mint_intent_token(secret, "pwd.fill")
        d = tok.to_dict()
        assert set(d.keys()) == {"request_id", "ts", "op", "hmac"}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class _FakeBridge(pa.BridgeClient):
    """Records calls and answers from a programmable reply table.

    Keyed by op; each entry can be a dict (returned as-is) or a
    callable ``(args) -> dict``. Default answer is
    ``{"ok": False, "error": "no_reply"}``.
    """
    answers: dict
    calls: list

    @classmethod
    def with_answers(cls, answers):
        return cls(answers=answers, calls=[])

    def call(self, op, args):
        self.calls.append((op, dict(args)))
        a = self.answers.get(op)
        if a is None:
            return {"ok": False, "error": "no_reply"}
        if callable(a):
            return a(args)
        return dict(a)


@dataclass
class _FakePrompt(pa.AutofillPromptClient):
    decision: pa.AutofillDecision
    prompted: list

    @classmethod
    def with_decision(cls, allow, username=None, reason=""):
        return cls(
            decision=pa.AutofillDecision(
                allow=allow, selected_username=username, reason=reason),
            prompted=[])

    def prompt(self, payload):
        self.prompted.append(payload)
        return self.decision


SECRET_HEX = ("00" * 32)


# ---------------------------------------------------------------------------
# Orchestrator branches
# ---------------------------------------------------------------------------

class TestOrchestratorFill:
    def test_no_session_secret(self):
        bridge = _FakeBridge.with_answers({})
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        r = o.fill("https://example.com/")
        assert r.ok is False
        assert r.error == "no_session"
        assert bridge.calls == []

    def test_missing_url(self):
        bridge = _FakeBridge.with_answers({})
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        o.set_session_secret(SECRET_HEX)
        r = o.fill("")
        assert r.ok is False
        assert r.error == "missing_url"

    def test_vault_locked(self):
        bridge = _FakeBridge.with_answers({
            "pwd.fill": {"ok": False, "error": "vault_locked"}})
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        o.set_session_secret(SECRET_HEX)
        r = o.fill("https://example.com/")
        assert r.ok is False
        assert r.error == "vault_locked"
        assert prompt.prompted == []  # never reached the compositor.

    def test_no_match(self):
        bridge = _FakeBridge.with_answers({
            "pwd.fill": {"ok": True, "credentials": []}})
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        o.set_session_secret(SECRET_HEX)
        r = o.fill("https://example.com/")
        assert r.ok is False
        assert r.error == "no_match"
        assert prompt.prompted == []

    def test_admin_deny(self):
        bridge = _FakeBridge.with_answers({"pwd.fill": {
            "ok": True,
            "credentials": [{"username": "alice", "password": "s3cret"}],
        }})
        prompt = _FakePrompt.with_decision(allow=False, reason="user_no")
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        o.set_session_secret(SECRET_HEX)
        r = o.fill("https://example.com/")
        assert r.ok is False
        assert r.error == "autofill_denied"
        assert len(prompt.prompted) == 1
        # The candidate usernames must have been surfaced to the prompt.
        assert prompt.prompted[0].candidate_usernames == ("alice",)

    def test_admin_allow_default_username(self):
        bridge = _FakeBridge.with_answers({
            "pwd.fill": {
                "ok": True,
                "credentials": [{"username": "alice"}],
            },
            "pwd.fill_confirm": {
                "ok": True,
                "credentials": [{"username": "alice", "password": "s3cret"}],
            },
        })
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt,
                                     silo="work")
        o.set_session_secret(SECRET_HEX)
        r = o.fill("https://example.com/")
        assert r.ok is True
        assert r.username == "alice"
        assert r.password == "s3cret"
        assert prompt.prompted[0].silo == "work"

    def test_admin_allow_with_selected_username(self):
        bridge = _FakeBridge.with_answers({
            "pwd.fill": {
                "ok": True,
                "credentials": [
                    {"username": "alice"},
                    {"username": "bob"},
                ],
            },
            "pwd.fill_confirm": {
                "ok": True,
                "credentials": [{"username": "bob", "password": "s2"}],
            },
        })
        prompt = _FakePrompt.with_decision(allow=True, username="bob")
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        o.set_session_secret(SECRET_HEX)
        r = o.fill("https://example.com/")
        assert r.ok is True
        assert r.username == "bob"
        assert r.password == "s2"

    @pytest.mark.cheat_aware(
        protects="every credential-fill request carries an intent token "
                 "HMAC-bound to the session secret, so the bridge can prove "
                 "the request came from this browser and was not replayed",
        severity="critical",
        cheats=[
            "drop or weaken the HMAC re-derivation assertion",
            "stop asserting intent_token is present in the bridge args",
            "assert only op/url and skip the request_id|ts|op canonical form",
        ],
        consequence="the browser bridge accepts unbound/forged fill requests, "
                    "letting a same-uid process pull vault credentials",
    )
    def test_intent_token_in_bridge_call(self):
        bridge = _FakeBridge.with_answers({
            "pwd.fill": {
                "ok": True,
                "credentials": [{"username": "alice"}],
            },
            "pwd.fill_confirm": {
                "ok": True,
                "credentials": [{"username": "alice", "password": "s"}],
            },
        })
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        o.set_session_secret(SECRET_HEX)
        o.fill("https://example.com/")
        assert len(bridge.calls) == 2
        op, args = bridge.calls[0]
        assert op == "pwd.fill"
        assert args["url"] == "https://example.com/"
        token = args["intent_token"]
        assert token["op"] == "pwd.fill"
        # HMAC matches what the bridge would compute.
        secret = bytes.fromhex(SECRET_HEX)
        canonical = (f"{token['request_id']}|{token['ts']}|"
                     f"{token['op']}").encode()
        expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
        assert token["hmac"] == expected
        confirm_op, confirm_args = bridge.calls[1]
        assert confirm_op == "pwd.fill_confirm"
        assert confirm_args["username"] == "alice"
        assert confirm_args["intent_token"]["op"] == "pwd.fill_confirm"


class TestFillResult:
    def test_to_extension_reply_ok(self):
        r = pa.FillResult(ok=True, username="u", password="p")
        assert r.to_extension_reply() == {
            "ok": True, "username": "u", "password": "p"}

    def test_to_extension_reply_err(self):
        r = pa.FillResult(ok=False, error="autofill_denied")
        assert r.to_extension_reply() == {
            "ok": False, "error": "autofill_denied"}

    def test_to_extension_reply_default_err(self):
        r = pa.FillResult(ok=False)
        assert r.to_extension_reply() == {
            "ok": False, "error": "autofill_failed"}


class TestPerformHandshake:
    def test_success(self):
        bridge = _FakeBridge.with_answers({
            "qdistro.handshake": {"ok": True,
                                    "session_secret_hex": "deadbeef"}})
        secret = pa.perform_handshake(bridge)
        assert secret == "deadbeef"

    def test_failure(self):
        bridge = _FakeBridge.with_answers({
            "qdistro.handshake": {"ok": False, "error": "x"}})
        assert pa.perform_handshake(bridge) is None

    def test_empty_secret(self):
        bridge = _FakeBridge.with_answers({
            "qdistro.handshake": {"ok": True, "session_secret_hex": ""}})
        assert pa.perform_handshake(bridge) is None


# ---------------------------------------------------------------------------
# Fix-pass: bridge-stale-secret recovery + concurrency + name selection
# ---------------------------------------------------------------------------

class TestSelectBridgeNames:
    """The selection routine pins all-digits suffixes — a same-uid
    attacker that claims org.qdistro.BrowserBridge.evil is filtered
    out (P04 H1 security review)."""

    @pytest.mark.cheat_aware(
        protects="only all-digit BrowserBridge.<pid> bus names are trusted, "
                 "so a same-uid attacker that claims "
                 "org.qdistro.BrowserBridge.evil cannot impersonate the bridge",
        severity="critical",
        cheats=[
            "loosen the suffix filter to a substring/startswith match",
            "add 'evil' to the expected list to make the assert pass",
            "stop filtering and accept all returned names",
        ],
        consequence="the autofill orchestrator hands the session secret and "
                    "credential requests to an attacker-controlled bridge",
    )
    def test_only_numeric_suffixes_accepted(self):
        names = ["org.qdistro.BrowserBridge.evil",
                 "org.qdistro.BrowserBridge.42",
                 "org.qdistro.BrowserBridge.99",
                 "org.foo.Bar",
                 ":1.123"]
        out = pa.select_bridge_names(names)
        assert [t[1] for t in out] == [
            "org.qdistro.BrowserBridge.42",
            "org.qdistro.BrowserBridge.99",
        ]

    def test_lowest_first(self):
        names = ["org.qdistro.BrowserBridge.99",
                 "org.qdistro.BrowserBridge.7"]
        out = pa.select_bridge_names(names)
        assert out[0][0] == 7

    def test_empty_input(self):
        assert pa.select_bridge_names([]) == []


class TestHandshakeRefreshOnBadHmac:
    """Bridge restart rotates the per-session secret. The orchestrator
    must drop the cached secret, re-handshake exactly once, retry the
    fill exactly once (P04 H2 correctness)."""

    def test_retry_succeeds_after_bridge_restart(self):
        # Sequence: pwd.fill #1 → bad_hmac (the bridge rotated its
        # secret). The orchestrator catches it, re-handshakes (which
        # returns a new secret), retries pwd.fill which now succeeds.
        state = {"call_count": 0}

        def fill_reply(args):
            state["call_count"] += 1
            if state["call_count"] == 1:
                return {"ok": False, "error": "intent_token_bad_hmac"}
            return {"ok": True,
                    "credentials": [
                        {"username": "alice"}]}

        bridge = _FakeBridge.with_answers({
            "pwd.fill": fill_reply,
            "pwd.fill_confirm": {
                "ok": True,
                "credentials": [{"username": "alice", "password": "s3cret"}],
            },
            "qdistro.handshake": {"ok": True,
                                  "session_secret_hex":
                                  ("11" * 32)},
        })
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        o.set_session_secret(SECRET_HEX)
        r = o.fill("https://example.com/")
        assert r.ok is True
        assert r.username == "alice"
        assert state["call_count"] == 2

    def test_refresh_failure_surfaces_clean_error(self):
        bridge = _FakeBridge.with_answers({
            "pwd.fill": {"ok": False, "error": "intent_token_bad_hmac"},
            "qdistro.handshake": {"ok": False, "error": "bridge_down"},
        })
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        o.set_session_secret(SECRET_HEX)
        r = o.fill("https://example.com/")
        assert r.ok is False
        assert r.error == "handshake_refresh_failed"

    def test_only_one_retry(self):
        """Even after a successful handshake refresh, if the second
        attempt also returns bad_hmac, the orchestrator does NOT loop
        — it surfaces the error."""
        bridge = _FakeBridge.with_answers({
            "pwd.fill": {"ok": False, "error": "intent_token_bad_hmac"},
            "qdistro.handshake": {"ok": True,
                                  "session_secret_hex": ("22" * 32)},
        })
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        o.set_session_secret(SECRET_HEX)
        r = o.fill("https://example.com/")
        assert r.ok is False
        assert r.error == "intent_token_bad_hmac"


class TestUsernameSelectionValidation:
    """If the compositor returns a username that isn't on the
    candidate list, refuse rather than silently picking
    credentials[0] (S4 correctness review)."""

    def test_bad_selection_refused(self):
        bridge = _FakeBridge.with_answers({"pwd.fill": {
            "ok": True,
            "credentials": [{"username": "alice", "password": "s"}],
        }})
        prompt = _FakePrompt.with_decision(allow=True, username="mallory")
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        o.set_session_secret(SECRET_HEX)
        r = o.fill("https://example.com/")
        assert r.ok is False
        assert r.error == "bad_username_selection"


class TestSiloDefaultFromEnv:
    """The orchestrator's default silo is `clipboard_silo.current_silo()`,
    NOT a hard-coded "user" string (M2 correctness)."""

    def test_silo_defaults_from_env(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_SILO", "work")
        bridge = _FakeBridge.with_answers({})
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt)
        assert o.silo == "work"

    def test_explicit_silo_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_SILO", "work")
        bridge = _FakeBridge.with_answers({})
        prompt = _FakePrompt.with_decision(allow=True)
        o = pa.AutofillOrchestrator(bridge=bridge, prompt=prompt,
                                     silo="personal")
        assert o.silo == "personal"


class TestSanitizeForPrompt:
    """Strip Unicode bidi-override chars before the URL is rendered
    in the prompt body (S8 security)."""

    def test_no_bidi_chars(self):
        assert pa._sanitize_for_prompt("https://example.com/") == \
            "https://example.com/"

    def test_strips_rtl_override(self):
        # U+202E RIGHT-TO-LEFT OVERRIDE
        url = "https://example.com/‮path"
        out = pa._sanitize_for_prompt(url)
        assert "‮" not in out
