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
import os
import stat
from dataclasses import dataclass

# The importer-owned receipt filename. Reserved: a payload file whose sanitized
# leaf collides with this is refused (the whole import fails) so a hostile
# disposable can never overwrite / forge the provenance record.
RECEIPT_NAME = "_receipt.json"
RECEIPT_VERSION = 1

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
        h = hashlib.sha256()
        written = 0
        dflags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                  | getattr(os, "O_CLOEXEC", 0))
        dfd = os.open(dst_name, dflags, 0o600, dir_fd=dst_dir_fd)
        try:
            _maybe_fchown(dfd, owner_uid, owner_gid)
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
        finally:
            os.close(dfd)
        return written, h.hexdigest()
    finally:
        os.close(sfd)


# ---------------------------------------------------------------------------
# Promotion (all-or-nothing, atomic, openat-rooted at state_path)
# ---------------------------------------------------------------------------


def promote_export(payload_dir: str, state_path: str, *,
                   meta: dict, now_epoch: float,
                   owner_uid: int | None = None,
                   owner_gid: int | None = None,
                   caps: ExportCaps | None = None) -> dict:
    """Promote the staged payload at ``payload_dir`` into the requesting silo's
    home at ``state_path``, returning the lineage receipt dict.

    The destination is ``<state_path>/Incoming/<class-leaf>/<token8>-<ts>/`` with
    one file per validated artifact plus ``_receipt.json``.

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
