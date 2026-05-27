"""qdistro-pwd daemon Fill / Save / FillConfirm unit tests.

Drives the daemon's browser-password methods against a real on-disk
vault without bringing up D-Bus.  Bypasses dbus.service.Object.__init__
so we can poke methods directly with a fake `sender` value and a
stubbed _peer_info / snapshot_caller.
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from unittest.mock import patch

# Ensure the pwd package directory is importable (same trick as the
# portal-key test suite).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pwd"))

import qdistro_pwd_daemon as d
from qdistro_pwd_audit import PwdAuditLog  # type: ignore
from qdistro_pwd_vault import (  # type: ignore
    add_item, create_vault, unlock_vault, list_items, get_item_payload,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BROWSER_EXE = "/usr/lib64/firefox/firefox"

# A caller snapshot that matches the pin_app_exe we set on stored items.
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
    """Build a daemon with a fresh passwords vault + audit db.

    Returns (daemon, vault_dir, audit_db_path).
    """
    vd = str(tmp_path / "vaults")
    audit_path = str(tmp_path / "audit.sqlite")
    monkeypatch.setattr(d, "VAULT_DIR", vd)
    monkeypatch.setattr(d, "AUDIT_DB", audit_path)
    monkeypatch.setattr(d, "BROWSER_PWD_VAULT", "passwords")
    monkeypatch.setattr(d, "_browser_bridge_allowed",
                        lambda _pid: (True, "test-bridge"))
    create_vault(vd, "passwords", b"vault-pass")
    daemon = d.PwdDaemon.__new__(d.PwdDaemon)
    daemon._unlocked = {}
    daemon._audit = PwdAuditLog(audit_path)
    return daemon, vd, audit_path


def _unlock(daemon, vd):
    """Pop the passwords vault into daemon._unlocked."""
    key = unlock_vault(vd, "passwords", b"vault-pass")
    daemon._unlocked["passwords"] = {
        "key": bytearray(key), "unlocked_at": 0, "last_use": 0,
    }
    return key


def _add_credential(vd, key, origin, username, password,
                    pin_app_exe=BROWSER_EXE):
    """Helper: add a pwd item directly to the vault on disk."""
    tag = f"pwd:{origin}/{username}"
    add_item(vd, "passwords", key, tag,
             password.encode("utf-8"),
             pin_app_exe=pin_app_exe,
             replace=True)


# ---------------------------------------------------------------------------
# URL normalisation helper
# ---------------------------------------------------------------------------

class TestNormalizeUrlOrigin:
    def test_basic_https(self):
        assert d._normalize_url_origin("https://example.com/path") == "https://example.com"

    def test_basic_http(self):
        assert d._normalize_url_origin("http://example.com/path") == "http://example.com"

    def test_non_standard_port(self):
        assert d._normalize_url_origin("https://example.com:8443/x") == "https://example.com:8443"

    def test_standard_https_port_stripped(self):
        assert d._normalize_url_origin("https://example.com:443/x") == "https://example.com"

    def test_standard_http_port_stripped(self):
        assert d._normalize_url_origin("http://example.com:80/x") == "http://example.com"

    def test_empty_url(self):
        assert d._normalize_url_origin("") == ""

    def test_no_host(self):
        assert d._normalize_url_origin("data:text/html,hello") == ""

    def test_case_normalised(self):
        assert d._normalize_url_origin("HTTPS://EXAMPLE.COM/Path") == "https://example.com"


# ---------------------------------------------------------------------------
# Fill — happy path
# ---------------------------------------------------------------------------

class TestFillHappyPath:
    def test_fill_returns_matching_credentials(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "s3cret")

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Fill(json.dumps({
                "url": "https://example.com/login",
                "username": None,
                "extension_id": "test@ext",
                "parent_exe": BROWSER_EXE,
            }), sender=":1.42"))

        assert result["ok"] is True
        assert len(result["credentials"]) == 1
        cred = result["credentials"][0]
        assert cred["username"] == "alice"
        assert cred["url"] == "https://example.com"
        # Fill must NOT return the password
        assert "password" not in cred

    def test_fill_returns_multiple_credentials(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "pw1")
        _add_credential(vd, key, "https://example.com", "bob", "pw2")

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Fill(json.dumps({
                "url": "https://example.com/login",
            }), sender=":1.42"))

        assert result["ok"] is True
        usernames = {c["username"] for c in result["credentials"]}
        assert usernames == {"alice", "bob"}

    def test_fill_filters_by_username(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "pw1")
        _add_credential(vd, key, "https://example.com", "bob", "pw2")

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Fill(json.dumps({
                "url": "https://example.com/",
                "username": "bob",
            }), sender=":1.42"))

        assert result["ok"] is True
        assert len(result["credentials"]) == 1
        assert result["credentials"][0]["username"] == "bob"


# ---------------------------------------------------------------------------
# Fill — error paths
# ---------------------------------------------------------------------------

class TestFillErrors:
    def test_fill_rejects_non_bridge_caller(self, staged, monkeypatch):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        monkeypatch.setattr(d, "_browser_bridge_allowed",
                            lambda _pid: (False, "not-browser-bridge"))

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Fill(json.dumps({
                "url": "https://example.com/",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "policy_denied"

    def test_fill_vault_locked(self, staged):
        daemon, vd, _ = staged
        # Do NOT unlock
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Fill(json.dumps({
                "url": "https://example.com/",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "vault_locked"

    def test_fill_no_match(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        # No credentials stored — empty vault

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Fill(json.dumps({
                "url": "https://example.com/",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "no_match"

    def test_fill_no_match_wrong_origin(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://other.com", "alice", "pw")

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Fill(json.dumps({
                "url": "https://example.com/",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "no_match"

    def test_fill_invalid_json(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Fill("not valid json", sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "invalid_request"

    def test_fill_missing_url(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Fill(json.dumps({}), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "invalid_request"

    def test_fill_policy_denied_different_exe(self, staged):
        """Credentials pinned to firefox are invisible to chromium."""
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "pw",
                        pin_app_exe=BROWSER_EXE)

        wrong_caller = dict(CALLER, exe="/usr/bin/chromium")
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller",
                   return_value=wrong_caller):
            result = json.loads(daemon.Fill(json.dumps({
                "url": "https://example.com/",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "no_match"


# ---------------------------------------------------------------------------
# Save — happy path
# ---------------------------------------------------------------------------

class TestSaveHappyPath:
    def test_save_new_credential(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Save(json.dumps({
                "url": "https://example.com/login",
                "username": "alice",
                "password": "s3cret",
                "extension_id": "test@ext",
                "parent_exe": BROWSER_EXE,
            }), sender=":1.42"))

        assert result["ok"] is True

        # Verify it's actually stored
        tag = "pwd:https://example.com/alice"
        payload = get_item_payload(vd, "passwords", key, tag)
        assert payload == b"s3cret"

    def test_save_updates_existing_credential(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "old-pw")

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Save(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
                "password": "new-pw",
                "extension_id": "test@ext",
                "parent_exe": BROWSER_EXE,
            }), sender=":1.42"))

        assert result["ok"] is True

        # Verify update
        tag = "pwd:https://example.com/alice"
        payload = get_item_payload(vd, "passwords", key, tag)
        assert payload == b"new-pw"

    def test_save_sets_pin_app_exe(self, staged):
        """Saved credentials carry the caller's exe as pin_app_exe so
        only the same browser can retrieve them via Fill."""
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            daemon.Save(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
                "password": "pw",
                "extension_id": "test@ext",
                "parent_exe": BROWSER_EXE,
            }), sender=":1.42")

        items = list_items(vd, "passwords")
        assert len(items) == 1
        assert items[0]["pin_app_exe"] == BROWSER_EXE


# ---------------------------------------------------------------------------
# Save — error paths
# ---------------------------------------------------------------------------

class TestSaveErrors:
    def test_save_rejects_non_bridge_caller(self, staged, monkeypatch):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        monkeypatch.setattr(d, "_browser_bridge_allowed",
                            lambda _pid: (False, "parent-not-browser"))

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Save(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
                "password": "pw",
                "extension_id": "test@ext",
                "parent_exe": BROWSER_EXE,
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "policy_denied"

    def test_save_vault_locked(self, staged):
        daemon, vd, _ = staged
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Save(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
                "password": "pw",
                "extension_id": "test@ext",
                "parent_exe": BROWSER_EXE,
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "vault_locked"

    def test_save_missing_url(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Save(json.dumps({
                "username": "alice",
                "password": "pw",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "invalid_request"

    def test_save_missing_username(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Save(json.dumps({
                "url": "https://example.com/",
                "password": "pw",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "invalid_request"

    def test_save_missing_password(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Save(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "invalid_request"

    def test_save_empty_password_rejected(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Save(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
                "password": "",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "invalid_request"

    def test_save_invalid_json(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.Save("{bad", sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "invalid_request"

    def test_save_policy_denied_on_update_different_exe(self, staged):
        """Cannot overwrite a credential pinned to firefox from chromium."""
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "old-pw",
                        pin_app_exe=BROWSER_EXE)

        wrong_caller = dict(CALLER, exe="/usr/bin/chromium")
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller",
                   return_value=wrong_caller):
            result = json.loads(daemon.Save(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
                "password": "evil-pw",
                "extension_id": "evil@ext",
                "parent_exe": "/usr/bin/chromium",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "policy_denied"

        # Original password unchanged
        tag = "pwd:https://example.com/alice"
        payload = get_item_payload(vd, "passwords", key, tag)
        assert payload == b"old-pw"


# ---------------------------------------------------------------------------
# FillConfirm — happy path
# ---------------------------------------------------------------------------

class TestFillConfirmHappyPath:
    def test_fill_confirm_returns_password(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "s3cret")

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.FillConfirm(json.dumps({
                "url": "https://example.com/login",
                "username": "alice",
                "extension_id": "test@ext",
                "parent_exe": BROWSER_EXE,
            }), sender=":1.42"))

        assert result["ok"] is True
        assert len(result["credentials"]) == 1
        cred = result["credentials"][0]
        assert cred["username"] == "alice"
        assert cred["password"] == "s3cret"
        assert cred["url"] == "https://example.com"


# ---------------------------------------------------------------------------
# FillConfirm — error paths
# ---------------------------------------------------------------------------

class TestFillConfirmErrors:
    def test_fill_confirm_rejects_non_bridge_caller(
            self, staged, monkeypatch):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "pw")
        monkeypatch.setattr(d, "_browser_bridge_allowed",
                            lambda _pid: (False, "not-browser-bridge"))

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.FillConfirm(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "policy_denied"

    def test_fill_confirm_vault_locked(self, staged):
        daemon, vd, _ = staged
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.FillConfirm(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "vault_locked"

    def test_fill_confirm_no_match(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.FillConfirm(json.dumps({
                "url": "https://example.com/",
                "username": "nonexistent",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "no_match"

    def test_fill_confirm_policy_denied(self, staged):
        """FillConfirm from wrong exe is denied even if the item exists."""
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "s3cret",
                        pin_app_exe=BROWSER_EXE)

        wrong_caller = dict(CALLER, exe="/usr/bin/chromium")
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller",
                   return_value=wrong_caller):
            result = json.loads(daemon.FillConfirm(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "policy_denied"

    def test_fill_confirm_missing_username(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.FillConfirm(json.dumps({
                "url": "https://example.com/",
            }), sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "invalid_request"

    def test_fill_confirm_invalid_json(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)
        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            result = json.loads(daemon.FillConfirm("bad json", sender=":1.42"))
        assert result["ok"] is False
        assert result["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

class TestAuditLogging:
    def test_fill_audit_logged(self, staged):
        daemon, vd, audit_path = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "pw")

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            daemon.Fill(json.dumps({
                "url": "https://example.com/",
            }), sender=":1.42")

        rows = daemon._audit.tail(10)
        fill_rows = [r for r in rows if r["op"] == "fill"]
        assert len(fill_rows) >= 1
        assert fill_rows[0]["decision"] == "allow"

    def test_fill_no_match_audit_logged(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            daemon.Fill(json.dumps({
                "url": "https://nothing.com/",
            }), sender=":1.42")

        rows = daemon._audit.tail(10)
        fill_rows = [r for r in rows if r["op"] == "fill"]
        assert len(fill_rows) >= 1
        assert fill_rows[0]["reason"] == "no-match"

    def test_save_audit_logged(self, staged):
        daemon, vd, _ = staged
        _unlock(daemon, vd)

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            daemon.Save(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
                "password": "pw",
                "extension_id": "test@ext",
                "parent_exe": BROWSER_EXE,
            }), sender=":1.42")

        rows = daemon._audit.tail(10)
        save_rows = [r for r in rows if r["op"] == "save"]
        assert len(save_rows) >= 1
        assert save_rows[0]["decision"] == "allow"
        assert save_rows[0]["reason"] == "saved"

    def test_fill_confirm_audit_logged(self, staged):
        daemon, vd, _ = staged
        key = _unlock(daemon, vd)
        _add_credential(vd, key, "https://example.com", "alice", "pw")

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            daemon.FillConfirm(json.dumps({
                "url": "https://example.com/",
                "username": "alice",
            }), sender=":1.42")

        rows = daemon._audit.tail(10)
        fc_rows = [r for r in rows if r["op"] == "fill-confirm"]
        assert len(fc_rows) >= 1
        assert fc_rows[0]["decision"] == "allow"

    def test_vault_locked_audit_logged(self, staged):
        daemon, vd, _ = staged
        # vault stays locked

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            daemon.Fill(json.dumps({
                "url": "https://example.com/",
            }), sender=":1.42")

        rows = daemon._audit.tail(10)
        fill_rows = [r for r in rows if r["op"] == "fill"]
        assert len(fill_rows) >= 1
        assert fill_rows[0]["decision"] == "deny"
        assert fill_rows[0]["reason"] == "vault-locked"


# ---------------------------------------------------------------------------
# End-to-end: Save then Fill then FillConfirm
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_save_then_fill_then_confirm(self, staged):
        """Full lifecycle: Save a credential, Fill to list it (no password),
        FillConfirm to get the password."""
        daemon, vd, _ = staged
        _unlock(daemon, vd)

        with patch.object(daemon, "_peer_info", return_value=(1500, 12345)), \
             patch("qdistro_pwd_daemon.snapshot_caller", return_value=CALLER):
            # Step 1: Save
            save_result = json.loads(daemon.Save(json.dumps({
                "url": "https://bank.com/login",
                "username": "user@bank.com",
                "password": "hunter2",
                "extension_id": "test@ext",
                "parent_exe": BROWSER_EXE,
            }), sender=":1.42"))
            assert save_result["ok"] is True

            # Step 2: Fill (no password returned)
            fill_result = json.loads(daemon.Fill(json.dumps({
                "url": "https://bank.com/",
            }), sender=":1.42"))
            assert fill_result["ok"] is True
            assert len(fill_result["credentials"]) == 1
            cred = fill_result["credentials"][0]
            assert cred["username"] == "user@bank.com"
            assert "password" not in cred

            # Step 3: FillConfirm (password returned)
            confirm_result = json.loads(daemon.FillConfirm(json.dumps({
                "url": "https://bank.com/",
                "username": "user@bank.com",
            }), sender=":1.42"))
            assert confirm_result["ok"] is True
            assert confirm_result["credentials"][0]["password"] == "hunter2"
