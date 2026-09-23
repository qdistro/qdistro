"""Minimal JSON-RPC client for the QTerminator ``agent_control`` socket.

QTerminator ships an agent_control plugin that listens on
``$XDG_RUNTIME_DIR/qterminator-agent-$UID.sock`` and speaks
newline-delimited JSON-RPC 2.0 over a unix-domain stream socket.

This module wraps the small subset of methods QFileMan needs:

* :func:`list_tabs`     — discover open tabs and their cwds
* :func:`attach`        — must be called before :func:`send_text` for a tab
* :func:`send_text`     — type text into the attached tab
* :func:`open_tab`      — open a fresh tab, optionally in a given cwd

Each public function opens its own short-lived connection. That's
cheap (unix socket, same process group) and avoids holding cross-call
state — important because attach is per-connection on the server side
so we keep attach+send_text inside a single helper, :func:`cd`.

If the socket isn't there, every public function raises
:class:`QTerminatorUnavailable` and the caller is expected to surface
the failure to the user (typically by hiding the relevant menu items
or showing a "QTerminator isn't running" warning).
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

log = logging.getLogger(__name__)


class QTerminatorUnavailable(RuntimeError):
    """Raised when the agent_control socket can't be opened."""


class QTerminatorError(RuntimeError):
    """Raised on a JSON-RPC error reply from agent_control."""


def default_socket_path() -> str:
    """Return the socket path the agent_control plugin binds to."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime_dir, f"qterminator-agent-{os.getuid()}.sock")


def is_available(path: str | None = None) -> bool:
    """Cheap predicate: does the socket exist on disk?

    Doesn't open a connection — that would be wasteful on every menu
    build. A stale socket left after a crash will pass this check and
    then fail loudly on the first real call.
    """
    return os.path.exists(path or default_socket_path())


class _Connection:
    """One-shot connection wrapper that handles framing."""

    def __init__(self, path: str | None = None, *, timeout: float = 2.0):
        self._path = path or default_socket_path()
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 1

    def __enter__(self) -> _Connection:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self._timeout)
        try:
            s.connect(self._path)
        except OSError as e:
            s.close()
            raise QTerminatorUnavailable(
                f"Cannot connect to {self._path}: {e}"
            ) from e
        self._sock = s
        return self

    def __exit__(self, *_exc):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def call(self, method: str, **params: Any) -> Any:
        assert self._sock is not None
        rid = self._next_id
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self._sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        while True:
            line = self._read_line()
            if not line.strip():
                continue
            msg = json.loads(line.decode("utf-8"))
            # Server events (no id) are not for us — skip them.
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                err = msg["error"]
                raise QTerminatorError(
                    f"{err.get('code')}: {err.get('message')}"
                )
            return msg.get("result")

    def _read_line(self) -> bytes:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise QTerminatorError("connection closed while reading reply")
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\n")
        self._buf = rest
        return line


# --------------------------------------------------------------------------
# High-level helpers used by the plugins.
# --------------------------------------------------------------------------

def list_tabs(path: str | None = None) -> list[dict]:
    """Return the ``list_tabs`` array (id, title, working_directory, ...)."""
    with _Connection(path) as c:
        return c.call("list_tabs") or []


def open_tab(working_directory: str | None = None,
             path: str | None = None) -> int:
    """Open a new QTerminator tab; return its tab id."""
    with _Connection(path) as c:
        result = c.call("open_tab", working_directory=working_directory)
    return int(result["id"])


def cd(tab_id: int, directory: str, path: str | None = None) -> None:
    """Type ``cd <directory>`` + Enter into ``tab_id``.

    Uses ``shlex``-style quoting so paths with spaces or shell
    metacharacters are pasted safely. attach() must be issued on the
    same connection as send_text(), so this helper does both inside
    a single ``_Connection``.
    """
    import shlex
    line = f"cd {shlex.quote(directory)}\n"
    with _Connection(path) as c:
        c.call("attach", tab_id=tab_id)
        try:
            c.call("send_text", tab_id=tab_id, text=line)
        finally:
            # Detach so we don't keep a subscriber slot occupied on the
            # server; non-fatal if it fails.
            try:
                c.call("detach", tab_id=tab_id)
            except QTerminatorError:
                pass


def send_text(tab_id: int, text: str, path: str | None = None) -> None:
    """Type ``text`` into ``tab_id`` verbatim (no quoting, no newline added)."""
    with _Connection(path) as c:
        c.call("attach", tab_id=tab_id)
        try:
            c.call("send_text", tab_id=tab_id, text=text)
        finally:
            try:
                c.call("detach", tab_id=tab_id)
            except QTerminatorError:
                pass
