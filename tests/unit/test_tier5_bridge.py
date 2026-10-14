"""qdistro-tier5-bridge: argument/environment refusal and launch-chain tests.

The bridge is the only root step of a tier-5 launch. These tests pin what it
accepts (bounded name/port/token, nothing else), what it builds (fixed exes,
a constructed environment, CID 2 except explicit loopback) and how it treats
the pid secctx-exec reports: verified against the real process tree after
exec, registered with typed fixed arguments, and every failure — bad report,
foreign or stale pid, broker refusal, caller EOF — tears the chain down
without leftovers. The chain runs for real, with stand-ins for runuser,
secctx-exec, waypipe and dbus-send.
"""
from __future__ import annotations

import ast
import io
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import qdistro_tier5_bridge as B

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "0123456789abcdef0123456789abcdef"


# -- argument refusal ---------------------------------------------------------

def _args(mode="--vm", name="t5-foot-0123456789abcdef", port="20001",
          token=TOKEN):
    return [mode, name, "--port", port, "--token", token]


@pytest.mark.parametrize("argv", [
    [],
    _args()[:-1],
    _args() + ["--extra"],
    ["--port", "20001", "--vm", "t5-x", "--token", TOKEN],
    _args(mode="--loopbak"),
    _args(name=""),
    _args(name="../etc"),
    _args(name="/usr/bin/sh"),
    _args(name="-rf"),
    _args(name="a" * 64),
    _args(name="t5 x"),
    _args(name="t5-x\n"),
    _args(name="t5-x;id"),
    _args(name="loopback-1"),           # loopback silo via --vm
    _args(mode="--loopback", name="t5-foot"),
    _args(mode="--loopback", name="loopback-x"),
    _args(port="0"), _args(port="80"), _args(port="65536"),
    _args(port="020001"), _args(port="2e4"), _args(port=" 20001"),
    _args(token=TOKEN.upper()), _args(token=TOKEN[:-1]),
    _args(token=TOKEN + "0"), _args(token="../" + TOKEN[3:]),
])
def test_parse_args_refuses_anything_but_bounded_tokens(argv):
    # ensures: the root bridge accepts no caller-chosen path, executable,
    #          argv, extra option or out-of-range value
    with pytest.raises(B.Refused):
        B.parse_args(argv)


def test_parse_args_derives_identity_internally():
    # ensures: engine/app-id/silo/CID are derived, never caller-supplied —
    #          and the silo is the vm-<name> key ClipboardSilo.js derives
    vm = B.parse_args(_args(name="t5-foot-0123456789abcdef"))
    assert (vm.cid, vm.app_id, vm.silo) == (
        2, "qdistro.tier5.t5-foot-0123456789abcdef",
        "vm-t5-foot-0123456789abcdef")
    lb = B.parse_args(_args(mode="--loopback", name="loopback-42"))
    assert (lb.cid, lb.app_id, lb.silo) == (
        1, "qdistro.tier5.loopback-42", "vm-loopback-42")


def test_silo_matches_qdshell_clipboard_derivation():
    # ensures: the broker silo registered for a tier-5 window equals the
    #          silo qdshell derives from that window's secctx (exact compare
    #          in broker enforcement)
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    js = (ROOT / "qdshell/Services/Qdshell/ClipboardSilo.js").as_posix()
    launch = B.parse_args(_args(name="t5-foot-0123456789abcdef"))
    out = subprocess.run(
        ["node", "-e",
         (f"const m=require({js!r});"
          f"process.stdout.write(m.fromSecctx({B.ENGINE!r},"
          f"{launch.app_id!r},{TOKEN!r}))")],
        capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout == launch.silo


def test_chain_argv_is_fixed_and_cid_follows_mode():
    # ensures: runuser targets the fixed admin user, the env is cleared
    #          (env -i) and fully constructed, the inner command is the
    #          fixed waypipe with CID 2 for a VM and CID 1 + --oneshot only
    #          in loopback mode
    env = B.chain_env(1000, "/home/admin", 9)
    vm = B.chain_argv(B.parse_args(_args(name="t5-a")), "/usr/bin/runuser",
                      "/usr/bin/env", "/usr/bin/qdistro-secctx-exec",
                      "/usr/bin/waypipe", env)
    assert vm[:6] == ["/usr/bin/runuser", "-u", "admin", "--",
                      "/usr/bin/env", "-i"]
    i = vm.index("/usr/bin/qdistro-secctx-exec")
    assert vm[6:i] == [f"{k}={v}" for k, v in env.items()]
    assert vm[i:] == [
        "/usr/bin/qdistro-secctx-exec", "--sandbox-engine", "qdistro.tier5",
        "--app-id", "qdistro.tier5.t5-a", "--instance-id", TOKEN, "--",
        "/usr/bin/waypipe", "-s", "2:20001", "--vsock", "--no-gpu",
        "--title-prefix", "[tier5:t5-a] ", "client"]
    lb = B.chain_argv(B.parse_args(_args(mode="--loopback",
                                         name="loopback-7")),
                      "r", "e", "s", "w", env)
    assert lb[lb.index("w"):] == [
        "w", "-s", "1:20001", "--vsock", "--oneshot", "--no-gpu",
        "--title-prefix", "[tier5:loopback-7] ", "client"]


def test_chain_env_ignores_the_callers_environment(monkeypatch):
    # ensures: nothing from the (admin-controlled) caller environment —
    #          loader, interpreter or path overrides — reaches the chain
    for k, v in (("LD_PRELOAD", "/tmp/evil.so"), ("PATH", "/tmp/evil"),
                 ("PYTHONPATH", "/tmp/evil"), ("WAYLAND_DISPLAY", "evil"),
                 ("XDG_RUNTIME_DIR", "/tmp/evil"),
                 ("QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED", "1")):
        monkeypatch.setenv(k, v)
    env = B.chain_env(1000, "/home/admin", 9)
    assert env == {
        "PATH": "/usr/bin:/bin", "HOME": "/home/admin", "USER": "admin",
        "LOGNAME": "admin", "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-1",
        "QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER": "1",
        "QDISTRO_LAUNCH_REPORT_FD": "9"}


def test_register_argv_is_typed_and_fixed():
    # ensures: registration goes through the broker's accepted root
    #          dbus-send path with typed fixed arguments; the exe and
    #          starttime are the verified ones
    argv = B.register_argv(B.parse_args(_args(name="t5-a")),
                           "/usr/bin/dbus-send", 4242, "/usr/bin/waypipe",
                           987654)
    assert argv == [
        "/usr/bin/dbus-send", "--system", "--print-reply",
        "--dest=org.qdistro.AdminBroker1", "/org/qdistro/AdminBroker1",
        "org.qdistro.AdminBroker1.RegisterLaunch",
        "string:vm-t5-a", "string:qdistro.tier5", "string:qdistro.tier5.t5-a",
        f"string:{TOKEN}", "string:/usr/bin/waypipe", "uint64:4242",
        "string:tier5", "uint64:987654"]


def test_bridge_starts_isolated_and_uses_fixed_absolute_paths():
    # ensures: python3 -I (no user site, no script-dir/cwd imports, no
    #          PYTHON* env) and absolute, fixed executables
    src = (ROOT / "tier5-vm/qdistro_tier5_bridge.py").read_text()
    assert src.splitlines()[0] == "#!/usr/bin/python3 -I"
    for p in (*B.RUNUSER_CANDIDATES, B.ENV_EXE, B.SECCTX_EXEC, B.WAYPIPE,
              B.DBUS_SEND):
        assert p.startswith("/usr/") and ".." not in p


def test_trusted_exe_refuses_files_a_user_could_replace(tmp_path):
    # ensures: a fixed path resolving into user-writable territory is
    #          refused before anything runs
    f = tmp_path / "waypipe"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    if os.geteuid() != 0:
        with pytest.raises(B.LaunchFailed):
            B.trusted_exe(str(f))
    assert B.trusted_exe("/usr/bin/env") == os.path.realpath("/usr/bin/env")


def test_invoker_must_be_root_via_pkexec_from_admin(monkeypatch):
    # ensures: the bridge acts only as root and only for the admin uid
    me = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(B, "ADMIN_USER", me.pw_name)
    monkeypatch.setattr(B.os, "geteuid", lambda: 1000)
    with pytest.raises(B.Refused):
        B.check_invoker({"PKEXEC_UID": str(me.pw_uid)})
    monkeypatch.setattr(B.os, "geteuid", lambda: 0)
    with pytest.raises(B.Refused):
        B.check_invoker({})
    with pytest.raises(B.Refused):
        B.check_invoker({"PKEXEC_UID": str(me.pw_uid + 1)})
    B.check_invoker({"PKEXEC_UID": str(me.pw_uid)})


def test_main_refuses_without_launching(monkeypatch, capsys):
    # ensures: a refused invocation starts no process at all
    monkeypatch.setattr(B.os, "chdir", lambda _p: None)
    monkeypatch.setattr(B.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("launched"))
    assert B.main(_args(name="../x")) == B.EXIT_REFUSED
    if os.geteuid() != 0:
        assert B.main(_args()) == B.EXIT_REFUSED
    assert "refused" in capsys.readouterr().err


# -- /proc verification -------------------------------------------------------

def _tree(tmp_path):
    """runuser-like parent R → secctx-like S → waypipe-like P (all real)."""
    leaf = tmp_path / "leaf.py"
    leaf.write_text("import time\ntime.sleep(60)\n")
    mid = tmp_path / "mid.py"
    mid.write_text(
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, {str(leaf)!r}])\n"
        f"open({str(tmp_path / 'leafpid')!r}, 'w').write(str(p.pid))\n"
        "p.wait()\n")
    r = subprocess.Popen([sys.executable, "-c",
                          ("import subprocess, sys\n"
                           f"subprocess.run([sys.executable, {str(mid)!r}])")])
    deadline = time.monotonic() + 10
    while not (tmp_path / "leafpid").exists() \
            or not (tmp_path / "leafpid").read_text():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    time.sleep(0.2)
    return r, int((tmp_path / "leafpid").read_text())


def test_verify_chain_accepts_only_our_launched_child(tmp_path):
    # ensures: the reported pid must be the grandchild of OUR runuser, run
    #          as admin, exec'd into the expected exe — foreign, stale,
    #          wrong-exe, wrong-uid and wrong-parent reports are refused
    exe = os.path.realpath(sys.executable)
    me = os.getuid()
    r, leaf = _tree(tmp_path)
    try:
        start = B.verify_chain(leaf, r.pid, me, exe, exe)
        assert start == B.proc_starttime(leaf) > 0
        bad = [
            (os.getpid(), r.pid, me, exe, exe),       # foreign pid
            (leaf, os.getpid(), me, exe, exe),        # not under our runuser
            (leaf, r.pid, me + 1, exe, exe),          # not admin
            (leaf, r.pid, me, exe, "/usr/bin/waypipe"),   # wrong exe
            (leaf, r.pid, me, "/usr/bin/qdistro-secctx-exec", exe),
            (2 ** 22 + 7, r.pid, me, exe, exe),       # no such pid
        ]
        for args in bad:
            with pytest.raises(B.LaunchFailed):
                B.verify_chain(*args)
        dead = subprocess.Popen(["true"])
        dead.wait()
        with pytest.raises(B.LaunchFailed):
            B.verify_chain(dead.pid, r.pid, me, exe, exe)
    finally:
        r.kill()
        os.kill(leaf, signal.SIGKILL)
        r.wait()


# -- the launch chain, end to end ------------------------------------------------

_RUNUSER = """#!{py}
import os, signal, subprocess, sys
a = sys.argv[1:]
assert a[:3] == ["-u", {user!r}, "--"], a
p = subprocess.Popen(a[3:], close_fds=False)
signal.signal(signal.SIGTERM, lambda *_: p.send_signal(signal.SIGTERM))
rc = p.wait()
sys.exit(128 - rc if rc < 0 else rc)
"""

_SECCTX = """#!{py}
import json, os, signal, subprocess, sys, time
S = {state!r}
def knob(n):
    p = os.path.join(S, n)
    return open(p).read() if os.path.exists(p) else None
json.dump({{"env": dict(os.environ), "argv": sys.argv}},
          open(os.path.join(S, "secctx.json"), "w"))
fd = int(os.environ["QDISTRO_LAUNCH_REPORT_FD"])
os.set_inheritable(fd, False)
inner = sys.argv[sys.argv.index("--") + 1:]
if knob("exec-fails") is not None:
    os.close(fd)
    sys.exit(127)
p = subprocess.Popen(inner)
signal.signal(signal.SIGTERM, lambda *_: p.send_signal(signal.SIGTERM))
time.sleep(float(knob("report-delay") or 0.3))
rep = knob("report")
if rep == "parent":
    pid = os.getppid()
elif rep == "stale":
    d = subprocess.Popen(["true"]); d.wait(); pid = d.pid
elif rep == "garbage":
    pid = "12x"
else:
    pid = p.pid
os.write(fd, f"{{pid}}\\n".encode())
os.close(fd)
rc = p.wait()
sys.exit(128 - rc if rc < 0 else rc)
"""

_WAYPIPE = """#!{py}
import os, signal, sys, time
S = {state!r}
fds = {{}}
for fd in os.listdir("/proc/self/fd"):
    try:
        fds[fd] = os.readlink(f"/proc/self/fd/{{fd}}")
    except OSError:
        pass
open(os.path.join(S, "waypipe.fds"), "w").write(repr(fds))
open(os.path.join(S, "waypipe.pid"), "w").write(str(os.getpid()))
# waypipe forks one child per connection; this one outlives its parent and
# shrugs off SIGTERM, so only a group-wide teardown with a SIGKILL reaches it
if os.path.exists(os.path.join(S, "conn-child")) and os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open(os.path.join(S, "conn.pid"), "w").write(str(os.getpid()))
    while True:
        time.sleep(1)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(1)
"""

_DBUS_SEND = """#!{py}
import json, os, sys
S = {state!r}
json.dump(sys.argv[1:], open(os.path.join(S, "register.json"), "w"))
sys.exit(int(open(os.path.join(S, "broker-rc")).read()))
"""


class Chain:
    def __init__(self, tmp_path, monkeypatch):
        self.state = tmp_path / "state"
        self.state.mkdir()
        (self.state / "broker-rc").write_text("0")
        me = pwd.getpwuid(os.getuid())
        stubs = {}
        for name, body in (("runuser", _RUNUSER), ("secctx", _SECCTX),
                           ("waypipe", _WAYPIPE), ("dbus-send", _DBUS_SEND)):
            p = tmp_path / name
            p.write_text(body.format(py=sys.executable, user=me.pw_name,
                                     state=str(self.state)))
            p.chmod(0o755)
            stubs[name] = str(p)
        monkeypatch.setattr(B, "ADMIN_USER", me.pw_name)
        monkeypatch.setattr(B, "RUNUSER_CANDIDATES", (stubs["runuser"],))
        monkeypatch.setattr(B, "SECCTX_EXEC", stubs["secctx"])
        monkeypatch.setattr(B, "WAYPIPE", stubs["waypipe"])
        monkeypatch.setattr(B, "DBUS_SEND", stubs["dbus-send"])
        monkeypatch.setattr(B, "REPORT_TIMEOUT_S", 10.0)
        monkeypatch.setattr(B, "TEARDOWN_GRACE_S", 2.0)
        py = os.path.realpath(sys.executable)
        # stand-ins are scripts: /proc/<pid>/exe is the interpreter
        stub_paths = (stubs["secctx"], stubs["waypipe"])
        monkeypatch.setattr(B, "trusted_exe", lambda p: py
                            if p in stub_paths else os.path.realpath(p))
        self.admin = (me.pw_uid, me.pw_gid, me.pw_dir)
        self.stdin_r, self.stdin_w = os.pipe()
        self.out = io.StringIO()
        self.rc = None
        self.thread = None

    def knob(self, name, value=""):
        (self.state / name).write_text(value)

    def start(self, launch=None):
        self.bridge = B.Bridge(launch or B.parse_args(_args(name="t5-a")),
                               stdin_fd=self.stdin_r, stdout=self.out,
                               admin=self.admin)
        self.thread = threading.Thread(
            target=lambda: setattr(self, "rc", self.bridge.run()))
        self.thread.start()
        return self

    def wait_ready(self, timeout=15):
        deadline = time.monotonic() + timeout
        while "READY" not in self.out.getvalue():
            assert self.thread.is_alive(), f"bridge exited rc={self.rc}"
            assert time.monotonic() < deadline, "no READY"
            time.sleep(0.02)
        return int(self.out.getvalue().split()[1])

    def caller_exits(self):
        os.close(self.stdin_w)
        self.stdin_w = None

    def join(self, timeout=20):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "bridge did not exit"
        return self.rc

    def close(self):
        if self.stdin_w is not None:
            os.close(self.stdin_w)
        if self.thread is not None:
            self.thread.join(20)
        os.close(self.stdin_r)


@pytest.fixture
def chain(tmp_path, monkeypatch):
    c = Chain(tmp_path, monkeypatch)
    yield c
    c.close()


def _gone(pid, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().split(") ")[1][0] == "Z":
                    return True
        except OSError:
            return True
        time.sleep(0.02)
    return False


def test_launch_registers_the_verified_client_then_tears_down_on_eof(chain):
    # ensures: the pid reported over the private pipe is verified and
    #          registered with the verified exe/starttime before READY, the
    #          chain gets exactly the constructed env and no report fd, and
    #          caller EOF removes every helper
    chain.start()
    pid = chain.wait_ready()
    assert pid == int((chain.state / "waypipe.pid").read_text())
    reg = json.loads((chain.state / "register.json").read_text())
    assert reg[5:9] == ["string:vm-t5-a", "string:qdistro.tier5",
                        "string:qdistro.tier5.t5-a", f"string:{TOKEN}"]
    assert reg[10] == f"uint64:{pid}"
    assert reg[12] == f"uint64:{B.proc_starttime(pid)}"
    seen = json.loads((chain.state / "secctx.json").read_text())
    env = dict(seen["env"])
    env.pop("QDISTRO_LAUNCH_REPORT_FD")
    assert env == {k: v for k, v in B.chain_env(
        chain.admin[0], chain.admin[2], 0).items()
        if k != "QDISTRO_LAUNCH_REPORT_FD"}
    # the report pipe stays with secctx-exec: waypipe inherits no pipe
    # beyond stdio
    fds = ast.literal_eval((chain.state / "waypipe.fds").read_text())
    assert not [t for fd, t in fds.items()
                if int(fd) > 2 and t.startswith("pipe:")], fds
    secctx_pid = B.proc_status(pid)[0]
    chain.caller_exits()
    assert chain.join() in (0, 143)
    assert _gone(pid) and _gone(secctx_pid)


def test_broker_refusal_aborts_the_launch(chain):
    # ensures: a failed RegisterLaunch never leaves an untagged/unregistered
    #          waypipe running and never reports READY
    chain.knob("broker-rc", "1")
    chain.start()
    assert chain.join() == B.EXIT_REGISTER
    assert "READY" not in chain.out.getvalue()
    assert _gone(int((chain.state / "waypipe.pid").read_text()))


def test_exec_failure_reports_nothing_and_registers_nothing(chain):
    chain.knob("exec-fails")
    chain.start()
    assert chain.join() == B.EXIT_REPORT
    assert not (chain.state / "register.json").exists()


@pytest.mark.parametrize("report", ["parent", "stale", "garbage"])
def test_foreign_stale_or_garbage_report_is_refused(chain, report):
    # ensures: a reported pid that is not the chain's own exec'd waypipe is
    #          never registered, and the chain is torn down
    chain.knob("report", report)
    chain.start()
    assert chain.join() in (B.EXIT_VERIFY, B.EXIT_REPORT)
    assert not (chain.state / "register.json").exists()
    assert _gone(int((chain.state / "waypipe.pid").read_text()))


def test_caller_eof_during_launch_cancels(chain):
    # ensures: a caller that dies or cancels before READY takes the
    #          half-started chain with it
    chain.knob("report-delay", "8")
    chain.start()
    deadline = time.monotonic() + 10
    while not (chain.state / "waypipe.pid").exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    t0 = time.monotonic()
    chain.caller_exits()
    assert chain.join() == B.EXIT_CANCELLED
    assert time.monotonic() - t0 < 6
    assert not (chain.state / "register.json").exists()
    assert _gone(int((chain.state / "waypipe.pid").read_text()))


def test_chain_exit_is_reported_and_reaped(chain):
    # ensures: when the waypipe client exits on its own (loopback --oneshot),
    #          the bridge exits with its status and leaves nothing behind
    chain.start(B.parse_args(_args(mode="--loopback", name="loopback-9")))
    pid = chain.wait_ready()
    os.kill(pid, signal.SIGTERM)
    assert chain.join() == 0
    assert _gone(pid)


def _conn_pid(chain, timeout=5):
    f = chain.state / "conn.pid"
    deadline = time.monotonic() + timeout
    while not f.exists():
        assert time.monotonic() < deadline, "no connection child"
        time.sleep(0.02)
    return int(f.read_text())


def test_caller_eof_reaps_waypipe_connection_children(chain):
    # ensures: teardown reaches the whole chain process group — a per-
    #          connection waypipe child that ignores SIGTERM is not left
    #          holding a tagged compositor connection
    chain.knob("conn-child")
    chain.start()
    chain.wait_ready()
    conn = _conn_pid(chain)
    chain.caller_exits()
    chain.join()
    assert _gone(conn)


def test_chain_exit_reaps_leftover_connection_children(chain):
    # ensures: a waypipe client that exits on its own does not leave its
    #          connection children behind either
    chain.knob("conn-child")
    chain.start(B.parse_args(_args(mode="--loopback", name="loopback-9")))
    pid = chain.wait_ready()
    conn = _conn_pid(chain)
    os.kill(pid, signal.SIGTERM)
    chain.join()
    assert _gone(conn)
