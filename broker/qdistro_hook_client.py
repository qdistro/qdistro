"""Broker-side client for the sandboxed hook executor.

The broker calls ``HookClient.query(action, event)`` when declarative
rules are inconclusive and before falling back to the admin prompt.
The client connects to the hook executor's AF_UNIX socket, sends a
length-prefixed JSON frame, and waits for the verdict.

Connection lifecycle: the client opens one connection per query and
closes it afterward.  The hook executor is a separate process; a
persistent connection would require reconnect logic for executor
restarts, and the query rate (one per admin-prompt-candidate request)
is low enough that per-query connect overhead is negligible.

Timeouts and errors are all mapped to ``None`` (fall through to admin
prompt) — the executor being unreachable or slow must never block the
broker's decision pipeline.
"""
from __future__ import annotations

import json
import os
import socket
import struct
from typing import Any

SOCKET_PATH = os.environ.get("QDISTRO_HOOK_SOCKET",
                             "/run/qdistro/hook-executor.sock")

# 5 seconds total timeout for connect + send + recv.
DEFAULT_TIMEOUT_S = float(os.environ.get("QDISTRO_HOOK_TIMEOUT", "5"))

_LEN_FMT = "!I"
_LEN_SIZE = struct.calcsize(_LEN_FMT)
MAX_FRAME_SIZE = 4 * 1024 * 1024


def _recv_exact(conn: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _recv_frame(conn: socket.socket) -> bytes | None:
    hdr = _recv_exact(conn, _LEN_SIZE)
    if hdr is None:
        return None
    (length,) = struct.unpack(_LEN_FMT, hdr)
    if length > MAX_FRAME_SIZE:
        return None
    return _recv_exact(conn, length)


def _send_frame(conn: socket.socket, data: bytes) -> bool:
    try:
        conn.sendall(struct.pack(_LEN_FMT, len(data)) + data)
        return True
    except OSError:
        return False


class HookClient:
    """Broker-side hook executor client.

    Usage::

        client = HookClient()
        verdict = client.query("clipboard_send", {
            "source_uid": 1001,
            "target_uid": 1002,
            "mime_type": "text/plain",
            "payload_size": 1234,
        })
        # verdict is None, or a dict like {"verdict": "allow"}, etc.
    """

    def __init__(self, socket_path: str = SOCKET_PATH,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 enabled: bool = True):
        self._socket_path = socket_path
        self._timeout_s = timeout_s
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def query(self, action: str, event: dict[str, Any]) -> dict | None:
        """Send an event to the hook executor and return its verdict.

        Returns a dict like ``{"verdict": "allow"}`` or
        ``{"verdict": "deny", "reason": "..."}`` or
        ``{"verdict": "transform", ...}`` on success.

        Returns ``None`` (fall through to admin prompt) on:
        - hooks disabled
        - executor socket unreachable
        - timeout
        - protocol error
        - executor returned verdict=null
        """
        if not self._enabled:
            return None
        try:
            return self._do_query(action, event)
        except Exception:
            return None

    def _do_query(self, action: str, event: dict) -> dict | None:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(self._timeout_s)
        try:
            conn.connect(self._socket_path)
            req = json.dumps({"action": action, "event": event})
            if not _send_frame(conn, req.encode("utf-8")):
                return None
            raw = _recv_frame(conn)
            if raw is None:
                return None
            resp = json.loads(raw)
            if not isinstance(resp, dict):
                return None
            verdict = resp.get("verdict")
            if verdict is None:
                return None
            return resp
        except (OSError, json.JSONDecodeError, UnicodeDecodeError,
                socket.timeout, ConnectionRefusedError):
            return None
        finally:
            try:
                conn.close()
            except OSError:
                pass
