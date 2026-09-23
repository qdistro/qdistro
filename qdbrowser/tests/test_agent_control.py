"""agent_control plugin: socket lifecycle + basic JSON-RPC verbs.

These tests run against a real MainWindow but never connect to a real
network — every URL is about:blank or a synthesized data: URL.
"""

import json
import os
import queue
import socket
import threading
import time
import traceback

import pytest

_RPC_TIMEOUT_S = float(os.environ.get("QDBROWSER_TEST_RPC_TIMEOUT", "15"))


def _connect_and_call(socket_path, method, qapp, **params):
    """Send a JSON-RPC request on a worker thread while pumping the Qt
    event loop in this thread — the server is on this same event loop,
    so blocking recv here would deadlock."""
    result_q: queue.Queue = queue.Queue()

    def _worker():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(_RPC_TIMEOUT_S)
                s.connect(socket_path)
                exe = os.readlink(f"/proc/{os.getpid()}/exe")
                hs = {"op": "handshake", "exe": exe, "pid": os.getpid(),
                      "id": 0}
                s.sendall((json.dumps(hs) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                }
                s.sendall((json.dumps(req) + "\n").encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                result_q.put(("ok", buf.split(b"\n", 1)[0]))
        except Exception:
            result_q.put(("error", traceback.format_exc()))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    deadline = time.monotonic() + _RPC_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            kind, payload = result_q.get_nowait()
        except queue.Empty:
            kind = payload = None
        if kind == "ok":
            assert payload, f"agent call {method} closed without a response"
            return json.loads(payload.decode())
        if kind == "error":
            pytest.fail(
                f"agent call {method} failed on {socket_path} "
                f"with params {params!r}:\n{payload}")
        qapp.processEvents()
        time.sleep(0.01)
    pytest.fail(
        f"agent call {method} timed out after {_RPC_TIMEOUT_S:.1f}s "
        f"on {socket_path} with params {params!r}; "
        f"worker_alive={t.is_alive()}")


@pytest.fixture
def agent_window(qtbot, themed_app, fresh_config, monkeypatch, tmp_path):
    """A window with agent_control forced on, socket pointing into tmp."""
    # Steer the socket path into tmp so we don't collide with a running
    # qdbrowser instance.
    sock_path = str(tmp_path / "agent.sock")
    monkeypatch.setattr(
        "qdbrowser.plugins.agent_control._socket_path",
        lambda: sock_path)
    monkeypatch.setenv("QDBROWSER_AGENT_CONTROL", "1")

    from qdbrowser.window import MainWindow
    w = MainWindow()
    w.new_tab(url="about:blank")
    qtbot.addWidget(w)
    w.show()
    qtbot.waitExposed(w)
    return w, sock_path


def test_socket_is_created(agent_window):
    _w, sock_path = agent_window
    assert os.path.exists(sock_path)
    st = os.stat(sock_path)
    assert st.st_mode & 0o777 == 0o600


def test_list_tabs(agent_window, qapp):
    _w, sock_path = agent_window
    resp = _connect_and_call(sock_path, "list_tabs", qapp)
    assert "result" in resp, resp
    tabs = resp["result"]
    assert len(tabs) >= 1
    assert "id" in tabs[0]
    assert "url" in tabs[0]
    assert "attached" in tabs[0]


def test_attach_required_for_navigate(agent_window, qapp):
    _w, sock_path = agent_window
    tabs = _connect_and_call(sock_path, "list_tabs", qapp)["result"]
    tid = tabs[0]["id"]
    resp = _connect_and_call(sock_path, "navigate", qapp,
                              tab_id=tid, url="about:blank")
    assert "error" in resp
    assert resp["error"]["code"] == -32001


def test_open_tab(agent_window, qapp):
    _w, sock_path = agent_window
    resp = _connect_and_call(sock_path, "open_tab", qapp, url="about:blank")
    assert "result" in resp
    assert "id" in resp["result"]
    tabs = _connect_and_call(sock_path, "list_tabs", qapp)["result"]
    assert any(t["id"] == resp["result"]["id"] for t in tabs)


def test_unknown_method(agent_window, qapp):
    _w, sock_path = agent_window
    resp = _connect_and_call(sock_path, "no_such_verb", qapp)
    assert "error" in resp
    assert resp["error"]["code"] == -32601
