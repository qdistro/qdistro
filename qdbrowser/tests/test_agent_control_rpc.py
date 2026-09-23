"""Deeper RPC coverage. Uses a *persistent* socket connection on a
worker thread so the per-client ``attached_tabs`` state is preserved
across calls and the Qt event loop in the main thread keeps pumping."""

import json
import os
import queue
import socket
import threading
import time
import traceback

import pytest

_RPC_TIMEOUT_S = float(os.environ.get("QDBROWSER_TEST_RPC_TIMEOUT", "15"))


class _ThreadedConn:
    """Run one socket on a worker thread; main thread pumps Qt events."""

    def __init__(self, path, qapp, timeout_s: float = _RPC_TIMEOUT_S):
        self._path = path
        self._qapp = qapp
        self._timeout_s = timeout_s
        self._next_id = 1
        self._sock = None
        self._lock = threading.Lock()

    def connect(self):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(self._timeout_s)
        self._sock.connect(self._path)

    def handshake(self):
        exe = os.readlink(f"/proc/{os.getpid()}/exe")
        return self.call_raw({"op": "handshake", "exe": exe,
                              "pid": os.getpid(), "id": 0})

    def call_raw(self, req):
        with self._lock:
            rid = req.get("id", 0)
            out_q: queue.Queue = queue.Queue()

            def _worker():
                try:
                    self._sock.sendall((json.dumps(req) + "\n").encode())
                    buf = b""
                    while True:
                        while b"\n" not in buf:
                            chunk = self._sock.recv(65536)
                            if not chunk:
                                out_q.put(("error", "agent_control closed"))
                                return
                            buf += chunk
                        line, _, rest = buf.partition(b"\n")
                        buf = rest
                        if not line.strip():
                            continue
                        msg = json.loads(line.decode())
                        if msg.get("id") != rid:
                            continue
                        out_q.put(("ok", msg))
                        return
                except Exception:
                    out_q.put(("error", traceback.format_exc()))

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            deadline = time.monotonic() + self._timeout_s
            while time.monotonic() < deadline:
                try:
                    kind, payload = out_q.get_nowait()
                except queue.Empty:
                    kind = payload = None
                if kind == "ok":
                    return payload
                if kind == "error":
                    raise RuntimeError(f"handshake/raw call failed: {payload}")
                self._qapp.processEvents()
                time.sleep(0.01)
            raise RuntimeError("handshake/raw call timed out")

    def call(self, method, **params):
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            out_q: queue.Queue = queue.Queue()

            def _worker():
                try:
                    req = {"jsonrpc": "2.0", "id": rid,
                           "method": method, "params": params}
                    self._sock.sendall((json.dumps(req) + "\n").encode())
                    buf = b""
                    while True:
                        while b"\n" not in buf:
                            chunk = self._sock.recv(65536)
                            if not chunk:
                                out_q.put(("error", "agent_control closed"))
                                return
                            buf += chunk
                        line, _, rest = buf.partition(b"\n")
                        buf = rest
                        if not line.strip():
                            continue
                        msg = json.loads(line.decode())
                        if msg.get("id") != rid:
                            continue
                        out_q.put(("ok", msg))
                        return
                except Exception:
                    out_q.put(("error", traceback.format_exc()))

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            deadline = time.monotonic() + self._timeout_s
            while time.monotonic() < deadline:
                try:
                    kind, payload = out_q.get_nowait()
                except queue.Empty:
                    kind = payload = None
                if kind == "ok":
                    return payload
                if kind == "error":
                    raise RuntimeError(
                        f"agent call {method} failed on {self._path} "
                        f"with params {params!r}:\n{payload}")
                self._qapp.processEvents()
                time.sleep(0.01)
            raise RuntimeError(
                f"agent call {method} timed out after "
                f"{self._timeout_s:.1f}s on {self._path} "
                f"with params {params!r}; worker_alive={t.is_alive()}")

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


@pytest.fixture
def agent_window(qtbot, themed_app, fresh_config, monkeypatch, tmp_path):
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


@pytest.fixture
def conn(agent_window, qapp):
    _w, sock_path = agent_window
    c = _ThreadedConn(sock_path, qapp)
    c.connect()
    c.handshake()
    yield c
    c.close()


def test_focus_tab_rpc(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    resp = conn.call("focus_tab", tab_id=tid)
    assert resp["result"]["ok"] is True


def test_attach_then_detach(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    a = conn.call("attach", tab_id=tid)
    assert a["result"]["ok"] is True
    d = conn.call("detach", tab_id=tid)
    assert d["result"]["ok"] is True


def test_detach_when_not_attached_errors(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    resp = conn.call("detach", tab_id=tid)
    assert "error" in resp


def test_unknown_tab_errors(conn):
    resp = conn.call("attach", tab_id=99999999)
    assert "error" in resp


def test_get_url_returns_fields(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    resp = conn.call("get_url", tab_id=tid)
    assert "url" in resp["result"]
    assert "title" in resp["result"]
    assert "loading" in resp["result"]


def test_close_unknown_tab_errors(conn):
    resp = conn.call("close_tab", tab_id=999999)
    assert "error" in resp


def test_open_then_close(conn):
    new = conn.call("open_tab", url="about:blank")
    tid = new["result"]["id"]
    resp = conn.call("close_tab", tab_id=tid)
    assert resp["result"]["ok"] is True


def test_screenshot_returns_png(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    resp = conn.call("screenshot", tab_id=tid)
    assert "png_b64" in resp["result"]
    assert resp["result"]["width"] > 0


def test_navigate_after_attach(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    conn.call("attach", tab_id=tid)
    resp = conn.call("navigate", tab_id=tid, url="about:blank")
    assert resp["result"]["ok"] is True


def test_navigate_without_attach_errors(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    resp = conn.call("navigate", tab_id=tid, url="about:blank")
    assert "error" in resp


def test_reload_after_attach(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    conn.call("attach", tab_id=tid)
    assert conn.call("reload", tab_id=tid)["result"]["ok"] is True


def test_stop_after_attach(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    conn.call("attach", tab_id=tid)
    assert conn.call("stop", tab_id=tid)["result"]["ok"] is True


def test_go_back_after_attach(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    conn.call("attach", tab_id=tid)
    assert conn.call("go_back", tab_id=tid)["result"]["ok"] is True


def test_scroll_after_attach(conn):
    tabs = conn.call("list_tabs")["result"]
    tid = tabs[0]["id"]
    conn.call("attach", tab_id=tid)
    assert conn.call("scroll", tab_id=tid, dx=0, dy=100)["result"]["ok"]


def test_list_tabs_includes_fields(conn):
    tabs = conn.call("list_tabs")["result"]
    keys = set(tabs[0].keys())
    for needed in ("id", "title", "url", "attached", "can_go_back",
                   "can_go_forward", "muted", "pinned", "group",
                   "profile", "zoom"):
        assert needed in keys, f"missing field {needed}"


def test_unknown_method_returns_error(conn):
    resp = conn.call("never_existed")
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_invalid_params_returns_error(conn):
    # `attach` requires tab_id.
    resp = conn.call("attach", bogus_param=1)
    assert "error" in resp
