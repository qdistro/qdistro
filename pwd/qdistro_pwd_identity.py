"""Layered caller identity for the qdistro password-manager daemon.

Mirrors the broker's _read_proc_layered (broker/qdistro_admin_broker.py)
but adds an authoritative fingerprint-from-/proc-and-SO_PEERCRED snapshot
captured at request time, used for the per-item app-pin gate.

The daemon NEVER trusts a caller-supplied identity claim — every value
is read from kernel-attested sources (SO_PEERCRED for uid/pid; /proc for
exe and SELinux label; cgroup line). This is the spec/13 §"App identity
verification" matrix.
"""
from __future__ import annotations

import hashlib
import os
import socket
import struct
from typing import Any

# Match the broker's bound to keep memory consumption bounded.
_EXE_HASH_BYTES_MAX = 64 * 1024 * 1024  # 64 MB


def read_peer_cred(conn_fd: int) -> tuple[int, int, int]:
    """Return (pid, uid, gid) from SO_PEERCRED on a Unix-domain socket fd.

    Caller must own conn_fd (the socket the peer is connected on). For the
    qdistro-pwd D-Bus path, the peer cred is normally surfaced via
    dbus.connection.list_names_with_credentials() — this helper exists for
    the lower-level peer socket path used by qdistro-pwd-get.
    """
    # struct ucred is { pid_t pid; uid_t uid; gid_t gid }, 12 bytes on Linux.
    sock = socket.socket(fileno=conn_fd)
    try:
        cred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    finally:
        # detach so we don't close the caller's fd
        sock.detach()
    pid, uid, gid = struct.unpack("3i", cred)
    return pid, uid, gid


def read_proc_exe(pid: int) -> str:
    """Return /proc/<pid>/exe target path, '' if process is gone."""
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def read_proc_exe_sha256(pid: int) -> str:
    """SHA-256 of the running binary (read through /proc/<pid>/exe so a
    re-exec is reflected). Truncated to 64 MB so a runaway 4 GB binary
    can't OOM the daemon."""
    try:
        h = hashlib.sha256()
        remaining = _EXE_HASH_BYTES_MAX
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


def read_proc_selinux(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/attr/current", "rb") as f:
            label = f.read(4096)
        return label.rstrip(b"\x00\n").decode("utf-8", "replace")
    except OSError:
        return ""


def read_proc_cgroup(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f.readlines()]
        unified = next(
            (ln.split("::", 1)[1] for ln in lines if ln.startswith("0::")),
            None)
        if unified:
            return unified
        if lines:
            return lines[0]
    except OSError:
        pass
    return ""


def snapshot_caller(pid: int, uid: int) -> dict[str, Any]:
    """Snapshot the layered identity attributes for a peer at request time.

    All keys are always present (empty string / None on read failure) so
    callers can render a stable layout. Race-tolerant: if the process
    exits between SO_PEERCRED and the /proc reads, the empty fields make
    the policy gate fail closed.
    """
    return {
        "uid":           uid,
        "pid":           pid,
        "exe":           read_proc_exe(pid),
        "exe_sha256":    read_proc_exe_sha256(pid),
        "selinux_label": read_proc_selinux(pid),
        "cgroup":        read_proc_cgroup(pid),
    }


def pin_match(item_pins: dict[str, Any], caller: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether a caller satisfies an item's pin set.

    Each non-empty pin field on the item must match the caller's
    corresponding kernel-attested attribute exactly. Empty pin fields
    are treated as wildcards. At least one pin field must be set on
    the item — a fully-unpinned item is admin-only and only readable
    via the admin GetItemAdmin path (enforced by the daemon's caller-uid
    check, not here).

    Returns (allowed, reason). reason is a short human-readable hint
    used in audit log + error messages.
    """
    pin_exe = (item_pins.get("pin_app_exe") or "").strip()
    pin_selinux = (item_pins.get("pin_selinux") or "").strip()
    pin_uid = item_pins.get("pin_uid")
    if not pin_exe and not pin_selinux and pin_uid is None:
        return False, "item has no pin set; admin-only retrieval"
    # caller exe is read fresh from /proc; an empty value means the
    # process is gone or unreadable (race). Fail closed.
    if pin_exe:
        if not caller.get("exe"):
            return False, "caller exe unreadable (race or unknown)"
        if caller["exe"] != pin_exe:
            return False, f"exe mismatch (caller={caller['exe']!r}, pin={pin_exe!r})"
    if pin_selinux:
        if not caller.get("selinux_label"):
            return False, "caller has no SELinux label (kernel disabled?)"
        if caller["selinux_label"] != pin_selinux:
            return False, (f"selinux mismatch (caller={caller['selinux_label']!r}, "
                           f"pin={pin_selinux!r})")
    if pin_uid is not None:
        if int(caller.get("uid", -1)) != int(pin_uid):
            return False, f"uid mismatch (caller={caller.get('uid')!r}, pin={pin_uid!r})"
    return True, "pin matched"
