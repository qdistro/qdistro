"""Sealed PIN stash for the portal-keys vault auto-unlock path.

spec/13 §"portal-keys auto-unlock at login": admin sets the portal-
keys vault PIN once via ``qdistro-pwd-admin store-portal-pin``; the
PIN is TPM-sealed (object auth-value left empty — it's the TPM's
hardware binding that's load-bearing, not a second factor on the
seal) and written to ``/var/lib/qdistro/portal-keys-pin.tpm``.

A session systemd unit calls ``Pwd1.AutoUnlockPortalKeys`` at login;
the daemon unseals the PIN and unlocks the portal-keys vault, so
unmodified Flatpak apps can fetch their per-app portal Secret keys
the moment the user lands on the desktop.

PCR binding (boot-integrity) is **deferred to spec/13 Phase-8.5**
(task 098) so a tampered initrd can't unseal even with the right
TPM. Today the seal uses object auth-value only; the auth-value
field is empty here because the unlock unit runs unattended.

File format (JSON):

    {
      "format_version": 1,
      "tpm_seal": { "backend": <str>, ... backend-specific blob ... },
      "created_at_unix": <int>
    }

The ``tpm_seal`` sub-blob is exactly what ``TpmBackend.seal`` returns
plus a ``backend`` discriminator. Same shape as the v2 vault's
``tpm_seal`` field — kept structurally compatible so the same
unseal helper handles both.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from qdistro_pwd_tpm import (  # type: ignore[import-not-found]
    TpmAuthFailed, TpmBackend, TpmBackendError, TpmUnavailable,
    configured_pcrs,
)


PIN_STASH_FORMAT_VERSION = 1
# Lives under /var/lib/qdistro/vaults/ because the daemon's qdistro-pwd
# uid owns that dir 0700 (install-pwd-for-vm.sh sets that up). Putting
# the pinstash directly under /var/lib/qdistro/ would land in a
# root-owned dir that the non-root daemon can't write atomically. The
# .tpm suffix keeps it distinguishable from the .vault files alongside.
DEFAULT_STASH_PATH = "/var/lib/qdistro/vaults/portal-keys-pin.tpm"

# Maximum sealed PIN length (input). Keep aligned with the TPM seal
# secret bound in qdistro_pwd_tpm (1..128 bytes).
MAX_PIN_BYTES = 128


class PinStashError(Exception):
    """Raised when the stash file is missing, malformed, or unsealable."""


def stash_pin(pin: bytes,
              backend: TpmBackend,
              path: str = DEFAULT_STASH_PATH,
              *,
              pcrs: str | None = None,
              umask: int = 0o077) -> dict[str, Any]:
    """Seal ``pin`` via ``backend`` and persist to ``path`` atomically.

    ``pcrs`` is an optional PCR selection string (e.g.
    ``"sha256:7,11"``) binding the seal to the live boot-integrity
    state. None defaults to the env-configured selection (DEFAULT_PCRS
    when unset). Pass empty string to explicitly disable PCR binding.

    Returns the on-disk dict (without the inner ciphertext) for audit
    use. Caller is responsible for the dir existing with the right
    owner — typically /var/lib/qdistro/ owned by qdistro-pwd.
    """
    if not isinstance(pin, (bytes, bytearray)):
        raise ValueError("pin must be bytes")
    if not pin:
        raise ValueError("pin must be non-empty")
    if len(pin) > MAX_PIN_BYTES:
        raise ValueError(f"pin too long ({len(pin)} > {MAX_PIN_BYTES} bytes)")
    if not backend.is_available():
        raise TpmUnavailable(
            f"TPM backend {backend.name!r} is not available")
    if pcrs is None:
        pcrs = configured_pcrs()
    seal_blob = backend.seal(bytes(pin), b"", pcrs=pcrs)
    seal_blob["backend"] = backend.name
    out = {
        "format_version": PIN_STASH_FORMAT_VERSION,
        "tpm_seal": seal_blob,
        "created_at_unix": int(time.time()),
    }
    body = json.dumps(out, separators=(",", ":")).encode("utf-8")
    tmp = f"{path}.new"
    old_umask = os.umask(umask)
    try:
        # O_CREAT | O_WRONLY | O_TRUNC with explicit mode keeps the
        # file from being world-readable even for a flash second.
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    finally:
        os.umask(old_umask)
    # Return a sanitised dict so audit logs don't print the seal blob.
    return {
        "format_version": out["format_version"],
        "backend": seal_blob.get("backend", ""),
        "created_at_unix": out["created_at_unix"],
    }


def unseal_pin(backend_lookup: Callable[[str], TpmBackend],
               path: str = DEFAULT_STASH_PATH) -> bytes:
    """Read ``path`` and unseal the PIN.

    ``backend_lookup`` is the same callable as the v2 vault unseal
    path (``qdistro_pwd_tpm.lookup_backend``) — given a backend name,
    return the backend instance. Raises:
      - ``PinStashError`` for missing / malformed files.
      - ``TpmAuthFailed`` if the seal blob is tampered or the TPM's
        DA-lockout is active.
      - ``TpmBackendError`` for backend-internal errors.
    """
    try:
        with open(path, "rb") as f:
            body = f.read()
    except FileNotFoundError as e:
        raise PinStashError(
            f"portal-keys PIN stash not found at {path!r} — "
            f"run `qdistro-pwd-admin store-portal-pin` first") from e
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise PinStashError(
            f"portal-keys PIN stash {path!r} is not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise PinStashError(
            f"portal-keys PIN stash {path!r} is not a JSON object")
    if int(doc.get("format_version", 0)) != PIN_STASH_FORMAT_VERSION:
        raise PinStashError(
            f"portal-keys PIN stash {path!r} has unsupported "
            f"format_version {doc.get('format_version')!r}")
    seal = doc.get("tpm_seal")
    if not isinstance(seal, dict) or "backend" not in seal:
        raise PinStashError(
            f"portal-keys PIN stash {path!r} missing tpm_seal.backend")
    backend = backend_lookup(str(seal["backend"]))
    return backend.unseal(seal, b"")


def stash_present(path: str = DEFAULT_STASH_PATH) -> bool:
    return os.path.exists(path)


def stash_meta(path: str = DEFAULT_STASH_PATH) -> dict[str, Any]:
    """Read the stash metadata (no unseal). Returns
    ``{"present": bool, "backend": <str>, "created_at_unix": <int>}``."""
    out = {"present": False, "backend": "", "created_at_unix": 0}
    try:
        with open(path, "rb") as f:
            body = f.read()
    except FileNotFoundError:
        return out
    try:
        doc = json.loads(body.decode("utf-8"))
    except Exception:
        return out
    out["present"] = True
    seal = doc.get("tpm_seal") or {}
    out["backend"] = str(seal.get("backend", ""))
    out["created_at_unix"] = int(doc.get("created_at_unix", 0))
    return out
