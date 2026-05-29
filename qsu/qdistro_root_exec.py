#!/usr/bin/python3
"""qdistro-root-exec — privileged exec service for qsu.

Runs as root, listens on a systemd-activated Unix socket, and for each
incoming connection:

1. Reads the peer's uid + pid from SO_PEERCRED (plus reads the peer's
   /proc/<pid>/exe for the audit record).
2. Parses a JSON request: `{"target_user": "<name>", "argv": [...],
   "run_id": "<optional workflow run id>"}`. When `run_id` is present,
   the daemon asks the broker for that run's non-secret, allowlisted
   `channel_env` (e.g. SSH_AUTH_SOCK) and folds it into the child env
   before exec (the git-sign external-consume bridge). The lookup
   forwards the qsu caller's SO_PEERCRED uid; the engine binds the run to
   it (run_id is not a bearer token). Fail-closed: unknown run / uid
   mismatch / not-published-within-bounded-wait → the exec is refused.
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

# Workflow-run ids are uuid4 hex-with-dashes (see WorkflowRun.run_id).
# Validate before it flows into the broker call / syslog: a hostile
# client could otherwise stuff control chars or an oversized blob in.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Allowlist of channel_env names the bridge will fold into a child's
# environment. This is a SECOND, independent gate to the engine's own
# allowlist (defense-in-depth: the privileged exec daemon does not trust
# the broker to have filtered correctly). Only non-secret *references*
# belong here — never a name that could carry secret material. Keep it
# tight; widening it is a deliberate, reviewable act.
_CHANNEL_ENV_ALLOWLIST = frozenset({"SSH_AUTH_SOCK"})

# Bounded wait for the run to publish its channel_env. The process_spawn
# model spawns the child (this exec) before the engine's deliver_secret
# step has necessarily published SSH_AUTH_SOCK, so we poll the broker for
# a short, bounded window. On timeout we FAIL CLOSED (refuse the exec)
# rather than running git with no agent — the run never reached the
# published state in time.
_CHANNEL_ENV_WAIT_S = 10.0
_CHANNEL_ENV_POLL_S = 0.1


# -- Strict-profile fail-closed identity resolution ------------------------
#
# security-hardening-carryforward.md §"Unresolved executable/starttime
# identity should deny in strict profiles":
#
# The baseline (non-strict) posture fails closed only when *neither* the
# caller exe NOR its starttime can be anchored — a process whose
# /proc/<pid>/exe is unreadable but whose starttime is readable (or vice
# versa) still proceeds on the single available anchor. Under SELinux
# enforcing this single-anchor fallback is a real exposure: if the
# qdistro-root-exec domain is denied read on /proc/<pid>/stat (starttime)
# but can still readlink /proc/<pid>/exe, the starttime anti-PID-reuse
# anchor silently drops out and the request proceeds on the exe path
# alone — exactly the "falls back open" failure the carryforward flags.
#
# STRICT mode closes that: BOTH the exe and the starttime anchor must be
# resolvable, or the request is denied. A strict deployment is one where
# the SELinux policy *should* grant both reads (the qdistro_qsu module
# does), so a missing anchor signals either a policy regression or an
# active attack — fail closed in both cases rather than degrade silently.
#
# Read at import from $QDISTRO_IDENTITY_STRICT or
# /etc/qdistro/broker.conf (key = identity_strict = true). Mirrors the
# broker's _read_require_silo_active / _read_secctx_launcher_gated
# toggle convention. Default OFF so existing permissive bakes keep the
# single-anchor fallback; tier-1/enforcing bakes flip it on.
_IDENTITY_STRICT_ENV = "QDISTRO_IDENTITY_STRICT"
_BROKER_CONF_PATH = "/etc/qdistro/broker.conf"


def _read_identity_strict() -> bool:
    """Resolve the strict-identity profile flag (env, then broker.conf)."""
    val = os.environ.get(_IDENTITY_STRICT_ENV, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    try:
        with open(_BROKER_CONF_PATH, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "identity_strict":
                    return v.strip().lower() in ("1", "true", "yes", "on")
    except OSError:
        pass
    return False


IDENTITY_STRICT = _read_identity_strict()


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


def _recheck_caller_identity(pid: int, exe_at_accept: str,
                              start_at_accept: int) -> None:
    """Re-read the caller's /proc identity and fail closed if it no
    longer matches the value captured at accept time.

    Called immediately before the privileged broker request to shrink
    the connect→request TOCTOU window to a minimum. Two anchors:

    - starttime: detects PID reuse (a different process took the slot
      after the original exited). Does NOT change across exec().
    - exe: detects an in-place exec() into a different binary — the
      attack starttime cannot see.

    Raises CallerIdentityChanged on any mismatch or if /proc can no
    longer be read (process gone). When the accept-time exe was
    unreadable ("?") we can't anchor on it, but the starttime check
    still applies.

    Matching is by resolved /proc/<pid>/exe path-string equality, the
    same notion used by the broker's _verify_delegated_claim and the
    rest of this service. A same-path inode swap (rename a binary then
    re-exec it) is NOT caught here; the broker's layered exe_sha256
    capture is the content-level anchor for that and is what
    forever_exe rules persist against.
    """
    has_exe_anchor = bool(exe_at_accept) and exe_at_accept != "?"
    has_start_anchor = start_at_accept != 0
    # Strict profile (security-hardening-carryforward): BOTH anchors must
    # be resolvable, or deny. A single readable anchor is not enough in a
    # deployment whose SELinux policy is supposed to grant both /proc
    # reads — a missing one is a policy regression or an attack, not a
    # benign degradation. Checked before the baseline no-anchor gate so
    # the deny reason is specific.
    if IDENTITY_STRICT and not (has_exe_anchor and has_start_anchor):
        missing = []
        if not has_exe_anchor:
            missing.append("exe")
        if not has_start_anchor:
            missing.append("starttime")
        raise CallerIdentityChanged(
            f"strict profile: caller pid={pid} identity not fully "
            f"resolvable (missing {'+'.join(missing)} anchor); refusing "
            f"to fall back to a single anchor")
    # Fail closed when neither anchor is usable — we cannot verify the
    # caller identity at all, so we must not let the request proceed
    # under an unverifiable (possibly pid-reused) process. Mirrors the
    # broker's _read_proc_layered_checked no-anchor policy.
    if not has_exe_anchor and not has_start_anchor:
        raise CallerIdentityChanged(
            f"caller pid={pid} identity unverifiable "
            f"(no starttime or exe anchor at accept)")
    start_now = _peer_start_time(pid)
    if has_start_anchor and start_now != start_at_accept:
        raise CallerIdentityChanged(
            f"caller starttime changed pid={pid} "
            f"accept={start_at_accept} now={start_now}")
    # Process gone: starttime read failed where it previously succeeded.
    if has_start_anchor and start_now == 0:
        raise CallerIdentityChanged(f"caller pid={pid} no longer live")
    exe_now = _peer_exe(pid)
    if has_exe_anchor and exe_now != exe_at_accept:
        raise CallerIdentityChanged(
            f"caller exe changed pid={pid} "
            f"accept={exe_at_accept!r} now={exe_now!r}")


# -- Broker request --------------------------------------------------------

class CallerIdentityChanged(Exception):
    """Raised when /proc/<pid>/exe (or starttime) no longer matches the
    value captured at accept time, just before the privileged broker
    call. Fail-closed signal — the handler turns this into a deny."""


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
    #
    # Final fail-closed re-check, performed AFTER the (potentially
    # slow, GIL-yielding) dbus proxy setup AND after the action/details
    # are fully built, IMMEDIATELY before the privileged
    # RequestPermissionAs call — nothing the caller could exploit runs
    # between this check and the request. exec() does not change a
    # process's starttime, so the starttime re-check alone cannot catch
    # a connect→exec→request swap; the exe comparison does. handle_one
    # already rechecked once, but anything between that check and this
    # call (bus connect, name resolution, details build) is a TOCTOU
    # window — close it here so the identity the broker is asked to
    # approve is the one live at the instant of the request. The broker
    # repeats the same verification in RequestPermissionAs as
    # defense-in-depth.
    _recheck_caller_identity(caller_pid, caller_exe, caller_start_time)
    rid = int(iface.RequestPermissionAs(
        int(caller_uid), int(caller_pid), str(caller_exe),
        action, details,
        timeout=90,
    ))
    return bool(iface.WaitForDecision(rid, timeout=900))


# -- Workflow channel-env bridge -------------------------------------------

class ChannelEnvUnavailable(Exception):
    """Raised when a --workflow-run handshake was requested but the run's
    allowlisted channel_env could not be obtained (unknown run, not yet
    published within the bounded wait, broker unreachable, or nothing
    allowlisted returned). Fail-closed signal — never exec without the
    env the caller explicitly asked the bridge to deliver."""


def _broker_get_run_channel_env(run_id: str, names: list[str],
                                caller_uid: int,
                                call_timeout: float = 5.0) -> dict[str, str]:
    """Single broker lookup of a run's allowlisted channel_env.

    Returns the (already engine-side-filtered) {name: value} map, or {} if
    the run hasn't published yet / doesn't exist / uid doesn't match.
    Raises on a broker/dbus transport failure so the caller can
    distinguish "not yet" (retry) from "broker down" (fail closed).

    ``caller_uid`` is the qsu caller's SO_PEERCRED uid, forwarded so the
    engine can bind the run to it. ``call_timeout`` bounds THIS dbus call
    so a wedged broker cannot stall past the overall fail-closed deadline.
    """
    bus = dbus.SystemBus()
    obj = bus.get_object(BUS_NAME, OBJ_PATH)
    iface = dbus.Interface(obj, BUS_NAME)
    ret = iface.GetRunChannelEnv(str(run_id),
                                 dbus.Array(names, signature="s"),
                                 dbus.Int32(int(caller_uid)),
                                 timeout=max(0.1, float(call_timeout)))
    return {str(k): str(v) for k, v in dict(ret).items()}


def _resolve_channel_env(run_id: str, caller_uid: int, *,
                         wait_s: float = _CHANNEL_ENV_WAIT_S,
                         poll_s: float = _CHANNEL_ENV_POLL_S,
                         _clock=None, _sleep=None,
                         _lookup=None) -> dict[str, str]:
    """Fold a workflow run's allowlisted channel_env into a name->value map.

    Bounded-poll the broker until the run publishes at least one
    allowlisted reference, then re-filter the result through this daemon's
    OWN allowlist (defense-in-depth — never trust the broker to have
    filtered). ``caller_uid`` (the qsu caller's SO_PEERCRED uid) is
    forwarded so the engine binds the run to it (run_id is not a bearer
    token). Fail closed (raise ChannelEnvUnavailable) on:

    - an invalid run_id;
    - the bounded wait elapsing with nothing published / uid mismatch
      ("run has not published channel_env yet");
    - the broker returning a name not on our allowlist (would mean the
      engine/broker allowlist drifted — refuse rather than trust it);
    - any value that isn't a non-empty string.

    Never blocks indefinitely; the TOTAL wait is bounded by ``wait_s`` —
    each dbus call is given only the remaining budget so a wedged broker
    cannot exceed it.

    ``_clock``/``_sleep``/``_lookup`` are injection points for tests.
    """
    if not _RUN_ID_RE.match(run_id or ""):
        raise ChannelEnvUnavailable(f"invalid workflow run_id: {run_id!r}")
    clock = _clock or __import__("time").monotonic
    sleep = _sleep or __import__("time").sleep
    lookup = _lookup or _broker_get_run_channel_env
    names = sorted(_CHANNEL_ENV_ALLOWLIST)
    start = clock()
    deadline = start + max(0.0, float(wait_s))
    last_err: Exception | None = None
    raw: dict[str, str] = {}
    attempted = False
    while True:
        # Bound each dbus call by the remaining deadline so a hung broker
        # cannot blow past the advertised total fail-closed window. Once
        # the deadline has passed we do NOT start another lookup (which
        # could overshoot by its own timeout) — except we always allow the
        # FIRST attempt, even for wait_s<=0 (a single non-blocking probe).
        remaining = deadline - clock()
        if remaining <= 0 and attempted:
            if last_err is not None:
                raise ChannelEnvUnavailable(
                    f"broker lookup for run {run_id!r} failed: {last_err!r}")
            raise ChannelEnvUnavailable(
                f"run {run_id!r} has not published channel_env yet "
                f"(waited {wait_s:.0f}s)")
        # Floor the dbus timeout to a small positive value so the call is
        # valid, but never larger than the remaining budget.
        call_timeout = min(max(remaining, 0.0), 5.0)
        if call_timeout <= 0:
            call_timeout = 0.5  # only reachable on the first attempt
        attempted = True
        try:
            raw = lookup(run_id, names, caller_uid, call_timeout)
        except Exception as e:  # noqa: BLE001 — transport failure
            last_err = e
            raw = {}
        if raw:
            break
        # Don't sleep past the deadline.
        if deadline - clock() <= 0:
            continue
        sleep(max(0.01, float(poll_s)))
    # Re-filter through our own allowlist. The broker already filters, but
    # this daemon is the privileged side and must not widen its trust to
    # whatever the broker returned.
    out: dict[str, str] = {}
    for k, v in raw.items():
        if k not in _CHANNEL_ENV_ALLOWLIST:
            raise ChannelEnvUnavailable(
                f"run {run_id!r} returned non-allowlisted env name {k!r}; "
                f"refusing")
        if not isinstance(v, str) or not v:
            raise ChannelEnvUnavailable(
                f"run {run_id!r} returned empty/invalid value for {k!r}")
        out[k] = v
    if not out:
        raise ChannelEnvUnavailable(
            f"run {run_id!r} published no allowlisted channel_env")
    return out


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
                       argv: list[str], *, caller_pid: int = 0,
                       caller_exe: str = "", caller_start_time: int = 0,
                       channel_env: dict[str, str] | None = None) -> None:
    """Fork off the target command; stream stdout/stderr back as JSON
    frames; send a final `exit` frame with the return code.

    `argv[0]` is expected to be absolute by this point (see
    `_resolve_argv` in the handler). Target user is resolved here
    solely to get uid/gid/home for env building and the subprocess
    user= kwarg.

    The caller_* args carry the accept-time identity so the FINAL
    fail-closed exe recheck runs immediately before Popen — after the
    (potentially blocking) NSS / getgrouplist lookups below — closing
    the last sliver of post-approval TOCTOU window. caller_pid==0 skips
    the recheck (used only by tests that exercise the streaming path
    directly).
    """
    uid, gid, home, _shell = _resolve_target(target_user)

    env = {
        "PATH":  "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME":  home,
        "USER":  target_user,
        "LOGNAME": target_user,
        "TERM":  "xterm",
    }
    # Workflow channel-env bridge: fold the run's allowlisted, non-secret
    # references (e.g. SSH_AUTH_SOCK) into the child env. This map was
    # already validated against the allowlist in _resolve_channel_env;
    # re-assert here so the only way a name reaches the child is via the
    # allowlist, even if a future caller passes an unvalidated dict.
    if channel_env:
        for k, v in channel_env.items():
            if k in _CHANNEL_ENV_ALLOWLIST and isinstance(v, str) and v:
                env[k] = v

    try:
        groups = os.getgrouplist(target_user, gid)
    except OSError:
        groups = [gid]

    # Last-instant fail-closed gate: NSS / getgrouplist above can block
    # on a slow network directory, so re-verify the caller's exe one
    # final time HERE — immediately before Popen — rather than only in
    # handle_one before this call. If the caller exec'd a different
    # binary while we were preparing, refuse: the admin's approval was
    # about the original identity.
    if caller_pid:
        _recheck_caller_identity(caller_pid, caller_exe, caller_start_time)

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
        # Optional workflow-run handshake. Empty/absent = current behaviour.
        run_id = str(req.get("run_id") or "")
        if run_id and not _RUN_ID_RE.match(run_id):
            _send(sock, {"type": "error",
                         "message": f"invalid run_id: {run_id!r}"})
            _send(sock, {"type": "exit",  "code": 1})
            return
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
        try:
            allowed = _ask_broker(target_user, argv, uid, pid, exe_at_accept,
                                  caller_start_time=start_at_accept,
                                  client_claimed_name=client_claimed_name)
        except CallerIdentityChanged as e:
            # Fail closed: the caller's executable or starttime changed
            # in the window between the handle_one recheck and the
            # actual RequestPermissionAs call. Never approve under a
            # stale identity.
            _send(sock, {"type": "error",
                         "message": "caller executable changed between connect "
                                    "and request; refusing"})
            _send(sock, {"type": "exit", "code": 1})
            syslog.syslog(syslog.LOG_WARNING,
                          f"qsu exe-race (pre-request): uid={uid} pid={pid} "
                          f"{e}")
            return
        if not allowed:
            _send(sock, {"type": "error", "message": "request denied"})
            _send(sock, {"type": "exit",  "code": 1})
            return

        # Workflow channel-env bridge. Resolve AFTER approval (so an
        # unapproved caller can never probe run state through this daemon)
        # and BEFORE spawn. Fail closed: if the run hasn't published its
        # allowlisted channel_env within the bounded wait, refuse to exec
        # rather than run git with no agent / a stale env.
        channel_env: dict[str, str] | None = None
        if run_id:
            try:
                # Bind to the qsu caller's authenticated uid (SO_PEERCRED),
                # not anything the client put in the request, so a run_id
                # is not a bearer capability for another uid's socket.
                channel_env = _resolve_channel_env(run_id, uid)
            except ChannelEnvUnavailable as e:
                _send(sock, {"type": "error",
                             "message": f"workflow channel_env unavailable: {e}"})
                _send(sock, {"type": "exit", "code": 1})
                syslog.syslog(syslog.LOG_WARNING,
                              f"qsu channel-env fail-closed: uid={uid} "
                              f"pid={pid} run_id={run_id!r} {e}")
                return
            syslog.syslog(syslog.LOG_NOTICE,
                          f"qsu channel-env bridge: uid={uid} pid={pid} "
                          f"run_id={run_id!r} names={sorted(channel_env)}")

        # Final fail-closed gate happens INSIDE _spawn_and_stream, right
        # before Popen (after its blocking NSS/getgrouplist lookups), so
        # the recheck covers the whole post-approval preparation window.
        # The admin's approval was made against the accept-time identity,
        # but WaitForDecision can block for minutes; if the caller exec'd
        # a different binary while the prompt was pending, refuse to run.
        # exec() leaves starttime unchanged, so the exe comparison is the
        # load-bearing check here.
        try:
            _spawn_and_stream(sock, target_user, argv,
                              caller_pid=pid, caller_exe=exe_at_accept,
                              caller_start_time=start_at_accept,
                              channel_env=channel_env)
        except CallerIdentityChanged as e:
            _send(sock, {"type": "error",
                         "message": "caller executable changed between connect "
                                    "and request; refusing"})
            _send(sock, {"type": "exit", "code": 1})
            syslog.syslog(syslog.LOG_WARNING,
                          f"qsu exe-race (post-approval): uid={uid} pid={pid} "
                          f"{e}")
            return
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
