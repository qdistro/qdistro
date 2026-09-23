#!/usr/bin/env python3
"""qdshell optional content-signing verify primitive (F1 color schemes, F9 plugins).

Signing is OPTIONAL. Whether to enforce is decided by LOCAL trust configuration
(``signing.json``), NOT by whether a signature happens to be present in a download.
Keying on remote-sig-presence would be a downgrade bypass (an attacker just omits
the sig), so the trusted helper reads the on-disk config itself — the caller (QML)
can never pass a "please verify" flag, and a compromised UI/argv cannot disable
verification once a key is configured.

Modes:
  - No ``signing.json``, or it has ``trustedKey: null`` / no ``trustedKey``:
    UNSIGNED mode. No verification. Current behavior, no regression. This is the
    default (the v1 release key does not exist yet, so nothing is trusted).
  - ``signing.json`` present with a well-formed ``trustedKey``: ENFORCED mode.
    Content is verified against a detached-signed SHA256 manifest, bound to the
    configured full 40-hex fingerprint, and FAILS CLOSED on any discrepancy.

Fail-closed rules (enforced mode):
  - Config parse is fail-closed: a present-but-malformed config (bad JSON, wrong
    version, short/invalid fingerprint, missing/relative/unreadable keyring) is an
    ERROR, never a silent fall-back to unsigned.
  - ``gpgv`` missing is fatal in enforced mode (never silently skipped).
  - Detached sig verified with ``gpgv`` and bound to the FULL 40-hex VALIDSIG
    fingerprint (exact, case-insensitive; short ids rejected). No reliance on any
    signer field inside the document.
  - Manifest (``SHA256SUMS``) parser is strict — any malformed line raises
    (no parse-fail-open).
  - Set-equality both directions: every in-scope regular file on disk has exactly
    one manifest entry with a matching digest, AND every manifest entry exists on
    disk with that digest. Catches add / remove / swap.
  - Non-regular files (symlinks, devices, FIFOs, sockets) under the verified tree
    are REFUSED; symlinks are never followed when hashing.
  - The manifest (``SHA256SUMS``) and its signature (``SHA256SUMS.sig``) live in the
    verified directory but are excluded from the verified content set by exact name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


# Detached-signature naming. Deliberately NOT "manifest.json" — F9 already ships a
# plugin manifest.json, so reusing that name would collide. These two names are the
# only files excluded from the verified content set.
MANIFEST_NAME = "SHA256SUMS"
SIGNATURE_NAME = "SHA256SUMS.sig"
EXCLUDED_NAMES = frozenset({MANIFEST_NAME, SIGNATURE_NAME})

FINGERPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
# Strict sha256sum-style line: "<64 hex>  <relpath>" (two spaces, binary marker form).
MANIFEST_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")


class TrustConfigError(Exception):
    """signing.json present but malformed — fail closed, never fall back to unsigned."""


class VerifyError(Exception):
    """Content failed verification in enforced mode — fail closed."""


# --------------------------------------------------------------------------- #
# Trust config
# --------------------------------------------------------------------------- #
def signing_config_path() -> Path:
    """Location of signing.json, mirroring Commons/Settings.qml configDir logic."""
    override = os.environ.get("QDSHELL_SIGNING_FILE")
    if override:
        return Path(override)
    cfg = os.environ.get("NOCTALIA_CONFIG_DIR")
    if not cfg:
        base = os.environ.get("XDG_CONFIG_HOME") or (
            os.environ.get("HOME", "") + "/.config"
        )
        cfg = base + "/qdshell/"
    return Path(cfg) / "signing.json"


def load_trust_config(path: Path | None = None):
    """Return a dict {fingerprint, keyringPath} in enforced mode, or None if unsigned.

    Fail-closed: truly-absent config OR explicit ``trustedKey: null`` / missing
    ``trustedKey`` => None (unsigned). ANY other anomaly raises TrustConfigError.
    """
    if path is None:
        path = signing_config_path()
    if not path.exists():
        return None  # unsigned (default)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrustConfigError(f"cannot read signing config: {exc}") from exc
    try:
        doc = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise TrustConfigError(f"signing config is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise TrustConfigError("signing config root must be an object")
    if doc.get("version") != 1:
        raise TrustConfigError("signing config: unsupported version (expected 1)")
    if "trustedKey" not in doc or doc["trustedKey"] is None:
        return None  # explicit opt-out => unsigned (default)
    tk = doc["trustedKey"]
    if not isinstance(tk, dict):
        raise TrustConfigError("trustedKey must be an object or null")
    fpr = tk.get("fingerprint")
    keyring = tk.get("keyringPath")
    if not isinstance(fpr, str) or not FINGERPRINT_RE.fullmatch(fpr):
        raise TrustConfigError(
            "trustedKey.fingerprint must be a full 40-hex OpenPGP fingerprint"
        )
    if not isinstance(keyring, str) or not keyring:
        raise TrustConfigError("trustedKey.keyringPath must be a non-empty string")
    kpath = Path(keyring)
    if not kpath.is_absolute():
        raise TrustConfigError("trustedKey.keyringPath must be an absolute path")
    if not kpath.is_file():
        raise TrustConfigError(f"trustedKey keyring not found: {keyring}")
    if not os.access(kpath, os.R_OK):
        raise TrustConfigError(f"trustedKey keyring not readable: {keyring}")
    return {"fingerprint": fpr.lower(), "keyringPath": str(kpath)}


def is_enforced(path: Path | None = None) -> bool:
    """True iff a well-formed trusted key is configured. Raises on malformed config."""
    return load_trust_config(path) is not None


# --------------------------------------------------------------------------- #
# Path / manifest helpers
# --------------------------------------------------------------------------- #
def _validate_relpath(relpath: str) -> None:
    """Reject absolute, traversal, control-char, empty-segment relative paths."""
    if not relpath:
        raise VerifyError("empty manifest path")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in relpath):
        raise VerifyError("manifest path contains control characters")
    if relpath.startswith("/"):
        raise VerifyError(f"manifest path is absolute: {relpath!r}")
    segments = relpath.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise VerifyError(f"manifest path has unsafe segment: {relpath!r}")


def parse_manifest(text: str) -> dict[str, str]:
    """Parse a strict SHA256SUMS manifest -> {relpath: sha256hex}. No parse-fail-open."""
    entries: dict[str, str] = {}
    lines = text.split("\n")
    # Allow a single trailing newline (last element empty) but nothing else blank.
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines:
        raise VerifyError("manifest is empty")
    for i, line in enumerate(lines, 1):
        m = MANIFEST_LINE_RE.fullmatch(line)
        if not m:
            raise VerifyError(f"malformed manifest line {i}: {line!r}")
        digest, relpath = m.group(1), m.group(2)
        _validate_relpath(relpath)
        if relpath in EXCLUDED_NAMES:
            raise VerifyError(f"manifest must not list itself or its signature: {relpath!r}")
        if relpath in entries:
            raise VerifyError(f"duplicate manifest entry: {relpath!r}")
        entries[relpath] = digest
    return entries


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    # Open with O_NOFOLLOW so a swapped-in symlink cannot be followed during hashing.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise VerifyError(f"not a regular file: {path}")
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            h.update(chunk)
    finally:
        os.close(fd)
    return h.hexdigest()


def _scan_content_files(
    content_dir: Path, extra_exclude: frozenset[str] = frozenset()
) -> dict[str, str]:
    """Walk content_dir, hashing every in-scope regular file -> {relpath: sha256hex}.

    Refuses any non-regular file (symlink, device, FIFO, socket). Symlinked dirs are
    refused too (os.walk with followlinks=False reports them and we check each).
    Excludes SHA256SUMS / SHA256SUMS.sig and any top-level names in extra_exclude
    (e.g. a user-local plugin settings.json that upstream does not sign).
    """
    top_excluded = EXCLUDED_NAMES | extra_exclude
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(content_dir, followlinks=False):
        dp = Path(dirpath)
        # Refuse symlinked subdirectories (os.walk lists them in dirnames).
        for d in dirnames:
            if (dp / d).is_symlink():
                raise VerifyError(f"refusing symlinked directory: {dp / d}")
        for name in filenames:
            full = dp / name
            rel = full.relative_to(content_dir).as_posix()
            if dp == content_dir and name in top_excluded:
                continue
            lst = full.lstat()
            if not stat.S_ISREG(lst.st_mode):
                raise VerifyError(f"refusing non-regular file: {full}")
            _validate_relpath(rel)
            found[rel] = _sha256_file(full)
    return found


# --------------------------------------------------------------------------- #
# Signature verification
# --------------------------------------------------------------------------- #
def _gpgv_verify(manifest_path: Path, sig_path: Path, keyring: str, expect_fpr: str) -> None:
    """Verify detached sig over manifest with gpgv; bind to full 40-hex fingerprint."""
    if shutil.which("gpgv") is None:
        raise VerifyError("gpgv not found (required in enforced signing mode)")
    with tempfile.NamedTemporaryFile(prefix="qd-gpgv-status-", delete=True) as status:
        proc = subprocess.run(
            ["gpgv", "--keyring", keyring, "--status-fd", "1",
             str(sig_path), str(manifest_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if proc.returncode != 0:
            raise VerifyError("gpgv rejected the manifest signature")
        status_text = proc.stdout.decode("utf-8", "replace")
    fpr = None
    for line in status_text.splitlines():
        # [GNUPG:] VALIDSIG <40hex-fpr> ...
        if line.startswith("[GNUPG:] VALIDSIG "):
            parts = line.split()
            if len(parts) >= 3:
                fpr = parts[2]
                break
    if not fpr:
        raise VerifyError("no VALIDSIG in gpgv status; cannot confirm signer")
    if not FINGERPRINT_RE.fullmatch(fpr):
        raise VerifyError(f"gpgv VALIDSIG fingerprint is not 40-hex: {fpr!r}")
    if fpr.lower() != expect_fpr.lower():
        raise VerifyError(
            f"manifest signed by {fpr}, not the configured trusted key {expect_fpr}"
        )


# --------------------------------------------------------------------------- #
# Public verify entrypoint
# --------------------------------------------------------------------------- #
def verify_dir(
    content_dir: Path, trust: dict, extra_exclude: frozenset[str] = frozenset()
) -> None:
    """Verify content_dir against its in-tree SHA256SUMS(.sig), bound to trust key.

    Raises VerifyError on ANY discrepancy. Returns None only on full success.
    `trust` is the dict from load_trust_config() (enforced mode); caller must have
    already decided enforcement. `extra_exclude` names top-level files that are
    user-local and not covered by the upstream signature (e.g. plugin settings.json).
    """
    content_dir = Path(content_dir)
    if not content_dir.is_dir() or content_dir.is_symlink():
        raise VerifyError(f"content dir missing or is a symlink: {content_dir}")
    manifest_path = content_dir / MANIFEST_NAME
    sig_path = content_dir / SIGNATURE_NAME
    if manifest_path.is_symlink() or sig_path.is_symlink():
        raise VerifyError("manifest or signature is a symlink")
    if not manifest_path.is_file():
        raise VerifyError(f"missing signed manifest: {MANIFEST_NAME}")
    if not sig_path.is_file():
        raise VerifyError(f"missing detached signature: {SIGNATURE_NAME}")

    _gpgv_verify(manifest_path, sig_path, trust["keyringPath"], trust["fingerprint"])

    declared = parse_manifest(manifest_path.read_text(encoding="utf-8"))
    actual = _scan_content_files(content_dir, extra_exclude=extra_exclude)

    declared_set = set(declared)
    actual_set = set(actual)
    missing = declared_set - actual_set
    extra = actual_set - declared_set
    if missing:
        raise VerifyError(f"manifest entries missing on disk: {sorted(missing)}")
    if extra:
        raise VerifyError(f"files on disk not in manifest: {sorted(extra)}")
    for rel, digest in declared.items():
        if actual[rel] != digest:
            raise VerifyError(f"digest mismatch for {rel!r}")


def maybe_verify_dir(content_dir: Path, config_path: Path | None = None) -> bool:
    """Consult trust config and verify if enforced. Returns True if verified,
    False if unsigned mode (no-op). Raises on malformed config or verify failure.
    """
    trust = load_trust_config(config_path)
    if trust is None:
        return False  # unsigned mode — current behavior, no regression
    verify_dir(content_dir, trust)
    return True
