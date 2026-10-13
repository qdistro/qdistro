#!/usr/bin/python3 -I
"""qdistro-tier5-bridge — the root half of a tier-5 launch.

spawn-tier5.sh runs the whole VM lifecycle as admin. Two things it cannot
do as admin, and this bridge does them instead, behind the polkit action
``org.qdistro.tier5.bridge`` (auth_admin_keep, exec.path pinned):

  * be the uid-0 direct parent qdistro-secctx-exec and qdwin require
    before they tag the host-side waypipe client's Wayland connection;
  * register that waypipe client with the broker (RegisterLaunch is
    root-helper-only).

Process tree (the bridge supervises; runuser is the direct root parent):

    spawn-tier5            admin
    └─ pkexec → bridge     root
       └─ runuser          root
          └─ qdistro-secctx-exec   admin
             └─ waypipe client     admin

The caller supplies only bounded tokens: a VM name (or loopback name), a
vsock port and the launch token. Everything else — executables, target
user, environment, CID, engine/app/silo identity — is fixed here.

Protocol with the caller: stdout gets one ``READY <pid>`` line once the
waypipe client is verified and registered; stdin is the caller's
liveness pipe — EOF (caller exit, cancel, crash) tears the chain down.
The bridge exits with the waypipe chain's status.

Usage (via pkexec only):
    qdistro-tier5-bridge --vm <name> --port <port> --token <hex32>
    qdistro-tier5-bridge --loopback loopback-<n> --port <port> --token <hex32>
"""
from __future__ import annotations

import ctypes
import os
import pwd
import re
import select
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass

ADMIN_USER = "admin"
RUNUSER_CANDIDATES = ("/usr/bin/runuser", "/usr/sbin/runuser")
ENV_EXE = "/usr/bin/env"
SECCTX_EXEC = "/usr/bin/qdistro-secctx-exec"
WAYPIPE = "/usr/bin/waypipe"
DBUS_SEND = "/usr/bin/dbus-send"
WAYLAND_DISPLAY = "wayland-1"

ENGINE = "qdistro.tier5"
NAMESPACE = "tier5"
# vsock CIDs: the host-side listener for a real guest is always the host
# CID; VMADDR_CID_LOCAL is reachable only through the explicit loopback
# mode, whose server half runs on the host and isolates nothing.
HOST_CID = 2
LOOPBACK_CID = 1

REPORT_TIMEOUT_S = 30.0
REGISTER_TIMEOUT_S = 30.0
TEARDOWN_GRACE_S = 5.0

_VM_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}")
_LOOPBACK_NAME_RE = re.compile(r"loopback-[0-9]{1,10}")
_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
_PORT_RE = re.compile(r"[1-9][0-9]{3,4}")
_REPORT_RE = re.compile(rb"([1-9][0-9]{0,9})\n")

EXIT_REFUSED = 2
EXIT_SETUP = 3
EXIT_REPORT = 4
EXIT_VERIFY = 5
EXIT_REGISTER = 6
EXIT_CANCELLED = 7


class Refused(Exception):
    """Caller input or invocation context the bridge will not act on."""


class LaunchFailed(Exception):
    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


@dataclass(frozen=True)
class Launch:
    loopback: bool
    name: str
    port: int
    token: str

    @property
    def cid(self) -> int:
        return LOOPBACK_CID if self.loopback else HOST_CID

    @property
    def app_id(self) -> str:
        return f"{ENGINE}.{self.name}"

    @property
    def silo(self) -> str:
        # The silo key qdshell's ClipboardSilo derives from this app-id;
        # the broker compares the two exactly.
        return f"vm-{self.name}"

    @property
    def title_prefix(self) -> str:
        return f"[tier5:{self.name}] "


def parse_args(argv: list[str]) -> Launch:
    """Accept exactly ``--vm|--loopback NAME --port PORT --token TOKEN``."""
    if len(argv) != 6:
        raise Refused("expected: --vm|--loopback NAME --port PORT "
                      "--token TOKEN")
    mode, name, popt, port, topt, token = argv
    if mode not in ("--vm", "--loopback") or popt != "--port" \
            or topt != "--token":
        raise Refused("expected: --vm|--loopback NAME --port PORT "
                      "--token TOKEN")
    loopback = mode == "--loopback"
    if loopback:
        if not _LOOPBACK_NAME_RE.fullmatch(name):
            raise Refused("loopback name must be loopback-<digits>")
    elif not _VM_NAME_RE.fullmatch(name) or name.startswith("loopback-"):
        raise Refused("invalid VM name")
    if not _PORT_RE.fullmatch(port) or not 1024 <= int(port) <= 65535:
        raise Refused("port must be 1024..65535")
    if not _TOKEN_RE.fullmatch(token):
        raise Refused("token must be 32 lowercase hex chars")
    return Launch(loopback, name, int(port), token)


def trusted_exe(path: str) -> str:
    """Resolve a fixed executable path; refuse anything a non-root uid
    could have replaced (the file or its directory). Returns the
    resolved path — what /proc/<pid>/exe reports for it."""
    real = os.path.realpath(path)
    for p in (real, os.path.dirname(real)):
        st = os.stat(p)
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise LaunchFailed(f"{path}: {p} is not root-owned/protected",
                               EXIT_SETUP)
    if not stat.S_ISREG(os.stat(real).st_mode):
        raise LaunchFailed(f"{path}: not a regular file", EXIT_SETUP)
    return real


def find_runuser() -> str:
    for cand in RUNUSER_CANDIDATES:
        if os.path.exists(cand):
            return cand
    raise LaunchFailed("runuser not found", EXIT_SETUP)


def admin_identity() -> tuple[int, int, str]:
    pw = pwd.getpwnam(ADMIN_USER)
    return pw.pw_uid, pw.pw_gid, pw.pw_dir


def chain_env(admin_uid: int, home: str, report_fd: int) -> dict[str, str]:
    """The complete environment of secctx-exec and waypipe. Nothing from
    the caller's environment reaches it."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "USER": ADMIN_USER,
        "LOGNAME": ADMIN_USER,
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": f"/run/user/{admin_uid}",
        "WAYLAND_DISPLAY": WAYLAND_DISPLAY,
        "QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER": "1",
        "QDISTRO_LAUNCH_REPORT_FD": str(report_fd),
    }


def waypipe_argv(launch: Launch, waypipe: str) -> list[str]:
    argv = [waypipe, "-s", f"{launch.cid}:{launch.port}", "--vsock"]
    if launch.loopback:
        argv.append("--oneshot")
    argv += ["--no-gpu", "--title-prefix", launch.title_prefix, "client"]
    return argv


def chain_argv(launch: Launch, runuser: str, env_exe: str, secctx: str,
               waypipe: str, env: dict[str, str]) -> list[str]:
    # runuser execs `env -i …` in place, so secctx-exec keeps runuser
    # (uid 0) as its direct parent — the check secctx-exec and qdwin make.
    return ([runuser, "-u", ADMIN_USER, "--", env_exe, "-i"]
            + [f"{k}={v}" for k, v in env.items()]
            + [secctx, "--sandbox-engine", ENGINE,
               "--app-id", launch.app_id,
               "--instance-id", launch.token, "--"]
            + waypipe_argv(launch, waypipe))


def register_argv(launch: Launch, dbus_send: str, pid: int, exe: str,
                  starttime: int) -> list[str]:
    return [dbus_send, "--system", "--print-reply",
            "--dest=org.qdistro.AdminBroker1",
            "/org/qdistro/AdminBroker1",
            "org.qdistro.AdminBroker1.RegisterLaunch",
            f"string:{launch.silo}", f"string:{ENGINE}",
            f"string:{launch.app_id}", f"string:{launch.token}",
            f"string:{exe}", f"uint64:{pid}",
            f"string:{NAMESPACE}", f"uint64:{starttime}"]


# -- /proc ---------------------------------------------------------------

def proc_status(pid: int) -> tuple[int, tuple[int, ...]]:
    """(ppid, (ruid, euid, suid, fsuid)) of a live pid; raises OSError."""
    ppid = None
    uids: tuple[int, ...] = ()
    with open(f"/proc/{pid}/status", encoding="ascii",
              errors="replace") as f:
        for line in f:
            if line.startswith("PPid:"):
                ppid = int(line.split()[1])
            elif line.startswith("Uid:"):
                uids = tuple(int(x) for x in line.split()[1:5])
    if ppid is None or len(uids) != 4:
        raise OSError(f"/proc/{pid}/status unparseable")
    return ppid, uids


def proc_starttime(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
    except OSError:
        return 0
    rest = raw[raw.rfind(b")") + 2:].split()
    # rest[0] is field 3 (state); starttime is field 22
    return int(rest[19]) if len(rest) > 19 else 0


def proc_exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def verify_chain(pid: int, runuser_pid: int, admin_uid: int,
                 secctx_exe: str, waypipe_exe: str) -> int:
    """Check the reported pid is the waypipe client this bridge launched:
    child of a secctx-exec that is the child of our runuser, both running
    as admin, the pid exec'd into the installed waypipe. Returns its
    starttime (the RegisterLaunch anti-reuse anchor)."""
    start = proc_starttime(pid)
    if start <= 0:
        raise LaunchFailed(f"reported pid {pid} is not live", EXIT_VERIFY)
    try:
        sppid, uids = proc_status(pid)
        rppid, suids = proc_status(sppid)
    except (OSError, ValueError) as e:
        raise LaunchFailed(f"reported pid {pid} vanished: {e}",
                           EXIT_VERIFY) from e
    if rppid != runuser_pid:
        raise LaunchFailed(f"pid {pid} is not in this bridge's launch chain",
                           EXIT_VERIFY)
    if set(uids) != {admin_uid} or set(suids) != {admin_uid}:
        raise LaunchFailed(f"pid {pid} chain does not run as {ADMIN_USER}",
                           EXIT_VERIFY)
    if proc_exe(sppid) != secctx_exe:
        raise LaunchFailed(f"pid {pid}'s parent is not {secctx_exe}",
                           EXIT_VERIFY)
    if proc_exe(pid) != waypipe_exe:
        raise LaunchFailed(f"pid {pid} is not {waypipe_exe}", EXIT_VERIFY)
    # A pid that died and was reused between the reads above shows a
    # different starttime — or none.
    if proc_starttime(pid) != start:
        raise LaunchFailed(f"pid {pid} changed during verification",
                           EXIT_VERIFY)
    return start


# -- the launch ------------------------------------------------------------

def _set_pdeathsig() -> None:
    # Runs in the forked runuser child: if the bridge dies, runuser gets
    # SIGTERM and forwards it down the chain.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


class Bridge:
    def __init__(self, launch: Launch, *, stdin_fd: int = 0,
                 stdout=None, admin: tuple[int, int, str] | None = None):
        self.launch = launch
        self.stdin_fd = stdin_fd
        self.stdout = stdout if stdout is not None else sys.stdout
        uid, _gid, home = admin or admin_identity()
        self.admin_uid: int = uid
        self.admin_home: str = home
        self.proc: subprocess.Popen | None = None
        self.pidfds: list[int] = []
        self._sig_r, self._sig_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._stopping = False

    # signals land on a self-pipe the event loops poll
    def install_signals(self) -> None:
        signal.set_wakeup_fd(self._sig_w)
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, lambda *_: None)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    def _caller_gone(self, ready: list) -> bool:
        if self._sig_r in ready:
            return True
        if self.stdin_fd in ready:
            try:
                return os.read(self.stdin_fd, 4096) == b""
            except OSError:
                return True
        return False

    def start(self) -> int:
        # Exec the fixed paths; compare /proc against what they resolve to.
        runuser = find_runuser()
        for p in (runuser, ENV_EXE, DBUS_SEND):
            trusted_exe(p)
        self.secctx_exe = trusted_exe(SECCTX_EXEC)
        self.waypipe_exe = trusted_exe(WAYPIPE)
        report_r, report_w = os.pipe2(os.O_CLOEXEC)
        try:
            argv = chain_argv(self.launch, runuser, ENV_EXE, SECCTX_EXEC,
                              WAYPIPE, chain_env(self.admin_uid,
                                                 self.admin_home, report_w))
            self.proc = subprocess.Popen(
                argv, env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                stdin=subprocess.DEVNULL, stdout=2, stderr=2,
                close_fds=True, pass_fds=(report_w,), cwd="/",
                # the bridge is single-threaded; PDEATHSIG has no
                # subprocess-native spelling
                preexec_fn=_set_pdeathsig)  # noqa: PLW1509
        finally:
            os.close(report_w)
        try:
            return self._await_report(report_r)
        finally:
            os.close(report_r)

    def _await_report(self, report_r: int) -> int:
        buf = b""
        deadline = time.monotonic() + REPORT_TIMEOUT_S
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise LaunchFailed("no launch report from secctx-exec",
                                   EXIT_REPORT)
            ready, _, _ = select.select(
                [report_r, self.stdin_fd, self._sig_r], [], [], left)
            if self._caller_gone(ready):
                raise LaunchFailed("caller went away during launch",
                                   EXIT_CANCELLED)
            if report_r in ready:
                chunk = os.read(report_r, 64)
                buf += chunk
                m = _REPORT_RE.fullmatch(buf)
                if m:
                    return int(m.group(1))
                if not chunk or b"\n" in buf or len(buf) > 16:
                    raise LaunchFailed(
                        "secctx-exec reported no launch (exec failed?)",
                        EXIT_REPORT)

    def register(self, pid: int) -> None:
        assert self.proc is not None
        start = verify_chain(pid, self.proc.pid, self.admin_uid,
                             self.secctx_exe, self.waypipe_exe)
        # Hold the verified process by pidfd from here on: a later signal
        # can never reach a recycled pid.
        fd = os.pidfd_open(pid)
        self.pidfds.append(fd)
        if proc_starttime(pid) != start:
            raise LaunchFailed(f"pid {pid} changed during verification",
                               EXIT_VERIFY)
        try:
            res = subprocess.run(
                register_argv(self.launch, DBUS_SEND, pid,
                              self.waypipe_exe, start),
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, close_fds=True, cwd="/",
                timeout=REGISTER_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LaunchFailed(f"RegisterLaunch failed: {e}",
                               EXIT_REGISTER) from e
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", "replace").strip()[:400]
            raise LaunchFailed(f"RegisterLaunch refused: {err}",
                               EXIT_REGISTER)
        print(f"[tier5-bridge] registered pid={pid} silo={self.launch.silo}",
              file=sys.stderr, flush=True)

    def supervise(self) -> int:
        """Wait for the chain to exit or the caller to go away."""
        assert self.proc is not None
        rfd = os.pidfd_open(self.proc.pid)
        try:
            while self.proc.poll() is None:
                ready, _, _ = select.select(
                    [rfd, self.stdin_fd, self._sig_r], [], [])
                if self._caller_gone(ready):
                    self.teardown()
                    break
        finally:
            os.close(rfd)
        rc = self.proc.wait()
        self._reap_stragglers()
        return 128 - rc if rc < 0 else rc

    def teardown(self) -> None:
        if self.proc is None or self._stopping:
            return
        self._stopping = True
        if self.proc.poll() is None:
            # runuser forwards SIGTERM to secctx-exec, which forwards it
            # to waypipe; the pidfd copy is the belt for a chain that
            # broke mid-way.
            self.proc.send_signal(signal.SIGTERM)
        self._signal_pidfds(signal.SIGTERM)
        try:
            self.proc.wait(TEARDOWN_GRACE_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._reap_stragglers()

    def _signal_pidfds(self, sig: int) -> None:
        for fd in self.pidfds:
            try:
                signal.pidfd_send_signal(fd, sig)
            except ProcessLookupError:
                pass

    def _reap_stragglers(self) -> None:
        # A waypipe that outlived its chain is reparented away from us;
        # its pidfd still reaches exactly it.
        deadline = time.monotonic() + TEARDOWN_GRACE_S
        for fd in self.pidfds:
            left = max(0.0, deadline - time.monotonic())
            if not select.select([fd], [], [], left)[0]:
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for fd in self.pidfds:
            os.close(fd)
        self.pidfds = []

    def run(self) -> int:
        try:
            pid = self.start()
            self.register(pid)
        except LaunchFailed as e:
            print(f"[tier5-bridge] FAIL: {e}", file=sys.stderr, flush=True)
            self.teardown()
            return e.code
        except OSError as e:
            # e.g. the verified pid died before pidfd_open, or an exec failed
            print(f"[tier5-bridge] FAIL: {e}", file=sys.stderr, flush=True)
            self.teardown()
            return EXIT_SETUP
        try:
            self.stdout.write(f"READY {pid}\n")
            self.stdout.flush()
        except OSError:
            self.teardown()
            return EXIT_CANCELLED
        return self.supervise()


def check_invoker(env: dict[str, str]) -> None:
    if os.geteuid() != 0:
        raise Refused("must run as root via pkexec")
    admin_uid = admin_identity()[0]
    if env.get("PKEXEC_UID") != str(admin_uid):
        raise Refused(f"only {ADMIN_USER} may invoke the bridge (pkexec)")


def main(argv: list[str]) -> int:
    os.umask(0o077)
    os.chdir("/")
    try:
        launch = parse_args(argv)
        check_invoker(dict(os.environ))
    except (Refused, KeyError) as e:
        print(f"[tier5-bridge] refused: {e}", file=sys.stderr, flush=True)
        return EXIT_REFUSED
    bridge = Bridge(launch)
    bridge.install_signals()
    return bridge.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
