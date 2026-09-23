"""Single-instance server: forwards CLI commands to a running GUI.

Server name: ``qnotebook-<sha1(notebook_abs_path)[:10]>`` — so each notebook
gets its own socket. CLI commands that target a notebook with a running
instance are forwarded as a JSON line over QLocalSocket and handled in the
GUI event loop; otherwise the CLI runs standalone.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def server_name(notebook_root: Path) -> str:
    h = hashlib.sha1(str(Path(notebook_root).resolve()).encode("utf-8")).hexdigest()[:10]
    return f"qnotebook-{h}"


def try_forward(notebook_root: Path, payload: dict, timeout_ms: int = 500
                ) -> bool:
    """Connect to a running instance's QLocalServer and send a JSON payload.

    Returns True iff a running instance accepted the message. Safe to call
    from a headless process — we create no QApplication.
    """
    try:
        from PyQt6.QtNetwork import QLocalSocket
    except Exception:
        return False
    sock = QLocalSocket()
    sock.connectToServer(server_name(notebook_root))
    if not sock.waitForConnected(timeout_ms):
        return False
    try:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        sock.write(data)
        sock.flush()
        sock.waitForBytesWritten(timeout_ms)
    finally:
        sock.disconnectFromServer()
    return True


class CommandServer:
    """Bind a QLocalServer that receives forwarded CLI payloads.

    Owner wires ``commandReceived(dict)`` to whatever handles reload /
    append / jump-to-page. Only constructed inside the GUI process.
    """

    def __init__(self, window) -> None:
        from PyQt6.QtNetwork import QLocalServer
        self._window = window
        self._server: QLocalServer | None = None
        self._name: str = ""

    def start(self, notebook_root: Path) -> bool:
        from PyQt6.QtNetwork import QLocalServer
        self.stop()
        self._name = server_name(notebook_root)
        srv = QLocalServer()
        # Remove stale sockets from dead previous instances.
        QLocalServer.removeServer(self._name)
        if not srv.listen(self._name):
            return False
        srv.newConnection.connect(self._on_new_conn)
        self._server = srv
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    def _on_new_conn(self) -> None:
        srv = self._server
        if srv is None:
            return
        sock = srv.nextPendingConnection()
        if sock is None:
            return
        sock.readyRead.connect(lambda: self._read_payload(sock))

    def _read_payload(self, sock) -> None:
        data = bytes(sock.readAll())
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            self._dispatch(payload)

    def _dispatch(self, payload: dict) -> None:
        cmd = payload.get("cmd", "")
        if cmd == "reload":
            page = payload.get("page")
            if page and self._window._current_page == page:
                self._window.load_page(page)
        elif cmd == "open":
            page = payload.get("page")
            if page:
                self._window.load_page(page)
