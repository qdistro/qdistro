"""Shared, fail-closed ``/proc`` identity readers for permission lineage.

Before this module there were three independent copies of the same
``/proc`` parsing logic — ``workflow/condition_eval.py``
(``_proc_starttime`` / ``_read_identity``), the broker's
``_read_proc_identity`` / ``_read_proc_uid`` / ``_read_proc_selinux_label``
/ ``_read_proc_layered``, and qsu's ``_peer_start_time`` / ``_peer_exe``.
The permission-lineage work (``issues/qdistro/permission-lineage-findings.md``)
adds a launch-record store and a resolver that need the *same* readers; per
the brief these are lifted here and shared rather than reimplemented a
fourth time.

Every reader is **fail-closed**: an unreadable ``/proc`` entry, a gone
process, or a malformed field yields the sentinel for that field
(``0`` for starttime, ``None`` for uid/gid, ``""`` / ``"?"`` for strings)
— never a guess and never an exception that a caller might paper over.

starttime (``/proc/<pid>/stat`` field 22, clock ticks since boot) is the
load-bearing anti-PID-reuse anchor used everywhere: a PID recycled into a
different process gets a different starttime, so a starttime match proves
the live PID is still the process a caller captured earlier.
"""
from __future__ import annotations

import grp
import hashlib
import os
import pwd
from typing import Any

# Cap how much of an exe we hash. Most binaries are well under 64 MiB;
# anything bigger is almost certainly a self-extracting bundle whose
# trailing payload doesn't change identity assertions for the wrapping
# binary. Lifted from the broker's _read_proc_layered.
EXE_HASH_BYTES_MAX = 64 * 1024 * 1024


def read_starttime(pid: int) -> int:
    """``/proc/<pid>/stat`` field 22 (starttime, clock ticks since boot).

    Returns ``0`` when the file is unreadable / the process is gone / the
    field is malformed. The stat file's comm field (field 2) is wrapped in
    parens and can contain spaces and ``)`` characters, so we split from
    the *right* of the closing paren to avoid a maliciously-named comm
    breaking the parse.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        rparen = data.rfind(b")")
        if rparen < 0:
            return 0
        fields = data[rparen + 2:].split()
        # starttime is field 22 overall; after splitting past (comm) it
        # lands at fields[19].
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return 0


def read_exe(pid: int) -> str:
    """``realpath`` of ``/proc/<pid>/exe`` via readlink, or ``"?"`` if gone.

    Returns the raw symlink target (not ``os.path.realpath``) to match the
    broker's long-standing ``_read_proc_identity`` semantics that
    ``forever_exe`` cache keys and the qsu recheck compare against.
    """
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return "?"


def read_exe_and_starttime(pid: int) -> tuple[str, int]:
    """Return ``(exe_path, starttime_ticks)`` for ``pid``, or ``("?", 0)``.

    This is the broker's historical ``_read_proc_identity`` contract.
    """
    return read_exe(pid), read_starttime(pid)


def read_uid(pid: int) -> int | None:
    """Real uid from ``/proc/<pid>/status``, or ``None`` if the process is
    gone / the field is unreadable."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("Uid:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1])
                    break
    except (OSError, ValueError):
        return None
    return None


def read_selinux_label(pid: int) -> str:
    """SELinux label from ``/proc/<pid>/attr/current``, or ``""`` if the
    file is unreadable (SELinux off, process gone). The kernel terminates
    the value with a NUL; trailing NUL/newline/space are stripped."""
    try:
        with open(f"/proc/{pid}/attr/current", "rb") as f:
            label = f.read(4096)
        return label.rstrip(b"\x00\n\r ").decode("utf-8", "replace")
    except OSError:
        return ""


def read_cgroup(pid: int) -> str:
    """The cgroup-v2 unified path (last ``0::`` line of
    ``/proc/<pid>/cgroup``), else the first non-empty line, else ``""``."""
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f.readlines()]
    except OSError:
        return ""
    unified = next(
        (ln.split("::", 1)[1] for ln in lines if ln.startswith("0::")),
        None)
    if unified:
        return unified
    if lines:
        return lines[0]
    return ""


def read_exe_sha256(pid: int) -> str:
    """SHA-256 of the binary behind ``/proc/<pid>/exe`` (bounded read), or
    ``""`` on any IO error. Reads *through* the live ``/proc/<pid>/exe``
    link so a re-exec into a different binary between request and hash is
    reflected rather than masked."""
    try:
        h = hashlib.sha256()
        remaining = EXE_HASH_BYTES_MAX
        with open(f"/proc/{pid}/exe", "rb") as f:
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                h.update(chunk)
                remaining -= len(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def read_identity(pid: int) -> dict[str, Any] | None:
    """Read uid/gid/exe/argv0/comm for ``pid`` — the ``condition_eval``
    contract. Returns ``None`` (fail-closed) only when the uid/gid lines
    can't be read, since those are the keys a recycled / gone process
    can't honestly supply; the string fields degrade to ``""``."""
    ident: dict[str, Any] = {}
    try:
        with open(f"/proc/{pid}/status", encoding="ascii",
                  errors="replace") as f:
            for line in f:
                if line.startswith("Uid:"):
                    # "Uid:\treal\teffective\tsaved\tfs"
                    ident["uid"] = int(line.split()[1])
                elif line.startswith("Gid:"):
                    ident["gid"] = int(line.split()[1])
                if "uid" in ident and "gid" in ident:
                    break
    except (OSError, ValueError, IndexError):
        return None
    if "uid" not in ident or "gid" not in ident:
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        ident["argv0"] = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
    except OSError:
        ident["argv0"] = ""
    try:
        ident["exe"] = os.path.realpath(f"/proc/{pid}/exe")
    except OSError:
        ident["exe"] = ""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8",
                  errors="replace") as f:
            ident["comm"] = f.read().strip()
    except OSError:
        ident["comm"] = ""
    return ident


def resolve_uid_name(value: Any) -> int | None:
    """Resolve an int / numeric-string / username to a uid, or ``None``."""
    if isinstance(value, bool):  # bool is an int subclass — reject early
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    try:
        return pwd.getpwnam(s).pw_uid
    except KeyError:
        return None


def resolve_gid_name(value: Any) -> int | None:
    """Resolve an int / numeric-string / group name to a gid, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    try:
        return grp.getgrnam(s).gr_gid
    except KeyError:
        return None
