"""Atomic snapshot writer. Stdlib only; no Qt, dbus, or HOME at import."""

from __future__ import annotations

import os
import stat
import time
import uuid
from dataclasses import dataclass

from .model import PresentationSnapshot, SnapshotError, SnapshotPathError, with_generation
from .paths import (
    _O_NOFOLLOW,
    _O_NONBLOCK,
    MANAGED_DIR,
    _close_quietly,
    load_deployment_meta,
    walk_open,
)

_TEMP_PREFIX = ".qdistro-presentation-"
_FILE_MODE = 0o644
_DIR_MODE = 0o755
_SELINUX_XATTR = b"security.selinux"


@dataclass(frozen=True)
class WriteResult:
    path: str
    generation: str
    wrote: bool
    reason: str


def _selinux_context(dir_fd: int) -> bytes | None:
    try:
        return os.getxattr(f"/proc/self/fd/{dir_fd}", _SELINUX_XATTR)
    except OSError:
        return None


def _apply_selinux(dir_fd: int, name: str, context: bytes | None) -> None:
    if context is None:
        return
    fd = -1
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW, dir_fd=dir_fd)
        os.setxattr(f"/proc/self/fd/{fd}", _SELINUX_XATTR, context)
    except OSError:
        pass
    finally:
        if fd >= 0:
            _close_quietly(fd)


def _fsync_dir(dir_fd: int) -> None:
    os.fsync(dir_fd)


def _cleanup_own_temps(dir_fd: int, keep: str | None = None) -> None:
    try:
        names = os.listdir(dir_fd)
    except OSError:
        return
    for name in names:
        if not name.startswith(_TEMP_PREFIX):
            continue
        if keep is not None and name == keep:
            continue
        try:
            os.unlink(name, dir_fd=dir_fd)
        except OSError:
            continue


def resolve_write_owner_uid(directory: str, owner_uid: int | None) -> int | None:
    """Owner for a write. Managed dest uses trusted metadata; standalone keeps ``owner_uid``."""
    if os.path.abspath(directory) != os.path.abspath(MANAGED_DIR):
        return owner_uid
    meta = load_deployment_meta()
    if meta is None:
        raise SnapshotPathError("trusted deployment metadata required for managed publication")
    if owner_uid is not None and owner_uid != meta.admin_uid:
        raise SnapshotPathError("owner-uid does not match trusted deployment metadata")
    return meta.admin_uid


def write_snapshot(
    directory: str,
    snapshot: PresentationSnapshot,
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
    file_mode: int = _FILE_MODE,
    filename: str = "current.json",
    skip_unchanged: bool = True,
    require_unwritable_dirs: bool = True,
) -> WriteResult:
    """Validate, hash, and atomically replace ``directory/filename``.

    Never deletes an existing valid snapshot on failure. Temp files are
    created exclusive inside ``directory`` and replaced with ``os.rename``.
    """
    owner_uid = resolve_write_owner_uid(directory, owner_uid)
    snapshot = with_generation(snapshot)
    from .model import parse_snapshot

    snapshot = parse_snapshot(snapshot.to_dict())
    payload = snapshot.to_json().encode("utf-8")
    if len(payload) > 64 * 1024:
        raise SnapshotError("serialized snapshot exceeds 64 KiB")

    dir_fd = walk_open(
        directory,
        leaf_directory=True,
        leaf_uid=owner_uid,
        require_unwritable_dirs=require_unwritable_dirs,
    )
    try:
        info = os.fstat(dir_fd)
        if not stat.S_ISDIR(info.st_mode):
            raise SnapshotPathError("destination is not a directory")
        if owner_uid is not None and info.st_uid != owner_uid:
            raise SnapshotPathError("destination directory owner mismatch")

        if skip_unchanged:
            try:
                existing_fd = os.open(
                    filename,
                    os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK,
                    dir_fd=dir_fd,
                )
            except OSError:
                existing_fd = -1
            if existing_fd >= 0:
                try:
                    from .model import parse_snapshot_bytes
                    from .paths import read_regular_fd

                    existing = parse_snapshot_bytes(read_regular_fd(existing_fd))
                    if existing.generation == snapshot.generation:
                        existing_info = os.fstat(existing_fd)
                        if owner_uid is None or (
                            stat.S_ISREG(existing_info.st_mode)
                            and existing_info.st_uid == owner_uid
                        ):
                            return WriteResult(
                                path=os.path.join(directory, filename),
                                generation=snapshot.generation,
                                wrote=False,
                                reason="unchanged",
                            )
                except (SnapshotError, SnapshotPathError, OSError):
                    pass
                finally:
                    _close_quietly(existing_fd)

        selinux = _selinux_context(dir_fd)
        temp_name = f"{_TEMP_PREFIX}{os.getpid()}-{time.time_ns()}-{uuid.uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
        temp_fd = os.open(temp_name, flags, file_mode, dir_fd=dir_fd)
        replaced = False
        try:
            os.fchmod(temp_fd, file_mode)
            if owner_uid is not None:
                gid = owner_gid if owner_gid is not None else -1
                try:
                    os.fchown(temp_fd, owner_uid, gid)
                except OSError as exc:
                    _close_quietly(temp_fd)
                    try:
                        os.unlink(temp_name, dir_fd=dir_fd)
                    except OSError:
                        pass
                    raise SnapshotPathError(f"cannot chown snapshot: {exc}") from exc
            written = 0
            view = memoryview(payload)
            while written < len(payload):
                n = os.write(temp_fd, view[written:])
                if n == 0:
                    raise SnapshotError("short write")
                written += n
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            _apply_selinux(dir_fd, temp_name, selinux)
            os.rename(temp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            replaced = True
            _apply_selinux(dir_fd, filename, selinux)
            _fsync_dir(dir_fd)
            _cleanup_own_temps(dir_fd)
        except Exception:
            if temp_fd >= 0:
                _close_quietly(temp_fd)
            if not replaced:
                try:
                    os.unlink(temp_name, dir_fd=dir_fd)
                except OSError:
                    pass
            raise
        return WriteResult(
            path=os.path.join(directory, filename),
            generation=snapshot.generation,
            wrote=True,
            reason="replaced",
        )
    finally:
        _close_quietly(dir_fd)


def write_disabled_envelope(
    directory: str,
    template: PresentationSnapshot,
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
    require_unwritable_dirs: bool = True,
) -> WriteResult:
    """Write a valid enabled=false envelope for the troubleshooting reset."""
    disabled = PresentationSnapshot(
        version=template.version,
        enabled=False,
        generation="00000000-0000-0000-0000-000000000000",
        mode=template.mode,
        colors=template.colors,
        fonts=template.fonts,
        metrics=template.metrics,
        motion=template.motion,
        tooltips_enabled=template.tooltips_enabled,
        icon_theme=template.icon_theme,
    )
    return write_snapshot(
        directory,
        disabled,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        require_unwritable_dirs=require_unwritable_dirs,
    )
