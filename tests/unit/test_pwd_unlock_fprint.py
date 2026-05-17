"""qdistro-pwd daemon Pwd1.UnlockVaultFprint tests.

Direct-fprintd unlock: caller supplies vault name + secret; daemon
runs a fprintd Verify cycle and unseals on match. Bypasses polkit
(faster path for app-driven dialogs), admin-uid only.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import qdistro_pwd_daemon as d
import qdistro_pwd_tpm as tpm
from qdistro_pwd_audit import PwdAuditLog  # type: ignore
from qdistro_pwd_vault import create_vault  # type: ignore


@pytest.fixture
def staged(tmp_path, monkeypatch):
    vd = str(tmp_path / "vaults")
    audit_path = str(tmp_path / "audit.sqlite")
    monkeypatch.setattr(d, "VAULT_DIR", vd)
    monkeypatch.setattr(d, "AUDIT_DB", audit_path)
    monkeypatch.setattr(d, "select_backend",
                        lambda name=None: tpm.MockBackend())
    monkeypatch.setattr(d, "lookup_backend",
                        lambda name: tpm.MockBackend())
    create_vault(vd, "secrets", b"my-vault-pwd")
    daemon = d.PwdDaemon.__new__(d.PwdDaemon)
    daemon._unlocked = {}
    daemon._audit = PwdAuditLog(audit_path)
    return daemon, vd, audit_path


class TestAccessControl:
    def test_non_admin_rejected(self, staged):
        daemon, *_ = staged
        with patch.object(daemon, "_peer_info", return_value=(2000, 999)):
            with pytest.raises(d.PwdPolicyError):
                daemon.UnlockVaultFprint("secrets", "my-vault-pwd",
                                         sender=":1.42")


class TestFprintGate:
    def test_unavailable_raises_policy_error(self, staged, monkeypatch):
        daemon, *_ = staged
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: False)
        monkeypatch.setattr(d, "fprint_verify",
                            lambda *a, **kw: pytest.fail("should not be called"))
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            with pytest.raises(d.PwdPolicyError):
                daemon.UnlockVaultFprint("secrets", "my-vault-pwd",
                                         sender=":1.42")
        # Vault stays locked.
        assert "secrets" not in daemon._unlocked

    def test_no_match_raises_bad_password(self, staged, monkeypatch):
        daemon, *_ = staged
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: True)
        monkeypatch.setattr(d, "fprint_verify",
                            lambda u, bus: (False, "fprint:verify-no-match"))
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            with pytest.raises(d.PwdBadPassword):
                daemon.UnlockVaultFprint("secrets", "my-vault-pwd",
                                         sender=":1.42")
        assert "secrets" not in daemon._unlocked

    def test_match_unlocks(self, staged, monkeypatch):
        daemon, *_ = staged
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: True)
        monkeypatch.setattr(d, "fprint_verify",
                            lambda u, bus: (True, "fprint-match"))
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                ok = daemon.UnlockVaultFprint(
                    "secrets", "my-vault-pwd", sender=":1.42")
        assert ok is True
        assert "secrets" in daemon._unlocked

    def test_match_but_wrong_pin(self, staged, monkeypatch):
        """Fingerprint passes but the PIN is wrong: must surface
        BadPassword (not silently unlock)."""
        daemon, *_ = staged
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: True)
        monkeypatch.setattr(d, "fprint_verify",
                            lambda u, bus: (True, "fprint-match"))
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                with pytest.raises(d.PwdBadPassword):
                    daemon.UnlockVaultFprint(
                        "secrets", "wrong-pin", sender=":1.42")
        assert "secrets" not in daemon._unlocked

    def test_unknown_vault(self, staged, monkeypatch):
        daemon, *_ = staged
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: True)
        monkeypatch.setattr(d, "fprint_verify",
                            lambda u, bus: (True, "fprint-match"))
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            with pytest.raises(d.PwdNotFound):
                daemon.UnlockVaultFprint(
                    "does-not-exist", "x", sender=":1.42")


class TestAuditTrail:
    def test_record_on_match(self, staged, monkeypatch):
        daemon, _, audit_path = staged
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: True)
        monkeypatch.setattr(d, "fprint_verify",
                            lambda u, bus: (True, "fprint-match"))
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                daemon.UnlockVaultFprint(
                    "secrets", "my-vault-pwd", sender=":1.42")
        log = PwdAuditLog(audit_path)
        rows = log.tail(5)
        assert any("fprint-pass" in str(r.get("reason", ""))
                   for r in rows)

    def test_record_on_unavailable(self, staged, monkeypatch):
        daemon, _, audit_path = staged
        monkeypatch.setattr(d, "fprint_is_available", lambda bus: False)
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            with pytest.raises(d.PwdPolicyError):
                daemon.UnlockVaultFprint(
                    "secrets", "my-vault-pwd", sender=":1.42")
        log = PwdAuditLog(audit_path)
        rows = log.tail(5)
        assert any("fprint-unavailable" in str(r.get("reason", ""))
                   for r in rows)
