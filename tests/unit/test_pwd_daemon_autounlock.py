"""qdistro-pwd daemon StashPortalPin / AutoUnlockPortalKeys tests.

Drives the daemon's portal-keys auto-unlock methods against a real
on-disk vault + a stash file in tmp_path, using the MockBackend so
the tests don't need a TPM. Bypasses dbus.service.Object.__init__
so the methods get poked directly.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import qdistro_pwd_daemon as d
import qdistro_pwd_pinstash as ps
import qdistro_pwd_tpm as tpm
from qdistro_pwd_audit import PwdAuditLog  # type: ignore
from qdistro_pwd_vault import create_vault  # type: ignore


@pytest.fixture
def staged(tmp_path, monkeypatch):
    vd = str(tmp_path / "vaults")
    audit_path = str(tmp_path / "audit.sqlite")
    stash_path = str(tmp_path / "portal-keys-pin.tpm")
    monkeypatch.setattr(d, "VAULT_DIR", vd)
    monkeypatch.setattr(d, "AUDIT_DB", audit_path)
    monkeypatch.setattr(d, "PORTAL_KEYS_VAULT", "portal-keys")
    monkeypatch.setattr(d, "PORTAL_PIN_STASH_PATH", stash_path)
    monkeypatch.setattr(ps, "DEFAULT_STASH_PATH", stash_path)
    # Force the daemon to use MockBackend regardless of host TPM.
    monkeypatch.setattr(d, "select_backend",
                        lambda name=None: tpm.MockBackend())
    monkeypatch.setattr(d, "lookup_backend",
                        lambda name: tpm.MockBackend())
    create_vault(vd, "portal-keys", b"my-portal-pin")
    daemon = d.PwdDaemon.__new__(d.PwdDaemon)
    daemon._unlocked = {}
    daemon._audit = PwdAuditLog(audit_path)
    return daemon, vd, audit_path, stash_path


# -- StashPortalPin -----------------------------------------------------

class TestStashPin:
    def test_admin_only(self, staged):
        daemon, *_ = staged
        with patch.object(daemon, "_peer_info", return_value=(1500, 999)):
            with pytest.raises(d.PwdPolicyError):
                daemon.StashPortalPin([0x6D, 0x79], sender=":1.42")

    def test_seals_and_writes(self, staged):
        daemon, _, _, stash_path = staged
        pin_bytes = b"my-portal-pin"
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            ok = daemon.StashPortalPin([int(b) for b in pin_bytes],
                                       sender=":1.42")
        assert ok is True
        assert os.path.exists(stash_path)
        meta = ps.stash_meta(stash_path)
        assert meta["present"] is True
        assert meta["backend"] == "mock"

    def test_empty_pin_rejected(self, staged):
        daemon, *_ = staged
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            with pytest.raises(d.PwdPolicyError):
                daemon.StashPortalPin([], sender=":1.42")

    def test_overlong_pin_rejected(self, staged):
        daemon, *_ = staged
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            with pytest.raises(d.PwdPolicyError):
                daemon.StashPortalPin([0x41] * 200, sender=":1.42")


# -- AutoUnlockPortalKeys -----------------------------------------------

class TestAutoUnlock:
    def test_admin_only(self, staged):
        daemon, *_ = staged
        with patch.object(daemon, "_peer_info", return_value=(1500, 999)):
            with pytest.raises(d.PwdPolicyError):
                daemon.AutoUnlockPortalKeys(sender=":1.42")

    def test_no_stash_raises_not_found(self, staged):
        daemon, *_ = staged
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            with pytest.raises(d.PwdNotFound):
                daemon.AutoUnlockPortalKeys(sender=":1.42")

    def test_already_unlocked_idempotent(self, staged):
        daemon, *_ = staged
        daemon._unlocked["portal-keys"] = {
            "key": bytearray(b"x" * 32),
            "unlocked_at": 0, "last_use": 0,
        }
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            ok = daemon.AutoUnlockPortalKeys(sender=":1.42")
        assert ok is True

    def test_round_trip(self, staged):
        """Stash + auto-unlock end-to-end against the MockBackend."""
        daemon, _, _, _ = staged
        pin_bytes = b"my-portal-pin"
        # Mock VaultUnlocked signal (dbus.service.signal needs the
        # dbus bus; bypass it).
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                ok = daemon.StashPortalPin([int(b) for b in pin_bytes],
                                           sender=":1.42")
                assert ok is True
                assert "portal-keys" not in daemon._unlocked
                ok = daemon.AutoUnlockPortalKeys(sender=":1.42")
                assert ok is True
                assert "portal-keys" in daemon._unlocked

    def test_stale_pin_after_vault_password_change(self, staged):
        """If the vault is rebuilt with a new password but the stash
        still holds the OLD PIN, AutoUnlockPortalKeys must fail with
        BadPassword (not silently lock with garbage)."""
        daemon, vd, _, _ = staged
        # Stash with the right PIN initially.
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                daemon.StashPortalPin(
                    [int(b) for b in b"my-portal-pin"],
                    sender=":1.42")
        # Now rebuild the vault with a different password.
        os.unlink(os.path.join(vd, "portal-keys.vault"))
        create_vault(vd, "portal-keys", b"NEW-different-pin")
        with patch.object(daemon, "VaultUnlocked", lambda *a, **kw: None):
            with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
                with pytest.raises(d.PwdBadPassword):
                    daemon.AutoUnlockPortalKeys(sender=":1.42")


# -- PortalPinStashInfo -------------------------------------------------

class TestStashInfo:
    def test_admin_only(self, staged):
        daemon, *_ = staged
        with patch.object(daemon, "_peer_info", return_value=(1500, 999)):
            with pytest.raises(d.PwdPolicyError):
                daemon.PortalPinStashInfo(sender=":1.42")

    def test_absent(self, staged):
        daemon, *_ = staged
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            info = daemon.PortalPinStashInfo(sender=":1.42")
        assert bool(info["present"]) is False
        assert str(info["backend"]) == ""

    def test_present_after_stash(self, staged):
        daemon, *_ = staged
        with patch.object(daemon, "_peer_info", return_value=(1000, 999)):
            daemon.StashPortalPin([0x70, 0x69, 0x6E], sender=":1.42")
            info = daemon.PortalPinStashInfo(sender=":1.42")
        assert bool(info["present"]) is True
        assert str(info["backend"]) == "mock"
