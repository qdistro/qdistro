"""qdistro-pwd daemon GetPortalKey unit tests.

Drives the daemon's GetPortalKey method against a real on-disk vault
without bringing up D-Bus. Bypasses dbus.service.Object.__init__ so
we can poke methods directly with a fake `sender` value and a stubbed
_peer_info.
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch

import qdistro_pwd_daemon as d
from qdistro_pwd_audit import PwdAuditLog  # type: ignore
from qdistro_pwd_vault import (  # type: ignore
    create_vault, unlock_vault, get_item_payload, list_items,
)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """Build a daemon with a fresh vault dir + audit db. Returns
    (daemon, vault_dir, audit_db_path)."""
    vd = str(tmp_path / "vaults")
    audit_path = str(tmp_path / "audit.sqlite")
    monkeypatch.setattr(d, "VAULT_DIR", vd)
    monkeypatch.setattr(d, "AUDIT_DB", audit_path)
    monkeypatch.setattr(d, "PORTAL_KEYS_VAULT", "portal-keys")
    create_vault(vd, "portal-keys", b"top-secret")
    daemon = d.PwdDaemon.__new__(d.PwdDaemon)
    daemon._unlocked = {}
    daemon._audit = PwdAuditLog(audit_path)
    return daemon, vd, audit_path


def _unlock(daemon, vd):
    """Pop the portal-keys vault into daemon._unlocked."""
    key = unlock_vault(vd, "portal-keys", b"top-secret")
    daemon._unlocked["portal-keys"] = {
        "key": bytearray(key), "unlocked_at": 0, "last_use": 0,
    }


def test_get_portal_key_locked_raises(staged):
    daemon, vd, _ = staged
    with patch.object(daemon, "_peer_info", return_value=(1500, 1234)):
        with pytest.raises(d.PwdNotUnlocked):
            daemon.GetPortalKey("org.example.App", sender=":1.42")


def test_get_portal_key_provisions_then_returns_same_bytes(staged):
    daemon, vd, _ = staged
    _unlock(daemon, vd)
    with patch.object(daemon, "_peer_info", return_value=(1500, 1234)):
        first = bytes(b for b in daemon.GetPortalKey("org.example.App",
                                                     sender=":1.42"))
        second = bytes(b for b in daemon.GetPortalKey("org.example.App",
                                                      sender=":1.42"))
    assert isinstance(first, bytes)
    assert len(first) == 32
    assert first == second  # stable across calls


def test_get_portal_key_distinct_app_ids_get_distinct_keys(staged):
    daemon, vd, _ = staged
    _unlock(daemon, vd)
    with patch.object(daemon, "_peer_info", return_value=(1500, 1234)):
        a = bytes(b for b in daemon.GetPortalKey("org.example.A", sender=":1.42"))
        b = bytes(b for b in daemon.GetPortalKey("org.example.B", sender=":1.42"))
    assert a != b


def test_get_portal_key_invalid_app_id_rejected(staged):
    daemon, vd, _ = staged
    _unlock(daemon, vd)
    with patch.object(daemon, "_peer_info", return_value=(1500, 1234)):
        for bad in ["", "../escape", ".dotfile", "with/slash"]:
            with pytest.raises(d.PwdPolicyError):
                daemon.GetPortalKey(bad, sender=":1.42")


def test_get_portal_key_persists_under_pin_uid(staged):
    """Auto-provisioned items carry pin_uid = caller uid so admin can
    audit which silo provisioned which app key."""
    daemon, vd, _ = staged
    _unlock(daemon, vd)
    with patch.object(daemon, "_peer_info", return_value=(1500, 1234)):
        daemon.GetPortalKey("org.example.X", sender=":1.42")
    items = list_items(vd, "portal-keys")
    assert len(items) == 1
    it = items[0]
    assert it["tag"] == "portal/org.example.X"
    assert it["pin_uid"] == 1500


def test_get_portal_key_audit_logged(staged):
    daemon, vd, audit_path = staged
    _unlock(daemon, vd)
    with patch.object(daemon, "_peer_info", return_value=(1500, 1234)):
        daemon.GetPortalKey("org.example.Y", sender=":1.42")
        daemon.GetPortalKey("org.example.Y", sender=":1.42")
    rows = daemon._audit.tail(10)
    ops = [r["op"] for r in rows]
    assert "portal-get" in ops
    portal_rows = [r for r in rows if r["op"] == "portal-get"]
    assert len(portal_rows) == 2
    # Order: most recent first; second call uses existing key.
    assert portal_rows[0]["reason"] == "existing-key"
    assert portal_rows[1]["reason"] == "provisioned"


def test_get_portal_key_distinct_callers_share_same_key_per_app_id(staged):
    """The portal key is keyed on app_id, NOT caller uid — multiple silos
    of the same flatpak app must converge to the same key (otherwise
    storage written by one silo's launch can't be decrypted by another's)."""
    daemon, vd, _ = staged
    _unlock(daemon, vd)
    with patch.object(daemon, "_peer_info", return_value=(1500, 1234)):
        a = bytes(b for b in daemon.GetPortalKey("org.example.Z", sender=":1.42"))
    with patch.object(daemon, "_peer_info", return_value=(1501, 5678)):
        b = bytes(b for b in daemon.GetPortalKey("org.example.Z", sender=":1.43"))
    assert a == b
