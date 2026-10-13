"""qdistro-secctx-exec's launch report (QDISTRO_LAUNCH_REPORT_FD), executed.

The tier-5 bridge learns the waypipe client's pid from secctx-exec over a
private pipe and verifies it against /proc. That is only sound if the pid is
reported AFTER the inner command exec'd (before, /proc shows secctx-exec's
own forked copy), if a failed exec reports nothing, and if the inner command
never inherits the pipe. This compiles the real wrapper and runs it against
a minimal in-process Wayland server that offers just
wp_security_context_manager_v1.
"""
from __future__ import annotations

import array
import os
import shutil
import socket
import struct
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "daemons/secctx-exec/qdistro-secctx-exec.c"

pytestmark = pytest.mark.slow


def _pkg(*args):
    return subprocess.run(["pkg-config", *args], capture_output=True,
                          text=True, check=False)


@pytest.fixture(scope="module")
def secctx_exec(tmp_path_factory):
    if not (shutil.which("gcc") and shutil.which("wayland-scanner")
            and shutil.which("pkg-config")
            and _pkg("--exists", "wayland-client",
                     "wayland-protocols").returncode == 0):
        pytest.skip("gcc/wayland-scanner/wayland-client not available")
    out = tmp_path_factory.mktemp("secctx")
    xml = (_pkg("--variable=pkgdatadir", "wayland-protocols").stdout.strip()
           + "/staging/security-context/security-context-v1.xml")
    hdr = out / "security-context-v1-client-protocol.h"
    code = out / "security-context-v1-protocol.c"
    subprocess.run(["wayland-scanner", "client-header", xml, str(hdr)],
                   check=True)
    subprocess.run(["wayland-scanner", "private-code", xml, str(code)],
                   check=True)
    exe = out / "qdistro-secctx-exec"
    flags = _pkg("--cflags", "--libs", "wayland-client").stdout.split()
    subprocess.run(["gcc", "-std=gnu11", "-O1", f"-I{out}", str(SRC),
                    str(code), "-o", str(exe), *flags], check=True)
    return exe


# -- a Wayland server that knows four interfaces --------------------------------

def _str(data, off):
    n = struct.unpack_from("=I", data, off)[0]
    s = data[off + 4:off + 4 + n - 1].decode()
    return s, off + 4 + ((n + 3) & ~3)


def _enc_str(s):
    b = s.encode() + b"\0"
    return struct.pack("=I", len(b)) + b + b"\0" * ((4 - len(b) % 4) % 4)


class FakeCompositor:
    """Serves wl_display/wl_registry/wl_callback and the security-context
    manager, recording each committed context."""

    def __init__(self, runtime: Path):
        self.path = runtime / "wayland-1"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        self.sock.listen(4)
        self.contexts: list[dict] = []
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,),
                             daemon=True).start()

    def _client(self, conn):
        objs = {1: "display"}
        ctx: dict = {}
        buf = b""
        fds: list[int] = []

        def send(obj, op, payload=b""):
            conn.sendall(struct.pack("=II", obj, ((8 + len(payload)) << 16)
                                     | op) + payload)

        while True:
            try:
                fdbuf = array.array("i")
                data, anc, _f, _a = conn.recvmsg(
                    4096, socket.CMSG_SPACE(16 * fdbuf.itemsize))
            except OSError:
                return
            if not data:
                for fd in fds:
                    os.close(fd)
                return
            for level, typ, cdata in anc:
                if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                    fdbuf.frombytes(cdata[:len(cdata) - len(cdata)
                                          % fdbuf.itemsize])
                    fds.extend(fdbuf)
            buf += data
            while len(buf) >= 8:
                obj, word = struct.unpack_from("=II", buf)
                size, op = word >> 16, word & 0xffff
                if len(buf) < size:
                    break
                msg, buf = buf[8:size], buf[size:]
                kind = objs.get(obj)
                if kind == "display" and op == 0:      # sync
                    cb = struct.unpack_from("=I", msg)[0]
                    send(cb, 0, struct.pack("=I", 0))
                    send(1, 1, struct.pack("=I", cb))
                elif kind == "display" and op == 1:    # get_registry
                    reg = struct.unpack_from("=I", msg)[0]
                    objs[reg] = "registry"
                    send(reg, 0, struct.pack("=I", 1)
                         + _enc_str("wp_security_context_manager_v1")
                         + struct.pack("=I", 1))
                elif kind == "registry" and op == 0:   # bind
                    _name = struct.unpack_from("=I", msg)[0]
                    _iface, off = _str(msg, 4)
                    _ver, new = struct.unpack_from("=II", msg, off)
                    objs[new] = "manager"
                elif kind == "manager" and op == 1:    # create_listener
                    new = struct.unpack_from("=I", msg)[0]
                    objs[new] = "context"
                    ctx = {}
                elif kind == "context" and op in (1, 2, 3):
                    key = {1: "engine", 2: "app_id", 3: "instance_id"}[op]
                    ctx[key] = _str(msg, 0)[0]
                elif kind == "context" and op == 4:    # commit
                    self.contexts.append(dict(ctx))

    def close(self):
        self._stop = True
        self.sock.close()


@pytest.fixture
def compositor(tmp_path):
    rt = tmp_path / "run"
    rt.mkdir(mode=0o700)
    c = FakeCompositor(rt)
    yield c
    c.close()


def _run(exe, compositor, inner, report=True, extra_env=None):
    rt = compositor.path.parent
    r, w = os.pipe()
    env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(rt),
           "WAYLAND_DISPLAY": "wayland-1",
           "QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED": "1"}
    pass_fds = ()
    if report:
        env["QDISTRO_LAUNCH_REPORT_FD"] = str(w)
        pass_fds = (w,)
    env.update(extra_env or {})
    p = subprocess.Popen(
        [str(exe), "--sandbox-engine", "qdistro.tier5",
         "--app-id", "qdistro.tier5.t5-a", "--instance-id", "a" * 32,
         "--", *inner],
        env=env, pass_fds=pass_fds, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        reported = f.read()
    out, err = p.communicate(timeout=30)
    return p, reported, out, err


def test_reports_a_pid_and_keeps_the_pipe_private(
        secctx_exec, compositor, tmp_path):
    # ensures: one "<pid>\n" report for a tagged launch, and the inner
    #          command does not hold the report pipe
    probe = tmp_path / "probe.sh"
    probe.write_text(
        "#!/bin/sh\n"
        f"ls -l /proc/$$/fd > {tmp_path}/fds\n")
    probe.chmod(0o755)
    p, reported, _out, err = _run(secctx_exec, compositor, [str(probe)])
    assert p.returncode == 0, err
    assert reported.endswith(b"\n") and reported[:-1].isdigit(), reported
    assert compositor.contexts == [{
        "engine": "qdistro.tier5", "app_id": "qdistro.tier5.t5-a",
        "instance_id": "a" * 32}]
    leaked = [ln for ln in (tmp_path / "fds").read_text().splitlines()
              if " -> pipe:" in ln
              and int(ln.split(" -> ")[0].split()[-1]) > 2]
    assert leaked == []


def test_reported_pid_names_the_exec_d_process(secctx_exec, compositor):
    # ensures: the reported pid's exe is the inner command at report time
    r, w = os.pipe()
    rt = compositor.path.parent
    p = subprocess.Popen(
        [str(secctx_exec), "--sandbox-engine", "qdistro.tier5",
         "--app-id", "qdistro.tier5.t5-a", "--", "/usr/bin/sleep", "5"],
        env={"PATH": "/usr/bin", "XDG_RUNTIME_DIR": str(rt),
             "QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED": "1",
             "QDISTRO_LAUNCH_REPORT_FD": str(w)},
        pass_fds=(w,), stderr=subprocess.PIPE)
    os.close(w)
    try:
        line = b""
        while not line.endswith(b"\n"):
            chunk = os.read(r, 32)
            assert chunk, "no report"
            line += chunk
        pid = int(line)
        assert os.readlink(f"/proc/{pid}/exe") \
            == os.path.realpath("/usr/bin/sleep")
        with open(f"/proc/{pid}/status") as f:
            ppid = next(int(x.split()[1]) for x in f if x.startswith("PPid"))
        assert ppid == p.pid
    finally:
        os.close(r)
        p.terminate()
        p.wait(10)


def test_failed_exec_reports_nothing(secctx_exec, compositor):
    # ensures: an inner command that cannot exec yields EOF without a pid,
    #          so the launcher has nothing to register
    p, reported, _out, err = _run(secctx_exec, compositor,
                                  ["/nonexistent/waypipe"])
    assert p.returncode == 127, err
    assert reported == b""


def test_malformed_report_fd_refuses_the_launch(secctx_exec, compositor):
    # ensures: a launcher that asked for a report never gets a silent,
    #          untracked launch
    for bad in ("x", "2", "-1", "99"):
        p, _rep, _out, err = _run(secctx_exec, compositor, ["/bin/true"],
                                  report=False,
                                  extra_env={"QDISTRO_LAUNCH_REPORT_FD": bad})
        assert p.returncode == 2, (bad, err)
    assert compositor.contexts == []


def test_no_report_requested_still_launches(secctx_exec, compositor):
    # ensures: tier-1..4 launches (no report fd) are unchanged
    p, reported, _out, err = _run(secctx_exec, compositor, ["/bin/true"],
                                  report=False)
    assert p.returncode == 0, err
    assert reported == b""
