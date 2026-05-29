"""qdistro-media-exec — brokered mount/unmount for removable media.

A root, systemd-socket-activated helper that lets the *unprivileged*
qdshell mount/unmount removable block devices WITHOUT ever making a
direct privileged call. It is the removable-media sibling of
``qsu/qdistro_root_exec.py`` and reuses the same privilege model:

1. SO_PEERCRED gives the authoritative (pid, uid) of the connecting
   peer — never a self-asserted value.
2. The request names ``op`` (``mount`` / ``unmount``) and a ``device``
   string. The device is validated against a strict allow-list and
   resolved to a canonical ``/dev/...`` path; anything else is refused
   BEFORE the broker is contacted.
3. We ask ``org.qdistro.AdminBroker1`` for permission using a NEW
   action ``qdistro.media.mount:<device>`` /
   ``qdistro.media.unmount:<device>`` via ``RequestPermissionAs`` (so
   the broker matches rules against the *caller's* identity, not root).
   The argv tuple is shipped in ``argv[NN]`` keys so admins can author
   argv-pinned (``forever_argv``) allow rules.
4. On ``allow`` we run ``udisksctl`` with a TOKENIZED argv list (never
   ``sh -c``); the device label / fstype / uuid are passed to the
   broker only as display ``details`` and never enter argv.

Autorun: this helper NEVER executes anything off the device. It only
ever mounts/unmounts. Opening a file manager (and the autorun policy)
lives entirely on the qdshell side and likewise never auto-executes.

Wire protocol (single newline-delimited JSON request → one reply):
  C → S  {"op": "mount", "device": "/dev/disk/by-id/...",
          "label": "MYUSB", "fstype": "vfat", "uuid": "..."}
  S → C  {"type": "result", "ok": true, "mountpoint": "/run/media/...",
          "device": "/dev/sdb1"}
  S → C  {"type": "result", "ok": false, "error": "request denied"}

The label/fstype/uuid fields are UNTRUSTED (they come off the on-disk
filesystem) and are used only for the admin prompt; they are never
shell-interpolated and never placed in argv.
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import sys
import syslog
import threading

import dbus
import dbus.mainloop.glib  # noqa: F401 — glib must import before bus access

BUS_NAME = "org.qdistro.AdminBroker1"
OBJ_PATH = "/org/qdistro/AdminBroker1"

SOCKET_PATH = "/run/qdistro-media-exec/sock"

UDISKSCTL = "/usr/bin/udisksctl"

# Per-uid in-flight cap: each request can block on admin approval, so a
# hostile uid opening many connections could DoS the admin queue.
MAX_INFLIGHT_PER_UID = 4
MAX_REQUEST_BYTES = 1_000_000

# Reply timeouts mirror qsu: enqueue is bounded, but admin attention is
# slow, so the WaitForDecision client cutoff is generous.
_REQUEST_TIMEOUT_S = 90
_WAIT_TIMEOUT_S = 900
# udisksctl itself must not hang the helper thread forever.
_UDISKS_TIMEOUT_S = 60

_VALID_OPS = frozenset(("mount", "unmount"))

# A device string is accepted only if, after realpath resolution, it
# names a node directly under /dev (e.g. /dev/sdb1, /dev/mmcblk0p1,
# /dev/nvme0n1p2). The *input* may be a /dev/disk/by-id|by-uuid|by-label
# symlink — udev-managed, owned root:root — which we realpath() before
# validating. The pre-realpath input is itself constrained so a crafted
# string can't smuggle shell metacharacters into argv even though argv
# is a list (defense-in-depth + clean audit lines).
_DEV_INPUT_RE = re.compile(r"^/dev/[A-Za-z0-9/_:.+-]+$")
# Post-realpath canonical node: /dev/ followed by a single path segment
# of the usual block-device character set. No further slashes — a
# block device node is never nested below /dev/<name>.
_DEV_NODE_RE = re.compile(r"^/dev/[A-Za-z0-9_]+$")


_inflight_lock = threading.Lock()
_inflight_by_uid: dict[int, int] = {}


def _inflight_acquire(uid: int) -> bool:
    with _inflight_lock:
        n = _inflight_by_uid.get(uid, 0)
        if n >= MAX_INFLIGHT_PER_UID:
            return False
        _inflight_by_uid[uid] = n + 1
        return True


def _inflight_release(uid: int) -> None:
    with _inflight_lock:
        n = _inflight_by_uid.get(uid, 0)
        if n <= 1:
            _inflight_by_uid.pop(uid, None)
        else:
            _inflight_by_uid[uid] = n - 1


# -- SO_PEERCRED + /proc identity (same anchors as qsu) -------------------

def _peer_cred(sock: socket.socket) -> tuple[int, int, int]:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                            struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", creds)
    return pid, uid, gid


def _peer_exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return "?"


def _peer_start_time(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        rparen = data.rfind(b")")
        if rparen < 0:
            return 0
        fields = data[rparen + 2:].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return 0


class CallerIdentityChanged(Exception):
    """Caller exec'd or the pid was recycled between connect and the
    privileged broker call — fail closed."""


def _recheck_caller_identity(pid: int, exe_at_accept: str,
                             start_at_accept: int) -> None:
    """Re-read /proc identity and fail closed on any mismatch. Mirrors
    qsu's anti-TOCTOU recheck: starttime detects PID reuse, exe detects
    in-place exec()."""
    has_exe_anchor = bool(exe_at_accept) and exe_at_accept != "?"
    has_start_anchor = start_at_accept != 0
    if not has_exe_anchor and not has_start_anchor:
        raise CallerIdentityChanged(
            f"caller pid={pid} identity unverifiable")
    start_now = _peer_start_time(pid)
    if has_start_anchor and start_now != start_at_accept:
        raise CallerIdentityChanged(
            f"caller starttime changed pid={pid} "
            f"accept={start_at_accept} now={start_now}")
    if has_start_anchor and start_now == 0:
        raise CallerIdentityChanged(f"caller pid={pid} no longer live")
    exe_now = _peer_exe(pid)
    if has_exe_anchor and exe_now != exe_at_accept:
        raise CallerIdentityChanged(
            f"caller exe changed pid={pid} "
            f"accept={exe_at_accept!r} now={exe_now!r}")


# -- Device validation (pure; unit-tested) --------------------------------

def validate_device(device: str) -> str:
    """Validate and canonicalize a device string.

    Returns the canonical ``/dev/<node>`` path (realpath of any
    by-id/by-uuid/by-label symlink). Raises ValueError on anything that
    is not a syntactically clean ``/dev/...`` input resolving to a plain
    block-device node directly under ``/dev``.

    This is the trust gate for the (untrusted) device argument. argv is
    always a list passed to subprocess, so this regex is not what stops
    shell injection — there is no shell — but it keeps a crafted string
    out of argv / audit and rejects path-traversal (``/dev/../etc/x``)
    and non-/dev targets before any privileged action.
    """
    if not isinstance(device, str) or not device:
        raise ValueError("device required")
    if "\x00" in device or "\n" in device:
        raise ValueError("device contains control characters")
    if not _DEV_INPUT_RE.match(device):
        raise ValueError(f"device must be a /dev path: {device!r}")
    # Resolve udev symlinks (by-id / by-uuid / by-label) to the real
    # node. realpath also collapses any ".." — a post-resolution path
    # that escaped /dev/<node> is rejected by _DEV_NODE_RE below.
    canon = os.path.realpath(device)
    if not _DEV_NODE_RE.match(canon):
        raise ValueError(
            f"device does not resolve to a /dev node: {device!r} -> {canon!r}")
    return canon


LSBLK = "/usr/bin/lsblk"


def parse_lsblk_removable(stdout: str) -> bool | None:
    """Parse `lsblk -dno RM,HOTPLUG <dev>` output. Returns True if the
    device (or its parent) is removable/hotpluggable, False if it is a
    fixed/system-internal disk, None if the output couldn't be parsed.

    lsblk prints one line per device with two 0/1 columns. We treat the
    device as removable if EITHER RM or HOTPLUG is 1 — USB sticks set
    HOTPLUG, SD card readers often set RM. A fixed internal SATA/NVMe
    disk has both 0, so this rejects unmounting the system disk via a
    broad qdistro.media.* rule (codex finding #2)."""
    saw_line = False
    for line in str(stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        saw_line = True
        try:
            rm = int(parts[0])
            hotplug = int(parts[1])
        except ValueError:
            continue
        if rm == 1 or hotplug == 1:
            return True
    return False if saw_line else None


def device_is_removable(device: str) -> bool:
    """Authoritatively decide (server-side, NOT trusting qdshell) whether
    ``device`` is removable/hotpluggable. We query the partition's parent
    disk too (a partition inherits its disk's RM/HOTPLUG). Fail-closed:
    any lsblk failure or unparseable output → not removable → refuse.

    The argv is tokenized; ``device`` is already a canonical /dev node
    from validate_device(), so no shell and no interpolation."""
    try:
        proc = subprocess.run(
            [LSBLK, "-dno", "RM,HOTPLUG", device],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"},
            close_fds=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    direct = parse_lsblk_removable(proc.stdout.decode(errors="replace"))
    if direct:
        return True
    # The node itself may be a partition whose RM/HOTPLUG is 0 while the
    # parent disk carries the flag. Walk to the parent disk and re-check.
    try:
        proc2 = subprocess.run(
            [LSBLK, "-no", "RM,HOTPLUG", device],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"},
            close_fds=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc2.returncode != 0:
        return False
    parent = parse_lsblk_removable(proc2.stdout.decode(errors="replace"))
    return bool(parent)


def build_argv(op: str, device: str) -> list[str]:
    """Build the tokenized udisksctl argv for ``op`` on ``device``.

    ``device`` MUST already be a canonical node from ``validate_device``.
    The result is a Python list handed straight to subprocess — no shell,
    no string interpolation. Raises ValueError on an unknown op.
    """
    if op not in _VALID_OPS:
        raise ValueError(f"unknown op: {op!r}")
    sub = "mount" if op == "mount" else "unmount"
    return [UDISKSCTL, sub, "-b", device]


# A filesystem UUID is hex + dashes (and FAT's short XXXX-XXXX form). We
# only ever embed a UUID into the action string if it matches this, so a
# crafted value can never inject `*`/`:` glob/structure into the action.
_UUID_RE = re.compile(r"^[A-Fa-f0-9-]{1,64}$")


def device_uuid(device: str) -> str:
    """Resolve the device's filesystem UUID server-side (tokenized lsblk,
    no shell). Returns "" if unreadable / no filesystem. This is a STABLE
    identity anchor: durable approvals are pinned to it so a re-used
    /dev/<node> assigned to a different volume re-prompts (codex #1)."""
    try:
        proc = subprocess.run(
            [LSBLK, "-dno", "UUID", device],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={"PATH": "/usr/bin:/bin"},
            close_fds=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    uuid = proc.stdout.decode(errors="replace").strip().splitlines()
    val = uuid[0].strip() if uuid else ""
    return val if _UUID_RE.match(val) else ""


def action_for(op: str, device: str, uuid: str = "") -> str:
    """The broker action string for this operation. The device suffix is
    the canonical node so admins can author ``qdistro.media.mount:*``
    rules; it is never a label / never interpolated. When a stable
    filesystem ``uuid`` is known it is appended (``...:/dev/sdb1?uuid=X``)
    so a durable approval pins the actual volume, not just the volatile
    /dev name (codex #1). The uuid is regex-validated by ``device_uuid``
    before it ever reaches here."""
    if op not in _VALID_OPS:
        raise ValueError(f"unknown op: {op!r}")
    base = f"qdistro.media.{op}:{device}"
    if uuid and _UUID_RE.match(uuid):
        return f"{base}?uuid={uuid}"
    return base


def build_details(op: str, device: str, argv: list[str], *,
                  label: str = "", fstype: str = "", uuid: str = "") -> dict:
    """Build the broker details dict. label/fstype/uuid are UNTRUSTED
    display-only fields; argv elements are shipped as argv[NN] keys so
    argv-pinned rules can match the exact command."""
    details: dict[str, object] = {
        "op": op,
        "device": device,
        # Display-only, untrusted. Coerced to str; never enters argv.
        "label": str(label or ""),
        "fstype": str(fstype or ""),
        "uuid": str(uuid or ""),
    }
    for i, a in enumerate(argv):
        details[f"argv[{i:02d}]"] = str(a)
    return details


# -- Broker request -------------------------------------------------------

def _ask_broker(op: str, device: str, argv: list[str], details: dict,
                caller_uid: int, caller_pid: int, caller_exe: str,
                *, caller_start_time: int = 0, fs_uuid: str = "") -> bool:
    """RequestPermissionAs on the broker for the media action; wait for
    the decision. Fail-closed: the final identity recheck runs right
    before the privileged call. ``fs_uuid`` (server-resolved) pins the
    action string to the actual volume."""
    bus = dbus.SystemBus()
    obj = bus.get_object(BUS_NAME, OBJ_PATH)
    iface = dbus.Interface(obj, BUS_NAME)
    action = action_for(op, device, fs_uuid)
    if caller_start_time:
        details = dict(details)
        details["caller_start_time"] = dbus.UInt64(int(caller_start_time))
    # Close the connect→request TOCTOU window immediately before the call.
    _recheck_caller_identity(caller_pid, caller_exe, caller_start_time)
    rid = int(iface.RequestPermissionAs(
        int(caller_uid), int(caller_pid), str(caller_exe),
        action, details,
        timeout=_REQUEST_TIMEOUT_S,
    ))
    return bool(iface.WaitForDecision(rid, timeout=_WAIT_TIMEOUT_S))


# -- udisksctl execution (no shell) ---------------------------------------

def _run_udisksctl(argv: list[str], uid: int, gid: int) -> tuple[int, str, str]:
    """Run udisksctl with a tokenized argv as the *caller's* uid/gid.

    Dropping to the caller's uid means udisks2 mounts under
    ``/run/media/<user>/`` owned by that user and the helper itself never
    holds the mount as root. No shell; ``env`` is minimal and fixed.
    """
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    try:
        groups = os.getgrouplist(_username_for_uid(uid), gid)
    except (OSError, KeyError):
        groups = [gid]
    proc = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        user=uid,
        group=gid,
        extra_groups=groups,
        env=env,
        close_fds=True,
        timeout=_UDISKS_TIMEOUT_S,
        check=False,
    )
    return (proc.returncode,
            proc.stdout.decode(errors="replace"),
            proc.stderr.decode(errors="replace"))


def _username_for_uid(uid: int) -> str:
    import pwd
    return pwd.getpwuid(int(uid)).pw_name


_MOUNTPOINT_RE = re.compile(r"at\s+(\S+)\s*\.?\s*$")


def parse_mount_output(stdout: str) -> str:
    """Extract the mountpoint from udisksctl's `Mounted /dev/sdb1 at
    /run/media/user/LABEL.` line. Returns "" if not found."""
    for line in str(stdout or "").splitlines():
        line = line.strip()
        m = _MOUNTPOINT_RE.search(line)
        if m:
            mp = m.group(1).rstrip(".")
            return mp
    return ""


# -- Wire helpers ---------------------------------------------------------

def _send(sock: socket.socket, frame: dict) -> None:
    sock.sendall((json.dumps(frame) + "\n").encode())


def _recv_request(sock: socket.socket) -> dict | None:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return None
        if len(buf) + len(chunk) > MAX_REQUEST_BYTES:
            raise ValueError("request too large")
        buf += chunk
    line, _ = buf.split(b"\n", 1)
    return json.loads(line.decode())


# -- Connection handler ---------------------------------------------------

def handle_one(sock: socket.socket) -> None:
    acquired_uid: int | None = None
    try:
        pid, uid, gid = _peer_cred(sock)
        exe_at_accept = _peer_exe(pid)
        start_at_accept = _peer_start_time(pid)

        if not _inflight_acquire(uid):
            _send(sock, {"type": "result", "ok": False,
                         "error": f"too many in-flight requests for uid={uid}"})
            return
        acquired_uid = uid

        req = _recv_request(sock)
        if not req or not isinstance(req, dict):
            _send(sock, {"type": "result", "ok": False,
                         "error": "empty or malformed request"})
            return

        op = str(req.get("op") or "")
        device_in = str(req.get("device") or "")
        if op not in _VALID_OPS:
            _send(sock, {"type": "result", "ok": False,
                         "error": f"invalid op: {op!r}"})
            return

        try:
            device = validate_device(device_in)
        except ValueError as e:
            _send(sock, {"type": "result", "ok": False, "error": str(e)})
            return

        # Authoritatively confirm the device is removable/hotpluggable
        # BEFORE asking the broker. Refuse fixed/system-internal disks so
        # a broad `qdistro.media.*` rule can never mount/unmount the OS
        # disk (codex finding #2). Server-side check — we do not trust any
        # removability claim from qdshell.
        if not device_is_removable(device):
            _send(sock, {"type": "result", "ok": False,
                         "error": f"not a removable device: {device}"})
            syslog.syslog(syslog.LOG_WARNING,
                          f"media refused non-removable device={device} "
                          f"uid={uid} pid={pid}")
            return

        # Resolve the STABLE filesystem UUID server-side; this anchors a
        # durable approval to the actual volume (codex #1). Falls back to
        # "" when the volume has no fs UUID — then the action is keyed on
        # the /dev node alone (and argv-pinned scopes still apply).
        fs_uuid = device_uuid(device)
        argv = build_argv(op, device)
        details = build_details(
            op, device, argv,
            label=str(req.get("label") or ""),
            fstype=str(req.get("fstype") or ""),
            # Prefer the server-resolved UUID over any client claim.
            uuid=fs_uuid or str(req.get("uuid") or ""))

        # Re-verify identity before the (privileged) broker call.
        start_now = _peer_start_time(pid)
        if start_at_accept != 0 and start_now != start_at_accept:
            _send(sock, {"type": "result", "ok": False,
                         "error": "caller exited between connect and request"})
            syslog.syslog(syslog.LOG_WARNING,
                          f"media pid-race uid={uid} pid={pid}")
            return

        syslog.syslog(syslog.LOG_NOTICE,
                      f"media request: uid={uid} pid={pid} exe={exe_at_accept} "
                      f"op={op} device={device}")
        try:
            allowed = _ask_broker(op, device, argv, details, uid, pid,
                                  exe_at_accept,
                                  caller_start_time=start_at_accept,
                                  fs_uuid=fs_uuid)
        except CallerIdentityChanged as e:
            _send(sock, {"type": "result", "ok": False,
                         "error": "caller identity changed; refusing"})
            syslog.syslog(syslog.LOG_WARNING, f"media identity-race: {e}")
            return

        if not allowed:
            _send(sock, {"type": "result", "ok": False,
                         "error": "request denied"})
            return

        # Final fail-closed recheck immediately before running udisksctl —
        # the admin prompt may have been pending for minutes.
        try:
            _recheck_caller_identity(pid, exe_at_accept, start_at_accept)
        except CallerIdentityChanged as e:
            _send(sock, {"type": "result", "ok": False,
                         "error": "caller identity changed; refusing"})
            syslog.syslog(syslog.LOG_WARNING,
                          f"media identity-race (post-approval): {e}")
            return

        try:
            rc, out, err = _run_udisksctl(argv, uid, gid)
        except subprocess.TimeoutExpired:
            _send(sock, {"type": "result", "ok": False,
                         "error": "udisksctl timed out"})
            return
        if rc != 0:
            _send(sock, {"type": "result", "ok": False,
                         "error": (err or out or "udisksctl failed").strip(),
                         "device": device})
            return
        mountpoint = parse_mount_output(out) if op == "mount" else ""
        _send(sock, {"type": "result", "ok": True,
                     "device": device, "mountpoint": mountpoint})
    except Exception as e:  # noqa: BLE001 — never let one request kill the service
        syslog.syslog(syslog.LOG_ERR, f"media handler crash: {e!r}")
        try:
            _send(sock, {"type": "result", "ok": False,
                         "error": f"internal error: {e}"})
        except OSError:
            pass
    finally:
        if acquired_uid is not None:
            _inflight_release(acquired_uid)


def _serve_one(conn: socket.socket) -> None:
    try:
        handle_one(conn)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    syslog.openlog("qdistro-media-exec", syslog.LOG_PID, syslog.LOG_DAEMON)
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    if os.environ.get("LISTEN_FDS") == "1":
        listener = socket.socket(fileno=3)
    else:
        os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o755, exist_ok=True)
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o666)
        listener.listen(16)
    syslog.syslog(syslog.LOG_INFO, "qdistro-media-exec listening")
    while True:
        conn, _ = listener.accept()
        t = threading.Thread(target=_serve_one, args=(conn,),
                             name="media-conn", daemon=True)
        t.start()


if __name__ == "__main__":
    sys.exit(main())
