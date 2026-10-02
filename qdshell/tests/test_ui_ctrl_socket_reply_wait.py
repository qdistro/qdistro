"""The UI harness must wait for a SLOW ctrl-socket reply, not return ''.

qdshell answers `capture` only after the capture completes, and its own pump
deadline is kCaptureTimeoutMs (8 s, qml-plugin/qdwin-binding.cpp). socat's
`-t` is how long it keeps reading after the request line hits EOF; with the
old `-t 2` a reply that took longer than 2 s was dropped and socat still
exited 0, so `screenshot_vm` raised `shell capture failed: ''`
(settings_sessionmenu, full-20260926T153217Z-3807077, 12 GUI VMs in parallel).

These tests run the REAL guest script `ctrl_socket_vm` builds, through a
local stand-in for vm-exec, against a UNIX socket server that replies late --
the widened timing window. Restoring `-t 2` makes
`test_capture_reply_slower_than_two_seconds_is_received` fail with ''.

Host-runnable: needs bash, base64 and socat (missing socat FAILS the
delayed-reply test); no VM, no compositor. Run by scripts/ci-local.sh.
"""

import re
import shutil
import socket
import threading
from pathlib import Path

import pytest

from tests.ui import runner

QDSHELL = Path(__file__).resolve().parents[1]


class SlowCtrlServer:
    """A qdshell.sock that reads one line, waits `delay` s, replies, closes."""

    def __init__(self, path: Path, delay: float, reply: str):
        self.delay = delay
        self.reply = reply
        self.requests: list[str] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            self.requests.append(buf.decode())
            threading.Event().wait(self.delay)
            # Like ctrl-server.cpp: write the reply, then disconnect.
            conn.sendall((self.reply + "\n").encode())

    def close(self):
        self.sock.close()


@pytest.fixture
def local_session(tmp_path, monkeypatch):
    # AF_UNIX paths are limited to ~108 bytes; pytest's tmp_path can exceed it.
    import tempfile
    rundir = Path(tempfile.mkdtemp(prefix="qdui-"))
    monkeypatch.setattr(runner, "VM_XDG_RUNTIME_DIR", str(rundir))
    # Stand-in for vm-exec: argv is [vm, guest_cmd]; run guest_cmd locally.
    session = runner.VMSession(
        vm="local", vm_exec=["bash", "-c", 'exec bash -c "$2"', "vm-exec-stub"],
        virsh=["false"])
    yield session, rundir
    shutil.rmtree(rundir, ignore_errors=True)


def test_capture_reply_slower_than_two_seconds_is_received(local_session):
    # A missing socat is a FAILURE, not a skip: a skipped run of this test
    # leaves the gate green with the regression unguarded.
    if shutil.which("socat") is None:
        pytest.fail("socat is required on the host to run the real ctrl-socket script")
    session, rundir = local_session
    reply = "ok output=Virtual-1 width=1280 height=800 path=/run/user/1000/x.png"
    srv = SlowCtrlServer(rundir / "qdshell.sock", delay=3.0, reply=reply)
    try:
        # A literal host deadline (not derived from CTRL_SOCAT_T) so this test
        # also runs -- and fails with the production '' -- against the old
        # `-t 2` code, which had no such constant.
        got = runner.ctrl_socket_vm(session, "capture Virtual-1 /run/user/1000/x.png",
                                    timeout=45.0)
    finally:
        srv.close()
    assert srv.requests == ["capture Virtual-1 /run/user/1000/x.png\n"]
    assert got == reply


def test_socat_reply_wait_outlasts_qdshell_capture_deadline():
    # Read the REAL deadline from the shell source, so raising it there without
    # raising the harness wait fails here instead of in a loaded CI run.
    src = (QDSHELL / "qml-plugin" / "qdwin-binding.cpp").read_text()
    m = re.search(r"constexpr int kCaptureTimeoutMs = (\d+);", src)
    assert m, "kCaptureTimeoutMs not found in qdwin-binding.cpp"
    assert runner.CTRL_SOCAT_T * 1000 > int(m.group(1))


def test_host_timeout_must_exceed_the_socat_wait(local_session):
    session, _ = local_session
    with pytest.raises(ValueError, match="must exceed CTRL_SOCAT_T"):
        runner.ctrl_socket_vm(session, "capture Virtual-1 /run/user/1000/x.png",
                              timeout=float(runner.CTRL_SOCAT_T))
