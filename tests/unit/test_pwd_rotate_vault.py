"""qdistro_pwd_vault rotate_vault / rotate_vault_tpm — task(108).

Pure unit; no D-Bus. Validates that rotating a vault's password (v1)
or PIN (v2) preserves all items byte-for-byte (master key unchanged)
while invalidating the old secret.
"""
from __future__ import annotations

import json
import pytest

from qdistro_pwd_vault import (  # type: ignore[import-not-found]
    VaultBadPassword, VaultIntegrityError,
    add_item, create_vault, create_vault_tpm,
    get_item_payload, list_items,
    rotate_vault, rotate_vault_tpm,
    unlock_vault, unlock_vault_tpm, vault_path,
)
from qdistro_pwd_tpm import (  # type: ignore[import-not-found]
    MockBackend, lookup_backend,
)


@pytest.fixture
def vd(tmp_path) -> str:
    return str(tmp_path / "vaults")


@pytest.fixture
def mock_be() -> MockBackend:
    return MockBackend()


# -- v1 / scrypt ----------------------------------------------------------

class TestRotateScrypt:
    def test_rotate_changes_unlock_secret(self, vd):
        create_vault(vd, "v", b"old-pass")
        rotate_vault(vd, "v", b"old-pass", b"new-pass")
        # Old password no longer works.
        with pytest.raises(VaultBadPassword):
            unlock_vault(vd, "v", b"old-pass")
        # New password works.
        key = unlock_vault(vd, "v", b"new-pass")
        assert len(key) == 32

    def test_rotate_preserves_items(self, vd):
        create_vault(vd, "v", b"old")
        master = unlock_vault(vd, "v", b"old")
        add_item(vd, "v", master, "gmail", b"swordfish")
        add_item(vd, "v", master, "github", b"hunter2",
                 pin_app_exe="/usr/bin/firefox")
        rotate_vault(vd, "v", b"old", b"new")
        new_master = unlock_vault(vd, "v", b"new")
        # Master key bytes are byte-for-byte unchanged — items decrypt
        # cleanly under the SAME master_key.
        assert new_master == master
        assert get_item_payload(vd, "v", new_master, "gmail") == b"swordfish"
        assert get_item_payload(vd, "v", new_master, "github") == b"hunter2"
        # Item metadata also unchanged (pin_app_exe survives).
        items = list_items(vd, "v")
        gh = next(it for it in items if it["tag"] == "github")
        assert gh["pin_app_exe"] == "/usr/bin/firefox"

    def test_rotate_wrong_old_raises(self, vd):
        create_vault(vd, "v", b"old")
        with pytest.raises(VaultBadPassword):
            rotate_vault(vd, "v", b"WRONG", b"new")
        # Vault still openable under old password.
        unlock_vault(vd, "v", b"old")

    def test_rotate_writes_new_salt(self, vd):
        create_vault(vd, "v", b"p")
        with open(vault_path(vd, "v")) as f:
            old_salt = json.load(f)["kdf"]["salt"]
        rotate_vault(vd, "v", b"p", b"q")
        with open(vault_path(vd, "v")) as f:
            new_body = json.load(f)
        assert new_body["kdf"]["salt"] != old_salt
        # rotated marker present.
        assert "rotated" in new_body
        assert isinstance(new_body["rotated"], int)

    def test_rotate_v2_through_v1_path_rejects(self, vd, mock_be):
        create_vault_tpm(vd, "tpmv", b"1234", mock_be)
        with pytest.raises(VaultIntegrityError):
            rotate_vault(vd, "tpmv", b"1234", b"5678")


# -- v2 / TPM -------------------------------------------------------------

class TestRotateTpm:
    def test_rotate_changes_unseal_pin(self, vd, mock_be):
        create_vault_tpm(vd, "v", b"old-pin", mock_be)
        rotate_vault_tpm(vd, "v", b"old-pin", b"new-pin",
                         mock_be, lookup_backend)
        with pytest.raises(VaultBadPassword):
            unlock_vault_tpm(vd, "v", b"old-pin", lookup_backend)
        key = unlock_vault_tpm(vd, "v", b"new-pin", lookup_backend)
        assert len(key) == 32

    def test_rotate_preserves_items(self, vd, mock_be):
        create_vault_tpm(vd, "v", b"123456", mock_be)
        master = unlock_vault_tpm(vd, "v", b"123456", lookup_backend)
        add_item(vd, "v", master, "ssh", b"id_rsa-blob")
        rotate_vault_tpm(vd, "v", b"123456", b"654321",
                         mock_be, lookup_backend)
        new_master = unlock_vault_tpm(vd, "v", b"654321", lookup_backend)
        assert new_master == master
        assert get_item_payload(vd, "v", new_master, "ssh") == b"id_rsa-blob"

    def test_rotate_wrong_old_raises(self, vd, mock_be):
        create_vault_tpm(vd, "v", b"correct", mock_be)
        with pytest.raises(VaultBadPassword):
            rotate_vault_tpm(vd, "v", b"WRONG", b"new",
                             mock_be, lookup_backend)
        unlock_vault_tpm(vd, "v", b"correct", lookup_backend)

    def test_rotate_preserves_pcr_binding(self, vd, mock_be, monkeypatch):
        # Vault sealed under STATE_X with PIN binding.
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "STATE_X")
        create_vault_tpm(vd, "v", b"pin", mock_be, pcrs="sha256:7,11")
        # Rotate under the SAME PCR state (the typical case — admin
        # changes PIN, firmware/initrd unchanged). Old PIN unseals,
        # new seal binds to current PCR state + new PIN.
        rotate_vault_tpm(vd, "v", b"pin", b"newpin",
                         mock_be, lookup_backend, pcrs="sha256:7,11")
        # New PIN unlocks under STATE_X.
        unlock_vault_tpm(vd, "v", b"newpin", lookup_backend)
        # Under STATE_Y + newpin: PCR mismatch → fails.
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "STATE_Y")
        with pytest.raises(VaultBadPassword):
            unlock_vault_tpm(vd, "v", b"newpin", lookup_backend)

    def test_rotate_v1_through_v2_path_rejects(self, vd, mock_be):
        create_vault(vd, "scrypt", b"p")
        with pytest.raises(VaultIntegrityError):
            rotate_vault_tpm(vd, "scrypt", b"p", b"q",
                             mock_be, lookup_backend)
