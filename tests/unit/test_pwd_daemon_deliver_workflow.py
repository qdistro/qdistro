"""qdistro-pwd daemon DeliverToWorkflow unit tests.

Drives the broker-only secret-delivery method against a real on-disk
vault without bringing up D-Bus, mirroring the GetPortalKey test
harness. The SecretDeliveredToWorkflow signal is stubbed (no live bus).
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

import qdistro_pwd_daemon as d
from qdistro_pwd_audit import PwdAuditLog  # type: ignore
from qdistro_pwd_vault import (  # type: ignore
    create_vault, unlock_vault, add_item,
)

PASSWORD = b"top-secret"
SECRET = "ssh-ed25519-PRIVATE-KEY-BYTES"


@pytest.fixture
def staged(tmp_path, monkeypatch):
    vd = str(tmp_path / "vaults")
    audit_path = str(tmp_path / "audit.sqlite")
    monkeypatch.setattr(d, "VAULT_DIR", vd)
    monkeypatch.setattr(d, "AUDIT_DB", audit_path)
    create_vault(vd, "dev", PASSWORD)
    key = unlock_vault(vd, "dev", PASSWORD)
    add_item(vd, "dev", key, "github-ssh-key", SECRET.encode("utf-8"))
    daemon = d.PwdDaemon.__new__(d.PwdDaemon)
    daemon._unlocked = {}
    daemon._audit = PwdAuditLog(audit_path)
    return daemon, vd, key


def _unlock(daemon, key):
    daemon._unlocked["dev"] = {
        "key": bytearray(key), "unlocked_at": 0, "last_use": 0,
    }


def test_root_gets_secret(staged):
    daemon, vd, key = staged
    _unlock(daemon, key)
    with patch.object(daemon, "_peer_info", return_value=(0, 4321)), \
         patch.object(daemon, "SecretDeliveredToWorkflow") as sig:
        out = daemon.DeliverToWorkflow("dev", "github-ssh-key", "run-77",
                                       sender=":1.5")
    assert out == SECRET
    sig.assert_called_once_with("run-77", "dev", "github-ssh-key")
    rows = daemon._audit.tail(5)
    deliver_rows = [r for r in rows if r["op"] == "deliver-workflow"]
    assert deliver_rows and deliver_rows[0]["decision"] == "allow"
    # The audit must NOT contain the secret value.
    assert all(SECRET not in str(r.values()) for r in rows)


def test_non_root_denied(staged):
    daemon, vd, key = staged
    _unlock(daemon, key)
    with patch.object(daemon, "_peer_info", return_value=(1000, 4321)):
        with pytest.raises(d.PwdPolicyError):
            daemon.DeliverToWorkflow("dev", "github-ssh-key", "run-1",
                                     sender=":1.5")


def test_locked_vault_denied(staged):
    daemon, vd, key = staged  # not unlocked
    with patch.object(daemon, "_peer_info", return_value=(0, 4321)), \
         patch.object(daemon, "SecretDeliveredToWorkflow"):
        with pytest.raises(d.PwdNotUnlocked):
            daemon.DeliverToWorkflow("dev", "github-ssh-key", "run-1",
                                     sender=":1.5")


def test_missing_item_denied(staged):
    daemon, vd, key = staged
    _unlock(daemon, key)
    with patch.object(daemon, "_peer_info", return_value=(0, 4321)), \
         patch.object(daemon, "SecretDeliveredToWorkflow"):
        with pytest.raises(d.PwdNotFound):
            daemon.DeliverToWorkflow("dev", "no-such-tag", "run-1",
                                     sender=":1.5")
    rows = daemon._audit.tail(5)
    deny = [r for r in rows if r["op"] == "deliver-workflow"]
    assert deny and deny[0]["decision"] == "deny"
