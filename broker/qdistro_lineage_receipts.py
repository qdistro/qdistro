"""Artifact-adjacent lineage receipt surfaces: emit + parse + verify.

doc/lineage.md §Storage asks for *artifact-adjacent records* that point back at
the central tamper-evident lineage chain: a sidecar file beside an exported
artifact, a ``user.qdistro.lineage`` xattr, a ``Qdistro-Lineage`` git trailer, a
``qdistro-export-manifest.json`` covering a batch, an upload receipt. The store
(:mod:`qdistro_lineage_store`) already records the *central* row
(:meth:`~qdistro_lineage_store.LineageStore.record_receipt`, sealed into the
hash chain) and defines the on-disk names (``RECEIPT_NAMES``). What it does NOT
do is write the surface itself or read one back. This module is that pure layer.

Trust split (matches the broker authority model and doc/lineage.md §Authority):

* The **broker** computes the artifact digest, records the sealed receipt row,
  and *builds* the receipt envelope (it is the only party allowed to mint a
  receipt whose digest the chain seals).
* The emit functions here are pure I/O — they write the EXACT envelope the
  broker produced. A user-context promoter may call them to drop the surface
  beside a user-owned artifact, but it never invents or seals content.
* A receipt surface is **never authoritative on its own**: a parsed envelope
  only means something after :func:`verify_against_store` checks it against the
  central chain. The sidecar/manifest are the durable path; the xattr is an
  opportunistic local pointer that may not survive copies/moves/archives.

Task 1 ships sidecar + export-manifest + xattr. git-trailer and upload-receipt
are later milestones (the envelope/canonical-bytes contract here is shared).

Style mirrors the other broker modules: plain functions, stdlib only, fail
closed on anything malformed.
"""
from __future__ import annotations

import errno
import json
import os
from typing import Any

from qdistro_lineage_store import RECEIPT_KINDS, RECEIPT_NAMES

# --- format ---------------------------------------------------------------

#: Receipt envelope schema tag. Bump the version suffix on an incompatible
#: change; parsers reject any other value (fail closed, no negotiation).
RECEIPT_SCHEMA = "qdistro-lineage-receipt/v1"
#: Container schema for the batch export manifest (holds N child envelopes).
EXPORT_MANIFEST_SCHEMA = "qdistro-lineage-export-manifest/v1"

#: The chain-seal algorithm the store uses today (H(prev||table||row); NOT a
#: signature — key custody is an open decision, doc/lineage.md §Open Decisions).
CHAIN_ALGO = "sha256-chain"

DEFAULT_ISSUER = "qdistro-broker"

#: Read caps (fail closed before allocating): a receipt is small structured
#: JSON, never a payload. A hostile/oversized surface is rejected, not parsed.
SIDECAR_MAX_BYTES = 1 << 20          # 1 MiB
MANIFEST_BASE_BYTES = 1 << 20        # 1 MiB base ...
MANIFEST_PER_RECEIPT_BYTES = 1 << 16  # ... + 64 KiB per declared child
MANIFEST_HARD_MAX_BYTES = 64 << 20   # absolute ceiling on a manifest read (bounds alloc)
XATTR_MAX_BYTES = 60 << 10           # stay under the ~64 KiB Linux xattr cap

#: Required envelope keys (others are optional/reserved). ``artifact_digest`` may
#: legitimately be ``None`` (a recorded-but-content-free entity), so it is
#: required-present but nullable.
_REQUIRED_KEYS = ("schema", "kind", "entity", "artifact_digest", "locator",
                  "issuer", "chain_algo", "chain_head", "signature_algo",
                  "key_id", "signature", "created_at")

#: The compact pointer stored in an xattr (xattrs are copied inconsistently and
#: size-capped, so they carry only enough to find + verify against the store).
_XATTR_KEYS = ("schema", "kind", "entity", "artifact_digest", "chain_head",
               "created_at", "issuer")


# --- errors ---------------------------------------------------------------


class ReceiptError(Exception):
    """Base for receipt emit/parse/verify failures."""


class MalformedReceipt(ReceiptError):
    """A surface payload is not a well-formed receipt envelope."""


class UnsupportedReceipt(ReceiptError):
    """A receipt kind/surface combination this module does not handle."""


class ReceiptVerificationError(ReceiptError):
    """A receipt could not be verified against the central lineage store."""


class ReceiptSurfaceUnavailable(ReceiptError):
    """The storage surface itself is unavailable (e.g. xattr unsupported). Only
    raised where a caller explicitly opts into hard failure; the default xattr
    path soft-fails to ``None``."""


# --- canonical bytes ------------------------------------------------------


def canonical_bytes(obj: dict[str, Any]) -> bytes:
    """The one canonical serialization used for every surface, the idempotency
    check, and any future signature input: UTF-8, sorted keys, no insignificant
    whitespace, no trailing newline. Rejects non-JSON-native values up front."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _no_dup_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise MalformedReceipt(f"duplicate key {k!r} in receipt JSON")
        seen[k] = v
    return seen


def _loads_strict(data: bytes) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise MalformedReceipt("receipt is not valid UTF-8") from e
    try:
        return json.loads(text, object_pairs_hook=_no_dup_pairs)
    except MalformedReceipt:
        raise
    except (ValueError, TypeError) as e:
        raise MalformedReceipt(f"receipt is not valid JSON: {e}") from e


# --- envelope build + validate --------------------------------------------


def build_envelope(*, entity: str, kind: str, chain_head: str, created_at: int,
                   artifact_digest: str | None = None,
                   locator: str | None = None,
                   issuer: str = DEFAULT_ISSUER,
                   extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the canonical receipt envelope. Reserved ``signature_*``/``key_id``
    fields are present-but-null so a real public-key signature can be added later
    WITHOUT a schema break; a future signature covers the canonical bytes with
    ``signature`` set back to ``null`` (it cannot cover itself). The hash-chain
    seal (``chain_algo``/``chain_head``) is a SEPARATE, weaker trust mechanism —
    it only makes tampering detectable given a trusted off-host head."""
    if kind not in RECEIPT_KINDS:
        raise UnsupportedReceipt(
            f"unknown receipt kind {kind!r} (allowed: {sorted(RECEIPT_KINDS)})"
        )
    if not isinstance(entity, str) or not entity:
        raise MalformedReceipt("entity must be a non-empty string")
    if not isinstance(chain_head, str) or not chain_head:
        raise MalformedReceipt("chain_head must be a non-empty string")
    if not isinstance(created_at, int) or isinstance(created_at, bool):
        raise MalformedReceipt("created_at must be an int (epoch seconds)")
    if artifact_digest is not None and not _is_hex_digest(artifact_digest):
        raise MalformedReceipt("artifact_digest must be a hex digest or None")
    env: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "kind": kind,
        "entity": entity,
        "artifact_digest": artifact_digest,
        "locator": locator,
        "issuer": issuer,
        "chain_algo": CHAIN_ALGO,
        "chain_head": chain_head,
        "signature_algo": None,
        "key_id": None,
        "signature": None,
        "created_at": created_at,
    }
    if extra is not None:
        if not isinstance(extra, dict):
            raise MalformedReceipt("extra must be a dict")
        env["extra"] = extra
    # Round-trip through the canonical serializer now so a non-JSON-native value
    # in `extra` fails at build time (the trusted broker), not at emit time.
    try:
        canonical_bytes(env)
    except (TypeError, ValueError) as e:
        raise MalformedReceipt(f"receipt is not JSON-serializable: {e}") from e
    return env


def _is_hex_digest(s: Any) -> bool:
    """v1 digest grammar: a bare 64-char lowercase sha256 hex string — exactly
    what the store records (``hashlib.sha256(...).hexdigest()``). One spelling
    only, so a receipt digest is never structurally valid yet unmatchable."""
    if not isinstance(s, str) or len(s) != 64:
        return False
    return all(c in "0123456789abcdef" for c in s)


def validate_envelope(envelope: Any, *, expected_kind: str | None = None) -> dict[str, Any]:
    """Strict structural validation of a parsed envelope. Returns it unchanged on
    success; raises :class:`MalformedReceipt`/:class:`UnsupportedReceipt`
    otherwise. Presence alone is never authority — callers still
    :func:`verify_against_store`."""
    if not isinstance(envelope, dict):
        raise MalformedReceipt("receipt envelope must be a JSON object")
    for key in _REQUIRED_KEYS:
        if key not in envelope:
            raise MalformedReceipt(f"receipt missing required field {key!r}")
    if envelope["schema"] != RECEIPT_SCHEMA:
        raise MalformedReceipt(
            f"unexpected receipt schema {envelope['schema']!r}"
        )
    if envelope["chain_algo"] != CHAIN_ALGO:
        raise MalformedReceipt(
            f"unexpected chain_algo {envelope['chain_algo']!r}"
        )
    kind = envelope["kind"]
    if kind not in RECEIPT_KINDS:
        raise UnsupportedReceipt(f"unknown receipt kind {kind!r}")
    if expected_kind is not None and kind != expected_kind:
        raise MalformedReceipt(
            f"receipt kind {kind!r} != expected {expected_kind!r}"
        )
    if not isinstance(envelope["entity"], str) or not envelope["entity"]:
        raise MalformedReceipt("entity must be a non-empty string")
    if not isinstance(envelope["chain_head"], str) or not envelope["chain_head"]:
        raise MalformedReceipt("chain_head must be a non-empty string")
    if not isinstance(envelope["issuer"], str) or not envelope["issuer"]:
        raise MalformedReceipt("issuer must be a non-empty string")
    ca = envelope["created_at"]
    if not isinstance(ca, int) or isinstance(ca, bool):
        raise MalformedReceipt("created_at must be an int")
    ad = envelope["artifact_digest"]
    if ad is not None and not _is_hex_digest(ad):
        raise MalformedReceipt("artifact_digest must be a hex digest or null")
    loc = envelope["locator"]
    if loc is not None and not isinstance(loc, str):
        raise MalformedReceipt("locator must be a string or null")
    # v1 reserves the signature fields but does NOT verify signatures yet, so
    # they MUST be null — otherwise a forged receipt could carry plausible
    # signature metadata that downstream tooling mistakes for a real signature.
    for sigfield in ("signature_algo", "key_id", "signature"):
        if envelope[sigfield] is not None:
            raise MalformedReceipt(
                f"{sigfield} must be null in {RECEIPT_SCHEMA} (signing not implemented)"
            )
    return envelope


# --- sidecar --------------------------------------------------------------


def sidecar_name(artifact_basename: str) -> str:
    """``<artifact>.qdistro-lineage.json`` — the sidecar suffix appended to the
    artifact's own name (RECEIPT_NAMES['sidecar'])."""
    return artifact_basename + RECEIPT_NAMES["sidecar"]


def write_sidecar(artifact_path: str, envelope: dict[str, Any], *,
                  dir_fd: int | None = None,
                  owner_uid: int | None = None,
                  owner_gid: int | None = None) -> str:
    """Atomically write the sidecar beside ``artifact_path``. When ``dir_fd`` is
    given, ``artifact_path`` is a basename resolved RELATIVE to that already-
    verified directory fd (Task 2 reuses the export's O_NOFOLLOW-rooted chain);
    otherwise it is a full path. ``owner_uid``/``owner_gid`` (when running as root)
    fchown the file to a less-trusted owner BEFORE publish, so a receipt landing in
    a silo-owned tree is readable by that silo — done on the open fd, never a path,
    so it is race-free. Returns the sidecar's name/path. Validates the envelope
    first (a malformed envelope never reaches disk)."""
    validate_envelope(envelope, expected_kind="sidecar")
    if dir_fd is not None:
        _require_basename(artifact_path)
    name = sidecar_name(artifact_path)
    _write_file_atomic(name, canonical_bytes(envelope), dir_fd=dir_fd,
                       owner_uid=owner_uid, owner_gid=owner_gid)
    return name


def read_sidecar(artifact_path: str, *, dir_fd: int | None = None) -> dict[str, Any]:
    """Read + structurally validate the sidecar beside ``artifact_path``. Does
    not follow symlinks and caps the read size. Raises on missing/malformed."""
    if dir_fd is not None:
        _require_basename(artifact_path)
    name = sidecar_name(artifact_path)
    data = _read_file_capped(name, SIDECAR_MAX_BYTES, dir_fd=dir_fd)
    env = _loads_strict(data)
    return validate_envelope(env, expected_kind="sidecar")


# --- xattr (opportunistic, best-effort) -----------------------------------


def _xattr_pointer(envelope: dict[str, Any]) -> dict[str, Any]:
    return {k: envelope[k] for k in _XATTR_KEYS}


def set_xattr(artifact_path: str, envelope: dict[str, Any], *,
              follow_symlinks: bool = False) -> str | None:
    """Best-effort: tag the artifact with a COMPACT lineage pointer in the
    ``user.qdistro.lineage`` xattr. Returns the xattr name on success, or
    ``None`` if the surface is unavailable (unsupported fs, permission, too
    large). A *malformed envelope* still raises (that is a programming error,
    not a storage limitation). The xattr is never the canonical record — pair it
    with a sidecar/manifest."""
    validate_envelope(envelope)  # any kind; the pointer is kind-agnostic
    name = RECEIPT_NAMES["xattr"]
    data = canonical_bytes(_xattr_pointer(envelope))
    if len(data) > XATTR_MAX_BYTES:
        return None
    try:
        os.setxattr(artifact_path, name, data, follow_symlinks=follow_symlinks)
    except OSError:
        # ENOTSUP/EOPNOTSUPP/EPERM/E2BIG/ENOSPC/EDQUOT/ELOOP ... — opportunistic.
        return None
    return name


def read_xattr(artifact_path: str, *,
               follow_symlinks: bool = False) -> dict[str, Any] | None:
    """Read the compact xattr pointer, or ``None`` if absent/unsupported. A
    PRESENT but malformed xattr raises :class:`MalformedReceipt` (tampering is
    not silently ignored)."""
    name = RECEIPT_NAMES["xattr"]
    try:
        data = os.getxattr(artifact_path, name, follow_symlinks=follow_symlinks)
    except OSError as e:
        # "absent / unsupported surface" → None (opportunistic). EPERM/EACCES are
        # deliberately NOT swallowed: a permission/policy denial on read is a real
        # signal, not "no xattr", and hiding it weakens diagnostics.
        if e.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOENT):
            return None
        raise
    if len(data) > XATTR_MAX_BYTES:
        raise MalformedReceipt("xattr lineage pointer is implausibly large")
    obj = _loads_strict(data)
    if not isinstance(obj, dict):
        raise MalformedReceipt("xattr lineage pointer must be a JSON object")
    # Exact key-set: the compact pointer carries ONLY _XATTR_KEYS (matches the
    # sidecar's strict posture), so a hostile pointer cannot smuggle extra keys.
    if set(obj) != set(_XATTR_KEYS):
        raise MalformedReceipt("xattr pointer has unexpected key set")
    if obj["schema"] != RECEIPT_SCHEMA:
        raise MalformedReceipt(f"unexpected xattr schema {obj['schema']!r}")
    if obj["kind"] not in RECEIPT_KINDS:
        raise MalformedReceipt(f"unknown xattr kind {obj['kind']!r}")
    if not isinstance(obj["entity"], str) or not obj["entity"]:
        raise MalformedReceipt("xattr entity must be a non-empty string")
    if not isinstance(obj["chain_head"], str) or not obj["chain_head"]:
        raise MalformedReceipt("xattr chain_head must be a non-empty string")
    if not isinstance(obj["issuer"], str) or not obj["issuer"]:
        raise MalformedReceipt("xattr issuer must be a non-empty string")
    if not isinstance(obj["created_at"], int) or isinstance(obj["created_at"], bool):
        raise MalformedReceipt("xattr created_at must be an int")
    ad = obj["artifact_digest"]
    if ad is not None and not _is_hex_digest(ad):
        raise MalformedReceipt("xattr artifact_digest must be a hex digest or null")
    return obj


# --- export manifest (batch container) ------------------------------------


def build_export_manifest(envelopes: list[dict[str, Any]], *, chain_head: str,
                          created_at: int, issuer: str = DEFAULT_ISSUER
                          ) -> dict[str, Any]:
    """Build the batch manifest container. Every child is a full receipt envelope
    with ``kind == 'export-manifest'`` and a ``chain_head`` equal to the
    container's (so an extracted child is self-describing and drift is caught)."""
    if not isinstance(chain_head, str) or not chain_head:
        raise MalformedReceipt("chain_head must be a non-empty string")
    children: list[dict[str, Any]] = []
    for env in envelopes:
        validate_envelope(env, expected_kind="export-manifest")
        if env["chain_head"] != chain_head:
            raise MalformedReceipt(
                "child receipt chain_head differs from manifest chain_head"
            )
        children.append(env)
    return {
        "schema": EXPORT_MANIFEST_SCHEMA,
        "issuer": issuer,
        "chain_algo": CHAIN_ALGO,
        "chain_head": chain_head,
        "created_at": created_at,
        "receipts": children,
    }


def write_export_manifest(dest_dir: str, manifest: dict[str, Any], *,
                          dir_fd: int | None = None,
                          owner_uid: int | None = None,
                          owner_gid: int | None = None) -> str:
    """Atomically write the manifest into ``dest_dir`` as
    ``qdistro-export-manifest.json``. With ``dir_fd``, ``dest_dir`` is ignored
    for opening (the manifest name resolves relative to ``dir_fd``).
    ``owner_uid``/``owner_gid`` fchown it to a less-trusted owner before publish
    (see :func:`write_sidecar`)."""
    _validate_manifest(manifest)
    name = RECEIPT_NAMES["export-manifest"]
    target = name if dir_fd is not None else os.path.join(dest_dir, name)
    _write_file_atomic(target, canonical_bytes(manifest), dir_fd=dir_fd,
                       owner_uid=owner_uid, owner_gid=owner_gid)
    return target


def read_export_manifest(dest_dir: str, *,
                         dir_fd: int | None = None) -> dict[str, Any]:
    """Read + validate the batch manifest. Caps the read proportional to the
    declared child count after a cheap base read. Returns the validated manifest
    (``["receipts"]`` is the list of child envelopes)."""
    name = RECEIPT_NAMES["export-manifest"]
    target = name if dir_fd is not None else os.path.join(dest_dir, name)
    # Read up to a hard ceiling (bounds allocation); the per-child proportional
    # cap in _validate_manifest then rejects a manifest that is large RELATIVE to
    # its declared child count (so a big-but-few-children file fails, while a
    # genuinely large many-children export within budget is allowed).
    data = _read_file_capped(target, MANIFEST_HARD_MAX_BYTES, dir_fd=dir_fd)
    obj = _loads_strict(data)
    return _validate_manifest(obj, declared_cap_bytes=len(data))


def _validate_manifest(manifest: Any, *, declared_cap_bytes: int | None = None
                       ) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise MalformedReceipt("export manifest must be a JSON object")
    if manifest.get("schema") != EXPORT_MANIFEST_SCHEMA:
        raise MalformedReceipt(
            f"unexpected manifest schema {manifest.get('schema')!r}"
        )
    if manifest.get("chain_algo") != CHAIN_ALGO:
        raise MalformedReceipt("unexpected manifest chain_algo")
    head = manifest.get("chain_head")
    if not isinstance(head, str) or not head:
        raise MalformedReceipt("manifest chain_head must be a non-empty string")
    if not isinstance(manifest.get("created_at"), int) or isinstance(
            manifest.get("created_at"), bool):
        raise MalformedReceipt("manifest created_at must be an int")
    receipts = manifest.get("receipts")
    if not isinstance(receipts, list):
        raise MalformedReceipt("manifest receipts must be a list")
    if declared_cap_bytes is not None:
        cap = MANIFEST_BASE_BYTES + MANIFEST_PER_RECEIPT_BYTES * max(1, len(receipts))
        if declared_cap_bytes > cap:
            raise MalformedReceipt("export manifest exceeds size budget")
    for env in receipts:
        validate_envelope(env, expected_kind="export-manifest")
        if env["chain_head"] != head:
            raise MalformedReceipt(
                "child receipt chain_head differs from manifest chain_head"
            )
    return manifest


# --- verification against the central store --------------------------------


def verify_against_store(store: Any, envelope: dict[str, Any], *,
                         expected_chain_head: str | None = None) -> bool:
    """Fail-closed check that a parsed surface corresponds to a SEALED central
    receipt row. A surface (sidecar/xattr/manifest) is attacker-writable in the
    Task-2 export model, so NOTHING in the envelope is trusted as authority — the
    gate is the sealed ``receipts`` row the broker recorded via
    :meth:`~qdistro_lineage_store.LineageStore.record_receipt`. The envelope is
    only "valid" when it matches such a row:

    * the envelope must be structurally valid (else this raises);
    * its ``entity`` must exist in the store;
    * there must be a sealed receipt row for that entity whose ``kind`` matches
      AND whose recorded ``payload`` canonically equals this entire envelope. A
      full-payload match authenticates EVERY field (digest, locator, chain_head,
      created_at, issuer, extra), so a "verified receipt" always means the exact
      surface bytes were sealed by the broker. A receipt with no such sealed row
      never verifies — writing a plausible sidecar for an existing entity, or a
      digest-only row that did not seal the envelope metadata, is NOT enough
      (closes the entity-digest-only, null-digest, and forged-metadata fail-opens);
    * additionally, when the entity has a recorded content digest and the
      envelope carries one, they must agree (defence in depth);
    * when ``expected_chain_head`` (an off-host-pinned head) is supplied, the
      whole chain must verify against it — this catches tail truncation.

    The envelope's own ``chain_head`` is the head AT EMISSION (a past head, not
    the current one), so it is NOT compared to ``expected_chain_head``; it is
    instead authenticated by the full-payload sealed-row match above (an attacker
    cannot alter it without breaking that match).

    NOTE: a verified receipt attests that the broker SEALED this exact envelope
    (whose ``artifact_digest`` was the artifact's content at export time) — it does
    NOT re-hash the artifact, so it does not by itself prove the *current* file
    bytes still match. A caller that needs "does the file on disk still match"
    must re-hash the artifact and compare it to ``artifact_digest`` separately.

    Returns ``True``/``False``; only a structurally malformed envelope raises."""
    validate_envelope(envelope)
    ent = store.get_entity(envelope["entity"])
    if ent is None:
        return False
    try:
        rows = store.receipts_for(envelope["entity"])
    except Exception:
        return False  # a store error is "not verified", never a pass
    if not _matches_sealed_row(envelope, rows):
        return False
    ad = envelope["artifact_digest"]
    if ad is not None and ent.digest is not None and ent.digest != ad:
        return False
    if expected_chain_head is not None and not store.verify_chain(expected_chain_head):
        return False
    return True


def _matches_sealed_row(envelope: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    """True iff a sealed receipt row recorded this EXACT surface: a row whose
    ``kind`` matches and whose ``payload`` canonically equals the whole envelope.

    There is deliberately NO digest-only fallback: a row that sealed only a digest
    (``payload is None``) does not authenticate the envelope's metadata
    (locator/chain_head/created_at/issuer/extra), so accepting it would let those
    forged fields pass as "verified". v1 requires the broker to seal the full
    envelope (which Task 2 does), so a genuine receipt always has a payload row."""
    want = canonical_bytes(envelope)
    for row in rows:
        if row.get("kind") != envelope["kind"]:
            continue
        payload = row.get("payload")
        if payload is None:
            continue  # digest-only row does not authenticate envelope metadata
        try:
            if canonical_bytes(payload) == want:
                return True
        except (TypeError, ValueError):
            continue  # uncanonicalizable stored payload → cannot match
    return False


# --- low-level file I/O (openat, O_NOFOLLOW, atomic) ----------------------


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _require_basename(name: str) -> None:
    """In ``dir_fd`` mode the name MUST be a single leaf basename resolved under
    the caller's already-verified directory fd. Reject anything that could
    traverse out of (or below) that directory — ``/`` separators, ``..``/``.``,
    an empty name, or any non-leaf form — so a less-trusted writer cannot make a
    root reader/writer escape the rooted export tree via an intermediate symlink
    or ``..`` (``O_NOFOLLOW`` only guards the final component). Nested
    export-relative paths, if ever needed, must be resolved by the caller's own
    O_NOFOLLOW openat chain and only the leaf passed here."""
    if not name or "/" in name or name in (".", "..") or os.path.basename(name) != name:
        raise ValueError(
            f"dir_fd mode requires a single basename, got {name!r}"
        )


def _write_file_atomic(name: str, data: bytes, *, dir_fd: int | None = None,
                       owner_uid: int | None = None,
                       owner_gid: int | None = None) -> None:
    """Write ``data`` to ``name`` via temp-file + fsync + atomic rename, never
    following a symlink at the final name. With ``dir_fd``, ``name`` is a
    basename resolved relative to that directory fd; otherwise it is a path whose
    parent dir is opened O_DIRECTORY for the rename + dir fsync. When ``owner_uid``
    is given and we run as root, the file is fchowned on the open fd (before the
    publish rename) so it lands owned by a less-trusted owner — race-free."""
    if dir_fd is not None:
        _require_basename(name)
        parent_fd = dir_fd
        base = name
        own_parent = False
    else:
        parent = os.path.dirname(name) or "."
        base = os.path.basename(name)
        parent_fd = os.open(parent, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
        own_parent = True
    tmp = f".{base}.qdtmp.{os.getpid()}"
    published = False
    try:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC)
        try:
            fd = os.open(tmp, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            # Clear a stale temp from a crashed prior write, then retry once.
            os.unlink(tmp, dir_fd=parent_fd)
            fd = os.open(tmp, flags, 0o600, dir_fd=parent_fd)
        try:
            if owner_uid is not None and os.geteuid() == 0:
                # fchown the fd we just created (never a path) before publish, so
                # the receipt lands owned by the silo, not root. Best-effort: a
                # chown failure must not abandon an otherwise-complete write.
                try:
                    os.fchown(fd, owner_uid,
                              owner_gid if owner_gid is not None else -1)
                except OSError:
                    pass
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        # Atomic publish; O_NOFOLLOW on create above means we never wrote through
        # a symlink, and rename replaces the final name in one step.
        os.rename(tmp, base, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        published = True  # renamed away; nothing to clean up
        os.fsync(parent_fd)
    finally:
        if not published:
            try:
                os.unlink(tmp, dir_fd=parent_fd)
            except OSError:
                pass
        if own_parent:
            os.close(parent_fd)


def _read_file_capped(name: str, cap: int, *, dir_fd: int | None = None) -> bytes:
    """Open ``name`` O_NOFOLLOW (no symlink), reject anything larger than ``cap``
    by fstat before reading, and read at most ``cap`` bytes."""
    flags = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC
    if dir_fd is not None:
        _require_basename(name)
        fd = os.open(name, flags, dir_fd=dir_fd)
    else:
        fd = os.open(name, flags)
    try:
        st = os.fstat(fd)
        if st.st_size > cap:
            raise MalformedReceipt(
                f"receipt surface {name!r} is {st.st_size} bytes (> {cap} cap)"
            )
        chunks: list[bytes] = []
        remaining = cap
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
