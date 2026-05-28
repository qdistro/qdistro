"""qdistro-root-exec — privileged exec service for qsu.

Runs as root, listens on a systemd-activated Unix socket, and for each
incoming connection:

1. Reads the peer's uid + pid from SO_PEERCRED (plus reads the peer's
   /proc/<pid>/exe for the audit record).
2. Parses a JSON request: `{"target_user": "<name>", "argv": [...]}`.
3. Calls the qdistro broker's RequestPermission with action `qsu.exec`
   and the argv / target_user in the details dict. Waits for the
   decision.
4. On allow: forks, drops to target_user's uid/gid, execs argv. Stdout
   and stderr are captured through pipes and streamed back to the
   client line-by-line as JSON frames.
5. On deny: sends a single error frame.

Deliberately non-pty in v1 — vim/top will not work. Pty support is a
spec/21 follow-up; the socket protocol is extensible (frame type is
a field) so the client and server can add "pty_data" / "winch" frames
without breaking the wire format.

Protocol (newline-delimited JSON, one frame per line):
  C → S  {"target_user": "root", "argv": ["id"]}
  S → C  {"type": "stdout", "data": "uid=0(root) ..."}
  S → C  {"type": "stderr", "data": "..."}    # (optional, per line)
  S → C  {"type": "exit",   "code": 0}
  S → C  {"type": "error",  "message": "Request denied."}

Security:
- Peer authentication via SO_PEERCRED is authoritative for identity.
- The socket is mode 0666 but requests are authorised via the broker;
  an attacker who opens the socket just burns a rate-limit slot and
  still has to pass admin approval.
- Target_user resolution uses getpwnam; unknown users → error.
- argv[0] is looked up via shutil.which to match PATH exactly the
  way the caller would expect — avoids arguing about shell-builtins.
"""
from __future__ import annotations

import grp
import json
import os
import pwd
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import syslog
import threading
from typing import Any

import dbus
import dbus.mainloop.glib  # noqa: F401 — ensures glib is imported before bus access

BUS_NAME = "org.qdistro.AdminBroker1"
OBJ_PATH = "/org/qdistro/AdminBroker1"

# Socket lives inside systemd's RuntimeDirectory so the kernel
# creates the parent 0700-owned-by-root — unlink-then-bind races on
# /run are out of reach for any non-root local user.
SOCKET_PATH = "/run/qdistro-root-exec/sock"

# Per-uid cap on in-flight requests. One hostile uid can open
# multiple connections and each blocks on admin approval; without a
# cap they DoS the whole qsu surface. 4 is generous for legitimate
# admin workflow (a couple of pending prompts is already unusual).
MAX_INFLIGHT_PER_UID = 4

# Recv cap must be checked *before* the append so a caller can't
# push allocation past the limit.
MAX_REQUEST_BYTES = 1_000_000

# target_user must be a plausible POSIX username. Without a whitelist,
# embedded newlines / control chars flow into the broker action string
# and the audit syslog line.
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


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


# -- SO_PEERCRED helper ----------------------------------------------------

def _peer_cred(sock: socket.socket) -> tuple[int, int, int]:
    """Return (pid, uid, gid) of the peer connected over sock."""
    # struct ucred: pid (int), uid (uint32), gid (uint32)
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
    """Read /proc/<pid>/stat field 22 (starttime in clock ticks).

    Paired with a later re-read to detect PID reuse between accept()
    and the broker call. Zero means "couldn't read".
    """
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


# -- Broker request --------------------------------------------------------

def _ask_broker(target_user: str, argv: list[str],
                 caller_uid: int, caller_pid: int, caller_exe: str,
                 *, caller_start_time: int = 0,
                 client_claimed_name: str = "") -> bool:
    """Route through the broker's standard approval flow.

    Uses RequestPermissionAs so the broker records (and matches rules
    against) the *real* caller identity — not root, which is our own
    uid. The dbus policy permits only uid 0 to call this method; the
    broker re-verifies the sender uid in-process.

    Action name includes target_user so a single admin click can't
    grant "run anything as root" when what they saw was "run id as
    nobody". argv is shipped to the broker as a typed list in
    `details.argv_list` plus a human-readable `shlex.join(argv)` for
    the admin UI — never as a lossy space-join, which would let
    `rm /tmp/foo bar` look identical to `rm "/tmp/foo bar"`.

    Broker-side policy forbids any scope > `once` on delegated
    requests, so even a cached approval can't wildcard future argv.
    """
    import shlex
    bus = dbus.SystemBus()
    obj = bus.get_object(BUS_NAME, OBJ_PATH)
    iface = dbus.Interface(obj, BUS_NAME)
    action = f"qsu.exec:{target_user}"
    details = {
        "target_user": target_user,
        # Human-readable, unambiguous — used by admin UI.
        "argv":        shlex.join(argv),
        # Also ship each argv element as its own key so a downstream
        # UI that wants to render them as a list (rather than parse
        # the shlex'd string) can. Keys sort lexicographically; the
        # admin app's detail pane renders them in order.
        **{f"argv[{i:02d}]": a for i, a in enumerate(argv)},
    }
    if caller_start_time:
        details["caller_start_time"] = dbus.UInt64(int(caller_start_time))
    if client_claimed_name:
        details["client_claimed_name"] = client_claimed_name
    # 2026-05-16: bumped dbus reply timeouts. Two manifestations of
    # broker serialisation tripped over the dbus-python default 25s:
    #
    # (a) 4 concurrent qsu calls serialise through _read_proc_layered
    #     and take 8-15s aggregate to enqueue, so the 4th can blow
    #     RequestPermissionAs's 25s budget (todo:
    #     broker-serialization-concurrent-qsu §1).
    # (b) admin's single-handed GUI two-step (OCR-find-radio + click
    #     + Ctrl+Y) routinely takes >25s on a vision-driven runner,
    #     so the qsu client bails out of WaitForDecision BEFORE the
    #     approve reaches the broker (§3). The broker still commits
    #     the approve — but the streamed stdout never reaches the
    #     user because qsu is already dead.
    #
    # WaitForDecision is async on the broker side (it enqueues
    # `(_reply, _error)` waiters and returns the response when admin
    # decides); the only timeout is dbus-python's client-side reply
    # cutoff. 900s = "admin's plausible attention span." If admin
    # really takes 15 minutes to click, the user almost certainly
    # ctrl+C'd qsu by then anyway.
    rid = int(iface.RequestPermissionAs(
        int(caller_uid), int(caller_pid), str(caller_exe),
        action, details,
        timeout=90,
    ))
    return bool(iface.WaitForDecision(rid, timeout=900))


# -- Target user handling --------------------------------------------------

def _resolve_target(target_user: str) -> tuple[int, int, str, str]:
    try:
        pw = pwd.getpwnam(target_user)
    except KeyError as e:
        raise ValueError(f"unknown target user: {target_user!r}") from e
    return pw.pw_uid, pw.pw_gid, pw.pw_dir, pw.pw_shell


# -- Subprocess exec with stdio capture ------------------------------------

def _resolve_argv(argv: list[str]) -> list[str] | None:
    """Resolve a short `argv[0]` to an absolute path via PATH.

    Called *before* the broker request so the admin approves a
    fully-qualified command. subprocess can also resolve at exec time,
    but by that point the admin has already seen the un-resolved
    string and their decision would not reflect what actually runs.
    """
    if "/" in argv[0]:
        return list(argv)
    resolved = shutil.which(argv[0])
    if resolved is None:
        return None
    return [resolved, *argv[1:]]


def _spawn_and_stream(sock: socket.socket, target_user: str,
                       argv: list[str]) -> None:
    """Fork off the target command; stream stdout/stderr back as JSON
    frames; send a final `exit` frame with the return code.

    `argv[0]` is expected to be absolute by this point (see
    `_resolve_argv` in the handler). Target user is resolved here
    solely to get uid/gid/home for env building and the subprocess
    user= kwarg.
    """
    uid, gid, home, _shell = _resolve_target(target_user)

    env = {
        "PATH":  "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME":  home,
        "USER":  target_user,
        "LOGNAME": target_user,
        "TERM":  "xterm",
    }

    try:
        groups = os.getgrouplist(target_user, gid)
    except OSError:
        groups = [gid]

    # Python 3.11+ `user=/group=/extra_groups=` kwargs do the right
    # thing inside subprocess itself — no `preexec_fn` deadlock risk
    # from running Python between fork and exec, which is documented
    # as a footgun when the parent has threads (dbus-glib mainloop
    # counts).
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        user=uid,
        group=gid,
        extra_groups=groups,
        env=env,
        close_fds=True,
    )

    fds = {proc.stdout.fileno(): "stdout", proc.stderr.fileno(): "stderr"}
    open_fds = set(fds.keys())
    while open_fds:
        r, _, _ = select.select(list(open_fds), [], [], 1.0)
        for fd in r:
            chunk = os.read(fd, 4096)
            if not chunk:
                open_fds.discard(fd)
                continue
            _send(sock, {"type": fds[fd], "data": chunk.decode(errors="replace")})
        # Don't exit the loop just because the child is dead — drain
        # any buffered output still sitting in the pipes first. The
        # `select` above with no ready fds confirms the kernel has
        # nothing more to hand us.
        if proc.poll() is not None and not r:
            break
    # Final drain: anything that arrived between the last select and
    # proc.poll() seeing the exit. Non-blocking to avoid hanging if
    # pipes were already closed.
    for fd in list(open_fds):
        try:
            os.set_blocking(fd, False)
            chunk = os.read(fd, 65536)
            if chunk:
                _send(sock, {"type": fds[fd], "data": chunk.decode(errors="replace")})
        except (OSError, BlockingIOError):
            pass
    proc.wait()
    _send(sock, {"type": "exit", "code": int(proc.returncode)})


# -- Wire format helpers ---------------------------------------------------

def _send(sock: socket.socket, frame: dict) -> None:
    sock.sendall((json.dumps(frame) + "\n").encode())


def _recv_request(sock: socket.socket) -> dict | None:
    """Read one newline-delimited JSON frame from the client.

    Size limit is checked *before* the append so an attacker who
    sends near-limit chunks can't push allocation past MAX_REQUEST_BYTES.
    """
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


# -- Connection handler ----------------------------------------------------

def handle_one(sock: socket.socket) -> None:
    """Serve a single accepted connection — runs in its own thread so
    the listener can accept more connections while this one blocks on
    admin approval.
    """
    acquired_uid: int | None = None
    try:
        # Step 1: capture peer identity IMMEDIATELY after accept, before
        # any blocking recv. If the peer exec's between connect and
        # now, we'd at least record the pre-request exe in audit; the
        # starttime re-verify below closes the race.
        pid, uid, _gid = _peer_cred(sock)
        exe_at_accept = _peer_exe(pid)
        start_at_accept = _peer_start_time(pid)

        # Step 2: per-uid in-flight cap. DoS defense against one uid
        # opening enough connections to starve the admin queue.
        if not _inflight_acquire(uid):
            _send(sock, {"type": "error",
                         "message": f"too many in-flight qsu requests for uid={uid}"})
            _send(sock, {"type": "exit", "code": 1})
            return
        acquired_uid = uid

        # Step 3: read the request. Every pre-broker validation
        # rejection MUST send both an `error` AND an `exit` frame so
        # qsu clients waiting on the wire-shape don't have to rely on
        # _stream's "no-exit-frame-seen-before-EOF → rc=1" fallback.
        # Other later error paths (broker-deny, internal-error, command-
        # not-found) already do this; keep the early ones consistent.
        req = _recv_request(sock)
        if not req or not isinstance(req, dict):
            _send(sock, {"type": "error", "message": "empty or malformed request"})
            _send(sock, {"type": "exit",  "code": 1})
            return

        target_user = str(req.get("target_user") or "")
        argv = list(req.get("argv") or [])
        client_claimed_name = str(req.get("caller_name") or "")
        if not target_user or not argv:
            _send(sock, {"type": "error", "message": "target_user and argv required"})
            _send(sock, {"type": "exit",  "code": 1})
            return

        # Step 4: validate target_user. Without this, embedded newlines
        # / control chars flow into the broker action string and the
        # audit syslog line; a well-formed POSIX username is enough
        # for everything qsu supports today.
        if not _USERNAME_RE.match(target_user):
            _send(sock, {"type": "error",
                         "message": f"invalid target_user: {target_user!r}"})
            _send(sock, {"type": "exit",  "code": 1})
            return

        # argv elements must be strings — json.loads preserves types
        # but a malicious client can send nested lists etc.
        if not all(isinstance(a, str) and a for a in argv):
            _send(sock, {"type": "error", "message": "argv must be a list of non-empty strings"})
            _send(sock, {"type": "exit",  "code": 1})
            return

        try:
            _resolve_target(target_user)
        except ValueError as e:
            _send(sock, {"type": "error", "message": str(e)})
            _send(sock, {"type": "exit",  "code": 1})
            return

        # Step 5: resolve argv[0] to absolute *before* the broker call
        # so the admin's approval is about a known-path command.
        resolved = _resolve_argv(argv)
        if resolved is None:
            _send(sock, {"type": "error", "message": f"command not found: {argv[0]}"})
            _send(sock, {"type": "exit",  "code": 127})
            return
        argv = resolved

        # Step 6: re-verify pid identity. If the caller exited, exec'd,
        # or the pid was recycled between accept and here, the admin
        # should not approve under the original exe.
        start_now = _peer_start_time(pid)
        if start_at_accept != 0 and start_now != start_at_accept:
            _send(sock, {"type": "error",
                         "message": "caller exited between connect "
                                    "and request; refusing"})
            _send(sock, {"type": "exit", "code": 1})
            syslog.syslog(syslog.LOG_WARNING,
                          f"qsu pid-race: uid={uid} pid={pid} "
                          f"start_accept={start_at_accept} start_now={start_now}")
            return
        exe_now = _peer_exe(pid)
        if exe_at_accept and exe_at_accept != "?" and exe_now != exe_at_accept:
            _send(sock, {"type": "error",
                         "message": "caller executable changed between connect "
                                    "and request; refusing"})
            _send(sock, {"type": "exit", "code": 1})
            syslog.syslog(syslog.LOG_WARNING,
                          f"qsu exe-race: uid={uid} pid={pid} "
                          f"exe_accept={exe_at_accept!r} exe_now={exe_now!r}")
            return

        syslog.syslog(syslog.LOG_NOTICE,
                      f"qsu exec request: caller uid={uid} pid={pid} "
                      f"exe={exe_at_accept} target={target_user!r} argv={argv!r}")
        allowed = _ask_broker(target_user, argv, uid, pid, exe_at_accept,
                              caller_start_time=start_at_accept,
                              client_claimed_name=client_claimed_name)
        if not allowed:
            _send(sock, {"type": "error", "message": "request denied"})
            _send(sock, {"type": "exit",  "code": 1})
            return
        _spawn_and_stream(sock, target_user, argv)
    except Exception as e:  # noqa: BLE001 — never let one bad request kill the service
        syslog.syslog(syslog.LOG_ERR, f"qsu exec handler crash: {e!r}")
        try:
            _send(sock, {"type": "error", "message": f"internal error: {e}"})
            _send(sock, {"type": "exit",  "code": 1})
        except OSError:
            pass
    finally:
        if acquired_uid is not None:
            _inflight_release(acquired_uid)


# -- Main loop -------------------------------------------------------------

def main() -> int:
    syslog.openlog("qdistro-root-exec", syslog.LOG_PID, syslog.LOG_DAEMON)
    # Wire the dbus main loop BEFORE any bus access so signals from
    # the broker reach subsequent proxies — dbus-python's default
    # mainloop takes effect per-process and is a foot-gun if set
    # after the first SystemBus() call.
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    # Accept systemd socket activation (LISTEN_FDS) or fall back to
    # binding ourselves (useful for dev / manual runs).
    if os.environ.get("LISTEN_FDS") == "1":
        listener = socket.socket(fileno=3)
    else:
        # Dev fallback: create the RuntimeDirectory-equivalent
        # parent ourselves so `bind()` doesn't ENOENT on a first
        # boot without systemd.
        os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o755, exist_ok=True)
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o666)
        listener.listen(16)
    syslog.syslog(syslog.LOG_INFO, "qdistro-root-exec listening")
    while True:
        conn, _ = listener.accept()
        # Serve each connection in its own thread so the accept loop
        # keeps draining while handle_one blocks on admin approval.
        # Daemon threads die with the service on shutdown.
        t = threading.Thread(
            target=_serve_one,
            args=(conn,),
            name="qsu-conn",
            daemon=True,
        )
        t.start()


def _serve_one(conn: socket.socket) -> None:
    try:
        handle_one(conn)
    finally:
        try:
            conn.close()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
