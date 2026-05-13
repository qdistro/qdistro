"""qdistro-print-proxy + audit integration test.

Spins the proxy under a per-test audit DB, sends a single connection,
and asserts the audit rows match the connect+close lifecycle.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

from qdistro_print_audit import PrintAuditLog


REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROXY = os.path.join(REPO, "print", "qdistro_print_proxy.py")


def _wait_for_socket(path: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.05)
    raise TimeoutError(f"socket {path} never appeared")


def _start_stub_backend(path: str) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(8)
    return s


def _start_proxy(env: dict):
    return subprocess.Popen(
        [sys.executable, PROXY], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _stop(p):
    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def test_audit_rows_recorded_for_allowed_connection(tmp_path):
    listen = str(tmp_path / "ipp.sock")
    backend_path = str(tmp_path / "cupsd.sock")
    audit_db = str(tmp_path / "print_audit.sqlite")
    bsock = _start_stub_backend(backend_path)

    env = os.environ.copy()
    env["QDISTRO_PRINT_LISTEN"] = listen
    env["QDISTRO_PRINT_BACKEND"] = "unix"
    env["QDISTRO_PRINT_UNIX_PATH"] = backend_path
    env["QDISTRO_PRINT_AUDIT_DB"] = audit_db
    # Make sure the proxy can find the audit module — print/
    # is on sys.path via the conftest, but the subprocess inherits a
    # fresh PYTHONPATH so the proxy needs an explicit hint.
    env["PYTHONPATH"] = (os.path.join(REPO, "print")
                         + os.pathsep + env.get("PYTHONPATH", ""))

    proxy = _start_proxy(env)
    try:
        _wait_for_socket(listen)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(listen)
        bsock.settimeout(5.0)
        bc, _ = bsock.accept()
        c.sendall(b"hello")
        assert bc.recv(64) == b"hello"
        c.close()
        bc.close()
        # Give the proxy a moment to record the close audit row.
        time.sleep(0.3)
    finally:
        _stop(proxy)
        bsock.close()

    audit = PrintAuditLog(audit_db)
    rows = audit.tail(10)
    ops = [r["op"] for r in rows]
    # Ordered LIFO: close, connect.
    assert "connect" in ops, f"expected a connect row, got {ops}"
    assert "close" in ops, f"expected a close row, got {ops}"
    decisions = {r["op"]: r["decision"] for r in rows}
    assert decisions["connect"] == "allow"


def test_audit_records_deny_on_gate_error(tmp_path):
    listen = str(tmp_path / "ipp.sock")
    backend_path = str(tmp_path / "cupsd.sock")
    audit_db = str(tmp_path / "print_audit.sqlite")
    bsock = _start_stub_backend(backend_path)

    env = os.environ.copy()
    env["QDISTRO_PRINT_LISTEN"] = listen
    env["QDISTRO_PRINT_BACKEND"] = "unix"
    env["QDISTRO_PRINT_UNIX_PATH"] = backend_path
    env["QDISTRO_PRINT_AUDIT_DB"] = audit_db
    # Force the broker gate ON with broker unreachable -> deny.
    env["QDISTRO_PRINT_GATE_REQUIRED"] = "1"
    env["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=/nonexistent/dbus"
    env["PYTHONPATH"] = (os.path.join(REPO, "print")
                         + os.pathsep + env.get("PYTHONPATH", ""))

    proxy = _start_proxy(env)
    try:
        _wait_for_socket(listen)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(listen)
        # Proxy should close us before any byte is forwarded.
        c.settimeout(5.0)
        try:
            data = c.recv(64)
        except OSError:
            data = b""
        assert data == b""
        c.close()
        time.sleep(0.3)
    finally:
        _stop(proxy)
        bsock.close()

    audit = PrintAuditLog(audit_db)
    rows = audit.tail(5)
    assert rows, "expected at least one audit row"
    deny = [r for r in rows if r["decision"] == "deny"]
    assert deny, f"expected a deny row, got {[r['decision'] for r in rows]}"
    assert "gate" in deny[0]["reason"]
