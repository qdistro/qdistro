"""AgentControlClient: connection reuse, reconnect, error handling."""

import json
import os
import socket
import threading

import pytest


class FakeServer:
    def __init__(self, path, handler):
        self.path = path
        self.handler = handler
        self.requests = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._sock.bind(path)
        self._sock.listen(5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,),
                              daemon=True).start()

    def _client(self, conn):
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    buf = rest
                    if not line.strip():
                        continue
                    req = json.loads(line.decode())
                    self.requests.append(req)
                    resp = self.handler(req)
                    if resp is None:
                        continue  # drop
                    conn.sendall((json.dumps(resp) + "\n").encode())
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        try:
            self._sock.close()
        except OSError:
            pass


def test_client_propagates_rpc_error(tmp_path):
    def handler(req):
        return {"jsonrpc": "2.0", "id": req["id"],
                "error": {"code": -99, "message": "synthetic"}}

    server = FakeServer(str(tmp_path / "s.sock"), handler)
    try:
        from qdbrowser.mcp_server import AgentControlClient
        c = AgentControlClient(server.path)
        with pytest.raises(RuntimeError) as e:
            c.call("test_method")
        assert "synthetic" in str(e.value)
        c.close()
    finally:
        server.stop()


def test_client_skips_events_keeps_id(tmp_path):
    """The server emits an event before the response — the client must
    ignore it and wait for the matching id."""
    def handler(req):
        # Out-of-band event + real response.
        return {
            "jsonrpc": "2.0", "id": req["id"],
            "result": {"after_event": True},
        }

    server = FakeServer(str(tmp_path / "s.sock"), handler)
    try:
        from qdbrowser.mcp_server import AgentControlClient
        c = AgentControlClient(server.path)
        res = c.call("anything")
        assert res == {"after_event": True}
        c.close()
    finally:
        server.stop()


def test_default_socket_path_uses_runtime(monkeypatch):
    from qdbrowser.mcp_server import default_socket_path
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    p = default_socket_path()
    assert p.startswith("/run/user/1000/")


def test_default_socket_path_falls_back(monkeypatch):
    from qdbrowser.mcp_server import default_socket_path
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    p = default_socket_path()
    assert p.startswith("/tmp/")


def test_parse_args_default(monkeypatch):
    monkeypatch.delenv("QDBROWSER_AGENT_SOCKET", raising=False)
    from qdbrowser.mcp_server import parse_args
    args = parse_args([])
    assert "qdbrowser-agent" in args.socket


def test_parse_args_socket_override():
    from qdbrowser.mcp_server import parse_args
    args = parse_args(["--socket", "/tmp/custom.sock"])
    assert args.socket == "/tmp/custom.sock"


def test_parse_args_env_override(monkeypatch):
    from qdbrowser.mcp_server import parse_args
    monkeypatch.setenv("QDBROWSER_AGENT_SOCKET", "/env/path.sock")
    args = parse_args([])
    assert args.socket == "/env/path.sock"
