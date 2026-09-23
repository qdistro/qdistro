"""Minimal JSON-RPC client over the qdbrowser agent_control socket.

Used by scenario scripts that drive a running qdbrowser without
spinning up an MCP stack. No pytest, no qt — just a socket and JSON.
"""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any


def default_socket_path() -> str:
    rt = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(rt, f"qdbrowser-agent-{os.getuid()}.sock")


class Client:
    def __init__(self, socket_path: str | None = None):
        self._path = socket_path or default_socket_path()
        self._conn: socket.socket | None = None
        self._buf = b""
        self._next_id = 1

    def connect(self, retries: int = 20, delay: float = 0.25):
        last_err = None
        for _ in range(retries):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self._path)
                self._conn = s
                self._handshake()
                return
            except OSError as e:
                last_err = e
                time.sleep(delay)
        raise RuntimeError(
            f"could not connect to {self._path}: {last_err}")

    def _handshake(self):
        exe = os.readlink(f"/proc/{os.getpid()}/exe")
        msg = {"op": "handshake", "exe": exe, "pid": os.getpid(), "id": 0}
        self._conn.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        while True:
            while b"\n" not in self._buf:
                chunk = self._conn.recv(65536)
                if not chunk:
                    raise RuntimeError("agent_control closed during handshake")
                self._buf += chunk
            line, _, rest = self._buf.partition(b"\n")
            self._buf = rest
            if not line.strip():
                continue
            reply = json.loads(line.decode("utf-8"))
            if reply.get("error"):
                raise RuntimeError(
                    f"handshake failed: {reply['error']}")
            return

    def call(self, method: str, **params) -> Any:
        if self._conn is None:
            self.connect()
        rid = self._next_id
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": rid,
               "method": method, "params": params}
        self._conn.sendall((json.dumps(req) + "\n").encode("utf-8"))
        while True:
            while b"\n" not in self._buf:
                chunk = self._conn.recv(65536)
                if not chunk:
                    raise RuntimeError("agent_control closed")
                self._buf += chunk
            line, _, rest = self._buf.partition(b"\n")
            self._buf = rest
            if not line.strip():
                continue
            msg = json.loads(line.decode("utf-8"))
            if msg.get("id") != rid:
                # An event or out-of-order response.
                continue
            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(
                    f"agent_control error {err.get('code')}: "
                    f"{err.get('message')}")
            return msg.get("result")

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
