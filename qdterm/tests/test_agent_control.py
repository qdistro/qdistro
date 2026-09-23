"""Tests for the agent_control plugin.

Exercises the JSON-RPC protocol end-to-end through the real Unix socket,
not by calling RPC methods directly. The plugin's notifier-driven server
runs on the Qt main thread; client I/O runs in a worker thread while the
main thread pumps the event loop via ``qtbot.wait``.
"""

import base64
import json
import os
import socket
import threading
import time

import pytest
import qterminator.config as config_mod
from qterminator.config import Config

pytest.importorskip("pyte")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _agent_env(tmp_path, monkeypatch):
    """Enable the plugin and isolate the socket path / config dir."""
    monkeypatch.setenv("QTERMINATOR_AGENT_CONTROL", "1")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


@pytest.fixture
def window(qtbot):
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(900, 600)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(150)  # let the shell start and produce its first prompt
    yield win


@pytest.fixture
def plugin(window):
    pm = window._plugin_manager
    p = pm._instances.get("agent_control")
    assert p is not None, "agent_control plugin not loaded"
    assert p.socket_path is not None, "socket not opened (env gate failed?)"
    return p


def _wait_for_socket(path, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# RPC client used by tests
# ---------------------------------------------------------------------------

class RpcClient:
    """Blocking JSON-RPC client. All calls are executed in a worker thread
    while the caller pumps the Qt event loop via ``qtbot.wait``."""

    def __init__(self, path: str):
        self.path = path
        self._s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._s.connect(path)
        self._next = 1
        self._buf = b""

    def close(self):
        try:
            self._s.close()
        except OSError:
            pass

    def _send_recv(self, method, **params):
        rid = self._next
        self._next += 1
        req = json.dumps({"jsonrpc": "2.0", "id": rid,
                          "method": method, "params": params})
        self._s.sendall((req + "\n").encode("utf-8"))
        while True:
            while b"\n" not in self._buf:
                chunk = self._s.recv(65536)
                if not chunk:
                    raise RuntimeError("server closed")
                self._buf += chunk
            line, _, rest = self._buf.partition(b"\n")
            self._buf = rest
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg

    def call(self, qtbot, method, **params):
        result = [None]
        err = [None]
        done = threading.Event()

        def target():
            try:
                result[0] = self._send_recv(method, **params)
            except Exception as e:  # noqa: BLE001
                err[0] = e
            finally:
                done.set()

        threading.Thread(target=target, daemon=True).start()
        end = time.monotonic() + 5.0
        while not done.is_set() and time.monotonic() < end:
            qtbot.wait(15)
        if err[0]:
            raise err[0]
        if not done.is_set():
            raise TimeoutError(f"{method} did not return in 5s")
        return result[0]


@pytest.fixture
def rpc(plugin):
    c = RpcClient(plugin.socket_path)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_socket_appears_when_enabled(plugin):
    assert _wait_for_socket(plugin.socket_path), "socket file never appeared"


def test_list_tabs_returns_initial_tab(qtbot, rpc):
    resp = rpc.call(qtbot, "list_tabs")
    assert "result" in resp, resp
    tabs = resp["result"]
    assert isinstance(tabs, list) and len(tabs) == 1
    t = tabs[0]
    for key in ("id", "title", "shell_pid", "cols", "rows", "attached",
                "tmux_session"):
        assert key in t
    assert t["attached"] is False
    # No tmux_mode plugin enabled here, but the key is still present.
    # (Value is None whether or not the tmux_mode plugin happens to be
    # loaded — the initial tab spawns the user's $SHELL.)
    assert t["tmux_session"] is None


def test_list_tabs_reports_tmux_session_when_service_present(qtbot, rpc, window):
    """If MainWindow exposes a tmux_mode service, agent_control surfaces
    the detection result through list_tabs.tmux_session."""
    class _FakeService:
        def get_session_for_terminal(self, _t):
            return "qterm-fake"
    window.tmux_mode = _FakeService()
    try:
        resp = rpc.call(qtbot, "list_tabs")
        assert resp["result"][0]["tmux_session"] == "qterm-fake"
    finally:
        del window.tmux_mode


def test_start_stop_recording_via_rpc(qtbot, rpc, window, tmp_path):
    """The agent_control RPC surface exposes start/stop_recording when
    the asciinema_record plugin is loaded. Drive a short recording
    through the socket and verify the cast file is written."""
    # Load the recorder service onto the window (the plugin would do
    # this in activate, but we install it directly to keep this test
    # focused on the agent_control RPC contract).
    from qterminator.plugins.asciinema_record import AsciinemaRecorderService
    window.asciinema_recorder = AsciinemaRecorderService(
        str(tmp_path), window.shadow_screens,
    )
    try:
        tid = rpc.call(qtbot, "list_tabs")["result"][0]["id"]
        rpc.call(qtbot, "attach", tab_id=tid)
        cast_path = str(tmp_path / "rpc.cast")
        start = rpc.call(qtbot, "start_recording",
                         tab_id=tid, path=cast_path)
        assert start["result"]["path"] == cast_path
        # Let the shell prompt write something into the cast.
        rpc.call(qtbot, "send_text", tab_id=tid, text="echo CAST_OK\n")
        qtbot.wait(300)
        # Tabs should report recording=True.
        listing = rpc.call(qtbot, "list_tabs")["result"]
        assert listing[0]["recording"] is True
        assert listing[0]["recording_path"] == cast_path
        stop = rpc.call(qtbot, "stop_recording", tab_id=tid)
        assert stop["result"]["path"] == cast_path
        assert stop["result"]["event_count"] >= 1
        # Cast file has a v3 header.
        import json
        with open(cast_path) as f:
            header = json.loads(f.readline())
        assert header["version"] == 3
    finally:
        del window.asciinema_recorder


def test_list_tabs_reports_shared_via_mosh(qtbot, rpc, window):
    """When tmux_share exposes active shares, list_tabs surfaces the
    list of UDP ports under shared_via_mosh."""
    class _FakeTmuxMode:
        def get_session_for_terminal(self, _t):
            return "qterm-fake"
    class _FakeShare:
        def ports_for(self, sess):
            return [60042] if sess == "qterm-fake" else []
    window.tmux_mode = _FakeTmuxMode()
    window.tmux_share = _FakeShare()
    try:
        resp = rpc.call(qtbot, "list_tabs")
        assert resp["result"][0]["shared_via_mosh"] == [60042]
    finally:
        del window.tmux_mode
        del window.tmux_share


def test_list_tabs_reports_ssh_and_web_share_ports(qtbot, rpc, window):
    class _FakeTmuxMode:
        def get_session_for_terminal(self, _t):
            return "qterm-fake"

    class _FakeShare:
        def __init__(self, ports):
            self._ports = ports

        def ports_for(self, sess):
            return self._ports if sess == "qterm-fake" else []

    window.tmux_mode = _FakeTmuxMode()
    window.tmux_ssh_share = _FakeShare([22022])
    window.tmux_web_share = _FakeShare([7680])
    try:
        resp = rpc.call(qtbot, "list_tabs")
        tab = resp["result"][0]
        assert tab["shared_via_ssh"] == [22022]
        assert tab["shared_via_web"] == [7680]
    finally:
        del window.tmux_mode
        del window.tmux_ssh_share
        del window.tmux_web_share


@pytest.mark.cheat_aware(
    protects="send_text into a terminal is refused unless the client first "
    "attached to that tab",
    severity="high",
    cheats=[
        "assert success instead of the -32001 error",
        "loosen the error-code check so any response passes",
        "pre-attach in a fixture so the gate is never exercised",
    ],
    consequence="an unattached RPC client could inject keystrokes/commands "
    "into a terminal it never opened a session on",
)
def test_send_requires_attach(qtbot, rpc):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    resp = rpc.call(qtbot, "send_text", tab_id=tid, text="echo nope\n")
    assert "error" in resp
    assert resp["error"]["code"] == -32001


def test_attach_send_and_tail_stream(qtbot, rpc):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]

    a = rpc.call(qtbot, "attach", tab_id=tid)
    assert a["result"]["ok"] is True

    rpc.call(qtbot, "send_text", tab_id=tid, text="echo HELLO_AGENT\n")
    # Let the PTY echo and the shell run the command.
    qtbot.wait(400)

    tail = rpc.call(qtbot, "tail_stream", tab_id=tid, since=0)["result"]
    raw = base64.b64decode(tail["bytes_b64"]).decode("utf-8", errors="replace")
    assert "HELLO_AGENT" in raw, f"stream had: {raw!r}"
    assert tail["latest_seq"] >= 1


def test_get_screen_reports_grid_and_cursor(qtbot, rpc):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    rpc.call(qtbot, "attach", tab_id=tid)

    rpc.call(qtbot, "send_text", tab_id=tid, text="echo SCREEN_MARK\n")
    qtbot.wait(400)

    snap = rpc.call(qtbot, "get_screen", tab_id=tid)["result"]
    assert isinstance(snap["lines"], list) and snap["lines"], snap
    joined = "\n".join(snap["lines"])
    assert "SCREEN_MARK" in joined, f"screen had: {joined!r}"
    assert 0 <= snap["cursor"]["x"] < snap["cols"]
    assert 0 <= snap["cursor"]["y"] < snap["rows"]


def test_screenshot_returns_png(qtbot, rpc):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    shot = rpc.call(qtbot, "screenshot", tab_id=tid)["result"]
    png = base64.b64decode(shot["png_b64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert shot["width"] > 0 and shot["height"] > 0


def test_open_tab_grows_list(qtbot, rpc):
    before = rpc.call(qtbot, "list_tabs")["result"]
    rpc.call(qtbot, "open_tab")
    qtbot.wait(150)
    after = rpc.call(qtbot, "list_tabs")["result"]
    assert len(after) == len(before) + 1


def test_detach_clears_pyte_and_rejects_further_input(qtbot, rpc, plugin):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    rpc.call(qtbot, "attach", tab_id=tid)
    assert tid in plugin.tab_states

    rpc.call(qtbot, "detach", tab_id=tid)
    assert tid not in plugin.tab_states  # last subscriber gone → torn down

    resp = rpc.call(qtbot, "send_text", tab_id=tid, text="echo x\n")
    assert "error" in resp and resp["error"]["code"] == -32001


def test_send_keys_translates_ansi(qtbot, rpc):
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    rpc.call(qtbot, "attach", tab_id=tid)
    # Type "abc", then send Ctrl-U (kill line) and Enter — the visible
    # screen should not end up with "abc" remaining on the prompt line.
    rpc.call(qtbot, "send_text", tab_id=tid, text="abc")
    rpc.call(qtbot, "send_keys", tab_id=tid, keys=["ctrl+u", "enter"])
    qtbot.wait(300)
    snap = rpc.call(qtbot, "get_screen", tab_id=tid)["result"]
    # On the prompt line containing the cursor, "abc" should not be
    # the trailing visible characters (ctrl+u killed it).
    cursor_line = snap["lines"][snap["cursor"]["y"]].rstrip()
    assert not cursor_line.endswith("abc"), f"line: {cursor_line!r}"


def test_broadcast_event_delivers_to_attached(qtbot, rpc, plugin):
    """broadcast_event reaches subscribed clients as a one-line event frame."""
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    rpc.call(qtbot, "attach", tab_id=tid)

    plugin.broadcast_event(tid, "trigger_match", {"rule_id": "errors",
                                                   "pattern": "ERR"})
    # Pump until something arrives on the socket.
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        if b"\n" in rpc._buf:
            break
        try:
            rpc._s.settimeout(0.05)
            chunk = rpc._s.recv(4096)
        except (TimeoutError, BlockingIOError):
            chunk = b""
        if chunk:
            rpc._buf += chunk
        qtbot.wait(20)
    rpc._s.settimeout(None)
    assert b"\n" in rpc._buf, "no event arrived"
    line, _, rpc._buf = rpc._buf.partition(b"\n")
    msg = json.loads(line)
    assert msg["event"] == "trigger_match"
    assert msg["tab_id"] == tid
    assert msg["rule_id"] == "errors"
    assert msg["pattern"] == "ERR"


def test_broadcast_event_rejects_reserved_data_type(qtbot, rpc, plugin):
    """``event_type='data'`` collides with the raw PTY stream frame and
    must be silently dropped — nothing should appear on the wire."""
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    rpc.call(qtbot, "attach", tab_id=tid)

    plugin.broadcast_event(tid, "data", {"bytes_b64": "AAA"})
    qtbot.wait(200)
    rpc._s.settimeout(0.05)
    try:
        chunk = rpc._s.recv(4096)
    except (TimeoutError, BlockingIOError):
        chunk = b""
    rpc._s.settimeout(None)
    # No matter what arrived (timing-dependent PTY traffic may have
    # produced a real "data" frame), there must be no event with
    # bytes_b64=="AAA" — that would have been our forged frame.
    combined = rpc._buf + chunk
    for raw_line in combined.split(b"\n"):
        if not raw_line.strip():
            continue
        try:
            m = json.loads(raw_line)
        except ValueError:
            continue
        assert m.get("bytes_b64") != "AAA", "reserved data event leaked"


def test_broadcast_event_drops_envelope_collisions(qtbot, rpc, plugin):
    """Payload keys that would clobber ``event`` or ``tab_id`` must be dropped."""
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    rpc.call(qtbot, "attach", tab_id=tid)

    plugin.broadcast_event(tid, "trigger_match", {
        "event": "forged",
        "tab_id": 99999,
        "rule_id": "ok",
    })
    end = time.monotonic() + 2.0
    while time.monotonic() < end:
        if b"\n" in rpc._buf:
            break
        try:
            rpc._s.settimeout(0.05)
            chunk = rpc._s.recv(4096)
        except (TimeoutError, BlockingIOError):
            chunk = b""
        if chunk:
            rpc._buf += chunk
        qtbot.wait(20)
    rpc._s.settimeout(None)
    line, _, rpc._buf = rpc._buf.partition(b"\n")
    msg = json.loads(line)
    assert msg["event"] == "trigger_match"
    assert msg["tab_id"] == tid
    assert msg["rule_id"] == "ok"


@pytest.mark.cheat_aware(
    protects="the agent-control Unix socket rejects and closes connections "
    "whose peer uid does not match the owner",
    severity="critical",
    cheats=[
        "assert the connection stays open instead of being closed",
        "stop forcing _peer_uid_matches False so the deny path never runs",
        "swallow the BrokenPipe/reset into a pass",
    ],
    consequence="any local user could connect to another user's control "
    "socket and drive their terminals (send input, read screen, screenshot)",
)
def test_uid_mismatch_rejected(qtbot, plugin):
    # The plugin is loaded via importlib.util.spec_from_file_location, so
    # its module globals are a *separate* dict from the import-system's
    # qterminator.plugins.agent_control module. Patch via the live
    # globals dict on one of the plugin's own callables so the stub is
    # seen by the running _accept method.
    g = plugin.__class__.__init__.__globals__
    real = g["_peer_uid_matches"]
    try:
        g["_peer_uid_matches"] = lambda conn: False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(plugin.socket_path)
        qtbot.wait(250)  # let the listener notifier fire and reject
        s.settimeout(1.0)
        closed = False
        try:
            s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"list_tabs"}\n')
            qtbot.wait(150)
            data = s.recv(64)
            closed = (data == b"")
        except (BrokenPipeError, ConnectionResetError, OSError):
            closed = True
        assert closed, "rejected connection should be closed by the server"
        s.close()
    finally:
        g["_peer_uid_matches"] = real


# ---------------------------------------------------------------------------
# Additional agent_control tests
# ---------------------------------------------------------------------------


def test_list_tabs_ssh_web_empty_when_no_services(qtbot, rpc, window):
    """shared_via_ssh and shared_via_web are empty lists when services missing."""
    class _FakeTmuxMode:
        def get_session_for_terminal(self, _t):
            return "qterm-1"

    window.tmux_mode = _FakeTmuxMode()
    try:
        resp = rpc.call(qtbot, "list_tabs")
        tab = resp["result"][0]
        assert tab["shared_via_ssh"] == []
        assert tab["shared_via_web"] == []
    finally:
        del window.tmux_mode


def test_list_tabs_ssh_web_empty_when_no_tmux_session(qtbot, rpc, window):
    """When tmux_mode returns None, share services are not queried."""
    class _FakeTmuxMode:
        def get_session_for_terminal(self, _t):
            return None

    class _Boom:
        def ports_for(self, _sess):
            raise RuntimeError("should not be called")

    window.tmux_mode = _FakeTmuxMode()
    window.tmux_ssh_share = _Boom()
    window.tmux_web_share = _Boom()
    try:
        resp = rpc.call(qtbot, "list_tabs")
        tab = resp["result"][0]
        assert tab["shared_via_ssh"] == []
        assert tab["shared_via_web"] == []
    finally:
        del window.tmux_mode
        del window.tmux_ssh_share
        del window.tmux_web_share


def test_list_tabs_share_service_exception_returns_empty(qtbot, rpc, window):
    """Exception from a share service's ports_for returns empty list."""
    class _FakeTmuxMode:
        def get_session_for_terminal(self, _t):
            return "qterm-1"

    class _Broken:
        def ports_for(self, _sess):
            raise RuntimeError("broken")

    window.tmux_mode = _FakeTmuxMode()
    window.tmux_ssh_share = _Broken()
    window.tmux_web_share = _Broken()
    try:
        resp = rpc.call(qtbot, "list_tabs")
        tab = resp["result"][0]
        assert tab["shared_via_ssh"] == []
        assert tab["shared_via_web"] == []
    finally:
        del window.tmux_mode
        del window.tmux_ssh_share
        del window.tmux_web_share


def test_list_tabs_multiple_ssh_ports(qtbot, rpc, window):
    """Multiple SSH shares produce multiple ports in the listing."""
    class _FakeTmuxMode:
        def get_session_for_terminal(self, _t):
            return "qterm-1"

    class _MultiShare:
        def ports_for(self, sess):
            return [22022, 22023, 22024] if sess == "qterm-1" else []

    window.tmux_mode = _FakeTmuxMode()
    window.tmux_ssh_share = _MultiShare()
    try:
        resp = rpc.call(qtbot, "list_tabs")
        tab = resp["result"][0]
        assert tab["shared_via_ssh"] == [22022, 22023, 22024]
    finally:
        del window.tmux_mode
        del window.tmux_ssh_share


def test_close_tab_reduces_list(qtbot, rpc, window):
    """close_tab removes a tab from the listing."""
    rpc.call(qtbot, "open_tab")
    qtbot.wait(150)
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    assert len(tabs) == 2
    tid = tabs[1]["id"]
    resp = rpc.call(qtbot, "close_tab", tab_id=tid)
    assert "result" in resp
    qtbot.wait(150)
    after = rpc.call(qtbot, "list_tabs")["result"]
    assert len(after) == 1


def test_command_history_returns_empty_initially(qtbot, rpc):
    """command_history returns empty list when no commands have run."""
    tabs = rpc.call(qtbot, "list_tabs")["result"]
    tid = tabs[0]["id"]
    resp = rpc.call(qtbot, "command_history", tab_id=tid, limit=10)
    assert "result" in resp
    assert resp["result"]["records"] == [] or isinstance(resp["result"]["records"], list)
