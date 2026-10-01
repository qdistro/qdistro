"""Snapshot path resolution and descriptor-relative trusted reads.

Importing this module does not read HOME, construct a Qt application, or
start a thread. Path lookups that need the user environment happen at call
time.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .model import (
    MAX_BYTES,
    PresentationSnapshot,
    SnapshotError,
    SnapshotPathError,
    parse_snapshot_bytes,
)

MANAGED_DIR = "/var/lib/qdistro/presentation"
MANAGED_FILE = MANAGED_DIR + "/current.json"
DEPLOYMENT_META = "/usr/share/qdistro/presentation/deployment.json"
ENV_OVERRIDE = "QDISTRO_PRESENTATION_FILE"

Role = Literal["ordinary", "polkit", "locker"]

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | _O_NOFOLLOW
_O_FILE = os.O_RDONLY | _O_NOFOLLOW


def _group_or_other_writable(mode: int) -> bool:
    return bool(mode & (stat.S_IWGRP | stat.S_IWOTH))


def _unsafe_dir_mode(mode: int, *, is_leaf: bool) -> bool:
    if not _group_or_other_writable(mode):
        return False
    # Sticky other-writable ancestors such as /tmp are valid parents of
    # developer fixtures. The leaf presentation directory must still be
    # private.
    if not is_leaf and (mode & stat.S_ISVTX) and (mode & stat.S_IWOTH):
        return False
    return True


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_component(dir_fd: int, name: str, flags: int) -> int:
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise SnapshotPathError(f"cannot open {name!r}: {exc}") from exc


def walk_open(
    path: str,
    *,
    leaf_directory: bool = False,
    ancestor_uids: dict[int, int] | None = None,
    leaf_uid: int | None = None,
    require_unwritable_dirs: bool = True,
) -> int:
    """Open ``path`` with O_NOFOLLOW at every component. Returns the leaf fd.

    ``ancestor_uids`` maps 0-based component index (after the root) to the
    required owner of that directory. ``leaf_uid`` is the required owner of
    the final component.
    """
    if not path.startswith("/") or not path:
        raise SnapshotPathError("path must be absolute")
    parts = [part for part in path.split("/") if part]
    if ".." in path.split("/") or "." in parts:
        raise SnapshotPathError("path must not contain . or ..")
    if not parts:
        raise SnapshotPathError("path must not be /")
    dir_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, name in enumerate(parts):
            is_leaf = index == len(parts) - 1
            flags = _O_DIRECTORY if (not is_leaf or leaf_directory) else _O_FILE
            new_fd = _open_component(dir_fd, name, flags)
            try:
                info = os.fstat(new_fd)
            except OSError:
                _close_quietly(new_fd)
                raise
            if is_leaf and not leaf_directory:
                if not stat.S_ISREG(info.st_mode):
                    _close_quietly(new_fd)
                    raise SnapshotPathError("leaf is not a regular file")
                if leaf_uid is not None and info.st_uid != leaf_uid:
                    _close_quietly(new_fd)
                    raise SnapshotPathError("leaf owner mismatch")
            else:
                if not stat.S_ISDIR(info.st_mode):
                    _close_quietly(new_fd)
                    raise SnapshotPathError("expected a directory")
                # The directory that contains a file leaf is the protected
                # presentation directory even though it is not the walk's last
                # component. Sticky /tmp may only be a non-protected ancestor.
                protect_as_leaf = is_leaf or (not leaf_directory and index == len(parts) - 2)
                if require_unwritable_dirs and _unsafe_dir_mode(
                    info.st_mode, is_leaf=protect_as_leaf
                ):
                    _close_quietly(new_fd)
                    raise SnapshotPathError("directory is group/other-writable")
                if ancestor_uids is not None and index in ancestor_uids:
                    if info.st_uid != ancestor_uids[index]:
                        _close_quietly(new_fd)
                        raise SnapshotPathError("directory owner mismatch")
                if protect_as_leaf and leaf_uid is not None and info.st_uid != leaf_uid:
                    _close_quietly(new_fd)
                    raise SnapshotPathError("directory owner mismatch")
            _close_quietly(dir_fd)
            dir_fd = new_fd
        return dir_fd
    except Exception:
        _close_quietly(dir_fd)
        raise


def read_regular_fd(fd: int, *, max_bytes: int = MAX_BYTES) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotPathError("not a regular file")
    if info.st_size > max_bytes:
        raise SnapshotError("document exceeds 64 KiB")
    data = os.read(fd, max_bytes + 1)
    if len(data) > max_bytes:
        raise SnapshotError("document exceeds 64 KiB")
    return data


def file_identity(fd: int) -> tuple[int, int, int, int]:
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


@dataclass(frozen=True)
class DeploymentMeta:
    version: int
    admin_uid: int


def load_deployment_meta(path: str = DEPLOYMENT_META) -> DeploymentMeta | None:
    """Return install metadata, or None if the managed source is unavailable."""
    fd = -1
    try:
        fd = walk_open(
            path,
            ancestor_uids={0: 0, 1: 0, 2: 0, 3: 0},  # usr, share, qdistro, presentation
            leaf_uid=0,
        )
        info = os.fstat(fd)
        if _group_or_other_writable(stat.S_IMODE(info.st_mode)):
            return None
        data = read_regular_fd(fd, max_bytes=4096)
        from .model import loads_strict

        obj = loads_strict(data.decode("utf-8"))
    except (OSError, SnapshotPathError, SnapshotError, RecursionError, UnicodeDecodeError):
        return None
    finally:
        if fd >= 0:
            _close_quietly(fd)
    if not isinstance(obj, dict):
        return None
    version = obj.get("version")
    admin_uid = obj.get("admin_uid")
    if version != 1 or not isinstance(admin_uid, int) or isinstance(admin_uid, bool):
        return None
    if admin_uid < 0:
        return None
    return DeploymentMeta(version=1, admin_uid=admin_uid)


def managed_dir_exists() -> bool:
    return os.path.isdir(MANAGED_DIR)


def developer_state_file(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_STATE_HOME")
    if xdg:
        root = xdg
    else:
        home = env.get("HOME")
        if not home:
            raise SnapshotPathError("HOME is unset")
        root = os.path.join(home, ".local", "state")
    return os.path.join(root, "qdistro", "presentation", "current.json")


def explicit_override_path(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    raw = env.get(ENV_OVERRIDE)
    if raw is None or raw == "":
        return None
    if not raw.startswith("/") or ".." in raw.split("/"):
        raise SnapshotPathError("malformed QDISTRO_PRESENTATION_FILE")
    return raw


@dataclass(frozen=True)
class ResolvedPath:
    path: str
    kind: Literal["managed", "state", "override"]
    expected_uid: int | None
    watch: bool


def resolve_snapshot_path(
    *,
    role: Role = "ordinary",
    environ: Mapping[str, str] | None = None,
    managed_dir: str = MANAGED_DIR,
) -> ResolvedPath | None:
    """Choose the snapshot file for this process.

    polkit/locker ignore developer overrides and use only the managed path.
    Unavailable data returns None (caller uses native fallback).
    """
    env = os.environ if environ is None else environ
    if role == "ordinary":
        try:
            override = explicit_override_path(env)
        except SnapshotPathError:
            return None
        if override is not None:
            return ResolvedPath(path=override, kind="override", expected_uid=None, watch=True)

    managed_file = managed_dir.rstrip("/") + "/current.json"
    if os.path.isdir(managed_dir):
        meta = load_deployment_meta()
        if meta is None:
            return None
        return ResolvedPath(
            path=managed_file,
            kind="managed",
            expected_uid=meta.admin_uid,
            watch=True,
        )

    if role in ("polkit", "locker"):
        return None
    try:
        state = developer_state_file(env)
    except SnapshotPathError:
        return None
    return ResolvedPath(path=state, kind="state", expected_uid=os.geteuid(), watch=True)


def _managed_ancestor_uids(admin_uid: int) -> dict[int, int]:
    # var, lib, qdistro are root; presentation (index 3) is admin-owned.
    return {0: 0, 1: 0, 2: 0, 3: admin_uid}


def read_snapshot_at(
    path: str,
    *,
    kind: Literal["managed", "state", "override"],
    expected_uid: int | None,
) -> tuple[bytes, tuple[int, int, int, int]]:
    """Read at most 64 KiB from a non-symlink regular file."""
    if kind == "managed":
        if expected_uid is None:
            raise SnapshotPathError("managed snapshot requires deployment admin uid")
        fd = walk_open(
            path,
            ancestor_uids=_managed_ancestor_uids(expected_uid),
            leaf_uid=expected_uid,
        )
    elif kind == "state":
        uid = os.geteuid() if expected_uid is None else expected_uid
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3:
            raise SnapshotPathError("state path too short")
        # Effective UID throughout the qdistro/presentation suffix.
        suffix_uids = {len(parts) - 3: uid, len(parts) - 2: uid}
        fd = walk_open(
            path,
            leaf_uid=uid,
            ancestor_uids=suffix_uids,
            require_unwritable_dirs=True,
        )
    else:
        fd = walk_open(path, leaf_uid=expected_uid, require_unwritable_dirs=False)
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if kind == "managed" and _group_or_other_writable(mode):
            raise SnapshotPathError("managed snapshot is group/other-writable")
        data = read_regular_fd(fd)
        identity = file_identity(fd)
        return data, identity
    finally:
        _close_quietly(fd)


def load_snapshot(
    resolved: ResolvedPath,
) -> tuple[PresentationSnapshot, tuple[int, int, int, int]]:
    data, identity = read_snapshot_at(
        resolved.path, kind=resolved.kind, expected_uid=resolved.expected_uid
    )
    return parse_snapshot_bytes(data), identity


def nearest_existing_parent(path: str) -> str:
    current = os.path.dirname(path)
    while current and not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            return current
        current = parent
    return current or "/"
