"""End-to-end password/autofill *approval* identity-binding test.

This is the POSITIVE companion to the negative tests in
``test_pwd_fill_bridge.py`` and ``test_pwd_daemon_fill_save.py``. It
proves, for one correctly-identified request, that the approval is
*bound to and validated against* every identity axis of the
password/autofill path — and, crucially, that each bound field is
actually *checked* (flip one axis at a time and the same request that
otherwise succeeds is now refused).

The path crosses two trust domains, wired together here so the
assertions are not vacuous:

  1. the native-messaging **bridge** (``qdistro_browser_bridge``) — the
     untrusted-input edge that derives the kernel-attested caller
     identity from ``argv`` + ``/proc`` and mints/validates intent
     tokens; and
  2. the **pwd daemon** (``qdistro_pwd_daemon``) — the SYSTEM-bus
     service that re-attests the peer, gates per-item pins, and issues
     single-use fill-tokens.

The bridge's outbound D-Bus client is replaced with an adapter that
calls the *real* daemon methods (no session/system bus needed), so a
Fill→FillConfirm round-trip flows: extension request → bridge intent
token + identity forwarding → daemon peer attestation + pin gate +
fill-token → bridge → daemon FillConfirm.

Axes asserted (each with a positive case AND a "flip it → denied"
negative twin so the binding is demonstrably load-bearing):

  * request origin URL
  * browser extension id
  * native-message peer identity (bridge process attestation)
  * browser parent executable
  * daemon session binding — defined here as the unlocked-vault
    precondition (the master key held in daemon memory) PLUS the
    originating-peer-pid binding on the fill-token (re-validated at
    confirm). There is no separate desktop/login session identifier in
    this path; "session" means exactly those two daemon-held facts.
  * approval expiry (intent-token TTL + fill-token TTL)
  * replay rejection (intent token AND fill-token are single-use)

Honesty note on "compositor/prompt target (window identity)": the pwd
approval path has NO compositor/qdshell window-id field today — qdshell
is qdwin-only and out of scope, and the approving "prompt" in this
design is the native bridge process itself (a browser child). The
daemon therefore binds the approval to the *originating peer pid* (the
bridge process that requested it), which is the only prompt/target
identity the daemon actually possesses and enforces. This file asserts
that real binding and does NOT fabricate a window-id check that the
code does not perform.

Two attestation styles are used so the peer/parent/extension axes are
not merely mocked:

  * the daemon's REAL ``_browser_bridge_allowed`` is exercised by
    patching only the three ``/proc`` readers it calls
    (``_read_proc_cmdline`` / ``_read_proc_ppid`` / ``_read_proc_exe``),
    so the actual "cmdline-is-bridge AND parent-is-browser" logic runs;
  * the bridge's REAL ``verify_parent`` derives ``extension_id`` from a
    synthetic argv and ``parent_exe`` from injected proc readers.

PyQt6/PySide6 is irrelevant here — this is a pure D-Bus-method-level
test driven without a bus.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module loading — bridge by spec (matches test_pwd_fill_bridge.py), daemon
# via sys.path insert (matches test_pwd_daemon_fill_save.py).
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent.parent
_BRIDGE = _REPO / "browser_bridge" / "qdistro_browser_bridge.py"
spec = importlib.util.spec_from_file_location("qdistro_browser_bridge", _BRIDGE)
bb = importlib.util.module_from_spec(spec)
sys.modules["qdistro_browser_bridge"] = bb
spec.loader.exec_module(bb)

sys.path.insert(0, str(_REPO / "pwd"))
import qdistro_pwd_daemon as d  # noqa: E402
from qdistro_pwd_audit import PwdAuditLog  # type: ignore  # noqa: E402
from qdistro_pwd_vault import (  # type: ignore  # noqa: E402
    add_item,
    create_vault,
    unlock_vault,
)

# ---------------------------------------------------------------------------
# Fixed identities for the happy path.
# ---------------------------------------------------------------------------

BROWSER_EXE = "/usr/lib64/firefox/firefox"
EXTENSION_ID = "qdistro@qdistro.local"
ORIGIN = "https://bank.example"
URL = "https://bank.example/login"
USERNAME = "alice@bank.example"
PASSWORD = "hunter2-correct-horse"

# The kernel-attested caller snapshot the daemon sees for the bridge
# process. The exe/uid here MUST match the pin set we store on the item,
# else pin_match denies regardless of everything else.
PEER_PID = 4242
PEER_UID = 1500
CALLER = {
    "uid": PEER_UID,
    "pid": PEER_PID,
    "exe": BROWSER_EXE,
    "exe_sha256": "deadbeef",
    "selinux_label": "user_u:user_r:qdistro_browser_t:s0",
    "cgroup": "",
}

# The bridge's view of its own parent (the browser). Used by verify_parent
# / handshake to derive extension_id + parent_exe.
BRIDGE_PARENT = {
    "ppid": 100,
    "parent_exe": BROWSER_EXE,
    "parent_selinux": "user_u:user_r:user_t:s0",
    "extension_id": EXTENSION_ID,
    "allowed": True,
}


# ---------------------------------------------------------------------------
# Daemon staging (real on-disk vault, no D-Bus). Mirrors the fixture in
# test_pwd_daemon_fill_save.py but kept local to avoid editing that file.
# ---------------------------------------------------------------------------

@pytest.fixture
def daemon(tmp_path, monkeypatch):
    vd = str(tmp_path / "vaults")
    audit_path = str(tmp_path / "audit.sqlite")
    monkeypatch.setattr(d, "VAULT_DIR", vd)
    monkeypatch.setattr(d, "AUDIT_DB", audit_path)
    monkeypatch.setattr(d, "BROWSER_PWD_VAULT", "passwords")
    create_vault(vd, "passwords", b"vault-pass")
    dmn = d.PwdDaemon.__new__(d.PwdDaemon)
    dmn._unlocked = {}
    dmn._audit = PwdAuditLog(audit_path)
    dmn._fill_tokens = {}
    dmn._fill_token_ttl = 120
    # Unlock the vault (daemon-session: the master key is held in memory).
    key = unlock_vault(vd, "passwords", b"vault-pass")
    dmn._unlocked["passwords"] = {
        "key": bytearray(key), "unlocked_at": int(time.time()),
        "last_use": int(time.time()),
    }
    # Store the credential pinned to the attested caller exe + uid +
    # selinux label — i.e. only the genuine browser process can read it.
    tag = f"pwd:{ORIGIN}/{USERNAME}"
    add_item(vd, "passwords", key, tag, PASSWORD.encode("utf-8"),
             pin_app_exe=CALLER["exe"],
             pin_selinux=CALLER["selinux_label"],
             pin_uid=CALLER["uid"],
             replace=True)
    return dmn, vd


# ---------------------------------------------------------------------------
# Bridge ↔ daemon adapter: route the bridge's outbound D-Bus call into the
# REAL daemon method, attesting the configurable peer identity. This is the
# native-message-peer / parent-exe seam.
# ---------------------------------------------------------------------------

class _DaemonRoutingClient(bb._BaseDBusClient):
    """Pretend D-Bus client that invokes the real daemon Fill/FillConfirm.

    ``peer`` is the kernel-attested caller snapshot the daemon will see;
    ``bridge_allowed`` is the result of the daemon's native-message
    peer / parent-exe attestation (``_browser_bridge_allowed``). Both
    are configurable per test so we can flip exactly one axis.
    """

    def __init__(self, dmn, *, peer=CALLER,
                 bridge_allowed=(True, "browser-bridge")):
        self.dmn = dmn
        self.peer = peer
        self.bridge_allowed = bridge_allowed
        self.calls = []

    def call(self, bus, service, object_path, interface, method,
             signature, body):
        # Pin the bus coordinates — the pwd daemon is SYSTEM-bus only.
        assert bus == "SYSTEM"
        assert service == "org.qdistro.Pwd1"
        self.calls.append({"method": method, "body": body})
        creds_json = body[0]
        with patch.object(self.dmn, "_peer_info",
                          return_value=(self.peer["uid"], self.peer["pid"])), \
             patch("qdistro_pwd_daemon.snapshot_caller",
                   return_value=self.peer), \
             patch("qdistro_pwd_daemon._browser_bridge_allowed",
                   return_value=self.bridge_allowed):
            fn = getattr(self.dmn, method)
            out = fn(creds_json, sender=":1.99")
        return json.loads(out)


@pytest.fixture(autouse=True)
def _fresh_bridge_secret():
    bb.reset_session_secret()
    bb._EXT_SECRETS.clear()
    yield
    bb._dbus_client = None
    bb._EXT_SECRETS.clear()


def _handshake(extension_id=EXTENSION_ID):
    """Run the bridge handshake so verify_intent_token has a per-extension
    secret on record. Returns the derived secret bytes."""
    identity = dict(BRIDGE_PARENT, extension_id=extension_id)
    reply = bb._handle_handshake({}, identity)
    assert reply["ok"] is True
    return bytes.fromhex(reply["session_secret_hex"])


def _mint_token(op, secret, *, ts_offset=0.0):
    import secrets as _s
    req_id = _s.token_hex(16)
    ts = time.time() + ts_offset
    mac = bb._compute_token_hmac(req_id, ts, op, secret=secret)
    return {"request_id": req_id, "ts": ts, "op": op, "hmac": mac}


def _fill(client, identity, token, *, url=URL, username=None):
    bb._dbus_client = client
    msg = {"url": url, "intent_token": token}
    if username is not None:
        msg["username"] = username
    return bb._handle_pwd_fill(msg, identity)


def _confirm(client, identity, token, fill_token, *, url=URL,
             username=USERNAME):
    bb._dbus_client = client
    return bb._handle_pwd_fill_confirm(
        {"url": url, "username": username, "fill_token": fill_token,
         "intent_token": token},
        identity)


# ---------------------------------------------------------------------------
# REAL-attestation helpers (no full-function mocking): drive the actual
# daemon _browser_bridge_allowed and bridge verify_parent through their
# /proc + argv seams so the peer / parent-exe / extension axes are
# genuinely exercised, per codex round-1 feedback.
# ---------------------------------------------------------------------------

# The on-disk bridge script the daemon expects as the peer's argv.
BRIDGE_SCRIPT = str(_REPO / "browser_bridge" / "qdistro_browser_bridge.py")
PPID_BROWSER = 100


def _attest_real(monkeypatch, dmn, *, peer_pid=PEER_PID,
                 cmdline=None, ppid=PPID_BROWSER, parent_exe=BROWSER_EXE):
    """Point the daemon's REAL _browser_bridge_allowed at synthetic /proc
    data by patching only the three proc readers it calls. The bridge
    script path must be on the daemon's allowlist."""
    monkeypatch.setattr(d, "BROWSER_BRIDGE_SCRIPT", BRIDGE_SCRIPT)
    monkeypatch.setattr(d, "_BROWSER_PARENT_EXES_ENV_OVERRIDE", (BROWSER_EXE,))
    cmd = cmdline if cmdline is not None else ["python3", BRIDGE_SCRIPT]
    monkeypatch.setattr(d, "_read_proc_cmdline",
                        lambda pid: cmd if pid == peer_pid else [])
    monkeypatch.setattr(d, "_read_proc_ppid",
                        lambda pid: ppid if pid == peer_pid else None)
    monkeypatch.setattr(d, "_read_proc_exe",
                        lambda pid: parent_exe if pid == ppid else "")


class _RealAttestClient(bb._BaseDBusClient):
    """Routes into the daemon WITHOUT stubbing _browser_bridge_allowed —
    the real attestation runs against the /proc seams set up by
    _attest_real. Only _peer_info / snapshot_caller (which the daemon
    derives from SO_PEERCRED + /proc in production) are supplied."""

    def __init__(self, dmn, peer=CALLER):
        self.dmn = dmn
        self.peer = peer
        self.calls = []

    def call(self, bus, service, object_path, interface, method,
             signature, body):
        self.calls.append({"method": method})
        with patch.object(self.dmn, "_peer_info",
                          return_value=(self.peer["uid"], self.peer["pid"])), \
             patch("qdistro_pwd_daemon.snapshot_caller",
                   return_value=self.peer):
            out = getattr(self.dmn, method)(body[0], sender=":1.99")
        return json.loads(out)


# ---------------------------------------------------------------------------
# The full happy path — all axes aligned → password delivered.
# ---------------------------------------------------------------------------

class TestApprovalHappyPathFullBinding:
    def test_correct_identity_succeeds_and_delivers_password(self, daemon):
        dmn, _ = daemon
        secret = _handshake()
        client = _DaemonRoutingClient(dmn)
        identity = dict(BRIDGE_PARENT)

        fill = _fill(client, identity, _mint_token("pwd.fill", secret))
        assert fill["ok"] is True, fill
        assert [c["username"] for c in fill["credentials"]] == [USERNAME]
        # Fill must NOT carry the password — only FillConfirm does.
        assert "password" not in fill["credentials"][0]
        fill_token = fill["fill_token"]

        confirm = _confirm(client, identity,
                           _mint_token("pwd.fill_confirm", secret),
                           fill_token)
        assert confirm["ok"] is True, confirm
        cred = confirm["credentials"][0]
        assert cred["username"] == USERNAME
        assert cred["password"] == PASSWORD
        assert cred["url"] == ORIGIN
        # The daemon actually fielded both calls on the SYSTEM bus.
        assert [c["method"] for c in client.calls] == ["Fill", "FillConfirm"]


# ---------------------------------------------------------------------------
# Axis 1 — request origin URL.
# ---------------------------------------------------------------------------

class TestOriginBinding:
    def test_wrong_origin_fill_finds_nothing(self, daemon):
        dmn, _ = daemon
        secret = _handshake()
        client = _DaemonRoutingClient(dmn)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret),
                     url="https://evil.example/login")
        assert fill["ok"] is False
        assert fill["error"] == "no_match"

    def test_confirm_origin_must_match_fill_token(self, daemon):
        """A fill-token minted for bank.example cannot be redeemed for a
        different origin even with a valid intent token."""
        dmn, _ = daemon
        secret = _handshake()
        client = _DaemonRoutingClient(dmn)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is True
        confirm = _confirm(client, dict(BRIDGE_PARENT),
                           _mint_token("pwd.fill_confirm", secret),
                           fill["fill_token"],
                           url="https://evil.example/login")
        assert confirm["ok"] is False
        assert confirm["error"] == "invalid_token"


# ---------------------------------------------------------------------------
# Axis 2 — browser extension id (bound via the per-extension HMAC secret).
# ---------------------------------------------------------------------------

class TestExtensionIdBinding:
    def test_token_minted_under_other_extension_secret_rejected(self, daemon):
        dmn, _ = daemon
        # Two extensions handshake on the same bridge process.
        secret_a = _handshake(extension_id="a" * 32)
        _handshake(extension_id="b" * 32)
        client = _DaemonRoutingClient(dmn)
        # Mint with A's secret, but present identity as extension B.
        token = _mint_token("pwd.fill", secret_a)
        identity_b = dict(BRIDGE_PARENT, extension_id="b" * 32)
        fill = _fill(client, identity_b, token)
        assert fill["ok"] is False
        assert fill["error"] == "intent_token_bad_hmac"
        # And the daemon was never reached — gated at the bridge.
        assert client.calls == []

    def test_token_minted_under_own_extension_secret_succeeds(self, daemon):
        dmn, _ = daemon
        ext = "c" * 32
        secret = _handshake(extension_id=ext)
        client = _DaemonRoutingClient(dmn)
        identity = dict(BRIDGE_PARENT, extension_id=ext)
        fill = _fill(client, identity, _mint_token("pwd.fill", secret))
        # Item is pinned to exe/uid, not extension_id, so a foreign
        # extension can't FORGE a token but a legit one still matches the
        # item by origin. The point: the token chain is extension-bound.
        assert fill["ok"] is True


# ---------------------------------------------------------------------------
# Axis 3 — native-message peer identity (daemon-side bridge attestation).
# ---------------------------------------------------------------------------

class TestNativeMessagePeerBinding:
    def test_daemon_rejects_when_peer_is_not_the_bridge(self, daemon):
        dmn, _ = daemon
        secret = _handshake()
        # The daemon's _browser_bridge_allowed says "not the bridge".
        client = _DaemonRoutingClient(
            dmn, bridge_allowed=(False, "not-browser-bridge"))
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is False
        assert fill["error"] == "policy_denied"


# ---------------------------------------------------------------------------
# Axis 4 — browser parent executable.
# ---------------------------------------------------------------------------

class TestParentExeBinding:
    def test_bridge_denies_when_parent_not_allowed(self, daemon):
        dmn, _ = daemon
        secret = _handshake()
        client = _DaemonRoutingClient(dmn)
        # verify_parent would have set allowed=False for a non-browser
        # parent; simulate that decision reaching the handler.
        identity = dict(BRIDGE_PARENT, allowed=False,
                        parent_exe="/usr/bin/python3")
        fill = _fill(client, identity, _mint_token("pwd.fill", secret))
        assert fill["ok"] is False
        assert fill["error"] == "parent_not_allowed"
        assert client.calls == []

    def test_daemon_pin_denies_foreign_exe_peer(self, daemon):
        """Even if the bridge attestation passed, the item is pinned to
        the firefox exe; a chromium-exe peer reading the same vault gets
        no match (the pin gate is the daemon's exe binding)."""
        dmn, _ = daemon
        secret = _handshake()
        foreign_peer = dict(CALLER, exe="/usr/bin/chromium")
        client = _DaemonRoutingClient(dmn, peer=foreign_peer)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is False
        assert fill["error"] == "no_match"


# ---------------------------------------------------------------------------
# Axis 5 — daemon session binding + originating-peer / prompt target.
#
# The daemon mints the fill-token for the ORIGINATING peer pid (the
# native bridge process that requested it). FillConfirm re-validates
# origin, username, AND the originating peer pid; pin_match
# independently re-validates the live caller's exe/selinux/uid. The
# vault being unlocked in daemon._unlocked is the session-binding
# precondition.
#
# Scope honesty: there is NO compositor/qdshell window-id field in the
# pwd approval path today (qdshell is qdwin-only and out of scope; the
# approving prompt is the bridge process itself). The only prompt/target
# identity the daemon possesses and enforces is the peer pid recorded on
# the fill-token — that is what we assert here, not a fabricated
# window-id check.
# ---------------------------------------------------------------------------

class TestDaemonSessionAndPromptTargetBinding:
    def test_fill_token_bound_to_originating_peer_pid(self, daemon):
        """A fill-token minted for peer pid X cannot be redeemed by a
        DIFFERENT process Y — even though Y satisfies the per-item pin
        set (same exe/uid/selinux). This is the prompt-target binding:
        the approval is single-*target*, not merely single-use."""
        dmn, _ = daemon
        secret = _handshake()
        # Fill from the genuine peer pid.
        fill_client = _DaemonRoutingClient(dmn, peer=CALLER)
        fill = _fill(fill_client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is True
        fill_token = fill["fill_token"]
        # A different process (other pid) with otherwise-identical,
        # pin-satisfying identity tries to redeem it.
        other_peer = dict(CALLER, pid=PEER_PID + 1)
        confirm_client = _DaemonRoutingClient(dmn, peer=other_peer)
        confirm = _confirm(confirm_client, dict(BRIDGE_PARENT),
                           _mint_token("pwd.fill_confirm", secret),
                           fill_token)
        assert confirm["ok"] is False
        assert confirm["error"] == "invalid_token"
        # And critically: the wrong peer must NOT have *burned* the
        # legitimate peer's approval (a DoS otherwise). The genuine
        # originating peer can still redeem it.
        good = _confirm(fill_client, dict(BRIDGE_PARENT),
                        _mint_token("pwd.fill_confirm", secret),
                        fill_token)
        assert good["ok"] is True, good
        assert good["credentials"][0]["password"] == PASSWORD

    def test_same_peer_pid_redeems_successfully(self, daemon):
        """Control for the negative above: the genuine originating peer
        DOES redeem the fill-token (so the pid check is not just always
        failing)."""
        dmn, _ = daemon
        secret = _handshake()
        client = _DaemonRoutingClient(dmn, peer=CALLER)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is True
        confirm = _confirm(client, dict(BRIDGE_PARENT),
                           _mint_token("pwd.fill_confirm", secret),
                           fill["fill_token"])
        assert confirm["ok"] is True
        assert confirm["credentials"][0]["password"] == PASSWORD

    def test_locked_vault_session_denies(self, daemon):
        """The unlocked-vault session precondition: a relocked vault (no
        master key in daemon memory) yields no approval even for a
        fully-correct request. This is one half of what "session
        binding" means in this path; the peer-pid binding above is the
        other half."""
        dmn, _ = daemon
        secret = _handshake()
        dmn._do_lock("passwords", reason="test")
        client = _DaemonRoutingClient(dmn)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is False
        assert fill["error"] == "vault_locked"


# ---------------------------------------------------------------------------
# Axis 6 — approval expiry (intent-token TTL + fill-token TTL).
# ---------------------------------------------------------------------------

class TestApprovalExpiry:
    def test_expired_intent_token_rejected(self, daemon):
        dmn, _ = daemon
        secret = _handshake()
        client = _DaemonRoutingClient(dmn)
        # Older than INTENT_TOKEN_TTL_S → expired at the bridge.
        stale = _mint_token("pwd.fill", secret,
                            ts_offset=-(bb.INTENT_TOKEN_TTL_S + 5.0))
        fill = _fill(client, dict(BRIDGE_PARENT), stale)
        assert fill["ok"] is False
        assert fill["error"] == "intent_token_expired"
        assert client.calls == []

    def test_future_intent_token_rejected(self, daemon):
        dmn, _ = daemon
        secret = _handshake()
        client = _DaemonRoutingClient(dmn)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret, ts_offset=60.0))
        assert fill["ok"] is False
        assert fill["error"] == "intent_token_future"

    def test_expired_fill_token_rejected_by_daemon(self, daemon):
        """The daemon's own fill-token TTL is load-bearing: once the
        recorded expiry passes, FillConfirm refuses even a fresh,
        same-peer intent token."""
        dmn, _ = daemon
        secret = _handshake()
        client = _DaemonRoutingClient(dmn)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is True
        # Expire every stored fill-token in place.
        for entry in dmn._fill_tokens.values():
            entry["expires"] = int(time.time()) - 1
        confirm = _confirm(client, dict(BRIDGE_PARENT),
                           _mint_token("pwd.fill_confirm", secret),
                           fill["fill_token"])
        assert confirm["ok"] is False
        assert confirm["error"] == "invalid_token"


# ---------------------------------------------------------------------------
# Axis 7 — replay rejection (intent token AND daemon fill-token).
# ---------------------------------------------------------------------------

class TestReplayRejection:
    def test_intent_token_single_use(self, daemon):
        dmn, _ = daemon
        secret = _handshake()
        client = _DaemonRoutingClient(dmn)
        token = _mint_token("pwd.fill", secret)
        first = _fill(client, dict(BRIDGE_PARENT), dict(token))
        assert first["ok"] is True
        # Same request_id again → replay reject at the bridge.
        second = _fill(client, dict(BRIDGE_PARENT), dict(token))
        assert second["ok"] is False
        assert second["error"] == "intent_token_replay"

    def test_consumed_fill_token_cannot_be_reused(self, daemon):
        """An already-consumed APPROVAL (fill-token) cannot be reused: the
        first FillConfirm pops it; a second, otherwise-valid confirm with
        a fresh intent token is refused."""
        dmn, _ = daemon
        secret = _handshake()
        client = _DaemonRoutingClient(dmn)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is True
        fill_token = fill["fill_token"]
        first = _confirm(client, dict(BRIDGE_PARENT),
                         _mint_token("pwd.fill_confirm", secret),
                         fill_token)
        assert first["ok"] is True
        assert first["credentials"][0]["password"] == PASSWORD
        # Replay the consumed approval with a brand-new intent token.
        second = _confirm(client, dict(BRIDGE_PARENT),
                          _mint_token("pwd.fill_confirm", secret),
                          fill_token)
        assert second["ok"] is False
        assert second["error"] == "invalid_token"


# ---------------------------------------------------------------------------
# REAL attestation — the daemon's actual _browser_bridge_allowed logic
# (axes 3 + 4) runs against synthetic /proc data; nothing about the
# cmdline-is-bridge / parent-is-browser decision is stubbed out.
# ---------------------------------------------------------------------------

class TestRealDaemonPeerAttestation:
    def test_genuine_bridge_peer_passes_real_attestation(self, daemon,
                                                          monkeypatch):
        dmn, _ = daemon
        _attest_real(monkeypatch, dmn)
        secret = _handshake()
        client = _RealAttestClient(dmn)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is True, fill
        assert fill["credentials"][0]["username"] == USERNAME

    def test_peer_cmdline_not_the_bridge_is_denied(self, daemon, monkeypatch):
        """Axis 3: a same-uid process whose argv is NOT the bridge script
        fails the daemon's real native-message peer attestation."""
        dmn, _ = daemon
        _attest_real(monkeypatch, dmn,
                     cmdline=["python3", "/tmp/evil_repl.py"])
        secret = _handshake()
        client = _RealAttestClient(dmn)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is False
        assert fill["error"] == "policy_denied"

    def test_peer_parent_not_a_browser_is_denied(self, daemon, monkeypatch):
        """Axis 4: the bridge script run by a NON-browser parent (e.g. a
        shell) fails the daemon's real parent-exe attestation."""
        dmn, _ = daemon
        _attest_real(monkeypatch, dmn, parent_exe="/usr/bin/bash")
        secret = _handshake()
        client = _RealAttestClient(dmn)
        fill = _fill(client, dict(BRIDGE_PARENT),
                     _mint_token("pwd.fill", secret))
        assert fill["ok"] is False
        assert fill["error"] == "policy_denied"


# ---------------------------------------------------------------------------
# REAL bridge identity derivation — verify_parent turns kernel-attested
# argv + /proc into the extension_id / parent_exe / allowed dict that
# the handlers consume (axes 2 + 4 at the bridge edge).
# ---------------------------------------------------------------------------

class TestRealBridgeIdentityDerivation:
    def test_verify_parent_derives_extension_id_from_argv(self):
        """Firefox passes the host-manifest path as argv[1] and the
        extension id as argv[2]; verify_parent must surface exactly that
        id (not a stdio-supplied one) and mark a browser parent allowed."""
        ext = "qdistro@qdistro.local"
        identity = bb.verify_parent(
            ppid_fn=lambda: PPID_BROWSER,
            exe_reader=lambda _p: BROWSER_EXE,
            selinux_reader=lambda _p: "user_u:user_r:user_t:s0",
            allowlist=(BROWSER_EXE,),
            argv=["bridge", "/path/host.json", ext])
        assert identity["allowed"] is True
        assert identity["parent_exe"] == BROWSER_EXE
        assert identity["extension_id"] == ext

    def test_verify_parent_denies_non_browser_parent(self):
        identity = bb.verify_parent(
            ppid_fn=lambda: 7,
            exe_reader=lambda _p: "/usr/bin/python3",
            selinux_reader=lambda _p: "",
            allowlist=(BROWSER_EXE,),
            argv=["bridge", "/path/host.json", "qdistro@qdistro.local"])
        assert identity["allowed"] is False

    def test_derived_identity_drives_full_approval(self, daemon, monkeypatch):
        """Glue: feed verify_parent's REAL output straight into the
        handlers + real daemon attestation. extension_id flows from argv
        through the handshake-derived secret, the daemon attests the peer
        for real, and the password is delivered — proving the axes line
        up end to end without a hand-built identity dict."""
        dmn, _ = daemon
        _attest_real(monkeypatch, dmn)
        ext = "qdistro@qdistro.local"
        identity = bb.verify_parent(
            ppid_fn=lambda: PPID_BROWSER,
            exe_reader=lambda _p: BROWSER_EXE,
            selinux_reader=lambda _p: "user_u:user_r:user_t:s0",
            allowlist=(BROWSER_EXE,),
            argv=["bridge", "/path/host.json", ext])
        # Handshake using the SAME derived identity (extension_id is
        # argv-attested here, not synthetic).
        reply = bb._handle_handshake({}, identity)
        secret = bytes.fromhex(reply["session_secret_hex"])
        client = _RealAttestClient(dmn)

        fill = _fill(client, identity, _mint_token("pwd.fill", secret))
        assert fill["ok"] is True, fill
        confirm = _confirm(client, identity,
                           _mint_token("pwd.fill_confirm", secret),
                           fill["fill_token"])
        assert confirm["ok"] is True, confirm
        assert confirm["credentials"][0]["password"] == PASSWORD
