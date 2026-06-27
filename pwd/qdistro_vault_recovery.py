"""Vault recovery bundle — the non-TPM escape hatch (06-backup-dr §3.4 / F1).

TPM-sealed vaults (qdistro_pwd_vault v2) are machine-bound: a perfect backup
of the sealed blob is unreadable on any other machine and after enough
firmware/PCR change. Today, machine death = vault loss regardless of backup
diligence. The recovery bundle fixes that: the vault MASTER KEY encrypted to
an owner recovery passphrase, stored in the backup metadata set, so a fresh
machine can recover the vault and re-seal it into the new TPM
(reseal_vault_with_master_key in qdistro_pwd_vault).

Crypto: scrypt(passphrase) -> AES-256-GCM(master_key) — the SAME construction
as the v1 scrypt vault, but with an ELEVATED scrypt work factor. The bundle is
a second unlock-everything secret opened only on recovery, so a high KDF cost
is affordable (codex 06 review §2 MED: "high work factor").

Deviation from the 06 sketch (which named an "age scrypt recipient"): reusing
the in-tree scrypt+AEAD primitive keeps a single vetted crypto path and a
NON-INTERACTIVE, TTY-free hook (rage's passphrase mode reads /dev/tty, which
is wrong for a daemon and pops an askpass dialog). The decrypt path is
qdistro's own tool, which the §3.5 restore runbook already ships in the
metadata set / git bundle. The owner-recoverability property is preserved;
only the decrypt *tool* changes from stock rage to qdistro-recovery.

Custody (06 §3.4): backup ciphertext + recovery passphrase = all silos.
v1 is global (one bundle for all vaults' material is the caller's choice);
per-silo is the bounded-blast-radius option, noted in the doc. Rotating the
passphrase re-encrypts the bundle, but pre-rotation backup generations retain
the old bundle until they age out — documented.
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

RECOVERY_FORMAT_VERSION = 1

# Elevated vs the v1 vault (N=2^15): the bundle is opened rarely and guards
# everything, so pay a higher KDF cost. 2^17 * r8 ~= 128 MiB, p1 — a few
# hundred ms, acceptable for a recovery-only operation.
RECOVERY_SCRYPT_N = 1 << 17
RECOVERY_SCRYPT_R = 8
RECOVERY_SCRYPT_P = 1
RECOVERY_SALT_BYTES = 16
GCM_NONCE_BYTES = 12
MASTER_KEY_BYTES = 32

# Upper bounds on the KDF parameters honoured from a bundle. The bundle is
# unauthenticated until the AEAD tag checks, so a hostile/corrupt bundle could
# otherwise set n=2^30 and OOM/hang the recovery host before the tag rejects
# it. These bound the cost while still allowing future hardening above the
# current constants.
MAX_SCRYPT_N = 1 << 22   # ~4 GiB at r=8; a hard memory ceiling
MAX_SCRYPT_R = 32
MAX_SCRYPT_P = 16

DEFAULT_LABEL = "qdistro-vault-recovery"


class RecoveryError(Exception):
    """Base class for recovery-bundle failures."""


class RecoveryBadPassphrase(RecoveryError):
    """Wrong recovery passphrase (AEAD auth failed)."""


class RecoveryIntegrityError(RecoveryError):
    """Bundle is malformed, tampered, or an unsupported version."""


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _derive_kek(passphrase: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase)


def _aad(label: str) -> bytes:
    """Bind the ciphertext to the bundle label so a bundle cannot be passed
    off as a different one."""
    return f"qdistro-recovery\x00{label}".encode()


def export_recovery_bundle(master_key: bytes, passphrase: bytes,
                           *, label: str = DEFAULT_LABEL) -> dict[str, Any]:
    """Encrypt ``master_key`` to ``passphrase`` and return a bundle dict.

    ``passphrase`` must be non-empty (a recovery key with no passphrase is a
    plaintext master key). ``master_key`` must be exactly 32 bytes."""
    if len(master_key) != MASTER_KEY_BYTES:
        raise RecoveryError(
            f"master_key must be {MASTER_KEY_BYTES} bytes, got {len(master_key)}")
    if not passphrase:
        raise RecoveryError("recovery passphrase must not be empty")
    salt = os.urandom(RECOVERY_SALT_BYTES)
    kek = _derive_kek(passphrase, salt, RECOVERY_SCRYPT_N,
                      RECOVERY_SCRYPT_R, RECOVERY_SCRYPT_P)
    nonce = os.urandom(GCM_NONCE_BYTES)
    ct = AESGCM(kek).encrypt(nonce, master_key, _aad(label))
    return {
        "version": RECOVERY_FORMAT_VERSION,
        "label": label,
        "created": int(time.time()),
        "kdf": {
            "alg": "scrypt",
            "n": RECOVERY_SCRYPT_N,
            "r": RECOVERY_SCRYPT_R,
            "p": RECOVERY_SCRYPT_P,
            "salt": _b64e(salt),
        },
        "aead": {
            "alg": "AES-256-GCM",
            "nonce": _b64e(nonce),
            "ciphertext": _b64e(ct),
        },
    }


def decrypt_recovery_bundle(bundle: dict[str, Any], passphrase: bytes,
                            *, label: str | None = None) -> bytes:
    """Recover the master key from a bundle. Raises RecoveryBadPassphrase on a
    wrong passphrase, RecoveryIntegrityError on tamper/format problems.

    The documented + tested decrypt path of §3.4."""
    if not isinstance(bundle, dict):
        raise RecoveryIntegrityError("bundle is not a JSON object")
    if bundle.get("version") != RECOVERY_FORMAT_VERSION:
        raise RecoveryIntegrityError(
            f"unsupported recovery bundle version {bundle.get('version')!r}")
    kdf = bundle.get("kdf") or {}
    aead = bundle.get("aead") or {}
    bundle_label = bundle.get("label", DEFAULT_LABEL)
    if label is not None and label != bundle_label:
        raise RecoveryIntegrityError(
            f"bundle label {bundle_label!r} != expected {label!r}")
    try:
        salt = _b64d(kdf["salt"])
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
        nonce = _b64d(aead["nonce"])
        ct = _b64d(aead["ciphertext"])
    except (KeyError, ValueError, TypeError) as e:
        raise RecoveryIntegrityError(f"malformed bundle: {e}") from e
    # Bound attacker-controlled KDF cost BEFORE deriving — the bundle is
    # unauthenticated until the tag check, so an absurd n would OOM/hang first.
    if not (1 <= n <= MAX_SCRYPT_N) or (n & (n - 1)) != 0:
        raise RecoveryIntegrityError(f"bundle scrypt n out of range: {n}")
    if not (1 <= r <= MAX_SCRYPT_R) or not (1 <= p <= MAX_SCRYPT_P):
        raise RecoveryIntegrityError(
            f"bundle scrypt r/p out of range: r={r} p={p}")
    kek = _derive_kek(passphrase, salt, n, r, p)
    try:
        master_key = AESGCM(kek).decrypt(nonce, ct, _aad(bundle_label))
    except Exception as e:  # InvalidTag etc.
        raise RecoveryBadPassphrase(
            "wrong recovery passphrase or tampered bundle") from e
    if len(master_key) != MASTER_KEY_BYTES:
        raise RecoveryIntegrityError(
            f"recovered master key has wrong length {len(master_key)}")
    return master_key


def write_recovery_bundle(path: str, master_key: bytes, passphrase: bytes,
                          *, label: str = DEFAULT_LABEL) -> None:
    """Encrypt + atomically write a recovery bundle to ``path`` (0600)."""
    atomic_write_json(path, export_recovery_bundle(
        master_key, passphrase, label=label))


def read_recovery_bundle(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        raise RecoveryIntegrityError(f"no recovery bundle at {path}")
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except ValueError as e:
            raise RecoveryIntegrityError(
                f"recovery bundle is not valid JSON: {e}") from e
