"""qdistro-pwd vault crypto + on-disk format tests.

Pure unit; no D-Bus, no daemon. Hits qdistro_pwd_vault directly with a
tmp vault dir.
"""
from __future__ import annotations

import json
import os
import pytest

from qdistro_pwd_vault import (  # type: ignore[import-not-found]
    VaultBadPassword, VaultDuplicate, VaultIntegrityError, VaultNotFound,
    add_item, create_vault, delete_item, get_item_payload, get_item_pins,
    list_items, list_vaults, unlock_vault, vault_path,
)


@pytest.fixture
def vd(tmp_path) -> str:
    return str(tmp_path / "vaults")


def test_create_then_unlock_returns_master_key(vd):
    create_vault(vd, "primary", b"hunter2")
    key = unlock_vault(vd, "primary", b"hunter2")
    assert isinstance(key, bytes)
    assert len(key) == 32


def test_unlock_wrong_password_raises(vd):
    create_vault(vd, "primary", b"hunter2")
    with pytest.raises(VaultBadPassword):
        unlock_vault(vd, "primary", b"wrong")


def test_create_duplicate_raises(vd):
    create_vault(vd, "v1", b"p")
    with pytest.raises(VaultDuplicate):
        create_vault(vd, "v1", b"p")


def test_invalid_vault_name_rejected(vd):
    with pytest.raises(ValueError):
        vault_path(vd, "")
    with pytest.raises(ValueError):
        vault_path(vd, "../escape")
    with pytest.raises(ValueError):
        vault_path(vd, ".hidden")


def test_add_item_then_list(vd):
    create_vault(vd, "v1", b"p")
    key = unlock_vault(vd, "v1", b"p")
    add_item(vd, "v1", key, "gmail.com", b"secret123",
             pin_app_exe="/usr/bin/firefox")
    items = list_items(vd, "v1")
    assert [it["tag"] for it in items] == ["gmail.com"]
    assert items[0]["pin_app_exe"] == "/usr/bin/firefox"
    assert items[0]["pin_uid"] is None


def test_get_item_roundtrip(vd):
    create_vault(vd, "v1", b"p")
    key = unlock_vault(vd, "v1", b"p")
    add_item(vd, "v1", key, "x", b"value-x")
    add_item(vd, "v1", key, "y", b"value-y")
    assert get_item_payload(vd, "v1", key, "x") == b"value-x"
    assert get_item_payload(vd, "v1", key, "y") == b"value-y"


def test_get_item_with_wrong_key_raises_integrity(vd):
    create_vault(vd, "v1", b"p")
    key = unlock_vault(vd, "v1", b"p")
    add_item(vd, "v1", key, "x", b"value-x")
    bogus = b"\x00" * 32
    with pytest.raises(VaultIntegrityError):
        get_item_payload(vd, "v1", bogus, "x")


def test_swap_two_item_ciphertexts_fails_integrity(vd, tmp_path):
    """AAD binding (vault, tag): swapping two items on disk must fail
    decrypt rather than silently mis-deliver."""
    create_vault(vd, "v1", b"p")
    key = unlock_vault(vd, "v1", b"p")
    add_item(vd, "v1", key, "alpha", b"alphavalue")
    add_item(vd, "v1", key, "beta",  b"betavalue")
    path = vault_path(vd, "v1")
    body = json.loads(open(path).read())
    a, b = body["items"][0], body["items"][1]
    a["ciphertext"], b["ciphertext"] = b["ciphertext"], a["ciphertext"]
    a["nonce"], b["nonce"] = b["nonce"], a["nonce"]
    open(path, "w").write(json.dumps(body))
    with pytest.raises(VaultIntegrityError):
        get_item_payload(vd, "v1", key, "alpha")


def test_add_item_duplicate_rejected_unless_replace(vd):
    create_vault(vd, "v1", b"p")
    key = unlock_vault(vd, "v1", b"p")
    add_item(vd, "v1", key, "x", b"a")
    with pytest.raises(VaultDuplicate):
        add_item(vd, "v1", key, "x", b"b")
    add_item(vd, "v1", key, "x", b"b", replace=True)
    assert get_item_payload(vd, "v1", key, "x") == b"b"


def test_delete_item_removes(vd):
    create_vault(vd, "v1", b"p")
    key = unlock_vault(vd, "v1", b"p")
    add_item(vd, "v1", key, "x", b"a")
    assert delete_item(vd, "v1", "x") is True
    assert delete_item(vd, "v1", "x") is False
    with pytest.raises(VaultNotFound):
        get_item_payload(vd, "v1", key, "x")


def test_get_item_pins_no_decrypt(vd):
    create_vault(vd, "v1", b"p")
    key = unlock_vault(vd, "v1", b"p")
    add_item(vd, "v1", key, "x", b"v",
             pin_app_exe="/bin/cat", pin_uid=1500)
    pins = get_item_pins(vd, "v1", "x")  # no key needed
    assert pins == {"pin_app_exe": "/bin/cat", "pin_selinux": "",
                    "pin_uid": 1500}


def test_list_vaults(vd):
    assert list_vaults(vd) == []
    create_vault(vd, "alpha", b"p")
    create_vault(vd, "beta",  b"p")
    assert list_vaults(vd) == ["alpha", "beta"]


def test_atomic_write_leaves_no_partial(vd):
    create_vault(vd, "v1", b"p")
    path = vault_path(vd, "v1")
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
    # vault dir mode must be 0700 on creation.
    assert (os.stat(vd).st_mode & 0o777) == 0o700


def test_vault_file_mode_is_600(vd):
    create_vault(vd, "v1", b"p")
    path = vault_path(vd, "v1")
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_format_version_reject_unknown(vd, tmp_path):
    create_vault(vd, "v1", b"p")
    path = vault_path(vd, "v1")
    body = json.loads(open(path).read())
    body["version"] = 999
    open(path, "w").write(json.dumps(body))
    with pytest.raises(VaultIntegrityError):
        unlock_vault(vd, "v1", b"p")


def test_swap_tag_in_aad_fails_integrity(vd):
    """Modifying just the tag string on disk (AAD field) MUST fail
    decryption — that's the AAD binding."""
    create_vault(vd, "v1", b"p")
    key = unlock_vault(vd, "v1", b"p")
    add_item(vd, "v1", key, "real", b"v1")
    path = vault_path(vd, "v1")
    body = json.loads(open(path).read())
    body["items"][0]["tag"] = "fake"
    open(path, "w").write(json.dumps(body))
    with pytest.raises(VaultIntegrityError):
        get_item_payload(vd, "v1", key, "fake")


def test_swap_vault_name_in_aad_fails_unlock(vd):
    """Renaming a vault on disk MUST fail at the unlock step — the
    AAD on the sealed master key binds the vault name, so even a
    correct password won't unseal once the file is renamed."""
    create_vault(vd, "v1", b"p")
    add_item(vd, "v1", unlock_vault(vd, "v1", b"p"), "x", b"v")
    src = vault_path(vd, "v1")
    dst = vault_path(vd, "v2")
    os.rename(src, dst)
    body = json.loads(open(dst).read())
    body["name"] = "v2"
    open(dst, "w").write(json.dumps(body))
    with pytest.raises(VaultBadPassword):
        unlock_vault(vd, "v2", b"p")
