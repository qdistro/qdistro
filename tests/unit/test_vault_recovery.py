"""Tests for the vault recovery bundle (06-backup-dr §3.4 / fix F1) and the
re-seal-on-new-TPM path. Pure unit; MockBackend stands in for the TPM."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PWD_DIR = REPO_ROOT / "pwd"
sys.path.insert(0, str(PWD_DIR))

# Plain imports, NOT spec_from_file_location, so every test module shares the
# SAME module objects. (test_vault_recovery_export.py's header already says
# this; this file did not follow it, and that was a real bug.)
#
# What went wrong: the old `_load()` helper re-executed each module and
# *overwrote* sys.modules["qdistro_pwd_tpm"] and ["qdistro_pwd_vault"] with
# fresh objects, at module scope — so it ran during pytest's COLLECTION pass,
# before any test in any file had run. Every class in those modules then
# existed twice under one name.
#
# qdistro_pwd_vault.unlock_vault_tpm imports TpmAuthFailed *lazily, at call
# time* (qdistro_pwd_vault.py:284-291, deliberately, to keep the v1 path
# decoupled from the TPM module), so it resolved the SECOND class object while
# the MockBackend under test — constructed from the first — raised the FIRST.
# `except TpmAuthFailed` therefore did not catch it, the exception escaped
# instead of being converted to VaultBadPassword, and five tests in
# test_pwd_tpm.py and test_pwd_rotate_vault.py failed.
#
# The tell was that they passed in isolation and failed in a full run, with no
# single file reproducing it — the poisoning happens at collection, so it
# depends on which files are collected, not on execution order. Cost: a
# permanently red full-suite run that everyone had learned to read past.
import qdistro_pwd_tpm as tpm  # noqa: E402
import qdistro_pwd_vault as vault  # noqa: E402
import qdistro_vault_recovery as rec  # noqa: E402


MK = bytes(range(32))  # a deterministic 32-byte master key


# ---- bundle round-trip ----------------------------------------------

def test_export_decrypt_roundtrip():
    bundle = rec.export_recovery_bundle(MK, b"correct horse battery staple")
    assert bundle["version"] == rec.RECOVERY_FORMAT_VERSION
    assert bundle["kdf"]["n"] == rec.RECOVERY_SCRYPT_N
    got = rec.decrypt_recovery_bundle(bundle, b"correct horse battery staple")
    assert got == MK


def test_wrong_passphrase_rejected():
    bundle = rec.export_recovery_bundle(MK, b"right")
    with pytest.raises(rec.RecoveryBadPassphrase):
        rec.decrypt_recovery_bundle(bundle, b"wrong")


def test_elevated_work_factor_vs_vault():
    # The recovery bundle must be at least as hard as the interactive vault.
    assert rec.RECOVERY_SCRYPT_N >= vault.SCRYPT_N
    assert rec.RECOVERY_SCRYPT_N > vault.SCRYPT_N  # explicitly elevated


def test_tamper_detected():
    bundle = rec.export_recovery_bundle(MK, b"pw")
    # Flip one ciphertext byte -> AEAD auth fails.
    import base64
    ct = bytearray(base64.b64decode(bundle["aead"]["ciphertext"]))
    ct[0] ^= 0xFF
    bundle["aead"]["ciphertext"] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(rec.RecoveryBadPassphrase):
        rec.decrypt_recovery_bundle(bundle, b"pw")


def test_label_binding():
    bundle = rec.export_recovery_bundle(MK, b"pw", label="vault-A")
    # Right label decrypts; a caller expecting a different label is refused
    # before any crypto.
    assert rec.decrypt_recovery_bundle(bundle, b"pw", label="vault-A") == MK
    with pytest.raises(rec.RecoveryIntegrityError):
        rec.decrypt_recovery_bundle(bundle, b"pw", label="vault-B")
    # Swapping the label field also fails (AAD is bound to the stored label).
    bundle["label"] = "vault-B"
    with pytest.raises(rec.RecoveryBadPassphrase):
        rec.decrypt_recovery_bundle(bundle, b"pw")


def test_rejects_bad_inputs():
    with pytest.raises(rec.RecoveryError):
        rec.export_recovery_bundle(b"short", b"pw")
    with pytest.raises(rec.RecoveryError):
        rec.export_recovery_bundle(MK, b"")  # empty passphrase


def test_unsupported_version():
    bundle = rec.export_recovery_bundle(MK, b"pw")
    bundle["version"] = 99
    with pytest.raises(rec.RecoveryIntegrityError):
        rec.decrypt_recovery_bundle(bundle, b"pw")


def test_write_read_roundtrip(tmp_path):
    path = str(tmp_path / "recovery.json")
    rec.write_recovery_bundle(path, MK, b"pw", label="lbl")
    assert oct(os.stat(path).st_mode)[-3:] == "600"
    bundle = rec.read_recovery_bundle(path)
    assert rec.decrypt_recovery_bundle(bundle, b"pw", label="lbl") == MK


# ---- full DR flow: machine death -> recover -> reseal ---------------

def test_machine_death_recover_and_reseal(tmp_path, monkeypatch):
    """The headline §3.4 scenario: a v2 TPM vault on machine A; a recovery
    bundle in the backup; the machine dies; on machine B the .vault file is
    restored, the master key is recovered from the bundle (+ passphrase) and
    re-sealed into the new TPM, and the items still decrypt."""
    monkeypatch.setenv("QDISTRO_PWD_TPM_BACKEND", "mock")
    vault_dir = str(tmp_path / "vaults")
    backend = tpm.MockBackend()

    # Machine A: create a TPM vault, add an item.
    vault.create_vault_tpm(vault_dir, "main", b"pin1234", backend)
    mk_a = vault.unlock_vault_tpm(vault_dir, "main", b"pin1234",
                                  tpm.lookup_backend)
    vault.add_item(vault_dir, "main", mk_a, "github", b"s3cret-token")

    # Owner exports the recovery bundle (into the backup metadata set).
    bundle_path = str(tmp_path / "recovery.json")
    rec.write_recovery_bundle(bundle_path, mk_a, b"owner-recovery-phrase")

    # Machine B: the .vault file is restored from backup (we already have it
    # on disk). Recover the master key from the bundle + passphrase.
    bundle = rec.read_recovery_bundle(bundle_path)
    mk_recovered = rec.decrypt_recovery_bundle(bundle, b"owner-recovery-phrase")
    assert mk_recovered == mk_a

    # Re-seal the recovered master key into the new machine's TPM.
    new_backend = tpm.MockBackend()
    vault.reseal_vault_with_master_key(vault_dir, "main", mk_recovered,
                                       b"new-pin", new_backend)

    # The vault now unlocks with the NEW pin, and the item survives.
    mk_b = vault.unlock_vault_tpm(vault_dir, "main", b"new-pin",
                                  tpm.lookup_backend)
    assert mk_b == mk_a
    assert vault.get_item_payload(vault_dir, "main", mk_b, "github") \
        == b"s3cret-token"


def test_reseal_rejects_wrong_master_key_length(tmp_path, monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_TPM_BACKEND", "mock")
    vault_dir = str(tmp_path / "vaults")
    backend = tpm.MockBackend()
    vault.create_vault_tpm(vault_dir, "main", b"pin", backend)
    with pytest.raises(vault.VaultIntegrityError):
        vault.reseal_vault_with_master_key(vault_dir, "main", b"short",
                                           b"pin", backend)


def test_reseal_rejects_wrong_master_key_against_items(tmp_path, monkeypatch):
    """A 32-byte but WRONG master key (e.g. the wrong recovery bundle) must be
    rejected before reseal — otherwise the vault silently splits: items become
    undecryptable and new writes use the wrong key."""
    monkeypatch.setenv("QDISTRO_PWD_TPM_BACKEND", "mock")
    vault_dir = str(tmp_path / "vaults")
    backend = tpm.MockBackend()
    vault.create_vault_tpm(vault_dir, "main", b"pin", backend)
    mk = vault.unlock_vault_tpm(vault_dir, "main", b"pin", tpm.lookup_backend)
    vault.add_item(vault_dir, "main", mk, "tag", b"secret")
    wrong = bytes([b ^ 0xFF for b in mk])  # valid length, wrong value
    with pytest.raises(vault.VaultIntegrityError, match="does not match"):
        vault.reseal_vault_with_master_key(vault_dir, "main", wrong,
                                           b"newpin", backend)
    # The vault is untouched: the original key still unlocks + decrypts.
    assert vault.unlock_vault_tpm(vault_dir, "main", b"pin",
                                  tpm.lookup_backend) == mk
    assert vault.get_item_payload(vault_dir, "main", mk, "tag") == b"secret"


def test_kdf_params_clamped_on_decrypt():
    """A hostile bundle cannot drive scrypt into an OOM before the tag check."""
    bundle = rec.export_recovery_bundle(MK, b"pw")
    bundle["kdf"]["n"] = 1 << 30  # ~1 TiB at r=8 — must be refused, not run
    with pytest.raises(rec.RecoveryIntegrityError, match="scrypt n"):
        rec.decrypt_recovery_bundle(bundle, b"pw")
    bundle = rec.export_recovery_bundle(MK, b"pw")
    bundle["kdf"]["n"] = 100  # not a power of two
    with pytest.raises(rec.RecoveryIntegrityError, match="scrypt n"):
        rec.decrypt_recovery_bundle(bundle, b"pw")
