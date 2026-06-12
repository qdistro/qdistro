"""qdistro backup manifest — integrity/authenticity for the encrypted
export pipeline (doc/filesystem.md draft 06-backup-dr §3.1–3.2).

`btrfs send | rage -e | ssh` gives per-chunk AEAD on the *payload*, but the
blob *names*, the blob *set*, and the parent lineage are unauthenticated: a
hostile backup target can serve an older consistent set (rollback), drop
incrementals, or swap blobs between subvolumes. This module adds the missing
layer — a per-run, hash-chained, signed manifest of the remote's expected
state — and the verification a restore runs before trusting anything.

What each layer buys (be precise — see 06 §3.2):

- The **signature** (ssh-keygen -Y, ssh-ed25519) authenticates a manifest
  against tampering and blob substitution. It does NOT prevent replaying an
  older, validly-signed manifest.
- **Freshness** comes only from the owner-side monotonic checkpoint: a
  restore MUST fail unless the newest verified ``seq`` is >= the checkpoint
  the owner recorded off-target (printed card / phone / password manager).
  Without that external record, rollback remains possible. ``check_freshness``
  enforces it; the doc states the residual.
- **Threat boundary**: host trusted at backup time, storage target hostile.
  A compromised host signs whatever it wants — the upgrade path (hardware
  token / append-only log) is out of scope for v1, stated in the doc.

Pure-ish: dict/bytes in, dict/bytes out, plus two thin subprocess wrappers
around ssh-keygen (present on every machine). No btrfs, no ssh, no rage.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from typing import Any

MANIFEST_VERSION = 1

# ssh-keygen -Y signature namespace. Domain-separates these signatures from
# any other ssh-keygen -Y use on the same key.
SIGN_NAMESPACE = "qdistro-backup"


class ManifestError(Exception):
    """A manifest failed to build, parse, or verify. On the restore path
    this is fatal — never downgrade to a warning."""


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, *, _chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_chunk), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------
# build / canonicalise
# --------------------------------------------------------------------------

def _safe_component(value: str, what: str) -> str:
    """Reject anything that is not a single, benign path component. Blob
    names and subvol names become path components under the backup dir; a
    ``..``/``/``/absolute value would let a (malicious or malformed) manifest
    read or write outside it."""
    if not value or not isinstance(value, str):
        raise ManifestError(f"{what} must be a non-empty string")
    if "/" in value or "\\" in value or value in (".", "..") \
            or value.startswith("."):
        raise ManifestError(
            f"{what} {value!r} must be a single path component "
            "(no '/', '\\', '..', or leading '.')")
    return value


def build_entry(subvol: str, blob: str, sha256: str, size: int,
                parent_blob: str | None) -> dict[str, Any]:
    _safe_component(subvol, "subvol")
    _safe_component(blob, "blob")
    if parent_blob is not None:
        _safe_component(parent_blob, "parent_blob")
    if not isinstance(sha256, str) or len(sha256) != 64 \
            or any(c not in "0123456789abcdef" for c in sha256):
        raise ManifestError(f"entry {blob!r}: sha256 must be 64 hex chars")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ManifestError(f"entry {blob!r}: size must be a non-negative int")
    return {
        "subvol": subvol,
        "blob": blob,
        "sha256": sha256,
        "size": int(size),
        "parent_blob": parent_blob,
    }


def build_manifest(seq: int, host_id: str, created_at: int,
                   entries: list[dict[str, Any]],
                   prev_manifest_sha256: str | None) -> dict[str, Any]:
    """Assemble a manifest dict. ``seq`` is the owner-visible monotonic
    counter; ``prev_manifest_sha256`` chains to the prior run's canonical
    bytes (None for the very first run)."""
    if seq < 0:
        raise ManifestError("seq must be non-negative")
    norm: list[dict[str, Any]] = []
    for e in entries:
        norm.append(build_entry(e["subvol"], e["blob"], e["sha256"],
                                e["size"], e.get("parent_blob")))
    return {
        "version": MANIFEST_VERSION,
        "seq": int(seq),
        "created_at": int(created_at),
        "host_id": host_id,
        "entries": norm,
        "prev_manifest_sha256": prev_manifest_sha256,
    }


def manifest_canonical_bytes(manifest: dict[str, Any]) -> bytes:
    """Deterministic byte encoding used for both hashing and signing.
    sort_keys + compact separators => the exact same bytes sign and verify
    on any machine, regardless of dict insertion order."""
    return json.dumps(manifest, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def manifest_sha256(manifest: dict[str, Any]) -> str:
    return sha256_hex(manifest_canonical_bytes(manifest))


def parse_manifest(text: str | bytes) -> dict[str, Any]:
    try:
        m = json.loads(text)
    except (ValueError, TypeError) as e:
        raise ManifestError(f"manifest is not valid JSON: {e}") from e
    if not isinstance(m, dict):
        raise ManifestError("manifest is not a JSON object")
    for key in ("version", "seq", "created_at", "host_id", "entries"):
        if key not in m:
            raise ManifestError(f"manifest missing required key {key!r}")
    if m["version"] != MANIFEST_VERSION:
        raise ManifestError(
            f"unsupported manifest version {m['version']!r}")
    if not isinstance(m["entries"], list):
        raise ManifestError("manifest entries must be a list")
    if isinstance(m["seq"], bool) or not isinstance(m["seq"], int):
        raise ManifestError("manifest seq must be an int")
    if isinstance(m["created_at"], bool) or not isinstance(m["created_at"], int):
        raise ManifestError("manifest created_at must be an int")
    if not isinstance(m["host_id"], str):
        raise ManifestError("manifest host_id must be a string")
    # Re-validate every entry through build_entry so a hostile/malformed
    # manifest raises a clean ManifestError here rather than a KeyError /
    # TypeError deep in verify_blobs or restore_order later.
    norm: list[dict[str, Any]] = []
    for entry in m["entries"]:
        if not isinstance(entry, dict):
            raise ManifestError("each manifest entry must be an object")
        try:
            norm.append(build_entry(
                entry["subvol"], entry["blob"], entry["sha256"], entry["size"],
                entry.get("parent_blob")))
        except KeyError as ke:
            raise ManifestError(f"entry missing field {ke}") from ke
    m["entries"] = norm
    prev = m.get("prev_manifest_sha256")
    if prev is not None and not isinstance(prev, str):
        raise ManifestError("prev_manifest_sha256 must be a string or null")
    # prev_manifest_sha256 is optional only for seq 0; normalise absence.
    m.setdefault("prev_manifest_sha256", None)
    return m


# --------------------------------------------------------------------------
# signing / verification (ssh-keygen -Y)
# --------------------------------------------------------------------------

def sign_manifest(canonical: bytes, key_path: str,
                  *, namespace: str = SIGN_NAMESPACE) -> str:
    """Sign canonical manifest bytes with an ssh private key.
    Returns the armored SSH signature (``-----BEGIN SSH SIGNATURE-----``).

    The private key is NOT stored on the backup target. ssh-keygen reads the
    payload on stdin; we pass it directly (no temp payload file)."""
    proc = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", key_path, "-n", namespace],
        input=canonical, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False)
    if proc.returncode != 0:
        raise ManifestError(
            "ssh-keygen -Y sign failed: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8")


def verify_signature(canonical: bytes, signature: str,
                     allowed_signers_path: str, identity: str,
                     *, namespace: str = SIGN_NAMESPACE) -> bool:
    """Verify an armored SSH signature over canonical manifest bytes.

    ``allowed_signers_path`` is an ssh allowed_signers file pinning the
    backup-signing public key to ``identity``; it lives OFF the backup
    target (the restore-time identity, 06 §2c). Returns True on a good
    signature, False otherwise. Never raises on a bad signature — a hostile
    target supplying garbage must read as "unverified", not as an error the
    caller might mishandle."""
    sig_fd, sig_path = tempfile.mkstemp(prefix="qdistro-manifest-", suffix=".sig")
    try:
        with os.fdopen(sig_fd, "w") as f:
            f.write(signature)
        proc = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", allowed_signers_path,
             "-I", identity, "-n", namespace, "-s", sig_path],
            input=canonical, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False)
        return proc.returncode == 0
    finally:
        try:
            os.unlink(sig_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# chain / freshness / blob verification (the restore gate)
# --------------------------------------------------------------------------

def verify_chain(manifests: list[dict[str, Any]]) -> None:
    """Validate an ordered list of manifests (oldest first). Raises
    ManifestError on the first violation — gapless and fail-closed:

    - ``seq`` strictly increasing,
    - ``prev_manifest_sha256`` equals the canonical sha256 of the previous
      manifest (hash chain — detects a dropped or substituted run),
    - per-subvol blob lineage gapless: every entry's ``parent_blob`` is
      either None (a full send) or a ``blob`` that appeared in an earlier
      manifest for the SAME subvol (detects a dropped incremental).
    """
    if not manifests:
        raise ManifestError("empty manifest list")

    seen_blobs_by_subvol: dict[str, set[str]] = {}
    prev: dict[str, Any] | None = None
    for i, m in enumerate(manifests):
        if prev is not None:
            if m["seq"] <= prev["seq"]:
                raise ManifestError(
                    f"seq not strictly increasing at index {i}: "
                    f"{prev['seq']} -> {m['seq']}")
            expect = manifest_sha256(prev)
            if m.get("prev_manifest_sha256") != expect:
                raise ManifestError(
                    f"broken hash chain at index {i}: prev_manifest_sha256="
                    f"{m.get('prev_manifest_sha256')!r} expected {expect!r}")
        else:
            if m.get("prev_manifest_sha256") is not None:
                raise ManifestError(
                    "first manifest must have prev_manifest_sha256 = null")

        for e in m["entries"]:
            subvol = e["subvol"]
            seen = seen_blobs_by_subvol.setdefault(subvol, set())
            parent = e.get("parent_blob")
            if parent is not None and parent not in seen:
                raise ManifestError(
                    f"manifest index {i}: entry {e['blob']!r} for subvol "
                    f"{subvol!r} references parent {parent!r} not present in "
                    "any earlier manifest (dropped incremental?)")
            seen.add(e["blob"])
        prev = m


def check_freshness(newest_manifest: dict[str, Any],
                    checkpoint_seq: int) -> None:
    """Anti-rollback. Restore MUST fail unless the newest verified seq is
    >= the owner's externally-recorded checkpoint (06 §3.2). Without this,
    a hostile target can replay an older, validly-signed set."""
    if newest_manifest["seq"] < checkpoint_seq:
        raise ManifestError(
            f"freshness check failed: newest manifest seq "
            f"{newest_manifest['seq']} < owner checkpoint {checkpoint_seq} "
            "(possible rollback — refusing to restore)")


def verify_blobs(manifest: dict[str, Any], blob_dir: str) -> list[str]:
    """Check every entry's blob exists in ``blob_dir`` with the recorded
    size and sha256. Returns a list of human-readable problems (empty =
    all good). Used by both ``verify`` (spot/full) and ``restore`` (full,
    before receive)."""
    problems: list[str] = []
    for e in manifest["entries"]:
        path = os.path.join(blob_dir, e["blob"])
        if not os.path.isfile(path):
            problems.append(f"{e['blob']}: missing")
            continue
        actual_size = os.path.getsize(path)
        if actual_size != e["size"]:
            problems.append(
                f"{e['blob']}: size {actual_size} != manifest {e['size']}")
        actual_sha = sha256_file(path)
        if actual_sha != e["sha256"]:
            problems.append(
                f"{e['blob']}: sha256 mismatch (corrupt or substituted)")
    return problems


def restore_order(manifests: list[dict[str, Any]], subvol: str
                  ) -> list[dict[str, Any]]:
    """Flatten the verified manifest chain into the receive order for one
    subvol: the full send first, then each incremental in manifest order.
    Caller has already run verify_chain + verify_blobs."""
    out: list[dict[str, Any]] = []
    for m in manifests:
        for e in m["entries"]:
            if e["subvol"] == subvol:
                out.append(e)
    if not out:
        raise ManifestError(f"no blobs for subvol {subvol!r} in the chain")
    if out[0].get("parent_blob") is not None:
        raise ManifestError(
            f"subvol {subvol!r} chain does not start with a full send")
    return out
