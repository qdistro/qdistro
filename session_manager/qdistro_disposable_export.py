"""Disposable export-back promoter (07-disposables-plan P2 — the D7 copy-exception).

A tier-2 disposable has no reachable host D-Bus (empty per-container /run/user,
cap-drop ALL, no-new-privileges, network none), so the ONLY channel for an
artifact to leave it is a bind-mounted filesystem path. spawn-tier2 binds a
per-launch host staging dir READ-WRITE at ``/mnt/output`` (only when the resolved
open class declares ``export = true`` AND the admin broker allows
``qdistro.dispose.export:<class>``). The disposable drops result files there.

This module is the trusted host-side IMPORTER — the qfile-unpacker analog. It
treats the staged payload as HOSTILE (the disposable may be compromised) and
promotes it into the requesting silo's home DEFENSIVELY:

- It runs ONLY after the disposable container is gone (the session-manager store
  force-disposes the token and verifies removal first), so a live disposable
  cannot race the lstat/open/read or grow/swap a file mid-copy.
- ALL-OR-NOTHING: it scans every top-level entry first; if ANY entry is not a
  plain regular file (symlink, fifo, socket, device, subdirectory) OR a size/
  count cap is exceeded, the WHOLE import is refused and NOTHING is promoted.
  A hostile tree never yields a clean-looking partial import.
- openat-style: it opens the payload directory once and does every lstat/open
  RELATIVE to that fd with ``O_NOFOLLOW`` — a symlink swapped in cannot redirect
  a read outside the payload, and the path it validated is the path it copies.
- The destination is built under a temp name, files are copied + fsync'd, the
  receipt is written, the dir is fsync'd, and only then is it atomically renamed
  into place — a crash never leaves a half-imported dir looking complete.
- A lineage receipt (``_receipt.json``) records token / class / request-silo /
  source-input lineage / per-file size+sha256 — provenance Qubes' qvm-copy lacks.

The walk/validation/sanitization logic is pure (filesystem only, no podman, no
D-Bus) so it is unit-testable on a headless dev host.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass

# The importer-owned receipt filename. Reserved: a payload file whose sanitized
# leaf collides with this is refused (the whole import fails) so a hostile
# disposable can never overwrite / forge the provenance record.
RECEIPT_NAME = "_receipt.json"
RECEIPT_VERSION = 1

# Reserved lineage-receipt surface names (mirror qdistro_lineage_receipts
# RECEIPT_NAMES — hardcoded here so the hot scan path never depends on the
# receipt library being importable). A payload artifact whose leaf would collide
# with an emitted sidecar/manifest is refused, so a hostile disposable can never
# clobber a surface or smuggle a forged one in the artifact set.
LINEAGE_SIDECAR_SUFFIX = ".qdistro-lineage.json"
LINEAGE_MANIFEST_NAME = "qdistro-export-manifest.json"

# Defensive caps (fail-closed when exceeded — a breach aborts the whole import,
# never a partial promotion). Tunable by the caller; these are the defaults.
DEFAULT_MAX_FILES = 256
DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024        # 256 MiB per file
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024      # 1 GiB total

# Copy chunk size (also the hashing granularity).
_CHUNK = 1024 * 1024

# The fixed top-level landing dir name under the requesting silo's home — the
# qdistro de-brand of Qubes' ``QubesIncoming`` (same one-dir-per-source shape).
# Proposed answer to fable-vs-qubes Round-3 Q2 (canonical landing-dir naming);
# a single constant, trivially renamable if Jan prefers another name.
INCOMING_DIRNAME = "Incoming"

# Edit-round-trip landing suffix. A file opened FOR EDITING in a disposable is
# promoted back BESIDE its source as ``<source-name><EDITED_SUFFIX>`` — never
# overwriting the source in place (the source is the user's authoritative copy;
# the disposable's output is an *offer*, not a replacement). On the (rare) name
# collision a ``-<n>`` is appended; the landing NEVER clobbers an existing file
# (the link is no-overwrite, see :func:`_link_into_place`).
EDITED_SUFFIX = ".disp-edited"


class ExportError(Exception):
    """Base for export-back promotion failures."""


class ExportPolicyError(ExportError):
    """The staged payload violates the defensive policy — an unsupported entry
    (symlink/fifo/socket/device/subdir), a reserved-name collision, or a size/
    count cap breach. ALL-OR-NOTHING: raised BEFORE anything is promoted, so a
    payload that trips this leaves the requesting silo untouched."""


class ExportStateError(ExportError):
    """A filesystem/I/O error reading the payload or writing the destination —
    distinct from a policy refusal so the caller maps it to a clean BadState
    rather than 'nothing to import'."""


@dataclass(frozen=True)
class ExportCaps:
    max_files: int = DEFAULT_MAX_FILES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES


# ---------------------------------------------------------------------------
# Pure name hygiene
# ---------------------------------------------------------------------------


def sanitize_class_leaf(class_name: str) -> str:
    """Encode an open-class name to ONE safe filesystem leaf for the per-source
    directory. Class names may contain ``/`` (e.g. ``text/plain``), which would
    otherwise create extra hierarchy, so ``/`` is percent-encoded as ``%2F`` (a
    reversible mapping). ``%`` itself is encoded as ``%25`` first so the mapping
    stays unambiguous. The registry already constrains class names to
    ``[a-z0-9./-]`` with no ``..``; this is defence in depth + the explicit
    one-leaf guarantee. The TRUE class is recorded verbatim in the receipt."""
    if not isinstance(class_name, str) or not class_name:
        raise ExportStateError(f"invalid open class for landing dir: {class_name!r}")
    leaf = class_name.replace("%", "%25").replace("/", "%2F")
    # After encoding there must be no path separators and it must be a single,
    # non-traversal component. (Belt and suspenders — the registry already
    # forbids '..' and the only separator a valid class can carry is '/'.)
    if leaf in ("", ".", "..") or "/" in leaf or "\x00" in leaf:
        raise ExportStateError(f"open class did not encode to a safe leaf: {class_name!r}")
    return leaf


def sanitize_filename(name: str) -> str | None:
    """Return a safe single-component landing leaf for a payload filename, or
    ``None`` if the name is unsafe and must be refused. Rejects the empty name,
    ``.``/``..``, any embedded ``/`` or NUL or control byte, and the reserved
    receipt name. The name is used verbatim as the leaf (no rewriting beyond the
    accept/reject decision) so the receipt's recorded name matches what landed."""
    if not isinstance(name, str) or not name:
        return None
    if name in (".", ".."):
        return None
    if "/" in name or "\x00" in name:
        return None
    # Any C0 control byte (including newline/tab) — a filename should not carry
    # them and they make audit/log lines ambiguous.
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in name):
        return None
    if name == RECEIPT_NAME:
        # Reserved for the importer-written provenance record.
        return None
    if name == LINEAGE_MANIFEST_NAME or name.endswith(LINEAGE_SIDECAR_SUFFIX):
        # Reserved for the importer-written lineage receipt surfaces.
        return None
    return name


# ---------------------------------------------------------------------------
# Defensive scan (all-or-nothing) — openat-relative, no symlink following
# ---------------------------------------------------------------------------


def _scan_payload(dir_fd: int, caps: ExportCaps) -> list[tuple[str, int]]:
    """Validate every top-level entry under the payload dir fd and return
    ``[(name, size)]`` for promotion, or raise. ALL-OR-NOTHING: the FIRST
    unsupported entry / reserved-name collision / cap breach raises
    :class:`ExportPolicyError` and nothing is promoted. lstat is done relative to
    ``dir_fd`` with ``follow_symlinks=False`` so a symlink entry is detected as a
    symlink (never followed)."""
    try:
        names = sorted(os.listdir(dir_fd))
    except OSError as e:
        raise ExportStateError(f"cannot list payload dir: {e}") from e

    files: list[tuple[str, int]] = []
    total = 0
    for name in names:
        if len(files) >= caps.max_files:
            raise ExportPolicyError(
                f"payload exceeds the {caps.max_files}-file cap")
        safe = sanitize_filename(name)
        if safe is None:
            raise ExportPolicyError(
                f"payload contains an unsafe/reserved entry name {name!r} "
                f"— refusing the whole import")
        try:
            st = os.lstat(name, dir_fd=dir_fd)
        except OSError as e:
            raise ExportStateError(f"cannot lstat payload entry {name!r}: {e}") from e
        mode = st.st_mode
        if stat.S_ISLNK(mode):
            raise ExportPolicyError(
                f"payload entry {name!r} is a symlink — refusing the whole import")
        if not stat.S_ISREG(mode):
            # Directories, fifos, sockets, char/block devices — none are a plain
            # artifact. v1 minimal parser: refuse the whole import.
            raise ExportPolicyError(
                f"payload entry {name!r} is not a regular file "
                f"(mode {stat.S_IFMT(mode):#o}) — refusing the whole import")
        size = st.st_size
        if size > caps.max_file_bytes:
            raise ExportPolicyError(
                f"payload entry {name!r} is {size} bytes, over the "
                f"{caps.max_file_bytes}-byte per-file cap")
        total += size
        if total > caps.max_total_bytes:
            raise ExportPolicyError(
                f"payload total exceeds the {caps.max_total_bytes}-byte cap")
        files.append((name, size))
    return files


def _copy_one(src_dir_fd: int, name: str, dst_dir_fd: int, dst_name: str,
              max_bytes: int, owner_uid: int | None,
              owner_gid: int | None) -> tuple[int, str]:
    """Copy one regular file ``name`` (under ``src_dir_fd``) to ``dst_name``
    (under ``dst_dir_fd``), returning ``(bytes_written, sha256_hex)``. BOTH ends
    are opened ``O_NOFOLLOW`` relative to their dir fd, so neither a payload-side
    symlink nor a destination-side symlink can redirect the read/write. The copy
    is bounded by ``max_bytes`` (the scanned size) so a file that grew after the
    scan is refused. Dest is created 0600, O_EXCL (the temp dir is fresh) and
    fchowned to the silo owner."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        sfd = os.open(name, flags, dir_fd=src_dir_fd)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise ExportPolicyError(
                f"payload entry {name!r} became a symlink — refusing") from e
        raise ExportStateError(f"cannot open payload entry {name!r}: {e}") from e
    try:
        st = os.fstat(sfd)
        if not stat.S_ISREG(st.st_mode):
            raise ExportPolicyError(
                f"payload entry {name!r} is not a regular file at open — refusing")
        dflags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                  | getattr(os, "O_CLOEXEC", 0))
        dfd = os.open(dst_name, dflags, 0o600, dir_fd=dst_dir_fd)
        try:
            _maybe_fchown(dfd, owner_uid, owner_gid)
            return _stream_to_fd(sfd, dfd, name, max_bytes)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)


def _stream_to_fd(sfd: int, dfd: int, name: str, max_bytes: int) -> tuple[int, str]:
    """Copy bytes from the open source fd ``sfd`` to the open destination fd
    ``dfd``, hashing as we go, and fsync ``dfd``. Returns
    ``(bytes_written, sha256_hex)``. The copy is bounded at ``max_bytes`` (the
    already-cap-checked scanned size): a file that grew after the scan trips
    :class:`ExportPolicyError` (nothing partial is kept by the caller). Both fds
    are caller-owned + already validated — this is the inner loop shared by the
    Incoming promoter (:func:`_copy_one`) and the edit-round-trip lander."""
    h = hashlib.sha256()
    written = 0
    while True:
        chunk = os.read(sfd, _CHUNK)
        if not chunk:
            break
        written += len(chunk)
        if written > max_bytes:
            raise ExportPolicyError(
                f"payload entry {name!r} grew past its scanned size "
                f"({max_bytes}-byte cap) during copy — refusing")
        h.update(chunk)
        off = 0
        while off < len(chunk):
            off += os.write(dfd, chunk[off:])
    os.fsync(dfd)
    return written, h.hexdigest()


# ---------------------------------------------------------------------------
# Promotion (all-or-nothing, atomic, openat-rooted at state_path)
# ---------------------------------------------------------------------------


def promote_export(payload_dir: str, state_path: str, *,
                   meta: dict, now_epoch: float,
                   owner_uid: int | None = None,
                   owner_gid: int | None = None,
                   caps: ExportCaps | None = None,
                   receipt_ctx: dict | None = None) -> dict:
    """Promote the staged payload at ``payload_dir`` into the requesting silo's
    home at ``state_path``, returning the lineage receipt dict.

    The destination is ``<state_path>/Incoming/<class-leaf>/<token8>-<ts>/`` with
    one file per validated artifact plus ``_receipt.json``.

    When ``receipt_ctx`` (``{"chain_head": str, "issuer": str}``) is supplied, a
    chain-anchored lineage RECEIPT surface is also emitted INTO the same temp dir
    (so it publishes atomically with the artifacts): a per-file sidecar
    ``<name>.qdistro-lineage.json`` and a batch ``qdistro-export-manifest.json``.
    The built envelopes are returned in the receipt dict as ``lineage_sidecars`` /
    ``lineage_manifest`` so the caller (session-manager) can SEAL them into its
    lineage store AFTER the durable rename. The surfaces alone are not authority —
    they verify only against a sealed store row.

    SECURITY: the importer runs as root, and the silo OWNER (a less-trusted user)
    can write under ``state_path``, so a pre-created symlink at ``Incoming`` or
    ``Incoming/<class-leaf>`` must NOT redirect the root writes outside the silo.
    Every destination component BELOW state_path is therefore created+opened
    ``O_NOFOLLOW`` RELATIVE to a fd chain rooted at ``state_path`` (a symlink in
    that chain raises ExportPolicyError), and the temp-dir creation, file copies,
    receipt write, and final rename all happen RELATIVE to those verified dir fds.

    ALL-OR-NOTHING: the payload is fully scanned first (any unsupported entry /
    cap breach raises before anything is written); files are copied into a TEMP
    dir, fsync'd, the receipt written, the dir fsync'd, and only then atomically
    renamed into place. ``meta`` is the launcher-written, already-validated
    metadata. When ``owner_uid`` is given and we run as root, the import dir +
    files are fchowned to the silo owner. An EMPTY payload is a clean zero-file
    receipt. Raises :class:`ExportPolicyError` (hostile/over-cap/symlinked
    destination — nothing promoted) or :class:`ExportStateError` (an I/O error)."""
    caps = caps or ExportCaps()
    token = str(meta.get("launch_token", ""))
    token8 = token[:8] if token else "00000000"
    open_class = str(meta.get("open_class", ""))
    class_leaf = sanitize_class_leaf(open_class)

    try:
        pdir_fd = os.open(
            payload_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        raise ExportStateError(
            f"cannot open payload dir {payload_dir!r}: {e}") from e

    sp_fd = inc_fd = class_fd = tmp_fd = -1
    tmpname: str | None = None
    placed = False
    real_class_dir = os.path.join(state_path, INCOMING_DIRNAME, class_leaf)
    try:
        pst = os.fstat(pdir_fd)
        if not stat.S_ISDIR(pst.st_mode):
            raise ExportStateError(f"payload path {payload_dir!r} is not a directory")
        # Full scan FIRST (all-or-nothing) — raises before any destination work.
        validated = _scan_payload(pdir_fd, caps)

        # state_path is admin/binding-controlled (trusted prefix); open it as the
        # root of the no-follow chain. The OWNER-writable components below it are
        # each created+opened O_NOFOLLOW so a pre-planted symlink is refused.
        try:
            sp_fd = os.open(state_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_CLOEXEC", 0))
        except OSError as e:
            raise ExportStateError(
                f"cannot open state_path {state_path!r}: {e}") from e
        inc_fd = _child_dir_at(sp_fd, INCOMING_DIRNAME, owner_uid, owner_gid)
        class_fd = _child_dir_at(inc_fd, class_leaf, owner_uid, owner_gid)

        ts = _format_ts(now_epoch)
        final_name = f"{token8}-{ts}"
        tmpname = _mkdir_temp_at(class_fd, f".import-{token8}-")
        try:
            tmp_fd = os.open(tmpname, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                             dir_fd=class_fd)
        except OSError as e:
            raise ExportStateError(f"cannot open temp import dir: {e}") from e
        _maybe_fchown(tmp_fd, owner_uid, owner_gid)

        receipt_files = []
        for name, scanned_size in validated:
            # Bound the copy at the SCANNED size: a file that grew after the scan
            # is refused, which also keeps the total within the scanned (already
            # cap-checked) budget. Container is gone — defence in depth.
            written, digest = _copy_one(
                pdir_fd, name, tmp_fd, name, scanned_size,
                owner_uid, owner_gid)
            receipt_files.append(
                {"name": name, "size": written, "sha256": digest})

        receipt = {
            "version": RECEIPT_VERSION,
            "launch_token": token,
            "container": meta.get("container"),
            "open_class": open_class,
            "request_silo": meta.get("request_silo"),
            "source_input": meta.get("input_basename"),
            "exported_at": int(now_epoch),
            "files": receipt_files,
        }
        _write_receipt_at(tmp_fd, receipt, owner_uid, owner_gid)
        if receipt_ctx is not None:
            # Lineage receipts are ADDITIVE provenance: their emission (incl. the
            # lazy receipt-library import, envelope build, and surface writes) must
            # NEVER abort an otherwise-complete export — the artifacts already sit
            # in the temp dir. Any failure degrades to "no receipts" (the caller
            # then seals nothing; any partial surface published is simply
            # unverifiable, the fail-closed direction). This is what guarantees the
            # invariant "lineage unavailability never blocks import" even though the
            # receipt library is imported here, not in the caller's probe.
            try:
                sidecars, manifest = _emit_lineage_surfaces(
                    tmp_fd, receipt_files, token, now_epoch, receipt_ctx,
                    owner_uid, owner_gid)
                receipt["lineage_sidecars"] = sidecars
                receipt["lineage_manifest"] = manifest
            except Exception as e:  # noqa: BLE001 - never fail export on lineage
                logging.getLogger(__name__).warning(
                    "export-back: lineage receipt emission failed "
                    "(artifacts land; no receipts): %s", e)
                receipt.pop("lineage_sidecars", None)
                receipt.pop("lineage_manifest", None)
        os.fsync(tmp_fd)

        final_name = _place_at(class_fd, tmpname, final_name)
        os.fsync(class_fd)
        placed = True
    finally:
        # On ANY failure before the rename completed, remove the temp import dir
        # FD-RELATIVE (via the still-open class_fd/tmp_fd) — never by rebuilding a
        # path through the owner-writable Incoming/<class-leaf> components, which a
        # symlink race could redirect (codex SHIP-r2 blocker). Cleanup MUST run
        # before the fds are closed, hence in this same finally, ordered first.
        if not placed and tmpname is not None:
            _cleanup_temp_at(class_fd, tmp_fd, tmpname)
        for fd in (tmp_fd, class_fd, inc_fd, sp_fd, pdir_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

    receipt["dest"] = os.path.join(real_class_dir, final_name)
    return receipt


def _lineage_eid(token: str, name: str) -> str:
    """Stable entity id for an exported artifact: the FULL launch token (unique
    per disposable launch — token8 could collide) + the relative artifact name.
    Path-independent, so the post-copy ``_place_at`` rename (which may change the
    landing dir) never invalidates it."""
    return f"disp-export:{token}:{name}"


def _emit_lineage_surfaces(tmp_fd: int, receipt_files: list,
                           token: str, now_epoch: float, receipt_ctx: dict,
                           owner_uid: int | None, owner_gid: int | None):
    """Build + write the chain-anchored receipt surfaces into the (unpublished)
    temp import dir, so they land atomically with the artifacts. Returns
    ``(sidecar_envelopes, manifest)`` for the caller to seal post-rename. The
    locator is the RELATIVE artifact name (the sidecar lives beside it); the
    entity id uses the full token. Lazily imports the receipt library so a runtime
    missing it still does core export (without receipts)."""
    import qdistro_lineage_receipts as lr

    chain_head = str(receipt_ctx["chain_head"])
    issuer = str(receipt_ctx.get("issuer", "qdistro-session-manager"))
    created_at = int(now_epoch)
    sidecars = []
    manifest_children = []
    for fr in receipt_files:
        name = fr["name"]
        eid = _lineage_eid(token, name)
        digest = fr["sha256"]
        sidecar = lr.build_envelope(
            entity=eid, kind="sidecar", chain_head=chain_head,
            created_at=created_at, artifact_digest=digest, locator=name,
            issuer=issuer)
        lr.write_sidecar(name, sidecar, dir_fd=tmp_fd,
                         owner_uid=owner_uid, owner_gid=owner_gid)
        sidecars.append(sidecar)
        manifest_children.append(lr.build_envelope(
            entity=eid, kind="export-manifest", chain_head=chain_head,
            created_at=created_at, artifact_digest=digest, locator=name,
            issuer=issuer))
    manifest = lr.build_export_manifest(
        manifest_children, chain_head=chain_head, created_at=created_at,
        issuer=issuer)
    lr.write_export_manifest("", manifest, dir_fd=tmp_fd,
                             owner_uid=owner_uid, owner_gid=owner_gid)
    return sidecars, manifest


def _cleanup_temp_at(class_fd: int, tmp_fd: int, tmpname: str) -> None:
    """Remove a not-yet-placed temp import dir using ONLY fd-relative operations
    (unlink each entry via ``tmp_fd``, then ``rmdir`` ``tmpname`` via
    ``class_fd``). No path is re-resolved through the owner-writable
    Incoming/<class-leaf> components, so a symlink swapped onto them during
    cleanup cannot redirect the removal. Best-effort: the temp dir contains only
    regular files we created (+ the receipt), no subdirs."""
    if tmp_fd >= 0:
        try:
            for nm in os.listdir(tmp_fd):
                try:
                    os.unlink(nm, dir_fd=tmp_fd)
                except OSError:
                    pass
        except OSError:
            pass
    try:
        os.rmdir(tmpname, dir_fd=class_fd)
    except OSError:
        pass


def _child_dir_at(parent_fd: int, name: str, uid: int | None,
                  gid: int | None) -> int:
    """mkdir ``name`` under ``parent_fd`` if absent, then open it
    ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW`` relative to ``parent_fd`` and return the
    fd. A symlink in that slot (e.g. an owner-planted ``Incoming`` -> elsewhere)
    raises :class:`ExportPolicyError` (the open fails ELOOP) — this is what keeps
    the root importer inside ``state_path``. A freshly-created dir is fchowned to
    the silo owner so the owner can traverse it."""
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    except OSError as e:
        raise ExportStateError(f"cannot create destination dir {name!r}: {e}") from e
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ExportPolicyError(
                f"destination component {name!r} is a symlink/non-dir — refusing "
                f"(a silo-planted symlink must not redirect the root importer)") from e
        raise ExportStateError(f"cannot open destination dir {name!r}: {e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise ExportStateError(f"destination component {name!r} is not a directory")
        if created:
            _maybe_fchown(fd, uid, gid)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _mkdir_temp_at(parent_fd: int, prefix: str) -> str:
    """Create a fresh 0700 temp dir under ``parent_fd`` (openat analog of
    tempfile.mkdtemp), returning its name. Retries on the (astronomically rare)
    name collision."""
    for _ in range(100):
        name = prefix + os.urandom(6).hex()
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            return name
        except FileExistsError:
            continue
        except OSError as e:
            raise ExportStateError(f"cannot create temp import dir: {e}") from e
    raise ExportStateError("could not create a unique temp import dir")


def _place_at(class_fd: int, tmpname: str, final_base: str) -> str:
    """Rename ``tmpname`` -> ``final_base`` (both relative to ``class_fd``, the
    verified class dir), appending a numeric suffix on the rare collision. Never
    overwrites an existing import (a free name is found first). Returns the placed
    name."""
    target = final_base
    n = 1
    while True:
        try:
            os.lstat(target, dir_fd=class_fd)
        except FileNotFoundError:
            break
        target = f"{final_base}-{n}"
        n += 1
        if n > 1000:
            raise ExportStateError("too many landing-dir collisions")
    try:
        os.rename(tmpname, target, src_dir_fd=class_fd, dst_dir_fd=class_fd)
    except OSError as e:
        raise ExportStateError(f"cannot place import dir: {e}") from e
    return target


def _format_ts(now_epoch: float) -> str:
    """YYYYMMDD-HHMMSS in UTC from an injected epoch (deterministic for tests)."""
    import time
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now_epoch))


def _maybe_fchown(fd: int, uid: int | None, gid: int | None) -> None:
    """fchown an open fd to the requesting silo's owner when we run as root and an
    owner was resolved. A no-op otherwise (unit tests run unprivileged, pass
    None). Race-free (operates on the fd we just created, never a path)."""
    if uid is None or os.geteuid() != 0:
        return
    g = gid if gid is not None else -1
    try:
        os.fchown(fd, uid, g)
    except OSError as e:
        # Best-effort: a chown failure must not abandon an otherwise-complete
        # import. Log it — a persistent failure leaves root-owned files the silo
        # user may not be able to read/remove.
        import logging
        logging.getLogger(__name__).warning(
            "export-back: fchown(fd -> uid=%s) failed: %s", uid, e)


def _write_receipt_at(dir_fd: int, receipt: dict,
                      uid: int | None, gid: int | None) -> None:
    data = json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8")
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
             | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(RECEIPT_NAME, flags, 0o600, dir_fd=dir_fd)
    except OSError as e:
        raise ExportStateError(f"cannot write receipt: {e}") from e
    try:
        _maybe_fchown(fd, uid, gid)
        off = 0
        while off < len(data):
            off += os.write(fd, data[off:])
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Edit-round-trip lander (promote a single edited file BESIDE its source)
# ---------------------------------------------------------------------------


def split_source_rel(source_rel: str) -> tuple[list[str], str]:
    """Split a source path RELATIVE to the silo state_path into
    ``(dir_components, leaf)`` after a strict hygiene pass, or raise
    :class:`ExportPolicyError`. ``source_rel`` is the source-of-edit's location
    inside the requesting silo (e.g. ``docs/report.txt``). It MUST be a real
    relative path with no escape: not absolute, no empty component, no ``.``/``..``
    component, no NUL/control byte; the leaf must additionally pass
    :func:`sanitize_filename` (so it can never be the reserved receipt name or a
    separator-bearing string). This is the pure half of the "stay inside the
    silo" guarantee; the walk in :func:`promote_edit` then enforces it again at
    the filesystem level with an ``O_NOFOLLOW`` fd-chain."""
    if not isinstance(source_rel, str) or not source_rel:
        raise ExportPolicyError("edit source path is empty")
    if source_rel.startswith("/"):
        raise ExportPolicyError(
            f"edit source path {source_rel!r} is absolute — refusing "
            f"(it must be relative to the silo state)")
    if "\x00" in source_rel:
        raise ExportPolicyError("edit source path contains a NUL byte")
    parts = source_rel.split("/")
    comps: list[str] = []
    for p in parts:
        if p == "" or p == "." or p == "..":
            raise ExportPolicyError(
                f"edit source path {source_rel!r} has an empty/'.'/'..' "
                f"component — refusing")
        comps.append(p)
    leaf = comps[-1]
    dir_components = comps[:-1]
    if sanitize_filename(leaf) is None:
        raise ExportPolicyError(
            f"edit source leaf {leaf!r} is unsafe/reserved — refusing")
    return dir_components, leaf


def _open_existing_dir_at(parent_fd: int, name: str) -> int:
    """Open an EXISTING child directory ``name`` under ``parent_fd``
    ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW`` and return its fd. Unlike
    :func:`_child_dir_at` this NEVER creates: the source's parent chain must
    already exist (the source itself lives there), and creating a component would
    be a sign the tree changed under us. A symlink/non-dir in the slot
    (owner-planted) raises :class:`ExportPolicyError` (ELOOP/ENOTDIR); a missing
    component raises :class:`ExportStateError` (the recorded source path no longer
    resolves)."""
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                     dir_fd=parent_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ExportPolicyError(
                f"edit source component {name!r} is a symlink/non-dir — refusing "
                f"(a silo-planted symlink must not redirect the root lander)") from e
        if e.errno == errno.ENOENT:
            raise ExportStateError(
                f"edit source parent component {name!r} no longer exists") from e
        raise ExportStateError(
            f"cannot open edit source component {name!r}: {e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise ExportStateError(
                f"edit source component {name!r} is not a directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_writable_temp(parent_fd: int) -> tuple[int, str | None]:
    """Create a fresh writable temp file in the directory referred to by
    ``parent_fd`` and return ``(fd, tmpname_or_None)``.

    PREFERRED: ``O_TMPFILE`` — an UNNAMED inode that has no directory entry until
    it is atomically linked into place (:func:`_link_into_place`). Because it
    never has a name, the owner of the (less-trusted) silo directory can never
    see, open, swap, or race the temp before it is finalized. ``tmpname`` is
    ``None`` in this path.

    FALLBACK (filesystems without ``O_TMPFILE`` support — EOPNOTSUPP/EISDIR/
    ENOTSUP/EINVAL): a named ``O_CREAT|O_EXCL|O_NOFOLLOW`` temp whose name starts
    with a dot. We still finalize by linking the temp's *fd* (via ``/proc/self/fd``
    — see :func:`_link_into_place`), never by ``rename`` (which would clobber an
    existing target), and unlink the temp name afterwards; an owner who swaps the
    temp NAME cannot affect the inode we already hold open. ``tmpname`` is the
    name to unlink on cleanup/finalize."""
    o_tmpfile = getattr(os, "O_TMPFILE", 0)
    if o_tmpfile:
        try:
            fd = os.open(".", os.O_WRONLY | o_tmpfile
                         | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=parent_fd)
            return fd, None
        except OSError as e:
            if e.errno not in (errno.EOPNOTSUPP, errno.EISDIR,
                               getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
                               errno.EINVAL):
                raise ExportStateError(
                    f"cannot create O_TMPFILE for edit landing: {e}") from e
            # fall through to the named-temp fallback
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
             | getattr(os, "O_CLOEXEC", 0))
    for _ in range(100):
        nm = f".disp-edited.tmp.{os.urandom(8).hex()}"
        try:
            fd = os.open(nm, flags, 0o600, dir_fd=parent_fd)
            return fd, nm
        except FileExistsError:
            continue
        except OSError as e:
            raise ExportStateError(
                f"cannot create temp file for edit landing: {e}") from e
    raise ExportStateError("could not create a unique edit temp file")


def _link_into_place(parent_fd: int, tmp_fd: int, base: str) -> str:
    """Link the open temp inode ``tmp_fd`` into ``parent_fd`` under ``base``
    (``<source-leaf>.disp-edited``), appending ``-<n>`` on a collision, and return
    the placed name.

    NO-OVERWRITE is enforced by ``linkat`` itself: linking onto an existing name
    fails ``EEXIST`` (there is NO atomic-replace, unlike ``rename``), so the
    source — or a prior edited copy — is never clobbered, with no TOCTOU window
    between a check and a create. The inode is reached by its ``/proc/self/fd``
    symlink with ``follow_symlinks=True`` (the AT_EMPTY_PATH analog usable from
    Python): this works both for an ``O_TMPFILE`` (nameless) inode and a named
    fallback temp, so finalization is a single fd-based path."""
    proc_src = f"/proc/self/fd/{tmp_fd}"
    target = base
    n = 1
    while True:
        try:
            os.link(proc_src, target, dst_dir_fd=parent_fd, follow_symlinks=True)
            return target
        except FileExistsError:
            target = f"{base}-{n}"
            n += 1
            if n > 1000:
                raise ExportStateError(
                    f"too many {base!r} edit-landing collisions") from None
        except OSError as e:
            raise ExportStateError(
                f"cannot link edited file into place: {e}") from e


def promote_edit(payload_dir: str, state_path: str, *, source_rel: str,
                 meta: dict, now_epoch: float,
                 owner_uid: int | None = None,
                 owner_gid: int | None = None,
                 caps: ExportCaps | None = None) -> dict:
    """Promote the SINGLE edited artifact at ``payload_dir`` BESIDE its source —
    the edit-round-trip landing (the export-back follow-on). Returns the lineage
    receipt dict (``mode == "edit"``).

    ``source_rel`` is the source-of-edit's path RELATIVE to ``state_path`` (the
    requesting silo's home); the caller (the session manager) has already required
    it to resolve strictly under ``state_path``. The edited file lands at
    ``<state_path>/<dirname(source_rel)>/<leaf><EDITED_SUFFIX>`` (``-<n>`` suffix
    on collision), NEVER overwriting the source or any existing file.

    Defensive, mirroring :func:`promote_export`:
    - ``source_rel`` is hygiene-checked (:func:`split_source_rel`) and the source
      parent is reached by an ``O_NOFOLLOW`` fd-walk rooted at ``state_path`` — a
      symlink anywhere in the chain (the dirs are owner-writable) refuses, so the
      root lander can never be redirected outside the silo.
    - the source leaf must still exist as a REGULAR FILE beside which we land (we
      are returning an *edit of it*); a missing/symlinked/non-regular source
      refuses.
    - the payload is treated as HOSTILE: it is fully scanned (:func:`_scan_payload`,
      regular-files-only / caps / O_NOFOLLOW). EXACTLY ONE file is expected —
      ZERO is a clean no-op ("nothing was edited"), MORE THAN ONE is an
      :class:`ExportPolicyError` (edit-round-trip is single-file).
    - finalization is fully fd-based: the edited bytes are written into a temp
      inode (``O_TMPFILE`` preferred — never named) and ``linkat``'d into place
      no-overwrite. No ``rename`` (which would clobber), no named temp the owner
      can race (in the preferred path).

    The receipt is RETURNED, not scattered beside the source (an edit lands in the
    user's own working directory; a ``_receipt.json`` sibling would be litter).
    Raises :class:`ExportPolicyError` (hostile/over-cap/symlinked path — nothing
    landed) or :class:`ExportStateError` (an I/O error / vanished source)."""
    caps = caps or ExportCaps()
    token = str(meta.get("launch_token", ""))
    open_class = str(meta.get("open_class", ""))
    dir_components, leaf = split_source_rel(source_rel)

    try:
        pdir_fd = os.open(
            payload_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        raise ExportStateError(
            f"cannot open payload dir {payload_dir!r}: {e}") from e

    sp_fd = -1
    walked: list[int] = []
    parent_fd = -1
    tmp_fd = -1
    tmpname: str | None = None
    linked = False
    landed: str | None = None
    try:
        pst = os.fstat(pdir_fd)
        if not stat.S_ISDIR(pst.st_mode):
            raise ExportStateError(
                f"payload path {payload_dir!r} is not a directory")
        # Full scan FIRST (all-or-nothing). Edit is single-file.
        validated = _scan_payload(pdir_fd, caps)
        if len(validated) == 0:
            # Nothing was edited/saved — a clean no-op (not an error).
            return {"version": RECEIPT_VERSION, "mode": "edit",
                    "launch_token": token, "open_class": open_class,
                    "request_silo": meta.get("request_silo"),
                    "source": source_rel, "files": [], "dest": None}
        if len(validated) > 1:
            raise ExportPolicyError(
                f"edit-round-trip expects a single edited file but the payload "
                f"has {len(validated)} — refusing (ambiguous)")
        payload_name, scanned_size = validated[0]

        # Walk to the source's parent dir, O_NOFOLLOW each component, rooted at
        # the trusted state_path. state_path is binding-resolved (trusted prefix).
        try:
            sp_fd = os.open(state_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_CLOEXEC", 0))
        except OSError as e:
            raise ExportStateError(
                f"cannot open state_path {state_path!r}: {e}") from e
        cur = sp_fd
        for comp in dir_components:
            nxt = _open_existing_dir_at(cur, comp)
            walked.append(nxt)
            cur = nxt
        parent_fd = cur

        # The source must still exist as a regular file beside which we land.
        try:
            sst = os.lstat(leaf, dir_fd=parent_fd)
        except FileNotFoundError as e:
            raise ExportStateError(
                f"edit source {source_rel!r} no longer exists — refusing") from e
        except OSError as e:
            raise ExportStateError(
                f"cannot stat edit source {source_rel!r}: {e}") from e
        if stat.S_ISLNK(sst.st_mode):
            raise ExportPolicyError(
                f"edit source {source_rel!r} is a symlink — refusing")
        if not stat.S_ISREG(sst.st_mode):
            raise ExportPolicyError(
                f"edit source {source_rel!r} is not a regular file — refusing")

        # Write the edited bytes into a temp inode, then link no-overwrite.
        tmp_fd, tmpname = _open_writable_temp(parent_fd)
        _maybe_fchown(tmp_fd, owner_uid, owner_gid)
        sfd = os.open(payload_name, os.O_RDONLY | os.O_NOFOLLOW
                      | getattr(os, "O_CLOEXEC", 0), dir_fd=pdir_fd)
        try:
            sfd_st = os.fstat(sfd)
            if not stat.S_ISREG(sfd_st.st_mode):
                raise ExportPolicyError(
                    f"payload entry {payload_name!r} is not a regular file at "
                    f"open — refusing")
            written, digest = _stream_to_fd(
                sfd, tmp_fd, payload_name, scanned_size)
        finally:
            os.close(sfd)

        landed = _link_into_place(parent_fd, tmp_fd, f"{leaf}{EDITED_SUFFIX}")
        linked = True
        os.fsync(parent_fd)
    finally:
        # If we created a NAMED fallback temp, remove its directory entry. When we
        # linked successfully the inode survives via the new (landed) link; when
        # we failed before/at link the temp must not be left behind. O_TMPFILE
        # (tmpname is None) needs no unlink. Done fd-relative to the source parent.
        if tmpname is not None and parent_fd >= 0:
            try:
                os.unlink(tmpname, dir_fd=parent_fd)
            except OSError:
                pass
        if tmp_fd >= 0:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        # Close the walked child dir fds (parent_fd is the last of them, or is
        # sp_fd when there were no dir components) + the payload dir fd. sp_fd is
        # never in `walked` (only child fds are appended), so it is closed exactly
        # once in its own branch below — no double close.
        for fd in (*reversed(walked), pdir_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if sp_fd >= 0:
            try:
                os.close(sp_fd)
            except OSError:
                pass

    dest = os.path.join(state_path, *dir_components, landed) if linked else None
    return {
        "version": RECEIPT_VERSION,
        "mode": "edit",
        "launch_token": token,
        "container": meta.get("container"),
        "open_class": open_class,
        "request_silo": meta.get("request_silo"),
        "source": source_rel,
        "exported_at": int(now_epoch),
        "files": [{"name": landed, "size": written, "sha256": digest}],
        "dest": dest,
    }
