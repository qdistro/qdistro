"""qdistro-print-proxy gate + spawn-on-demand tests.

Drives the proxy as a subprocess with the gate / spawn env vars set
to exercise the broker.CheckPermission and spawn-on-demand paths.

The broker is mocked by pointing PYTHONPATH at a tiny shim that
intercepts the import; for "spawn helper", we set
QDISTRO_PRINT_VM_SPAWN to a script that records its invocation.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import textwrap

import pytest

PROXY_PY = os.path.join(
    os.path.dirname(__file__), "..", "..", "print", "qdistro_print_proxy.py")


def _wait_for_socket(path: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.05)
    raise TimeoutError(f"socket did not appear: {path}")


def _start_stub_backend(path: str) -> socket.socket:
    if os.path.exists(path):
        os.unlink(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(1)
    return s


def _start_proxy(env: dict, stderr=subprocess.STDOUT):
    return subprocess.Popen(
        [sys.executable, PROXY_PY],
        env=env, stdout=subprocess.PIPE, stderr=stderr)


def _stop_proxy(p):
    p.terminate()
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        p.kill()


# -- gate disabled by default ------------------------------------------------

def test_gate_disabled_by_default_forwards_normally(tmp_path):
    listen = str(tmp_path / "ipp.sock")
    backend = str(tmp_path / "cupsd.sock")
    backend_sock = _start_stub_backend(backend)
    env = os.environ.copy()
    env["QDISTRO_PRINT_LISTEN"] = listen
    env["QDISTRO_PRINT_BACKEND"] = "unix"
    env["QDISTRO_PRINT_UNIX_PATH"] = backend
    # No QDISTRO_PRINT_GATE_REQUIRED -> default 0.
    proxy = _start_proxy(env)
    try:
        _wait_for_socket(listen)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(listen)
        backend_sock.settimeout(5.0)
        bc, _ = backend_sock.accept()
        c.sendall(b"X")
        assert bc.recv(8) == b"X"
        c.close()
        bc.close()
    finally:
        _stop_proxy(proxy)
        backend_sock.close()


# -- gate enabled with broker missing fails closed ---------------------------

def test_gate_required_with_broker_unreachable_fails_closed(tmp_path):
    """When the gate is required and the broker is unreachable, the
    proxy should refuse the connection (gate-error → deny)."""
    listen = str(tmp_path / "ipp.sock")
    backend = str(tmp_path / "cupsd.sock")
    backend_sock = _start_stub_backend(backend)
    env = os.environ.copy()
    env["QDISTRO_PRINT_LISTEN"] = listen
    env["QDISTRO_PRINT_BACKEND"] = "unix"
    env["QDISTRO_PRINT_UNIX_PATH"] = backend
    env["QDISTRO_PRINT_GATE_REQUIRED"] = "1"
    # Force broker call to fail by pointing at a bus address that
    # doesn't exist. dbus will raise an exception immediately.
    env["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/nonexistent/dbus"
    proxy = _start_proxy(env)
    try:
        _wait_for_socket(listen)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(listen)
        c.settimeout(5.0)
        # Backend should NOT receive anything; backend.accept times out.
        backend_sock.settimeout(0.5)
        with pytest.raises(socket.timeout):
            backend_sock.accept()
        # Client gets EOF.
        data = c.recv(64)
        assert data == b""
        c.close()
    finally:
        _stop_proxy(proxy)
        backend_sock.close()


# -- spawn helper invoked when backend unreachable ---------------------------

def test_spawn_helper_invoked_for_vsock_backend_unreachable(tmp_path):
    """vsock backend unreachable + QDISTRO_PRINT_VM_SPAWN set → helper
    runs once. We can't easily simulate vsock failure on the host, so
    we exercise the function directly via a subprocess that calls
    `_open_backend_with_spawn` with a mocked backend that always raises
    ECONNREFUSED."""
    spawn_marker = tmp_path / "spawned"
    spawn_helper = tmp_path / "spawn.sh"
    spawn_helper.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo invoked > {spawn_marker}
        exit 0
    """))
    spawn_helper.chmod(0o755)

    # Run an inline Python that imports the module and exercises the
    # spawn path with backend forced to vsock + a mocked _open_backend.
    code = textwrap.dedent(f"""\
        import errno, importlib, os, sys
        os.environ['QDISTRO_PRINT_BACKEND'] = 'vsock'
        os.environ['QDISTRO_PRINT_VM_SPAWN'] = {str(spawn_helper)!r}
        os.environ['QDISTRO_PRINT_SPAWN_BACKOFF_S'] = '0.01'
        sys.path.insert(0, {os.path.join(os.path.dirname(PROXY_PY))!r})
        import qdistro_print_proxy as p
        importlib.reload(p)
        calls = {{'n': 0}}
        def fake_open():
            calls['n'] += 1
            raise OSError(errno.ECONNREFUSED, 'vsock refused')
        p._open_backend = fake_open
        try:
            p._open_backend_with_spawn()
        except OSError:
            pass
        # spawn ran once; backend retried (so calls == 2).
        assert calls['n'] == 2, calls
        print('OK')
    """)
    rc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True)
    assert rc.returncode == 0, (rc.stdout, rc.stderr)
    assert spawn_marker.exists()


def test_spawn_helper_skipped_for_non_vsock_backend(tmp_path):
    """Non-vsock backends never trigger the spawn helper even when set."""
    spawn_marker = tmp_path / "spawned"
    spawn_helper = tmp_path / "spawn.sh"
    spawn_helper.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        echo invoked > {spawn_marker}
        exit 0
    """))
    spawn_helper.chmod(0o755)
    code = textwrap.dedent(f"""\
        import errno, importlib, os, sys
        os.environ['QDISTRO_PRINT_BACKEND'] = 'unix'
        os.environ['QDISTRO_PRINT_UNIX_PATH'] = '/dev/null/nope'
        os.environ['QDISTRO_PRINT_VM_SPAWN'] = {str(spawn_helper)!r}
        sys.path.insert(0, {os.path.join(os.path.dirname(PROXY_PY))!r})
        import qdistro_print_proxy as p
        importlib.reload(p)
        try:
            p._open_backend_with_spawn()
        except OSError:
            pass
        print('OK')
    """)
    rc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True)
    assert rc.returncode == 0, (rc.stdout, rc.stderr)
    assert not spawn_marker.exists()


def test_gate_function_returns_disabled_when_off(monkeypatch):
    """Direct unit-style check on the _gate helper without spawning the
    proxy subprocess."""
    monkeypatch.delenv("QDISTRO_PRINT_GATE_REQUIRED", raising=False)
    sys.path.insert(0, os.path.join(os.path.dirname(PROXY_PY)))
    try:
        import importlib
        import qdistro_print_proxy as p
        importlib.reload(p)
        allowed, reason = p._gate(uid=1500, pid=12345)
        assert allowed is True
        assert reason == "gate-disabled"
    finally:
        sys.path.pop(0)


def test_spawn_helper_script_handles_missing_virsh(tmp_path):
    """The spawn helper itself: when virsh is missing, exit 1 with
    a SKIP message — no crash, no infinite retry from the proxy side."""
    helper = os.path.join(
        os.path.dirname(PROXY_PY), "spawn-print-vm.sh")
    assert os.path.exists(helper)
    # Run with a PATH that excludes virsh.
    env = {"PATH": "/nonexistent"}
    rc = subprocess.run([helper], env=env,
                        capture_output=True, text=True, timeout=5)
    assert rc.returncode == 1
    assert "SKIP" in rc.stderr or "not installed" in rc.stderr


def test_spawn_helper_skips_when_domain_missing(tmp_path):
    """When the qdistro-print domain isn't defined, helper exits 0
    cleanly with a SKIP banner. Only meaningful if virsh is available;
    skip otherwise."""
    import shutil
    if shutil.which("virsh") is None:
        pytest.skip("virsh not installed on host")
    helper = os.path.join(
        os.path.dirname(PROXY_PY), "spawn-print-vm.sh")
    env = os.environ.copy()
    env["QDISTRO_PRINT_VM_NAME"] = "this-domain-definitely-does-not-exist"
    rc = subprocess.run([helper], env=env,
                        capture_output=True, text=True, timeout=10)
    assert rc.returncode == 0
    assert "SKIP" in rc.stderr or "not defined" in rc.stderr
