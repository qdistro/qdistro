"""Hardened atomic JSON writer for the pwd-area daemons.

Single source of truth for "write a small secret-ish JSON state file safely"
inside `/usr/libexec/qdistro/` (the vault, recovery bundle, …). It replaces the
per-module `_atomic_write` copies that had drifted: the vault one created the
temp file at the umask default and chmod'd to 0600 only *after* writing the
ciphertext, leaving a brief window where the secret state was group/other
readable; the recovery one already closed that window. This module standardizes
on the hardened behavior for every pwd writer.

Scope is deliberately tiny — JSON dict, mode no broader than 0600. It is
co-located with its only consumers (same install dir, same SELinux domain, same
`qdistro-pwd` uid) and is NOT a qdistro-wide primitive; cross-area sharing would
pull in per-area install + SELinux labeling cost the duplication does not
justify (see the de-dup review).
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def atomic_write_json(path: str, body: dict[str, Any], *, mode: int = 0o600) -> None:
    """Atomically write ``body`` as pretty-printed JSON to ``path``.

    The destination never exists in a half-written or over-permissive state:
    the temp is created via ``mkstemp`` (0600, O_EXCL, a unique name so a stale
    temp from a crashed writer can never block or be confused with this one),
    ``fchmod``'d to exactly ``mode`` (so even a pathological umask cannot strip
    the owner bits, and group/other are never granted), fully written + fsync'd,
    then ``os.replace``d into place (atomic on the same filesystem). The
    containing directory is fsync'd best-effort so the rename survives a crash.
    The temp is removed on any failure.

    ``mode`` must not be broader than 0600 — these are pwd secrets.
    """
    if mode & 0o077:
        raise ValueError(f"refusing pwd atomic write with mode {mode:#o} broader than 0600")

    dir_path = os.path.dirname(path) or "."
    base = os.path.basename(path)
    # mkstemp: unique name, O_EXCL, 0600, fd non-inheritable (PEP 446).
    fd, tmp = tempfile.mkstemp(prefix=base + ".", suffix=".tmp", dir=dir_path)
    fd_owned_by_file = False
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # fdopen now owns the fd; its close handles every later failure.
            fd_owned_by_file = True
            json.dump(body, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # If we failed before fdopen took ownership (e.g. fchmod/fdopen raised),
        # close the raw fd ourselves so it does not leak. Then drop the temp.
        if not fd_owned_by_file:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # Best-effort directory fsync so the rename is durable across a crash.
    try:
        dfd = os.open(dir_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)
