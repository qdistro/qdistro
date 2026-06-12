"""Negative (adversarial) tests for the pwd/browser approval path.

Goal: prove the password autofill path is **non-replayable** and
**identity-bound**, mirroring the phone-path replay/expiry suite
(``test_phone_security.py``) but for the pwd daemon's Fill→FillConfirm
``fill_token`` and the native-messaging bridge attestation.

Each test here is constructed so it would FAIL if the corresponding
fail-closed guard in ``qdistro_pwd_daemon`` were removed:

  * fill_token single-use (replay) — daemon ``.pop()``s the token, so a
    second FillConfirm with the same token must be rejected;
  * fill_token expiry / staleness — a token whose ``expires`` is in the
    past must be rejected;
  * fill_token target binding — a token minted for one origin/username
    must not unlock a different origin/username (the prompt-target bind);
  * process-restart invalidation — tokens live only in the daemon's
    in-memory ``_fill_tokens`` dict, so a fresh daemon instance must not
    honour a token minted by the previous process;
  * spoofed native-messaging bridge — a caller whose ``/proc`` parent
    chain is not an allowlisted browser launching the real bridge script
    is rejected by ``_browser_bridge_allowed`` (tested against the real
    function, not the monkeypatched stub other suites use);
  * forged secctx / app-id — a valid fill_token does NOT bypass the
    per-item pin gate; a caller whose kernel-attested exe/selinux/uid
    differs from the item's stored pins is denied even with a live token.

These complement (do not duplicate) the bridge-side intent-token replay
tests in ``test_pwd_fill_bridge.py`` and the
``_browser_bridge_allowed``-monkeypatched cases in
``test_pwd_daemon_fill_save.py``.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pwd"))

import qdistro_pwd_daemon as d
from qdistro_pwd_audit import PwdAuditLog  # type: ignore
from qdistro_pwd_vault import (  # type: ignore
    add_item,
    create_vault,
    get_item_pins,
    unlock_vault,
)

# Capture the genuine attestation function before any fixture stubs it out,
# so the spoofed-bridge end-to-end test can restore the real implementation.
_REAL_BRIDGE_ALLOWED = d._browser_bridge_allowed


BROWSER_EXE = "/usr/lib64/firefox/firefox"

# Kernel-attested caller snapshot that matches the pin we store below.
CALLER = {
    "uid": 1500,
    "pid": 12345,
    "exe": BROWSER_EXE,
    "exe_sha256": "aabbccdd",
    "selinux_label": "",
    "cgroup": "",
}


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """A daemon with an unlocked passwords vault holding one credential.

    The bridge attestation is stubbed to *allow* here so each test can
    isolate the specific guard under test (token replay / expiry / target
    binding / identity). The spoofed-bridge guard itself is exercised
    separately in ``TestSpoofedBridgeAttestation`` against the real
    ``_browser_bridge_allowed`` function.
    """
    vd = str(tmp_path / "vaults")
    audit_path = str(tmp_path / "audit.sqlite")
    monkeypatch.setattr(d, "VAULT_DIR", vd)
    monkeypatch.setattr(d, "AUDIT_DB", audit_path)
    monkeypatch.setattr(d, "BROWSER_PWD_VAULT", "passwords")
    monkeypatch.setattr(d, "_browser_bridge_allowed",
                        lambda _pid: (True, "test-bridge"))
    create_vault(vd, "passwords", b"vault-pass")
    key = unlock_vault(vd, "passwords", b"vault-pass")
    add_item(vd, "passwords", key, "pwd:https://example.com/alice",
             b"s3cret", pin_app_exe=BROWSER_EXE, pin_uid=1500, replace=True)
    daemon = d.PwdDaemon.__new__(d.PwdDaemon)
    daemon._unlocked = {
        "passwords": {"key": bytearray(key), "unlocked_at": 0, "last_use": 0},
    }
    daemon._audit = PwdAuditLog(audit_path)
    daemon._fill_tokens = {}
    daemon._fill_token_ttl = 120
    return daemon, vd, audit_path


def _fill(daemon, url, username=None, caller=CALLER, peer=(1500, 12345)):
    with patch.object(daemon, "_peer_info", return_value=peer), \
         patch("qdistro_pwd_daemon.snapshot_caller", return_value=caller):
        return json.loads(daemon.Fill(json.dumps(
            {"url": url, "username": username}), sender=":1.42"))


def _confirm(daemon, url, username, fill_token, caller=CALLER,
             peer=(1500, 12345)):
    with patch.object(daemon, "_peer_info", return_value=peer), \
         patch("qdistro_pwd_daemon.snapshot_caller", return_value=caller):
        return json.loads(daemon.FillConfirm(json.dumps({
            "url": url, "username": username, "fill_token": fill_token,
        }), sender=":1.42"))


def _mint_token(daemon, url="https://example.com/login", username=None):
    res = _fill(daemon, url, username)
    assert res["ok"] is True, f"Fill failed to mint a token: {res}"
    return res["fill_token"]


# ---------------------------------------------------------------------------
# 1. Replay: a consumed fill_token cannot be reused.
# ---------------------------------------------------------------------------

class TestFillTokenReplay:
    def test_token_single_use_then_replay_rejected(self, staged):
        daemon, _, _ = staged
        token = _mint_token(daemon)

        first = _confirm(daemon, "https://example.com/login", "alice", token)
        assert first["ok"] is True
        assert first["credentials"][0]["password"] == "s3cret"

        # Second use of the SAME token (the daemon pops it on success) must
        # be rejected — this is the replay defence. If the .pop()/consume
        # were removed the token would still be live and this would leak the
        # password a second time.
        second = _confirm(daemon, "https://example.com/login", "alice", token)
        assert second["ok"] is False
        assert second["error"] == "invalid_token"

    def test_token_string_not_reusable_across_usernames(self, staged):
        """The token store is keyed per (token, username): an attacker who
        observes the opaque token string cannot pivot it to a username the
        Fill response never offered. A confirm for an un-minted username
        is rejected, and — separately — the legitimate single use still
        consumes the alice entry so no replay survives."""
        daemon, _, _ = staged
        token = _mint_token(daemon)
        # The token string alone, aimed at a username Fill never matched,
        # is rejected (no token:mallory entry exists).
        bad = _confirm(daemon, "https://example.com/login", "mallory", token)
        assert bad["ok"] is False
        assert bad["error"] == "invalid_token"
        # The legitimate (token, alice) pair works exactly once...
        first = _confirm(daemon, "https://example.com/login", "alice", token)
        assert first["ok"] is True
        # ...and is consumed, so a replay of that same pair is rejected.
        replay = _confirm(daemon, "https://example.com/login", "alice", token)
        assert replay["ok"] is False
        assert replay["error"] == "invalid_token"


# ---------------------------------------------------------------------------
# 2. Stale token: expired by TTL, or surviving a daemon restart.
# ---------------------------------------------------------------------------

class TestFillTokenStale:
    def test_token_expired_by_ttl_rejected(self, staged):
        daemon, _, _ = staged
        token = _mint_token(daemon)
        # Force every minted token entry to be already expired. The daemon
        # stores entries keyed by "<token>:<username>"; backdate them all.
        for entry in daemon._fill_tokens.values():
            entry["expires"] = 1  # epoch second 1 — far in the past
        out = _confirm(daemon, "https://example.com/login", "alice", token)
        assert out["ok"] is False
        assert out["error"] == "invalid_token"

    def test_token_invalid_after_process_restart(self, staged):
        """fill_tokens live only in the daemon's in-memory dict; a fresh
        daemon instance (process restart) has no record of the token."""
        daemon, vd, audit_path = staged
        token = _mint_token(daemon)

        # Simulate a restart: a brand-new daemon object that re-opens the
        # same on-disk vault but starts with an empty token store.
        restarted = d.PwdDaemon.__new__(d.PwdDaemon)
        key = unlock_vault(vd, "passwords", b"vault-pass")
        restarted._unlocked = {
            "passwords": {"key": bytearray(key), "unlocked_at": 0,
                          "last_use": 0}}
        restarted._audit = PwdAuditLog(audit_path)
        restarted._fill_tokens = {}
        restarted._fill_token_ttl = 120

        out = _confirm(restarted, "https://example.com/login", "alice", token)
        assert out["ok"] is False
        assert out["error"] == "invalid_token"

    def test_token_invalid_after_lock_and_reunlock(self, staged):
        """An approval is scoped to the unlocked session. Locking the vault
        (explicit / idle / rotate) drops outstanding fill tokens, so a
        token minted before the lock must NOT be honoured after a fresh
        re-unlock within the TTL window."""
        daemon, vd, _ = staged
        token = _mint_token(daemon)
        # Lock then re-unlock the vault within the token TTL.
        daemon._do_lock("passwords", reason="explicit-lock")
        assert daemon._fill_tokens == {}
        key = unlock_vault(vd, "passwords", b"vault-pass")
        daemon._unlocked["passwords"] = {
            "key": bytearray(key), "unlocked_at": 0, "last_use": 0}
        out = _confirm(daemon, "https://example.com/login", "alice", token)
        assert out["ok"] is False
        assert out["error"] == "invalid_token"


# ---------------------------------------------------------------------------
# 3. Target binding: a token is bound to its origin AND username.
# ---------------------------------------------------------------------------

class TestFillTokenTargetBinding:
    def test_token_for_one_origin_rejected_on_another(self, staged):
        """A token minted while filling example.com must not unlock a
        credential on attacker.com (intended-prompt-target binding)."""
        daemon, vd, _ = staged
        # Stash a victim credential on a different origin, same username,
        # readable by this caller's pins.
        key = unlock_vault(vd, "passwords", b"vault-pass")
        add_item(vd, "passwords", key, "pwd:https://other.com/alice",
                 b"other-secret", pin_app_exe=BROWSER_EXE, pin_uid=1500,
                 replace=True)
        token = _mint_token(daemon, "https://example.com/login")
        # Replay that token against the other origin.
        out = _confirm(daemon, "https://other.com/login", "alice", token)
        assert out["ok"] is False
        assert out["error"] == "invalid_token"

    def test_token_for_one_username_rejected_on_another(self, staged):
        """Fill for alice mints a token keyed to alice; it must not be
        usable to confirm bob's password on the same origin."""
        daemon, vd, _ = staged
        key = unlock_vault(vd, "passwords", b"vault-pass")
        add_item(vd, "passwords", key, "pwd:https://example.com/bob",
                 b"bob-secret", pin_app_exe=BROWSER_EXE, pin_uid=1500,
                 replace=True)
        # Mint specifically for alice.
        res = _fill(daemon, "https://example.com/login", "alice")
        token = res["fill_token"]
        # Try to confirm bob with alice's token.
        out = _confirm(daemon, "https://example.com/login", "bob", token)
        assert out["ok"] is False
        assert out["error"] == "invalid_token"
        # bob's password must NOT have leaked.
        assert "credentials" not in out

    def test_forged_random_token_rejected(self, staged):
        """A token the daemon never minted (attacker fabricates a
        token_urlsafe string) is rejected outright."""
        daemon, _, _ = staged
        # Prime the store so it's non-empty (proves we don't accidentally
        # accept any value when a real token also exists).
        _mint_token(daemon)
        out = _confirm(daemon, "https://example.com/login", "alice",
                       "forged-token-deadbeef")
        assert out["ok"] is False
        assert out["error"] == "invalid_token"


    def test_token_username_field_mismatch_rejected(self, staged):
        """Second layer of username binding: even if an attacker forges a
        store key so the lookup hits, the daemon re-checks that the stored
        entry's bound username equals the requested one. We craft a token
        entry whose key says 'bob' but whose bound username is 'alice'."""
        daemon, vd, _ = staged
        key = unlock_vault(vd, "passwords", b"vault-pass")
        add_item(vd, "passwords", key, "pwd:https://example.com/bob",
                 b"bob-secret", pin_app_exe=BROWSER_EXE, pin_uid=1500,
                 replace=True)
        forged = "forged-key-token"
        # Key claims bob; bound payload says alice → the explicit
        # token_entry["username"] != username guard must fire.
        daemon._fill_tokens[f"{forged}:bob"] = {
            "origin": "https://example.com",
            "username": "alice",
            "pid": 12345,
            "expires": 2 ** 31,
        }
        out = _confirm(daemon, "https://example.com/login", "bob", forged)
        assert out["ok"] is False
        assert out["error"] == "invalid_token"


# ---------------------------------------------------------------------------
# 3b. Process binding: a token is bound to the minting bridge pid.
# ---------------------------------------------------------------------------

class TestFillTokenProcessBinding:
    def test_token_from_other_pid_rejected(self, staged):
        """A token minted by one bridge process (pid 12345) cannot be
        redeemed by a different process (pid 54321) even with identical
        uid/exe pins — defends against token theft across same-uid
        bridge processes. The Fill records the minting pid; FillConfirm
        rejects a pid mismatch."""
        daemon, _, _ = staged
        # Mint under pid 12345 (the fixture CALLER pid).
        token = _mint_token(daemon)
        # Redeem from a different pid; uid/exe still match the item pins.
        thief = dict(CALLER, pid=54321)
        out = _confirm(daemon, "https://example.com/login", "alice", token,
                       caller=thief, peer=(1500, 54321))
        assert out["ok"] is False
        assert out["error"] == "invalid_token"


# ---------------------------------------------------------------------------
# 4. Forged secctx / app-id: a live token does NOT bypass the pin gate.
# ---------------------------------------------------------------------------

class TestForgedIdentityWithValidToken:
    def test_valid_token_wrong_exe_denied(self, staged):
        """Even with a freshly minted, on-target token, a caller whose
        kernel-attested exe differs from the item pin is denied. The token
        proves intent; the pin proves identity — both are required."""
        daemon, _, _ = staged
        token = _mint_token(daemon)
        attacker = dict(CALLER, exe="/usr/bin/python3")
        out = _confirm(daemon, "https://example.com/login", "alice", token,
                       caller=attacker)
        assert out["ok"] is False
        # Token matches (origin+username+live), so the failure is the pin
        # gate, surfaced as policy_denied — not invalid_token.
        assert out["error"] == "policy_denied"

    def test_valid_token_wrong_uid_denied(self, staged):
        """Same, for a forged/mismatched uid (app-id) on the pin."""
        daemon, _, _ = staged
        token = _mint_token(daemon)
        attacker = dict(CALLER, uid=4242)
        out = _confirm(daemon, "https://example.com/login", "alice", token,
                       caller=attacker, peer=(4242, 12345))
        assert out["ok"] is False
        assert out["error"] == "policy_denied"

    def test_valid_token_forged_selinux_denied(self, staged, monkeypatch):
        """A credential pinned to a SELinux label cannot be read by a
        caller presenting a different (forged) label, even with a token."""
        daemon, vd, _ = staged
        key = unlock_vault(vd, "passwords", b"vault-pass")
        # Re-pin alice's credential to a specific SELinux context.
        add_item(vd, "passwords", key, "pwd:https://example.com/alice",
                 b"s3cret",
                 pin_app_exe=BROWSER_EXE, pin_uid=1500,
                 pin_selinux="system_u:system_r:firefox_t:s0", replace=True)
        # Fill needs to see the matching label to mint a token for alice.
        good = dict(CALLER, selinux_label="system_u:system_r:firefox_t:s0")
        res = _fill(daemon, "https://example.com/login", "alice",
                    caller=good)
        assert res["ok"] is True, res
        token = res["fill_token"]
        # Now confirm presenting a *different* (forged) label.
        forged = dict(CALLER, selinux_label="system_u:system_r:unconfined_t:s0")
        out = _confirm(daemon, "https://example.com/login", "alice", token,
                       caller=forged)
        assert out["ok"] is False
        assert out["error"] == "policy_denied"


# ---------------------------------------------------------------------------
# 5. Spoofed native-messaging bridge — real _browser_bridge_allowed.
# ---------------------------------------------------------------------------
# These drive the actual attestation function (the daemon calls it before
# touching tokens) by injecting fake /proc readers, rather than stubbing the
# whole function out as the happy-path suites do. If the parent-exe check or
# the bridge-script-in-cmdline check were removed, these would fail.

class TestSpoofedBridgeAttestation:
    SCRIPT = "/usr/libexec/qdistro/qdistro_browser_bridge.py"

    def _install_proc(self, monkeypatch, *, cmdline, ppid, parent_exe):
        # Hermetic: pin the script + allowlist rather than relying on the
        # env-derived parent-exe override captured at import time.
        monkeypatch.setattr(d, "BROWSER_BRIDGE_SCRIPT", self.SCRIPT)
        monkeypatch.setattr(
            d, "_BROWSER_PARENT_EXES_ENV_OVERRIDE", (BROWSER_EXE,))
        monkeypatch.setattr(d, "_read_proc_cmdline", lambda _pid: cmdline)
        monkeypatch.setattr(d, "_read_proc_ppid", lambda _pid: ppid)
        monkeypatch.setattr(
            d, "_read_proc_exe",
            lambda pid: parent_exe if pid == ppid else "")

    def test_genuine_bridge_under_firefox_allowed(self, monkeypatch):
        self._install_proc(
            monkeypatch,
            cmdline=["python3", self.SCRIPT],
            ppid=999, parent_exe=BROWSER_EXE)
        ok, reason = d._browser_bridge_allowed(12345)
        assert ok is True
        assert reason == "browser-bridge"

    def test_same_uid_python_not_running_bridge_rejected(self, monkeypatch):
        """A random same-uid python process that is NOT the bridge script
        (so it could otherwise satisfy the per-item uid pin) is rejected."""
        self._install_proc(
            monkeypatch,
            cmdline=["python3", "/home/user/evil.py"],
            ppid=999, parent_exe=BROWSER_EXE)
        ok, reason = d._browser_bridge_allowed(12345)
        assert ok is False
        assert reason == "not-browser-bridge"

    def test_script_path_as_data_arg_rejected(self, monkeypatch):
        """A hostile native host that passes the real bridge-script path as
        a *data* argument (``python3 evil.py <bridge-script>``) must NOT be
        accepted: only the executed script (first non-flag arg after the
        interpreter) counts. This is the classic argv-smuggling spoof."""
        self._install_proc(
            monkeypatch,
            cmdline=["python3", "/home/user/evil.py", self.SCRIPT],
            ppid=999, parent_exe=BROWSER_EXE)
        ok, reason = d._browser_bridge_allowed(12345)
        assert ok is False
        assert reason == "not-browser-bridge"

    def test_operand_flag_smuggle_rejected(self, monkeypatch):
        """``python3 -W <bridge-script> evil.py``: the ``-W`` option
        consumes the bridge path as its *value* while Python actually
        executes evil.py. A naive flag-skipper would mis-identify the
        bridge path as the executed script; we must reject any
        operand-consuming / unknown interpreter flag and fail closed."""
        self._install_proc(
            monkeypatch,
            cmdline=["python3", "-W", self.SCRIPT, "/home/user/evil.py"],
            ppid=999, parent_exe=BROWSER_EXE)
        ok, reason = d._browser_bridge_allowed(12345)
        assert ok is False
        assert reason == "not-browser-bridge"

    def test_dash_c_rejected(self, monkeypatch):
        """``python3 -c <code>`` has no script path at all; reject."""
        self._install_proc(
            monkeypatch,
            cmdline=["python3", "-c", "import os", self.SCRIPT],
            ppid=999, parent_exe=BROWSER_EXE)
        ok, reason = d._browser_bridge_allowed(12345)
        assert ok is False
        assert reason == "not-browser-bridge"

    def test_genuine_bridge_with_interpreter_flags_allowed(self, monkeypatch):
        """Interpreter flags before the script (python3 -E -S <script>) are
        tolerated — the executed-script detection skips leading flags."""
        self._install_proc(
            monkeypatch,
            cmdline=["python3", "-E", "-S", self.SCRIPT, "ext-arg"],
            ppid=999, parent_exe=BROWSER_EXE)
        ok, reason = d._browser_bridge_allowed(12345)
        assert ok is True
        assert reason == "browser-bridge"

    def test_bridge_script_under_non_browser_parent_rejected(self, monkeypatch):
        """The real bridge script, but launched by a shell / arbitrary
        process rather than an allowlisted browser, is rejected."""
        self._install_proc(
            monkeypatch,
            cmdline=["python3", self.SCRIPT],
            ppid=999, parent_exe="/usr/bin/bash")
        ok, reason = d._browser_bridge_allowed(12345)
        assert ok is False
        assert reason == "parent-not-browser"

    def test_unreadable_parent_rejected(self, monkeypatch):
        """If the parent pid can't be read (process gone / race), fail
        closed rather than allowing the call."""
        self._install_proc(
            monkeypatch,
            cmdline=["python3", self.SCRIPT],
            ppid=None, parent_exe=BROWSER_EXE)
        ok, reason = d._browser_bridge_allowed(12345)
        assert ok is False
        assert reason == "parent-unreadable"

    def test_spoofed_bridge_blocks_fillconfirm_end_to_end(
            self, staged, monkeypatch):
        """End-to-end: a spoofed bridge caller can't even reach the token
        check — FillConfirm returns policy_denied before consulting the
        token store. Uses the real attestation function with a non-browser
        parent."""
        daemon, _, _ = staged
        # Mint a legitimate token first (bridge stubbed-allow in fixture).
        token = _mint_token(daemon)
        # Now swap in the REAL attestation with a spoofed (shell) parent.
        monkeypatch.setattr(d, "BROWSER_BRIDGE_SCRIPT", self.SCRIPT)
        monkeypatch.setattr(
            d, "_BROWSER_PARENT_EXES_ENV_OVERRIDE", (BROWSER_EXE,))
        monkeypatch.setattr(d, "_browser_bridge_allowed", _REAL_BRIDGE_ALLOWED)
        monkeypatch.setattr(d, "_read_proc_cmdline",
                            lambda _pid: ["python3", self.SCRIPT])
        monkeypatch.setattr(d, "_read_proc_ppid", lambda _pid: 999)
        monkeypatch.setattr(d, "_read_proc_exe",
                            lambda pid: "/usr/bin/bash" if pid == 999 else "")
        out = _confirm(daemon, "https://example.com/login", "alice", token)
        assert out["ok"] is False
        assert out["error"] == "policy_denied"
        # The spoofed caller is rejected BEFORE the token store is consulted:
        # the legitimate token must survive untouched so the rejection is not
        # also a free token-burn DoS (and proves ordering — attestation
        # first, token check second).
        assert f"{token}:alice" in daemon._fill_tokens


# ---------------------------------------------------------------------------
# 6. Save persists the kernel-attested SELinux pin (identity binding at write).
# ---------------------------------------------------------------------------
# Regression: Save read the wrong caller key ("selinux" instead of the
# snapshot_caller key "selinux_label"), silently dropping the SELinux pin so
# browser-saved creds were never SELinux-bound. A later confirm from a
# different domain would then NOT be blocked by SELinux. These prove the pin
# is written from the kernel-attested label and enforced on read-back.

class TestSavePersistsSelinuxPin:
    def test_save_writes_selinux_pin_from_caller_label(self, staged):
        daemon, vd, _ = staged
        label = "system_u:system_r:firefox_t:s0"
        caller = dict(CALLER, selinux_label=label)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=caller):
            res = json.loads(daemon.Save(json.dumps({
                "url": "https://saveme.example/login",
                "username": "carol",
                "password": "sekret",
            }), sender=":1.42"))
        assert res["ok"] is True
        pins = get_item_pins(vd, "passwords",
                             "pwd:https://saveme.example/carol")
        assert pins.get("pin_selinux") == label

    def test_forged_label_cannot_read_saved_cred(self, staged):
        """End-to-end Save→Fill: a credential saved under one SELinux label
        is invisible to a caller presenting a different (forged) label."""
        daemon, vd, _ = staged
        label = "system_u:system_r:firefox_t:s0"
        good = dict(CALLER, selinux_label=label)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=good):
            res = json.loads(daemon.Save(json.dumps({
                "url": "https://saveme.example/login",
                "username": "carol",
                "password": "sekret",
            }), sender=":1.42"))
        assert res["ok"] is True
        # Fill from a forged label → the pinned cred must not match.
        forged = dict(CALLER, selinux_label="system_u:system_r:evil_t:s0")
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=forged):
            fres = json.loads(daemon.Fill(json.dumps({
                "url": "https://saveme.example/login",
            }), sender=":1.42"))
        assert fres["ok"] is False
        assert fres["error"] == "no_match"
