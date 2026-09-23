"""MCP wrapper: AgentControlClient I/O and tool registration."""

import json
import os
import socket
import threading

import pytest


class FakeAgentServer:
    """Fake agent_control socket: echoes minimal canned responses."""

    def __init__(self, path):
        self.path = path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._sock.bind(path)
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.requests: list = []

    def start(self):
        self._thread.start()

    def stop(self):
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        buf = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
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
                    method = req.get("method")
                    rid = req.get("id")
                    result = self._handle(method, req.get("params") or {})
                    resp = {"jsonrpc": "2.0", "id": rid, "result": result}
                    conn.sendall((json.dumps(resp) + "\n").encode())

    def _handle(self, method, params):
        if method == "list_tabs":
            return [{"id": 1, "title": "Fake", "url": "about:blank",
                     "attached": False}]
        if method == "open_tab":
            return {"id": 42}
        if method == "attach":
            return {"ok": True, "page_load_seq": 0}
        return {"ok": True}


@pytest.fixture
def fake_agent(tmp_path):
    server = FakeAgentServer(str(tmp_path / "fake.sock"))
    server.start()
    yield server
    server.stop()


def test_client_call_roundtrip(fake_agent):
    from qdbrowser.mcp_server import AgentControlClient
    c = AgentControlClient(fake_agent.path)
    res = c.call("list_tabs")
    assert isinstance(res, list)
    assert res[0]["id"] == 1
    c.close()


def test_client_multiple_calls_reuse_connection(fake_agent):
    from qdbrowser.mcp_server import AgentControlClient
    c = AgentControlClient(fake_agent.path)
    c.call("list_tabs")
    res = c.call("open_tab", url="about:blank")
    assert res["id"] == 42
    c.close()
    # Server saw two requests on one connection.
    assert len(fake_agent.requests) >= 2


def test_mcp_server_registers_tools(fake_agent):
    """build_server should register at least these tools by name."""
    pytest.importorskip("mcp.server.fastmcp")
    from qdbrowser.mcp_server import AgentControlClient, build_server

    client = AgentControlClient(fake_agent.path)
    server = build_server(client)

    # FastMCP stores tools internally; access them via the introspection
    # method that all FastMCP versions expose.
    expected_min = {
        "list_tabs", "attach", "detach", "open_tab", "close_tab",
        "navigate", "reload", "go_back", "go_forward", "get_url",
        "wait_for_load", "click_at", "type_text", "send_keys", "scroll",
        "screenshot", "get_dom", "get_visible_text", "eval_js",
        "query_selector", "wait_for_selector",
    }
    # Try the public list_tools coroutine; fall back to private attrs.
    try:
        import asyncio
        tools = asyncio.run(server.list_tools())
        names = {t.name for t in tools}
    except Exception:
        names = set(getattr(server, "_tools", {}).keys())
    missing = expected_min - names
    assert not missing, f"missing tools: {missing}"
    client.close()
