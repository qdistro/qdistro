"""Tests for the owner-side vault-recovery EXPORT tool (06-backup-dr §3.4).

The export tool PRODUCES the encrypted recovery bundle that the daily backup
collector later CONSUMES. The security properties under test:

  - round-trip: an exported bundle PASSES the collector's fail-closed gate
    validate_recovery_input(kind="recovery_bundle") AND decrypts back to the
    LIVE vault master key;
  - ciphertext-only: the bundle file contains NO plaintext master-key bytes and
    no private-key markers — only AEAD ciphertext + public metadata;
  - perms: the bundle is written 0600;
  - fail-closed: wrong vault secret, empty/mismatched recovery passphrase,
    missing vault, unsafe/symlink output path, existing output without --force,
    and an unsafe parent dir all RAISE with NO partial bundle published;
  - end-to-end: the export tool's output, fed through materialise_recovery,
    lands in the collector's recovery/ subdir intact.

Pure host unit tests; MockBackend stands in for the TPM. Test files are owned by
the running uid, so the output-parent owner gate is exercised with
owner_uids={getuid()} for the accept cases and the wrong-owner refusal is
checked structurally.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PWD_DIR = REPO_ROOT / "pwd"
SNAP_DIR = REPO_ROOT / "snapshots"

# The pwd-side modules are on the pytest pythonpath (pyproject) — import them
# plainly (NOT via spec_from_file_location) so we share the SAME module objects
# the other pwd tests use. Re-executing them into sys.modules under the same
# name would reset their module-level state and break sibling test files.
import qdistro_pwd_vault as vault  # noqa: E402
import qdistro_pwd_tpm  # noqa: E402,F401
import qdistro_vault_recovery as rec  # noqa: E402
import qdistro_vault_recovery_export as exp  # noqa: E402


def _load(label: str, path: Path):
    """Load a snapshots-side module by explicit path under its own alias so it
    does not collide with (or reset) any sibling test's binding."""
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


# The collector-side gate + consumer, loaded under private aliases so this file
# never clobbers the names test_backup_recovery.py loads.
gate = _load("_vrexp_backup_recovery",
             SNAP_DIR / "qdistro_backup_recovery.py")
_load("qdistro_backup_manifest", SNAP_DIR / "qdistro_backup_manifest.py")
_load("qdistro_backup_cli", SNAP_DIR / "qdistro_backup_cli.py")
svc = _load("_vrexp_backup_service",
            SNAP_DIR / "qdistro_backup_service.py")


UID = os.getuid()
RECOVERY_PP = b"correct horse battery staple recovery"


# ---------------------------------------------------------------------------
# fixtures: real vaults whose master key we can independently learn
# ---------------------------------------------------------------------------

def _make_scrypt_vault(tmp_path) -> tuple[str, str, bytes, bytes]:
    """A v1 scrypt vault. Returns (vault_dir, name, vault_password, master_key)."""
    vault_dir = str(tmp_path / "vaults")
    name = "main"
    pw = b"vault-pass-v1"
    vault.create_vault(vault_dir, name, pw)
    mk = vault.unlock_vault(vault_dir, name, pw)
    return vault_dir, name, pw, mk


def _make_tpm_vault(tmp_path, monkeypatch) -> tuple[str, str, bytes, bytes]:
    """A v2 TPM vault (MockBackend). Returns (vault_dir, name, pin, master_key).

    Resolves the tpm module through sys.modules so that, regardless of which
    sibling test re-bound the name, the backend instance + lookup match what
    qdistro_pwd_vault / the export tool resolve lazily."""
    _tpm = sys.modules["qdistro_pwd_tpm"]
    monkeypatch.setenv("QDISTRO_PWD_TPM_BACKEND", "mock")
    vault_dir = str(tmp_path / "vaults")
    name = "main"
    pin = b"pin1234"
    vault.create_vault_tpm(vault_dir, name, pin, _tpm.MockBackend())
    mk = vault.unlock_vault_tpm(vault_dir, name, pin, _tpm.lookup_backend)
    return vault_dir, name, pin, mk


def _export(out, vault_dir, name, secret, pp, *, label=None, force=False,
            owner_uids=None):
    kwargs = dict(
        vault_dir=vault_dir, vault_name=name, out_path=str(out),
        label=label or rec.DEFAULT_LABEL, force=force,
        owner_uids=owner_uids if owner_uids is not None else {UID, 0},
        vault_secret=secret, recovery_passphrase=pp)
    exp.run_export(**kwargs)


# ---------------------------------------------------------------------------
# round-trip: exported bundle passes the collector gate + decrypts to the MK
# ---------------------------------------------------------------------------

def test_scrypt_export_passes_gate_and_roundtrips(tmp_path):
    vault_dir, name, pw, mk = _make_scrypt_vault(tmp_path)
    out = tmp_path / "vault-recovery.json"
    _export(out, vault_dir, name, pw, RECOVERY_PP)

    # The collector's fail-closed gate ACCEPTS the produced bundle.
    data = gate.validate_recovery_input(
        str(out), "recovery_bundle", service_owner_uid=UID)
    bundle = json.loads(data)

    # And it decrypts back to the LIVE master key with the recovery passphrase.
    assert rec.decrypt_recovery_bundle(bundle, RECOVERY_PP) == mk


def test_tpm_export_passes_gate_and_roundtrips(tmp_path, monkeypatch):
    vault_dir, name, pin, mk = _make_tpm_vault(tmp_path, monkeypatch)
    out = tmp_path / "vault-recovery.json"
    _export(out, vault_dir, name, pin, RECOVERY_PP)
    data = gate.validate_recovery_input(
        str(out), "recovery_bundle", service_owner_uid=UID)
    assert rec.decrypt_recovery_bundle(json.loads(data), RECOVERY_PP) == mk


def test_label_is_bound_and_roundtrips(tmp_path):
    vault_dir, name, pw, mk = _make_scrypt_vault(tmp_path)
    out = tmp_path / "b.json"
    _export(out, vault_dir, name, pw, RECOVERY_PP, label="silo-A")
    bundle = json.loads(out.read_text())
    assert bundle["label"] == "silo-A"
    assert rec.decrypt_recovery_bundle(bundle, RECOVERY_PP, label="silo-A") == mk
    with pytest.raises(rec.RecoveryIntegrityError):
        rec.decrypt_recovery_bundle(bundle, RECOVERY_PP, label="silo-B")


# ---------------------------------------------------------------------------
# ciphertext-only contract + perms
# ---------------------------------------------------------------------------

def test_bundle_is_ciphertext_only_no_plaintext_secret(tmp_path):
    vault_dir, name, pw, mk = _make_scrypt_vault(tmp_path)
    out = tmp_path / "b.json"
    _export(out, vault_dir, name, pw, RECOVERY_PP)
    raw = out.read_bytes()

    # The plaintext master key must NOT appear anywhere in the bundle file.
    assert mk not in raw
    # Neither the recovery passphrase nor the vault password.
    assert RECOVERY_PP not in raw
    assert pw not in raw
    # No private-key markers (the gate refuses these; assert independently).
    assert not gate._has_private_marker(raw)

    # Only the allowed public fields are present.
    bundle = json.loads(raw)
    assert set(bundle) == {"version", "label", "created", "kdf", "aead"}
    assert set(bundle["kdf"]) == {"alg", "n", "r", "p", "salt"}
    assert set(bundle["aead"]) == {"alg", "nonce", "ciphertext"}


def test_bundle_perms_are_0600(tmp_path):
    vault_dir, name, pw, _ = _make_scrypt_vault(tmp_path)
    out = tmp_path / "b.json"
    _export(out, vault_dir, name, pw, RECOVERY_PP)
    assert oct(os.stat(out).st_mode)[-3:] == "600"


def test_no_tmp_files_left_behind(tmp_path):
    vault_dir, name, pw, _ = _make_scrypt_vault(tmp_path)
    out = tmp_path / "b.json"
    _export(out, vault_dir, name, pw, RECOVERY_PP)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "vaults"]
    assert leftovers == ["b.json"], leftovers


# ---------------------------------------------------------------------------
# fail-closed: nothing published on error
# ---------------------------------------------------------------------------

def test_empty_recovery_passphrase_refused(tmp_path):
    vault_dir, name, pw, _ = _make_scrypt_vault(tmp_path)
    out = tmp_path / "b.json"
    with pytest.raises(exp.ExportError):
        _export(out, vault_dir, name, pw, b"")
    assert not out.exists()


def test_wrong_vault_secret_refused_no_bundle(tmp_path):
    vault_dir, name, _, _ = _make_scrypt_vault(tmp_path)
    out = tmp_path / "b.json"
    with pytest.raises(exp.ExportError):
        _export(out, vault_dir, name, b"WRONG", RECOVERY_PP)
    assert not out.exists()


def test_missing_vault_refused(tmp_path):
    out = tmp_path / "b.json"
    with pytest.raises(exp.ExportError):
        _export(out, str(tmp_path / "nope"), "ghost", b"x", RECOVERY_PP)
    assert not out.exists()


def test_existing_output_without_force_refused(tmp_path):
    vault_dir, name, pw, _ = _make_scrypt_vault(tmp_path)
    out = tmp_path / "b.json"
    out.write_text("pre-existing")
    os.chmod(out, 0o600)
    with pytest.raises(exp.ExportError):
        _export(out, vault_dir, name, pw, RECOVERY_PP)
    # The original file is untouched.
    assert out.read_text() == "pre-existing"


def test_force_overwrites_regular_file(tmp_path):
    vault_dir, name, pw, mk = _make_scrypt_vault(tmp_path)
    out = tmp_path / "b.json"
    out.write_text("old")
    _export(out, vault_dir, name, pw, RECOVERY_PP, force=True)
    bundle = json.loads(out.read_text())
    assert rec.decrypt_recovery_bundle(bundle, RECOVERY_PP) == mk


def test_symlink_output_refused_even_with_force(tmp_path):
    vault_dir, name, pw, _ = _make_scrypt_vault(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("do not clobber")
    link = tmp_path / "b.json"
    os.symlink(str(victim), str(link))
    with pytest.raises(exp.ExportError):
        _export(link, vault_dir, name, pw, RECOVERY_PP, force=True)
    # The symlink target was NOT written through.
    assert victim.read_text() == "do not clobber"


def test_symlinked_parent_dir_refused(tmp_path):
    vault_dir, name, pw, _ = _make_scrypt_vault(tmp_path)
    realdir = tmp_path / "real"
    realdir.mkdir()
    linkdir = tmp_path / "link"
    os.symlink(str(realdir), str(linkdir))
    out = linkdir / "b.json"
    with pytest.raises(exp.ExportError):
        _export(out, vault_dir, name, pw, RECOVERY_PP)
    assert not (realdir / "b.json").exists()


def test_world_writable_parent_refused(tmp_path):
    vault_dir, name, pw, _ = _make_scrypt_vault(tmp_path)
    bad = tmp_path / "open"
    bad.mkdir()
    os.chmod(bad, 0o777)
    out = bad / "b.json"
    with pytest.raises(exp.ExportError):
        _export(out, vault_dir, name, pw, RECOVERY_PP, owner_uids={UID})
    assert not out.exists()


def test_wrong_owner_parent_refused(tmp_path):
    vault_dir, name, pw, _ = _make_scrypt_vault(tmp_path)
    out = tmp_path / "b.json"
    # The tmp parent is owned by UID; restricting owner_uids to {0} (root only)
    # must refuse it (we are not root in the test).
    if UID == 0:
        pytest.skip("running as root; the wrong-owner case is uid-relative")
    with pytest.raises(exp.ExportError):
        _export(out, vault_dir, name, pw, RECOVERY_PP, owner_uids={0})
    assert not out.exists()


def test_missing_tpm_backend_refused(tmp_path, monkeypatch):
    vault_dir, name, pin, _ = _make_tpm_vault(tmp_path, monkeypatch)
    out = tmp_path / "b.json"
    # Simulate a host where the backend the vault was sealed with is no longer
    # available: make the backend lookup raise as it would for an unknown name.
    # Patch via sys.modules so we hit the SAME module object the export tool
    # resolves lazily (a sibling test may have re-bound the name).
    def _no_backend(name):
        raise ValueError(f"unknown TPM backend {name!r}")

    monkeypatch.setattr(sys.modules["qdistro_pwd_tpm"],
                        "lookup_backend", _no_backend)
    with pytest.raises(exp.ExportError):
        _export(out, vault_dir, name, pin, RECOVERY_PP)
    assert not out.exists()


# ---------------------------------------------------------------------------
# passphrase-fd input path (the non-interactive opt-in)
# ---------------------------------------------------------------------------

def test_passphrase_fd_read_strips_one_newline(tmp_path):
    p = tmp_path / "pp"
    p.write_bytes(b"my-recovery-phrase\n")
    fd = os.open(str(p), os.O_RDONLY)
    try:
        assert exp._read_passphrase_fd(fd) == b"my-recovery-phrase"
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# CLI: no env-var path for the recovery passphrase
# ---------------------------------------------------------------------------

def test_recovery_passphrase_not_read_from_env(monkeypatch, tmp_path):
    """The recovery passphrase must NOT be sourced from an environment variable
    by default (env secrets leak). The prompt path is TTY-only; assert the CLI
    does not silently pick up a QDISTRO_RECOVERY_PASSPHRASE env var."""
    vault_dir, name, pw, _ = _make_scrypt_vault(tmp_path)
    monkeypatch.setenv("QDISTRO_RECOVERY_PASSPHRASE", "sneaky")
    monkeypatch.setenv("QDISTRO_PWD_PASSWORD", pw.decode())
    out = tmp_path / "b.json"

    # No TTY in pytest -> getpass would fail; confirm the prompt is reached
    # (i.e. the env var is NOT consulted) by stubbing the prompt helper.
    called = {}

    def fake_prompt(confirm=True):
        called["hit"] = True
        return RECOVERY_PP

    monkeypatch.setattr(exp, "_prompt_recovery_passphrase", fake_prompt)
    rc = exp.main(["export", "--vault", name, "--vault-dir", vault_dir,
                   "--out", str(out), "--owner-uid", str(UID)])
    assert rc == 0
    assert called.get("hit") is True
    # The bundle decrypts with the PROMPTED phrase, not the env var.
    bundle = json.loads(out.read_text())
    assert rec.decrypt_recovery_bundle(bundle, RECOVERY_PP)
    with pytest.raises(rec.RecoveryBadPassphrase):
        rec.decrypt_recovery_bundle(bundle, b"sneaky")


# ---------------------------------------------------------------------------
# end-to-end: export -> materialise_recovery consumes it
# ---------------------------------------------------------------------------

def test_e2e_export_then_collector_materialises(tmp_path):
    """The headline contract: the bundle this tool produces, fed through the
    collector's materialise_recovery, lands in recovery/vault-recovery.json
    intact and still decrypts to the live master key."""
    vault_dir, name, pw, mk = _make_scrypt_vault(tmp_path)
    bundle_path = tmp_path / "exported.json"
    _export(bundle_path, vault_dir, name, pw, RECOVERY_PP)

    # A collector stage dir with the [recovery] table pointing at our bundle.
    dest = tmp_path / "collect-stage"
    dest.mkdir()
    rec_cfg = {
        "collector": "metadata",
        "bundle": str(bundle_path),
        "service_owner_uid": UID,
    }
    cfg = {
        "host_id": "test-host", "remote": "/dev/null",
        "recipients": "/etc/qdistro/r.txt", "subvols": [],
    }
    svc.materialise_recovery(rec_cfg, cfg, str(dest))

    landed = dest / "recovery" / "vault-recovery.json"
    assert landed.exists()
    # Byte-identical to what we exported, and still recovers the master key.
    assert landed.read_bytes() == bundle_path.read_bytes()
    bundle = json.loads(landed.read_text())
    assert rec.decrypt_recovery_bundle(bundle, RECOVERY_PP) == mk
