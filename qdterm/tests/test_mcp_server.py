"""Tests for the MCP transport wrapper.

Three layers:
  - Unit tests on AgentControlClient against a fake Unix-socket server.
  - Tool registration tests: build the FastMCP server with a mock
    client, list tools via the MCP API, assert names/schemas.
  - End-to-end stdio test: spawn ``qterminator-mcp`` as a subprocess
    pointed at our fake agent_control socket, drive tools via the
    MCP client SDK, assert round-trips.
"""

import asyncio
import json
import os
import socket
import threading
import time

import pytest

mcp_pkg = pytest.importorskip("mcp")

from qterminator.mcp_server import (  # noqa: E402
    AgentControlClient,
    build_server,
    default_socket_path,
)

# ---------------------------------------------------------------------------
# Fake agent_control server for tests
# ---------------------------------------------------------------------------

class FakeAgentServer:
    """Tiny Unix-socket JSON-RPC server that mimics agent_control.
    Handlers return either a static result or call a callable."""

    def __init__(self, path: str):
        self.path = path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._sock.bind(path)
        self._sock.listen(4)
        self._handlers: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def handle(self, method: str, result):
        """Register a method to return ``result`` (or call it if callable)."""
        self._handlers[method] = result

    def _run(self):
        self._sock.settimeout(0.2)
        clients: list[socket.socket] = []
        while not self._stop:
            try:
                c, _ = self._sock.accept()
                c.setblocking(True)
                clients.append(c)
                threading.Thread(
                    target=self._serve, args=(c,), daemon=True,
                ).start()
            except TimeoutError:
                continue
            except OSError:
                break
        for c in clients:
            try:
                c.close()
            except OSError:
                pass

    def _serve(self, c: socket.socket):
        buf = b""
        try:
            while not self._stop:
                chunk = c.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    buf = rest
                    if not line.strip():
                        continue
                    msg = json.loads(line.decode("utf-8"))
                    method = msg.get("method")
                    params = msg.get("params") or {}
                    self.calls.append((method, params))
                    h = self._handlers.get(method)
                    if callable(h):
                        result = h(**params)
                    else:
                        result = h if h is not None else {"ok": True}
                    if isinstance(result, dict) and "_error" in result:
                        resp = {
                            "jsonrpc": "2.0", "id": msg.get("id"),
                            "error": result["_error"],
                        }
                    else:
                        resp = {
                            "jsonrpc": "2.0", "id": msg.get("id"),
                            "result": result,
                        }
                    c.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except OSError:
            return

    def stop(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


@pytest.fixture
def fake_server(tmp_path):
    path = str(tmp_path / "fake-agent.sock")
    srv = FakeAgentServer(path)
    yield srv
    srv.stop()


# ---------------------------------------------------------------------------
# AgentControlClient
# ---------------------------------------------------------------------------

def test_client_returns_result(fake_server):
    fake_server.handle("list_tabs", [{"id": 1, "title": "test"}])
    c = AgentControlClient(fake_server.path)
    try:
        assert c.call("list_tabs") == [{"id": 1, "title": "test"}]
    finally:
        c.close()


def test_client_forwards_params(fake_server):
    seen = []
    fake_server.handle("send_text", lambda **p: (seen.append(p), {"ok": True})[1])
    c = AgentControlClient(fake_server.path)
    try:
        c.call("send_text", tab_id=42, text="hello")
    finally:
        c.close()
    assert seen == [{"tab_id": 42, "text": "hello"}]


def test_client_raises_on_rpc_error(fake_server):
    fake_server.handle("send_text", {"_error": {
        "code": -32001, "message": "not attached",
    }})
    c = AgentControlClient(fake_server.path)
    try:
        with pytest.raises(RuntimeError, match="not attached"):
            c.call("send_text", tab_id=1, text="x")
    finally:
        c.close()


def test_client_reconnects_after_close(fake_server):
    fake_server.handle("list_tabs", [])
    c = AgentControlClient(fake_server.path)
    try:
        c.call("list_tabs")
        # Drop the connection underneath and try again.
        c._conn.close()
        c._conn = None
        assert c.call("list_tabs") == []
    finally:
        c.close()


def test_default_socket_path_uses_xdg():
    p = default_socket_path()
    assert p.endswith(f"qterminator-agent-{os.getuid()}.sock")


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

class _StubClient:
    def __init__(self):
        self.calls = []
        self.responses: dict[str, object] = {}

    def call(self, method, **params):
        self.calls.append((method, params))
        return self.responses.get(method, {"ok": True})

    def close(self):
        pass


def _list_tools(srv) -> dict:
    """FastMCP exposes its tool registry. Returns name -> tool object."""
    tm = srv._tool_manager
    return {t.name: t for t in tm.list_tools()}


def test_all_expected_tools_registered():
    srv = build_server(_StubClient())
    names = set(_list_tools(srv))
    expected = {
        "list_tabs", "attach", "detach", "send_text", "send_keys",
        "get_screen", "tail_stream", "screenshot", "open_tab", "close_tab",
        "start_recording", "stop_recording", "command_history",
    }
    assert expected.issubset(names), f"missing: {expected - names}"


def test_tool_descriptions_are_informative():
    """Each tool's description must be non-empty and mention what the
    tool does, since this is what the harness shows the model."""
    srv = build_server(_StubClient())
    for name, tool in _list_tools(srv).items():
        desc = tool.description or ""
        assert len(desc) > 30, f"tool {name} has thin description: {desc!r}"


def test_tool_invocation_round_trips_to_client():
    stub = _StubClient()
    stub.responses["list_tabs"] = [{"id": 7, "title": "x"}]
    srv = build_server(stub)
    # Invoke via the tool manager's call path.
    tm = srv._tool_manager
    result = asyncio.run(tm.call_tool("list_tabs", arguments={}))
    # FastMCP returns a tuple (unstructured, structured). The
    # structured form is what callers actually read.
    if isinstance(result, tuple) and len(result) == 2:
        _content, structured = result
    else:
        structured = result
    # Structured output is dict-or-list. For list_tabs, agent_control
    # returns a list — FastMCP wraps it under the "result" key when
    # the underlying tool returns a non-dict.
    if isinstance(structured, dict) and "result" in structured:
        structured = structured["result"]
    assert structured == [{"id": 7, "title": "x"}]
    assert stub.calls == [("list_tabs", {})]


def test_tool_params_pass_through():
    stub = _StubClient()
    stub.responses["send_text"] = {"ok": True}
    srv = build_server(stub)
    tm = srv._tool_manager
    asyncio.run(tm.call_tool(
        "send_text", arguments={"tab_id": 42, "text": "hi"},
    ))
    assert stub.calls == [("send_text", {"tab_id": 42, "text": "hi"})]


# ---------------------------------------------------------------------------
# Smoke test: tools/list against a real subprocess
# ---------------------------------------------------------------------------

def test_subprocess_tools_list(fake_server, tmp_path):
    """Spawn qterminator-mcp as a child process pointed at our fake
    socket, send the MCP initialize + tools/list handshake, assert
    every expected tool name is in the list."""
    import subprocess

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        ["python3", "-m", "qterminator.mcp_server",
         "--socket", fake_server.path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env,
    )
    try:
        def send(obj):
            proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            proc.stdin.flush()

        def recv():
            for _ in range(200):  # 2s polling
                line = proc.stdout.readline()
                if not line:
                    time.sleep(0.01)
                    continue
                try:
                    return json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
            raise TimeoutError("no response from MCP server")

        send({
            "jsonrpc": "2.0", "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        })
        init = recv()
        assert init.get("id") == 1, init
        # initialized notification
        send({"jsonrpc": "2.0",
              "method": "notifications/initialized", "params": {}})

        send({"jsonrpc": "2.0", "id": 2,
              "method": "tools/list", "params": {}})
        tools_resp = recv()
        assert tools_resp.get("id") == 2, tools_resp
        names = {t["name"] for t in tools_resp["result"]["tools"]}
        for required in ("list_tabs", "attach", "send_text", "get_screen"):
            assert required in names
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
