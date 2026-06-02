"""qdistro-print-proxy integration test — exercises the AF_UNIX listener
end-to-end with a stub backend cupsd.

Spawns the proxy as a subprocess pointed at QDISTRO_PRINT_BACKEND=unix,
QDISTRO_PRINT_UNIX_PATH=<test cups stub>, opens a client connection
through the proxy's listen socket, sends a request, and confirms the
backend received it byte-for-byte.

Pure phase-9 §step 0 transport check; no IPP parsing, just bytes.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

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


def _start_stub_backend(path: str) -> tuple[socket.socket, list[bytes]]:
    """Bind a UNIX listener that records the first chunk it receives."""
    if os.path.exists(path):
        os.unlink(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(1)
    return s, []


def test_proxy_forwards_bytes_unix_backend(tmp_path):
    listen = str(tmp_path / "ipp.sock")
    backend = str(tmp_path / "cupsd.sock")
    listen_dir = str(tmp_path)

    backend_sock, _ = _start_stub_backend(backend)

    env = os.environ.copy()
    env["QDISTRO_PRINT_LISTEN"] = listen
    env["QDISTRO_PRINT_BACKEND"] = "unix"
    env["QDISTRO_PRINT_UNIX_PATH"] = backend
    # This test exercises byte-forwarding mechanics, not the broker gate;
    # use the explicit dev opt-out so the (absent) broker doesn't deny.
    env["QDISTRO_PRINT_GATE_REQUIRED"] = "0"
    proxy = subprocess.Popen(
        [sys.executable, PROXY_PY],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_for_socket(listen)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(listen)

        # Backend should accept right after the proxy forwards.
        backend_sock.settimeout(5.0)
        backend_conn, _ = backend_sock.accept()
        backend_conn.settimeout(5.0)

        client.sendall(b"PING-TO-CUPSD")
        got = backend_conn.recv(64)
        assert got == b"PING-TO-CUPSD"

        # Backend → client direction.
        backend_conn.sendall(b"PONG-FROM-CUPSD")
        client.settimeout(5.0)
        reply = client.recv(64)
        assert reply == b"PONG-FROM-CUPSD"

        client.close()
        backend_conn.close()
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proxy.kill()
        backend_sock.close()


def test_proxy_listen_socket_perm_660(tmp_path):
    listen = str(tmp_path / "ipp.sock")
    backend = str(tmp_path / "cupsd.sock")

    backend_sock, _ = _start_stub_backend(backend)

    env = os.environ.copy()
    env["QDISTRO_PRINT_LISTEN"] = listen
    env["QDISTRO_PRINT_BACKEND"] = "unix"
    env["QDISTRO_PRINT_UNIX_PATH"] = backend
    proxy = subprocess.Popen(
        [sys.executable, PROXY_PY],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_for_socket(listen)
        mode = os.stat(listen).st_mode & 0o777
        assert mode == 0o660
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proxy.kill()
        backend_sock.close()


def test_proxy_handles_backend_unreachable(tmp_path):
    """If the backend is dead, the proxy should accept and immediately
    close (rather than hang the listener)."""
    listen = str(tmp_path / "ipp.sock")
    backend = str(tmp_path / "definitely-absent.sock")

    env = os.environ.copy()
    env["QDISTRO_PRINT_LISTEN"] = listen
    env["QDISTRO_PRINT_BACKEND"] = "unix"
    env["QDISTRO_PRINT_UNIX_PATH"] = backend
    # Isolate the backend-unreachable behavior from the broker gate.
    env["QDISTRO_PRINT_GATE_REQUIRED"] = "0"
    proxy = subprocess.Popen(
        [sys.executable, PROXY_PY],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait_for_socket(listen)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(listen)
        client.settimeout(2.0)
        # Should get EOF quickly because backend connect failed.
        data = client.recv(64)
        assert data == b""
        client.close()
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proxy.kill()
