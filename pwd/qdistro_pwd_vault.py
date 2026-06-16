"""qdistro password-manager vault — encryption + on-disk format.

spec/13 vault crypto. Two on-disk formats coexist:

**Version 1 (Phase-8 MVP, scrypt password-only):**

    {
      "version": 1,
      "name":    "<vault name>",
      "created": <unix epoch>,
      "kdf":     {"alg": "scrypt", "n": 32768, "r": 8, "p": 1, "salt": "<b64>"},
      "kek":     {"alg": "AES-GCM", "nonce": "<b64>", "ciphertext": "<b64>"},
      "items":   [...]
    }

**Version 2 (Phase-8.1, TPM-sealed master key + PIN):**

    {
      "version": 2,
      "name":    "<vault name>",
      "created": <unix epoch>,
      "tpm_seal": {
        "backend": "tpm2tools" | "mock",
        "blob":    { backend-specific fields },
      },
      "items": [...]
    }

Item shape (identical in v1 and v2):

    {
      "tag":          "<string, e.g. gmail.com>",
      "nonce":        "<b64>",
      "ciphertext":   "<b64>",                 # AES-GCM(master_key, value)
      "pin_app_exe":  "<absolute path or empty>",
      "pin_selinux":  "<label or empty>",
      "pin_uid":      <int or null>,
      "created":      <unix epoch>
    }

Crypto choices (PyCA cryptography, packaged as python313-cryptography):

- v1 KEK derived from password via scrypt (memory-hard; resists GPU
  brute force vs PBKDF2). N=32768 r=8 p=1 → ~32MB ram, ~150ms on Zen4.
- v2 master key sealed directly by the TPM with the PIN as auth-value.
  TPM enforces dictionary-attack lockout, so a short PIN (6-12 digits)
  is acceptable.
- Vault master key is 32 random bytes. Held in daemon RAM only between
  Unlock and Lock.
- Per-item value encrypted with the master key using its own random
  12-byte nonce. AES-GCM AAD includes (vault_name || tag) to bind
  ciphertext to its identity (so an attacker who swaps two entries on
  disk gets a decrypt failure, not silent cross-talk).

On-disk format is JSON for grep-ability under recovery; bytes are
base64. The `version` field is the first dispatch — never reuse
slot numbers, always add a new one for any breaking change.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from qdistro_pwd_atomic import atomic_write_json

VAULT_FORMAT_VERSION_SCRYPT = 1
VAULT_FORMAT_VERSION_TPM = 2
SUPPORTED_VAULT_VERSIONS = (VAULT_FORMAT_VERSION_SCRYPT, VAULT_FORMAT_VERSION_TPM)
# Back-compat alias for older imports — points at the original v1 default.
VAULT_FORMAT_VERSION = VAULT_FORMAT_VERSION_SCRYPT
DEFAULT_VAULT_DIR = "/var/lib/qdistro/vaults"

# scrypt cost parameters. N must be power of 2, r * p < 2^30.
# These give ~150ms derivation on a modern CPU and ~32MB RAM.
SCRYPT_N = 32768
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16

GCM_NONCE_BYTES = 12
MASTER_KEY_BYTES = 32  # AES-256


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _derive_kek(password: bytes, salt: bytes) -> bytes:
    kdf = Scrypt(
        salt=salt,
        length=32,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    )
    return kdf.derive(password)


def _aad_for(vault_name: str, tag: str) -> bytes:
    """Bind ciphertext to its identity. Swapping entries fails decrypt."""
    return f"{vault_name}\x00{tag}".encode()


class VaultLocked(Exception):
    """Raised when an operation needs the master key but the vault is locked."""


class VaultBadPassword(Exception):
    """Raised when UnlockVault is called with the wrong password."""


class VaultIntegrityError(Exception):
    """Raised when a stored item fails authenticated decryption (tamper)."""


class VaultNotFound(Exception):
    """Raised when a vault file or item is missing."""


class VaultDuplicate(Exception):
    """Raised when creating a vault or item that already exists."""


def vault_path(vault_dir: str, name: str) -> str:
    if "/" in name or name.startswith(".") or not name:
        raise ValueError(f"invalid vault name: {name!r}")
    return os.path.join(vault_dir, f"{name}.vault")


def create_vault(vault_dir: str, name: str, password: bytes) -> None:
    """Create a new empty vault file. Fails if one already exists."""
    path = vault_path(vault_dir, name)
    os.makedirs(vault_dir, mode=0o700, exist_ok=True)
    if os.path.exists(path):
        raise VaultDuplicate(f"vault {name!r} already exists at {path}")
    salt = os.urandom(SCRYPT_SALT_BYTES)
    kek = _derive_kek(password, salt)
    master_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(GCM_NONCE_BYTES)
    sealed = AESGCM(kek).encrypt(nonce, master_key, _aad_for(name, "__master__"))
    body = {
        "version": VAULT_FORMAT_VERSION,
        "name":    name,
        "created": int(time.time()),
        "kdf": {
            "alg":   "scrypt",
            "n":     SCRYPT_N,
            "r":     SCRYPT_R,
            "p":     SCRYPT_P,
            "salt":  _b64e(salt),
        },
        "kek": {
            "alg":        "AES-GCM",
            "nonce":      _b64e(nonce),
            "ciphertext": _b64e(sealed),
        },
        "items": [],
    }
    atomic_write_json(path, body)


def _load(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        raise VaultNotFound(f"no vault at {path}")
    with open(path, encoding="utf-8") as f:
        body = json.load(f)
    if body.get("version") not in SUPPORTED_VAULT_VERSIONS:
        raise VaultIntegrityError(
            f"unsupported vault format version {body.get('version')!r}")
    return body


def vault_version(vault_dir: str, name: str) -> int:
    """Read just the format version of a vault without unsealing it. Used
    by the daemon to dispatch UnlockVault to the right path."""
    body = _load(vault_path(vault_dir, name))
    return int(body["version"])


def unlock_vault(vault_dir: str, name: str, password: bytes) -> bytes:
    """Return the unsealed master key (v1 / scrypt path).

    Raises VaultBadPassword on mismatch, VaultIntegrityError if the
    vault is actually a v2 (TPM-sealed) vault — caller must use
    `unlock_vault_tpm` for those.
    """
    body = _load(vault_path(vault_dir, name))
    if body["version"] != VAULT_FORMAT_VERSION_SCRYPT:
        raise VaultIntegrityError(
            f"vault {name!r} is version {body['version']}; "
            f"use unlock_vault_tpm for TPM-sealed vaults")
    salt = _b64d(body["kdf"]["salt"])
    kek = _derive_kek(password, salt)
    nonce = _b64d(body["kek"]["nonce"])
    sealed = _b64d(body["kek"]["ciphertext"])
    try:
        master_key = AESGCM(kek).decrypt(
            nonce, sealed, _aad_for(name, "__master__"))
    except Exception as exc:
        raise VaultBadPassword("wrong vault password") from exc
    if len(master_key) != MASTER_KEY_BYTES:
        raise VaultIntegrityError(
            f"unsealed master key has wrong length {len(master_key)}")
    return master_key


# ---------------------------------------------------------------------------
# v2 / TPM path
# ---------------------------------------------------------------------------

def create_vault_tpm(vault_dir: str, name: str, pin: bytes,
                     tpm_backend, pcrs: str | None = None) -> None:
    """Create a new v2 TPM-sealed vault. `tpm_backend` is a TpmBackend from
    qdistro_pwd_tpm. PIN is used as the TPM auth-value. PIN may be empty
    only if the backend explicitly tolerates it (mock does, real TPM does
    too but loses the lockout).

    `pcrs` is an optional PCR selection string (e.g. ``"sha256:7,11"``)
    binding the seal to the live boot-integrity state. None defers to
    the backend's call-site default (no PCR binding). Phase-8.5 wires
    `qdistro_pwd_tpm.configured_pcrs()` here so the daemon's
    CreateVaultTPM picks the env-configured selection.
    """
    if tpm_backend is None:
        raise ValueError("create_vault_tpm requires a TPM backend")
    path = vault_path(vault_dir, name)
    os.makedirs(vault_dir, mode=0o700, exist_ok=True)
    if os.path.exists(path):
        raise VaultDuplicate(f"vault {name!r} already exists at {path}")
    master_key = AESGCM.generate_key(bit_length=256)
    blob = tpm_backend.seal(master_key, pin, pcrs=pcrs)
    body = {
        "version": VAULT_FORMAT_VERSION_TPM,
        "name":    name,
        "created": int(time.time()),
        "tpm_seal": {
            "backend": tpm_backend.name,
            "blob":    blob,
        },
        "items": [],
    }
    atomic_write_json(path, body)


def unlock_vault_tpm(vault_dir: str, name: str, pin: bytes,
                     tpm_backend_lookup) -> bytes:
    """Unseal a v2 TPM-sealed vault. `tpm_backend_lookup` is a callable
    `(backend_name: str) -> TpmBackend` (typically
    `qdistro_pwd_tpm.lookup_backend`).

    Raises VaultBadPassword on TPM auth failure (wrong PIN / lockout),
    VaultIntegrityError on backend-internal errors / format issues.
    """
    body = _load(vault_path(vault_dir, name))
    if body["version"] != VAULT_FORMAT_VERSION_TPM:
        raise VaultIntegrityError(
            f"vault {name!r} is version {body['version']}; "
            f"use unlock_vault for scrypt-only vaults")
    seal = body.get("tpm_seal") or {}
    backend_name = seal.get("backend", "")
    blob = seal.get("blob") or {}
    if not backend_name or not blob:
        raise VaultIntegrityError(
            f"vault {name!r} has malformed tpm_seal section")
    # Resolve the backend at unseal time so the daemon can fail clearly
    # when a vault sealed with one backend is opened on a host where
    # that backend is no longer available.
    try:
        backend = tpm_backend_lookup(backend_name)
    except Exception as exc:
        raise VaultIntegrityError(
            f"vault {name!r} sealed by backend {backend_name!r}: "
            f"{exc}") from exc
    # Import lazily to keep the v1 path decoupled from the TPM module.
    from qdistro_pwd_tpm import (  # type: ignore[import-not-found]
        TpmAuthFailed,
        TpmBackendError,
        TpmUnavailable,
    )
    try:
        master_key = backend.unseal(blob, pin)
    except TpmAuthFailed as exc:
        raise VaultBadPassword("wrong PIN or TPM lockout") from exc
    except (TpmBackendError, TpmUnavailable) as exc:
        raise VaultIntegrityError(f"TPM unseal failed: {exc}") from exc
    if len(master_key) != MASTER_KEY_BYTES:
        raise VaultIntegrityError(
            f"unsealed master key has wrong length {len(master_key)}")
    return master_key


def reseal_vault_with_master_key(vault_dir: str, name: str,
                                 master_key: bytes, pin: bytes,
                                 tpm_backend, pcrs: str | None = None) -> None:
    """Re-seal an existing v2 vault's master key into a (new) TPM, preserving
    its items. The recovery path of 06-backup-dr §3.4: on a fresh machine the
    restored ``.vault`` file carries the items (encrypted under the master
    key) plus an OLD tpm_seal blob that the new TPM cannot unseal; recover the
    master key from the recovery bundle (qdistro_vault_recovery) and call this
    to bind it to the new machine's TPM.

    The items are NOT touched — they are encrypted under ``master_key``, which
    is unchanged; only the tpm_seal section is replaced."""
    if tpm_backend is None:
        raise ValueError("reseal_vault_with_master_key requires a TPM backend")
    if len(master_key) != MASTER_KEY_BYTES:
        raise VaultIntegrityError(
            f"master key has wrong length {len(master_key)}")
    path = vault_path(vault_dir, name)
    body = _load(path)
    if body["version"] != VAULT_FORMAT_VERSION_TPM:
        raise VaultIntegrityError(
            f"vault {name!r} is version {body['version']}; reseal targets "
            "v2 TPM-sealed vaults")
    # Verify the candidate master key against an existing item BEFORE
    # committing — otherwise a wrong recovered key (e.g. the wrong recovery
    # bundle) reseals cleanly but leaves every item undecryptable, and any
    # later add_item writes under the wrong key (split-brain vault). The
    # items are AES-GCM sealed with the master key + per-item AAD, so one
    # successful decrypt proves the key is the vault's.
    items = body.get("items") or []
    if items:
        probe = items[0]
        try:
            AESGCM(master_key).decrypt(
                _b64d(probe["nonce"]), _b64d(probe["ciphertext"]),
                _aad_for(name, probe["tag"]))
        except Exception as exc:
            raise VaultIntegrityError(
                f"master key does not match vault {name!r} (wrong recovery "
                "bundle/passphrase?); refusing to reseal") from exc
    blob = tpm_backend.seal(master_key, pin, pcrs=pcrs)
    body["tpm_seal"] = {"backend": tpm_backend.name, "blob": blob}
    atomic_write_json(path, body)


def rotate_vault(vault_dir: str, name: str,
                 old_password: bytes, new_password: bytes) -> None:
    """Rotate a v1/scrypt vault's password without re-encrypting items.

    The master key is sealed under a KEK derived from (password, salt);
    items are sealed under the master key. Rotating the password means
    re-deriving a fresh KEK with a new salt and re-sealing the
    *existing* master key. Items stay byte-for-byte unchanged.

    Raises VaultBadPassword on wrong old_password, VaultIntegrityError
    if the file is actually a v2 vault (caller must use
    rotate_vault_tpm).
    """
    path = vault_path(vault_dir, name)
    body = _load(path)
    if body["version"] != VAULT_FORMAT_VERSION_SCRYPT:
        raise VaultIntegrityError(
            f"vault {name!r} is version {body['version']}; "
            f"use rotate_vault_tpm for TPM-sealed vaults")
    # Unseal the existing master key with old_password.
    old_salt = _b64d(body["kdf"]["salt"])
    old_kek = _derive_kek(old_password, old_salt)
    old_nonce = _b64d(body["kek"]["nonce"])
    old_sealed = _b64d(body["kek"]["ciphertext"])
    try:
        master_key = AESGCM(old_kek).decrypt(
            old_nonce, old_sealed, _aad_for(name, "__master__"))
    except Exception as exc:
        raise VaultBadPassword("wrong vault password") from exc
    if len(master_key) != MASTER_KEY_BYTES:
        raise VaultIntegrityError(
            f"unsealed master key has wrong length {len(master_key)}")
    # Re-seal under a fresh salt + KEK derived from new_password.
    new_salt = os.urandom(SCRYPT_SALT_BYTES)
    new_kek = _derive_kek(new_password, new_salt)
    new_nonce = os.urandom(GCM_NONCE_BYTES)
    new_sealed = AESGCM(new_kek).encrypt(
        new_nonce, master_key, _aad_for(name, "__master__"))
    body["kdf"] = {
        "alg":  "scrypt",
        "n":    SCRYPT_N,
        "r":    SCRYPT_R,
        "p":    SCRYPT_P,
        "salt": _b64e(new_salt),
    }
    body["kek"] = {
        "alg":        "AES-GCM",
        "nonce":      _b64e(new_nonce),
        "ciphertext": _b64e(new_sealed),
    }
    body["rotated"] = int(time.time())
    atomic_write_json(path, body)


def rotate_vault_tpm(vault_dir: str, name: str,
                     old_pin: bytes, new_pin: bytes,
                     tpm_backend, tpm_backend_lookup,
                     pcrs: str | None = None) -> None:
    """Rotate a v2/TPM vault's PIN without re-encrypting items.

    Unseal under old_pin, re-seal under new_pin. If the host's PCR
    state matches the existing seal, the unseal succeeds; the new
    seal binds to whatever ``pcrs`` selection is supplied (defaults
    to None = no PCR re-binding, but typically the caller passes
    ``configured_pcrs()`` so a rotation also picks up the current
    PCR state — closing any drift from earlier UEFI/initrd updates).

    `tpm_backend` is used for the new seal; `tpm_backend_lookup` is
    used to resolve the old vault's backend name for the unseal
    (vault may have been sealed by a different backend).
    """
    if tpm_backend is None:
        raise ValueError("rotate_vault_tpm requires a TPM backend")
    path = vault_path(vault_dir, name)
    body = _load(path)
    if body["version"] != VAULT_FORMAT_VERSION_TPM:
        raise VaultIntegrityError(
            f"vault {name!r} is version {body['version']}; "
            f"use rotate_vault for scrypt vaults")
    seal = body.get("tpm_seal") or {}
    old_backend_name = seal.get("backend", "")
    old_blob = seal.get("blob") or {}
    if not old_backend_name or not old_blob:
        raise VaultIntegrityError(
            f"vault {name!r} has malformed tpm_seal section")
    try:
        old_backend = tpm_backend_lookup(old_backend_name)
    except Exception as exc:
        raise VaultIntegrityError(
            f"vault {name!r} sealed by backend {old_backend_name!r}: "
            f"{exc}") from exc
    from qdistro_pwd_tpm import (  # type: ignore[import-not-found]
        TpmAuthFailed,
        TpmBackendError,
        TpmUnavailable,
    )
    try:
        master_key = old_backend.unseal(old_blob, old_pin)
    except TpmAuthFailed as exc:
        raise VaultBadPassword("wrong PIN or TPM lockout") from exc
    except (TpmBackendError, TpmUnavailable) as exc:
        raise VaultIntegrityError(f"TPM unseal failed: {exc}") from exc
    if len(master_key) != MASTER_KEY_BYTES:
        raise VaultIntegrityError(
            f"unsealed master key has wrong length {len(master_key)}")
    new_blob = tpm_backend.seal(master_key, new_pin, pcrs=pcrs)
    body["tpm_seal"] = {
        "backend": tpm_backend.name,
        "blob":    new_blob,
    }
    body["rotated"] = int(time.time())
    atomic_write_json(path, body)


def get_tpm_seal_meta(vault_dir: str, name: str) -> dict[str, Any]:
    """Return the {backend, pcrs, ...} subset of a v2 vault's tpm_seal
    section without including the sealed blob. For admin-tooling
    display."""
    body = _load(vault_path(vault_dir, name))
    if body["version"] != VAULT_FORMAT_VERSION_TPM:
        return {}
    seal = body.get("tpm_seal") or {}
    blob = seal.get("blob") or {}
    return {
        "backend": seal.get("backend", ""),
        "pcrs":    blob.get("pcrs", "") if isinstance(blob, dict) else "",
    }


def list_items(vault_dir: str, name: str) -> list[dict[str, Any]]:
    """Return a list of {tag, pin_app_exe, pin_selinux, pin_uid, created}.
    Does not require the master key — only metadata is exposed.
    """
    body = _load(vault_path(vault_dir, name))
    out = []
    for item in body.get("items", []):
        out.append({
            "tag":         item["tag"],
            "pin_app_exe": item.get("pin_app_exe", ""),
            "pin_selinux": item.get("pin_selinux", ""),
            "pin_uid":     item.get("pin_uid"),
            "created":     item.get("created", 0),
        })
    return out


def add_item(vault_dir: str, name: str, master_key: bytes,
             tag: str, value: bytes, *,
             pin_app_exe: str = "", pin_selinux: str = "",
             pin_uid: int | None = None,
             replace: bool = False) -> None:
    """Encrypt + persist a value under tag. Raises VaultDuplicate on conflict
    unless replace=True."""
    if not tag:
        raise ValueError("item tag must be non-empty")
    path = vault_path(vault_dir, name)
    body = _load(path)
    items = body.setdefault("items", [])
    existing_idx = next(
        (i for i, it in enumerate(items) if it["tag"] == tag), None)
    if existing_idx is not None and not replace:
        raise VaultDuplicate(f"item {tag!r} already exists in vault {name!r}")
    nonce = os.urandom(GCM_NONCE_BYTES)
    ct = AESGCM(master_key).encrypt(nonce, value, _aad_for(name, tag))
    entry = {
        "tag":         tag,
        "nonce":       _b64e(nonce),
        "ciphertext":  _b64e(ct),
        "pin_app_exe": pin_app_exe,
        "pin_selinux": pin_selinux,
        "pin_uid":     pin_uid,
        "created":     int(time.time()),
    }
    if existing_idx is not None:
        items[existing_idx] = entry
    else:
        items.append(entry)
    atomic_write_json(path, body)


def get_item_payload(vault_dir: str, name: str, master_key: bytes,
                     tag: str) -> bytes:
    """Decrypt + return the payload for tag. Raises VaultNotFound if missing,
    VaultIntegrityError on tamper."""
    body = _load(vault_path(vault_dir, name))
    item = next((it for it in body.get("items", []) if it["tag"] == tag), None)
    if item is None:
        raise VaultNotFound(f"no item {tag!r} in vault {name!r}")
    nonce = _b64d(item["nonce"])
    ct = _b64d(item["ciphertext"])
    try:
        return AESGCM(master_key).decrypt(nonce, ct, _aad_for(name, tag))
    except Exception as exc:
        raise VaultIntegrityError(
            f"item {tag!r} failed authenticated decryption") from exc


def get_item_pins(vault_dir: str, name: str, tag: str) -> dict[str, Any]:
    """Return the pin metadata for tag without decrypting. Used by the
    daemon's policy gate before deciding whether to attempt decryption."""
    body = _load(vault_path(vault_dir, name))
    item = next((it for it in body.get("items", []) if it["tag"] == tag), None)
    if item is None:
        raise VaultNotFound(f"no item {tag!r} in vault {name!r}")
    return {
        "pin_app_exe": item.get("pin_app_exe", ""),
        "pin_selinux": item.get("pin_selinux", ""),
        "pin_uid":     item.get("pin_uid"),
    }


def delete_item(vault_dir: str, name: str, tag: str) -> bool:
    """Remove an item. Returns True if removed, False if absent."""
    path = vault_path(vault_dir, name)
    body = _load(path)
    items = body.setdefault("items", [])
    before = len(items)
    body["items"] = [it for it in items if it["tag"] != tag]
    if len(body["items"]) == before:
        return False
    atomic_write_json(path, body)
    return True


def list_vaults(vault_dir: str) -> list[str]:
    if not os.path.isdir(vault_dir):
        return []
    out = []
    for entry in sorted(os.listdir(vault_dir)):
        if entry.endswith(".vault"):
            out.append(entry[:-len(".vault")])
    return out
