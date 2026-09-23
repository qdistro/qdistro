"""Tests for tmux SSH/web sharing transports."""

import base64
import os
import socket
import struct
import urllib.error
import urllib.request

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from qterminator.plugins.tmux_ssh_share import SSHShare, TmuxSSHShareService
from qterminator.plugins.tmux_web_share import (
    TmuxWebShareService,
    _load_authorized_keys,
    _verify_ed25519,
    _ws_accept,
)


def test_tmux_ssh_share_ports_prune_dead_entries(tmp_path):
    class _Proc:
        def poll(self):
            return 1

    svc = TmuxSSHShareService(
        bind="127.0.0.1",
        authorized_keys=str(tmp_path / "authorized_keys"),
    )
    share = SSHShare(
        session="qterm-1",
        bind="127.0.0.1",
        port=22022,
        proc=_Proc(),
        temp_dir=str(tmp_path),
        authorized_keys=str(tmp_path / "authorized_keys"),
    )
    svc._shares["qterm-1"] = [share]
    assert svc.ports_for("qterm-1") == []


def test_tmux_ssh_start_share_writes_forced_tmux_config(monkeypatch, tmp_path):
    captured = {}

    class _Proc:
        stderr = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("qterminator.plugins.tmux_ssh_share.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("qterminator.plugins.tmux_ssh_share._free_tcp_port", lambda _bind: 22222)

    def fake_run(argv, **_kwargs):
        captured["keygen"] = argv

    def fake_popen(argv, **_kwargs):
        captured["sshd"] = argv
        return _Proc()

    monkeypatch.setattr("qterminator.plugins.tmux_ssh_share.subprocess.run", fake_run)
    monkeypatch.setattr("qterminator.plugins.tmux_ssh_share.subprocess.Popen", fake_popen)
    svc = TmuxSSHShareService(
        bind="127.0.0.1",
        authorized_keys=str(tmp_path / "authorized_keys"),
    )
    share = svc.start_share("qterm-1")
    try:
        assert share.port == 22222
        config_path = captured["sshd"][captured["sshd"].index("-f") + 1]
        text = open(config_path, encoding="utf-8").read()
        assert "ForceCommand tmux attach -t qterm-1" in text
        assert f"AuthorizedKeysFile {tmp_path / 'authorized_keys'}" in text
        assert captured["keygen"][0] == "ssh-keygen"
    finally:
        share.stop()


def test_tmux_web_share_serves_browser_page():
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        # The share URL carries the per-share capability token.
        assert f"?t={share.token}" in share.url
        assert len(share.token) >= 32
        with urllib.request.urlopen(share.url, timeout=2) as resp:
            body = resp.read().decode("utf-8")
            # The bearer token must not leak via Referer or shared caches.
            assert resp.headers.get("Referrer-Policy") == "no-referrer"
            assert resp.headers.get("Cache-Control") == "no-store"
        assert "QTerminator tmux share" in body
        assert "WebSocket" in body
    finally:
        svc.stop_all()


def _raw_status(share, path, *, extra_headers="") -> str:
    """Send a raw WS upgrade for ``path`` and return the HTTP status line."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock = socket.create_connection(("127.0.0.1", share.port), timeout=2)
    sock.settimeout(2)
    try:
        sock.sendall((
            f"GET {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{share.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"{extra_headers}"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii"))
        head, _ = _read_ws_headers(sock)
        return head.splitlines()[0]
    finally:
        sock.close()


def test_web_page_requires_token():
    """J7: fetching the page without the token is refused (404) — the URL
    itself is the capability, so a port-scanning drive-by learns nothing."""
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        base = f"http://127.0.0.1:{share.port}/"
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base, timeout=2)
        assert ei.value.code == 404
    finally:
        svc.stop_all()


def test_websocket_upgrade_requires_token():
    """J7: a /ws upgrade with no token is rejected even on loopback — closes
    unauthenticated keystroke injection / frame reads by a drive-by page."""
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=False)
    share = svc.start_share("qterm-1")
    try:
        assert "403" in _raw_status(share, "/ws")
        assert "403" in _raw_status(share, "/ws?t=wrong-token")
        # Correct token still upgrades.
        assert "101" in _raw_status(share, f"/ws?t={share.token}")
    finally:
        svc.stop_all()


def test_websocket_upgrade_rejects_cross_origin():
    """J7: a cross-origin Origin header is rejected even with a valid token —
    WebSockets bypass the same-origin policy, so this blocks browser CSRF."""
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        path = f"/ws?t={share.token}"
        assert "403" in _raw_status(
            share, path, extra_headers="Origin: http://evil.example\r\n")
        # The classic origin-check bypass: a sandboxed/redirect-laundered
        # opaque origin sends the literal string "null" — must be rejected.
        assert "403" in _raw_status(
            share, path, extra_headers="Origin: null\r\n")
        # Same-origin (Origin == Host) is accepted.
        assert "101" in _raw_status(
            share, path,
            extra_headers=f"Origin: http://127.0.0.1:{share.port}\r\n")
    finally:
        svc.stop_all()


def test_websocket_same_origin_still_needs_token():
    """The origin gate and the token gate are independent: a same-origin
    request with a wrong/absent token is still rejected."""
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        same_origin = f"Origin: http://127.0.0.1:{share.port}\r\n"
        assert "403" in _raw_status(
            share, "/ws?t=wrong", extra_headers=same_origin)
        assert "403" in _raw_status(share, "/ws", extra_headers=same_origin)
        # A non-ASCII token can never match and must not crash the handler.
        assert "403" in _raw_status(share, "/ws?t=%ff%fe")
    finally:
        svc.stop_all()


def _masked_frame(text: str) -> bytes:
    payload = text.encode("utf-8")
    mask = b"abcd"
    header = bytearray([0x81])
    if len(payload) < 126:
        header.append(0x80 | len(payload))
    elif len(payload) < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", len(payload)))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", len(payload)))
    return bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _read_ws_headers(sock: socket.socket) -> tuple[str, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        data += sock.recv(1024)
    head, rest = data.split(b"\r\n\r\n", 1)
    return head.decode("ascii"), rest


def _recv_ws_text(sock: socket.socket, initial: bytes = b"") -> str:
    buf = bytearray(initial)

    def take(n: int) -> bytes:
        while len(buf) < n:
            buf.extend(sock.recv(n - len(buf)))
        out = bytes(buf[:n])
        del buf[:n]
        return out

    first = take(2)
    length = first[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", take(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", take(8))[0]
    return take(length).decode("utf-8")



def test_websocket_roundtrip_sends_input_to_tmux(monkeypatch):
    sent = []
    monkeypatch.setattr("qterminator.plugins.tmux_web_share._capture", lambda session: f"{session}:SCREEN")
    monkeypatch.setattr("qterminator.plugins.tmux_web_share._send_text", lambda session, text: sent.append((session, text)))

    svc = TmuxWebShareService(bind="127.0.0.1", read_only=False)
    share = svc.start_share("qterm-1")
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock = socket.create_connection(("127.0.0.1", share.port), timeout=2)
        sock.settimeout(2)
        try:
            req = (
                f"GET /ws?t={share.token} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{share.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(req.encode("ascii"))
            headers, rest = _read_ws_headers(sock)
            assert "101 Switching Protocols" in headers
            assert _ws_accept(key) in headers
            assert _recv_ws_text(sock, rest) == "qterm-1:SCREEN"
            sock.sendall(_masked_frame("x"))
            for _ in range(20):
                if sent:
                    break
                sock.settimeout(0.1)
                try:
                    _recv_ws_text(sock)
                except TimeoutError:
                    pass
            assert sent == [("qterm-1", "x")]
        finally:
            sock.close()
    finally:
        svc.stop_all()


def test_websocket_read_only_drops_input(monkeypatch):
    sent = []
    monkeypatch.setattr("qterminator.plugins.tmux_web_share._capture", lambda _session: "SCREEN")
    monkeypatch.setattr("qterminator.plugins.tmux_web_share._send_text", lambda session, text: sent.append((session, text)))
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        sock = socket.create_connection(("127.0.0.1", share.port), timeout=2)
        sock.settimeout(2)
        try:
            sock.sendall((
                f"GET /ws?t={share.token} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{share.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii"))
            headers, rest = _read_ws_headers(sock)
            assert "101 Switching Protocols" in headers
            _recv_ws_text(sock, rest)
            sock.sendall(_masked_frame("x"))
            sock.settimeout(0.2)
            try:
                _recv_ws_text(sock)
            except TimeoutError:
                pass
            assert sent == []
        finally:
            sock.close()
    finally:
        svc.stop_all()


# ---------------------------------------------------------------------------
# SSHShare.connect_string
# ---------------------------------------------------------------------------

def test_ssh_share_connect_string_format(tmp_path):
    import getpass

    from qterminator.plugins.tmux_ssh_share import SSHShare

    class _Proc:
        def poll(self):
            return None

    share = SSHShare(
        session="qterm-1", bind="10.0.0.5", port=22022,
        proc=_Proc(), temp_dir=str(tmp_path),
        authorized_keys=str(tmp_path / "ak"),
    )
    s = share.connect_string
    assert f"ssh -p 22022 {getpass.getuser()}@10.0.0.5" == s


def test_ssh_share_connect_string_with_0000_bind(tmp_path):
    """0.0.0.0 bind should map to 127.0.0.1 in connect string."""
    import getpass

    class _Proc:
        def poll(self):
            return None

    share = SSHShare(
        session="qterm-1", bind="0.0.0.0", port=22022,
        proc=_Proc(), temp_dir=str(tmp_path),
        authorized_keys=str(tmp_path / "ak"),
    )
    s = share.connect_string
    assert "127.0.0.1" in s
    assert "0.0.0.0" not in s
    assert f"{getpass.getuser()}@127.0.0.1" in s


# ---------------------------------------------------------------------------
# SSHShare.is_alive with dead process
# ---------------------------------------------------------------------------

def test_ssh_share_is_alive_with_dead_process(tmp_path):
    class _DeadProc:
        def poll(self):
            return 1

    share = SSHShare(
        session="qterm-1", bind="127.0.0.1", port=22022,
        proc=_DeadProc(), temp_dir=str(tmp_path),
        authorized_keys=str(tmp_path / "ak"),
    )
    assert share.is_alive() is False


def test_ssh_share_is_alive_with_running_process(tmp_path):
    class _LiveProc:
        def poll(self):
            return None

    share = SSHShare(
        session="qterm-1", bind="127.0.0.1", port=22022,
        proc=_LiveProc(), temp_dir=str(tmp_path),
        authorized_keys=str(tmp_path / "ak"),
    )
    assert share.is_alive() is True


# ---------------------------------------------------------------------------
# SSHShare.stop cleans up temp_dir
# ---------------------------------------------------------------------------

def test_ssh_share_stop_cleans_up_temp_dir(tmp_path):
    td = tmp_path / "sshd-temp"
    td.mkdir()
    (td / "host_key").write_text("fake")

    class _Proc:
        def poll(self):
            return None

        def terminate(self):
            self._terminated = True

        def wait(self, timeout=None):
            return 0

    share = SSHShare(
        session="qterm-1", bind="127.0.0.1", port=22022,
        proc=_Proc(), temp_dir=str(td),
        authorized_keys=str(tmp_path / "ak"),
    )
    share.stop()
    assert not td.exists()


# ---------------------------------------------------------------------------
# TmuxSSHShareService.available when tools missing
# ---------------------------------------------------------------------------

def test_ssh_service_available_when_tools_missing(monkeypatch):
    monkeypatch.setattr("qterminator.plugins.tmux_ssh_share.shutil.which", lambda name: None)
    svc = TmuxSSHShareService(bind="127.0.0.1")
    assert svc.available() is False


def test_ssh_service_available_when_sshd_only(monkeypatch):
    monkeypatch.setattr(
        "qterminator.plugins.tmux_ssh_share.shutil.which",
        lambda name: "/usr/sbin/sshd" if name == "sshd" else None,
    )
    svc = TmuxSSHShareService(bind="127.0.0.1")
    assert svc.available() is False


# ---------------------------------------------------------------------------
# TmuxSSHShareService.stop_all stops all sessions
# ---------------------------------------------------------------------------

def test_ssh_service_stop_all(tmp_path):
    stopped = []

    class _Proc:
        def poll(self):
            return None

        def terminate(self):
            stopped.append(True)

        def wait(self, timeout=None):
            return 0

    svc = TmuxSSHShareService(bind="127.0.0.1")
    td1 = tmp_path / "td1"
    td1.mkdir()
    td2 = tmp_path / "td2"
    td2.mkdir()
    s1 = SSHShare(
        session="qterm-1", bind="127.0.0.1", port=22022,
        proc=_Proc(), temp_dir=str(td1),
        authorized_keys=str(tmp_path / "ak"),
    )
    s2 = SSHShare(
        session="qterm-2", bind="127.0.0.1", port=22023,
        proc=_Proc(), temp_dir=str(td2),
        authorized_keys=str(tmp_path / "ak"),
    )
    svc._shares["qterm-1"] = [s1]
    svc._shares["qterm-2"] = [s2]
    svc.stop_all()
    assert len(stopped) == 2
    assert svc._shares == {}


# ---------------------------------------------------------------------------
# TmuxSSHShareService.shares_for prunes multiple dead entries
# ---------------------------------------------------------------------------

def test_ssh_shares_for_prunes_multiple_dead(tmp_path):
    class _DeadProc:
        def poll(self):
            return 1

    svc = TmuxSSHShareService(bind="127.0.0.1")
    shares = []
    for i in range(3):
        s = SSHShare(
            session="qterm-1", bind="127.0.0.1", port=22022 + i,
            proc=_DeadProc(), temp_dir=str(tmp_path / f"td{i}"),
            authorized_keys=str(tmp_path / "ak"),
        )
        shares.append(s)
    svc._shares["qterm-1"] = shares
    live = svc.shares_for("qterm-1")
    assert live == []
    assert "qterm-1" not in svc._shares


# ---------------------------------------------------------------------------
# WebShare.url format
# ---------------------------------------------------------------------------

def test_web_share_url_format():
    from qterminator.plugins.tmux_web_share import WebShare
    share = WebShare(
        session="qterm-1", bind="10.0.0.5", port=8080,
        read_only=False, server=None, thread=None,
    )
    assert share.url == "http://10.0.0.5:8080/"


def test_web_share_url_with_0000_bind():
    from qterminator.plugins.tmux_web_share import WebShare
    share = WebShare(
        session="qterm-1", bind="0.0.0.0", port=9090,
        read_only=True, server=None, thread=None,
    )
    assert share.url == "http://127.0.0.1:9090/"
    assert "0.0.0.0" not in share.url


# ---------------------------------------------------------------------------
# TmuxWebShareService.shares_for prunes stopped entries
# ---------------------------------------------------------------------------

def test_web_shares_for_prunes_stopped():
    from qterminator.plugins.tmux_web_share import TmuxWebShareService, WebShare
    svc = TmuxWebShareService(bind="127.0.0.1")
    alive = WebShare(
        session="qterm-1", bind="127.0.0.1", port=8080,
        read_only=False, server=None, thread=None, stopped=False,
    )
    dead = WebShare(
        session="qterm-1", bind="127.0.0.1", port=8081,
        read_only=False, server=None, thread=None, stopped=True,
    )
    svc._shares["qterm-1"] = [alive, dead]
    live = svc.shares_for("qterm-1")
    assert len(live) == 1
    assert live[0].port == 8080


# ---------------------------------------------------------------------------
# TmuxWebShareService.stop_all stops all sessions
# ---------------------------------------------------------------------------

def test_web_service_stop_all():
    svc = TmuxWebShareService(bind="127.0.0.1")
    s1 = svc.start_share("qterm-1")
    s2 = svc.start_share("qterm-2")
    assert not s1.stopped
    assert not s2.stopped
    svc.stop_all()
    assert s1.stopped
    assert s2.stopped
    assert svc._shares == {}


# ---------------------------------------------------------------------------
# _ws_accept produces correct hash
# ---------------------------------------------------------------------------

def test_ws_accept_known_value():
    """RFC 6455 example: known key produces known accept value."""
    import hashlib
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    expected = base64.b64encode(
        hashlib.sha1((key + magic).encode("ascii")).digest()
    ).decode("ascii")
    assert _ws_accept(key) == expected
    assert _ws_accept(key) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


# ---------------------------------------------------------------------------
# _free_tcp_port returns a usable port
# ---------------------------------------------------------------------------

def test_free_tcp_port_returns_usable_port():
    from qterminator.plugins.tmux_web_share import _free_tcp_port
    port = _free_tcp_port("127.0.0.1")
    assert isinstance(port, int)
    assert 1024 <= port <= 65535
    # Verify we can actually bind to it (ephemeral port race is possible
    # but very unlikely in a test).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


# ---------------------------------------------------------------------------
# Multiple web shares for same session
# ---------------------------------------------------------------------------

def test_multiple_web_shares_for_same_session():
    svc = TmuxWebShareService(bind="127.0.0.1")
    s1 = svc.start_share("qterm-1")
    s2 = svc.start_share("qterm-1")
    try:
        assert s1.port != s2.port
        live = svc.shares_for("qterm-1")
        assert len(live) == 2
        ports = svc.ports_for("qterm-1")
        assert s1.port in ports
        assert s2.port in ports
    finally:
        svc.stop_all()


# ---------------------------------------------------------------------------
# Web share HTML page contains expected elements
# ---------------------------------------------------------------------------

def test_web_share_html_page_content():
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        with urllib.request.urlopen(share.url, timeout=2) as resp:
            body = resp.read().decode("utf-8")
        assert "<!doctype html>" in body.lower() or "<!DOCTYPE html>" in body
        assert "<pre id=\"term\"" in body
        assert "WebSocket" in body
        assert "keydown" in body
        assert "QTerminator tmux share" in body
    finally:
        svc.stop_all()


# ---------------------------------------------------------------------------
# _WebHandler returns 200 for GET /
# ---------------------------------------------------------------------------

def test_web_handler_returns_200_for_get():
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=True)
    share = svc.start_share("qterm-1")
    try:
        with urllib.request.urlopen(share.url, timeout=2) as resp:
            assert resp.status == 200
            content_type = resp.headers.get("Content-Type", "")
            assert "text/html" in content_type
    finally:
        svc.stop_all()


# ---------------------------------------------------------------------------
# Security: non-loopback bind requires auth
# ---------------------------------------------------------------------------

def test_web_share_public_bind_no_keys_raises():
    """Non-loopback bind without authorized_keys raises."""
    with pytest.raises(RuntimeError, match="at least one Ed25519 key"):
        TmuxWebShareService(bind="0.0.0.0", read_only=False,
                            authorized_keys_path="/nonexistent/path")


def test_web_share_public_read_only_requires_keys(tmp_path):
    """Non-loopback read-only still exposes terminal contents."""
    with pytest.raises(RuntimeError, match="at least one Ed25519 key"):
        TmuxWebShareService(
            bind="0.0.0.0", read_only=True,
            authorized_keys_path=str(tmp_path / "missing"),
        )


def test_web_share_public_read_only_with_keys_requires_auth(tmp_path):
    pk = Ed25519PrivateKey.generate()
    akfile = tmp_path / "authorized_keys"
    akfile.write_text(_make_ssh_ed25519_line(pk) + "\n")
    svc = TmuxWebShareService(
        bind="0.0.0.0", read_only=True,
        authorized_keys_path=str(akfile),
    )
    assert svc.read_only is True
    assert svc._auth_required is True


def test_web_share_loopback_allows_read_write():
    """Loopback bind permits read_only=False."""
    svc = TmuxWebShareService(bind="127.0.0.1", read_only=False)
    assert svc.read_only is False


def test_web_share_default_is_read_only():
    """Default read_only is True."""
    svc = TmuxWebShareService(bind="127.0.0.1")
    assert svc.read_only is True


# ---------------------------------------------------------------------------
# Security: SSH session name validation
# ---------------------------------------------------------------------------

def test_ssh_start_share_rejects_unsafe_session_name(monkeypatch, tmp_path):
    """Session names with shell metacharacters are rejected."""
    monkeypatch.setattr(
        "qterminator.plugins.tmux_ssh_share.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    svc = TmuxSSHShareService(
        bind="127.0.0.1",
        authorized_keys=str(tmp_path / "authorized_keys"),
    )
    import pytest as _pytest
    with _pytest.raises(ValueError, match="unsafe characters"):
        svc.start_share("qterm-1; rm -rf /")
    with _pytest.raises(ValueError, match="unsafe characters"):
        svc.start_share("session\nnewline")
    with _pytest.raises(ValueError, match="unsafe characters"):
        svc.start_share("session with spaces")


def test_ssh_start_share_accepts_safe_session_names(tmp_path):
    """Clean session names pass validation."""
    svc = TmuxSSHShareService(
        bind="127.0.0.1",
        authorized_keys=str(tmp_path / "authorized_keys"),
    )
    for name in ["qterm-1", "my_session.2", "Test-Session_3"]:
        assert svc._SAFE_SESSION.match(name), f"{name} should be valid"


# ---------------------------------------------------------------------------
# WebSocket: opcode handling
# ---------------------------------------------------------------------------

def test_ws_recv_text_handles_close_opcode():
    """Close frame (opcode 8) returns None."""
    import io

    from qterminator.plugins.tmux_web_share import _ws_recv_text

    frame = bytes([0x88, 0x00])

    class _FakeSock:
        def __init__(self, data):
            self._buf = io.BytesIO(data)
        def recv(self, n):
            return self._buf.read(n)

    assert _ws_recv_text(_FakeSock(frame)) is None


def test_ws_recv_text_handles_ping_opcode():
    """Ping frame (opcode 9) returns empty string (not None)."""
    from qterminator.plugins.tmux_web_share import _ws_recv_text

    frame = bytes([0x89, 0x00])

    class _FakeSock:
        def __init__(self, data):
            import io
            self._buf = io.BytesIO(data)
        def recv(self, n):
            return self._buf.read(n)

    assert _ws_recv_text(_FakeSock(frame)) == ""


# ---------------------------------------------------------------------------
# Ed25519 authorized_keys helpers
# ---------------------------------------------------------------------------

def _make_ssh_ed25519_line(privkey: Ed25519PrivateKey, comment: str = "test") -> str:
    """Build an ``ssh-ed25519 <b64> <comment>`` line from a private key."""
    raw = privkey.public_key().public_bytes_raw()
    blob = (
        struct.pack("!I", 11) + b"ssh-ed25519"
        + struct.pack("!I", 32) + raw
    )
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


def test_load_authorized_keys_parses_valid_file(tmp_path):
    pk1 = Ed25519PrivateKey.generate()
    pk2 = Ed25519PrivateKey.generate()
    akfile = tmp_path / "authorized_keys"
    akfile.write_text(
        f"# a comment\n"
        f"{_make_ssh_ed25519_line(pk1, 'user1')}\n"
        f"\n"
        f"ssh-rsa AAAA... ignored-rsa-key\n"
        f"{_make_ssh_ed25519_line(pk2, 'user2')}\n"
    )
    keys = _load_authorized_keys(str(akfile))
    assert len(keys) == 2
    assert keys[0].public_bytes_raw() == pk1.public_key().public_bytes_raw()
    assert keys[1].public_bytes_raw() == pk2.public_key().public_bytes_raw()


def test_load_authorized_keys_missing_file(tmp_path):
    keys = _load_authorized_keys(str(tmp_path / "nonexistent"))
    assert keys == []


def test_load_authorized_keys_with_options_prefix(tmp_path):
    """Lines with leading options (e.g. from=...) are parsed correctly."""
    pk = Ed25519PrivateKey.generate()
    bare_line = _make_ssh_ed25519_line(pk, "bare")
    # Construct a line with options before the key type
    parts = bare_line.split()
    options_line = f'from="10.0.0.0/8" {parts[0]} {parts[1]} with-options'
    akfile = tmp_path / "authorized_keys"
    akfile.write_text(f"{options_line}\n")
    keys = _load_authorized_keys(str(akfile))
    assert len(keys) == 1
    assert keys[0].public_bytes_raw() == pk.public_key().public_bytes_raw()


def test_load_authorized_keys_rejects_malformed_blob(tmp_path):
    """Malformed base64 or wrong blob length is rejected."""
    akfile = tmp_path / "authorized_keys"
    akfile.write_text(
        # truncated blob
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA truncated\n"
        # invalid base64
        "ssh-ed25519 !!!not-base64!!! bad\n"
        # wrong key type in blob (blob says rsa but line says ed25519)
        "ssh-ed25519 AAAAB3NzaC1yc2EAAAA= fake-rsa\n"
    )
    keys = _load_authorized_keys(str(akfile))
    assert keys == []


def test_load_authorized_keys_rejects_wrong_blob_length(tmp_path):
    """Blob with extra trailing bytes is rejected."""
    pk = Ed25519PrivateKey.generate()
    raw = pk.public_key().public_bytes_raw()
    # Build correct blob + extra byte
    blob = (
        struct.pack("!I", 11) + b"ssh-ed25519"
        + struct.pack("!I", 32) + raw + b"\x00"
    )
    line = f"ssh-ed25519 {base64.b64encode(blob).decode()} extra-byte"
    akfile = tmp_path / "authorized_keys"
    akfile.write_text(line + "\n")
    keys = _load_authorized_keys(str(akfile))
    assert keys == []


def test_verify_ed25519_valid_signature():
    pk = Ed25519PrivateKey.generate()
    challenge = os.urandom(32)
    sig = pk.sign(challenge)
    authorized = [pk.public_key()]
    assert _verify_ed25519(authorized, challenge, pk.public_key().public_bytes_raw(), sig)


def test_verify_ed25519_wrong_signature():
    pk = Ed25519PrivateKey.generate()
    challenge = os.urandom(32)
    bad_sig = b"\x00" * 64
    authorized = [pk.public_key()]
    assert not _verify_ed25519(authorized, challenge, pk.public_key().public_bytes_raw(), bad_sig)


def test_verify_ed25519_unauthorized_key():
    pk = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    challenge = os.urandom(32)
    sig = other.sign(challenge)
    authorized = [pk.public_key()]
    assert not _verify_ed25519(authorized, challenge, other.public_key().public_bytes_raw(), sig)


def test_verify_ed25519_malformed_pubkey():
    pk = Ed25519PrivateKey.generate()
    challenge = os.urandom(32)
    authorized = [pk.public_key()]
    assert not _verify_ed25519(authorized, challenge, b"short", b"\x00" * 64)


# ---------------------------------------------------------------------------
# Ed25519 WebSocket auth: full integration tests
# ---------------------------------------------------------------------------

def _ws_connect_and_upgrade(port: int, token: str = "") -> tuple[socket.socket, str, bytes]:
    """Open a raw WebSocket connection, return (sock, headers, leftover)."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    sock.settimeout(3)
    path = f"/ws?t={token}" if token else "/ws"
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode("ascii"))
    headers, rest = _read_ws_headers(sock)
    return sock, headers, rest


def test_auth_challenge_response_valid_key(monkeypatch, tmp_path):
    """Full auth flow: valid key signs the challenge and gets AUTH_OK."""
    monkeypatch.setattr(
        "qterminator.plugins.tmux_web_share._capture",
        lambda session: f"{session}:OK",
    )
    sent_to_tmux = []
    monkeypatch.setattr(
        "qterminator.plugins.tmux_web_share._send_text",
        lambda session, text: sent_to_tmux.append((session, text)),
    )

    pk = Ed25519PrivateKey.generate()
    akfile = tmp_path / "authorized_keys"
    akfile.write_text(_make_ssh_ed25519_line(pk) + "\n")

    svc = TmuxWebShareService(
        bind="127.0.0.1", read_only=False,
        authorized_keys_path=str(akfile),
    )
    # Manually enable auth to simulate non-loopback behavior on loopback
    svc._auth_required = True
    svc._authorized_keys = _load_authorized_keys(str(akfile))
    share = svc.start_share("qterm-auth")
    # Patch the server's auth settings too
    share.server.auth_required = True
    share.server.authorized_keys = svc._authorized_keys
    try:
        sock, headers, rest = _ws_connect_and_upgrade(share.port, share.token)
        try:
            assert "101 Switching Protocols" in headers
            # First message should be AUTH_CHALLENGE
            challenge_msg = _recv_ws_text(sock, rest)
            assert challenge_msg.startswith("AUTH_CHALLENGE ")
            challenge_b64 = challenge_msg.split(" ", 1)[1]
            challenge = base64.b64decode(challenge_b64)
            assert len(challenge) == 32

            # Sign the challenge
            sig = pk.sign(challenge)
            pubkey_b64 = base64.b64encode(
                pk.public_key().public_bytes_raw()
            ).decode("ascii")
            sig_b64 = base64.b64encode(sig).decode("ascii")
            sock.sendall(_masked_frame(f"AUTH_RESPONSE {pubkey_b64} {sig_b64}"))

            # Expect AUTH_OK
            auth_result = _recv_ws_text(sock)
            assert auth_result == "AUTH_OK"

            # Now we should get terminal output
            screen = _recv_ws_text(sock)
            assert screen == "qterm-auth:OK"

            # Input should work (not read-only)
            sock.sendall(_masked_frame("z"))
            for _ in range(20):
                if sent_to_tmux:
                    break
                sock.settimeout(0.1)
                try:
                    _recv_ws_text(sock)
                except TimeoutError:
                    pass
            assert sent_to_tmux == [("qterm-auth", "z")]
        finally:
            sock.close()
    finally:
        svc.stop_all()


def test_auth_rejects_invalid_signature(monkeypatch, tmp_path):
    """Auth flow rejects a bad signature with AUTH_FAIL."""
    monkeypatch.setattr(
        "qterminator.plugins.tmux_web_share._capture",
        lambda session: "SCREEN",
    )

    pk = Ed25519PrivateKey.generate()
    akfile = tmp_path / "authorized_keys"
    akfile.write_text(_make_ssh_ed25519_line(pk) + "\n")

    svc = TmuxWebShareService(
        bind="127.0.0.1", read_only=False,
        authorized_keys_path=str(akfile),
    )
    svc._auth_required = True
    svc._authorized_keys = _load_authorized_keys(str(akfile))
    share = svc.start_share("qterm-bad")
    share.server.auth_required = True
    share.server.authorized_keys = svc._authorized_keys
    try:
        sock, headers, rest = _ws_connect_and_upgrade(share.port, share.token)
        try:
            challenge_msg = _recv_ws_text(sock, rest)
            assert challenge_msg.startswith("AUTH_CHALLENGE ")

            # Send a bogus signature
            pubkey_b64 = base64.b64encode(
                pk.public_key().public_bytes_raw()
            ).decode("ascii")
            bad_sig = base64.b64encode(b"\x00" * 64).decode("ascii")
            sock.sendall(_masked_frame(f"AUTH_RESPONSE {pubkey_b64} {bad_sig}"))

            auth_result = _recv_ws_text(sock)
            assert auth_result == "AUTH_FAIL"
        finally:
            sock.close()
    finally:
        svc.stop_all()


def test_auth_rejects_unauthorized_key(monkeypatch, tmp_path):
    """Auth flow rejects a key not in authorized_keys."""
    monkeypatch.setattr(
        "qterminator.plugins.tmux_web_share._capture",
        lambda session: "SCREEN",
    )

    authorized_pk = Ed25519PrivateKey.generate()
    intruder_pk = Ed25519PrivateKey.generate()
    akfile = tmp_path / "authorized_keys"
    akfile.write_text(_make_ssh_ed25519_line(authorized_pk) + "\n")

    svc = TmuxWebShareService(
        bind="127.0.0.1", read_only=False,
        authorized_keys_path=str(akfile),
    )
    svc._auth_required = True
    svc._authorized_keys = _load_authorized_keys(str(akfile))
    share = svc.start_share("qterm-intruder")
    share.server.auth_required = True
    share.server.authorized_keys = svc._authorized_keys
    try:
        sock, headers, rest = _ws_connect_and_upgrade(share.port, share.token)
        try:
            challenge_msg = _recv_ws_text(sock, rest)
            assert challenge_msg.startswith("AUTH_CHALLENGE ")
            challenge = base64.b64decode(challenge_msg.split(" ", 1)[1])

            # Sign with the intruder's key (valid sig, but wrong key)
            sig = intruder_pk.sign(challenge)
            pubkey_b64 = base64.b64encode(
                intruder_pk.public_key().public_bytes_raw()
            ).decode("ascii")
            sig_b64 = base64.b64encode(sig).decode("ascii")
            sock.sendall(_masked_frame(f"AUTH_RESPONSE {pubkey_b64} {sig_b64}"))

            auth_result = _recv_ws_text(sock)
            assert auth_result == "AUTH_FAIL"
        finally:
            sock.close()
    finally:
        svc.stop_all()


def test_loopback_share_skips_auth(monkeypatch):
    """Loopback shares skip the auth handshake entirely."""
    monkeypatch.setattr(
        "qterminator.plugins.tmux_web_share._capture",
        lambda session: f"{session}:SCREEN",
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_web_share._send_text",
        lambda session, text: None,
    )

    svc = TmuxWebShareService(bind="127.0.0.1", read_only=False)
    share = svc.start_share("qterm-local")
    try:
        sock, headers, rest = _ws_connect_and_upgrade(share.port, share.token)
        try:
            assert "101 Switching Protocols" in headers
            # First message should be terminal output, NOT AUTH_CHALLENGE
            first_msg = _recv_ws_text(sock, rest)
            assert not first_msg.startswith("AUTH_CHALLENGE")
            assert first_msg == "qterm-local:SCREEN"
        finally:
            sock.close()
    finally:
        svc.stop_all()


def test_nonloopback_rw_requires_keys_file(tmp_path):
    """Non-loopback read-write without authorized_keys file raises."""
    with pytest.raises(RuntimeError, match="Non-loopback web share"):
        TmuxWebShareService(
            bind="0.0.0.0", read_only=False,
            authorized_keys_path=str(tmp_path / "missing"),
        )


def test_nonloopback_rw_with_empty_keys_file_raises(tmp_path):
    """Non-loopback read-write with empty authorized_keys raises."""
    akfile = tmp_path / "authorized_keys"
    akfile.write_text("# only comments\n\n")
    with pytest.raises(RuntimeError, match="Non-loopback web share"):
        TmuxWebShareService(
            bind="0.0.0.0", read_only=False,
            authorized_keys_path=str(akfile),
        )


def test_nonloopback_rw_with_valid_keys_succeeds(tmp_path, monkeypatch):
    """Non-loopback read-write with authorized_keys succeeds."""
    monkeypatch.setattr(
        "qterminator.plugins.tmux_web_share._capture",
        lambda session: "SCREEN",
    )
    pk = Ed25519PrivateKey.generate()
    akfile = tmp_path / "authorized_keys"
    akfile.write_text(_make_ssh_ed25519_line(pk) + "\n")
    svc = TmuxWebShareService(
        bind="0.0.0.0", read_only=False,
        authorized_keys_path=str(akfile),
    )
    assert svc.read_only is False
    assert svc._auth_required is True
    share = svc.start_share("qterm-pub")
    try:
        assert share.read_only is False
    finally:
        svc.stop_all()


# ---------------------------------------------------------------------------
# TmuxSSHShareService session-name validation
#
# The session name is interpolated into the sshd ForceCommand
# ``tmux attach -t {session}``. An unsafe name (spaces, ``;``, ``$()``,
# backticks, newlines) could break out of that command, so start_share
# enforces ``^[a-zA-Z0-9_.\-]+$`` and raises ValueError before spawning
# anything. These tests assert the gate rejects hostile names and accepts
# benign ones. All subprocess work is mocked -- nothing is executed.
# ---------------------------------------------------------------------------

class _FakeSSHDProc:
    stderr = None

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def _ssh_service_with_mocked_tools(monkeypatch, tmp_path):
    """A service whose external tools are all present and mocked out."""
    monkeypatch.setattr(
        "qterminator.plugins.tmux_ssh_share.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_ssh_share._free_tcp_port", lambda _bind: 22321,
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_ssh_share.subprocess.run", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "qterminator.plugins.tmux_ssh_share.subprocess.Popen",
        lambda *a, **k: _FakeSSHDProc(),
    )
    return TmuxSSHShareService(
        bind="127.0.0.1", authorized_keys=str(tmp_path / "authorized_keys"),
    )


@pytest.mark.parametrize("bad_name", [
    "with spaces",
    "evil; rm -rf /",
    "$(reboot)",
    "`whoami`",
    "name&&other",
    "pipe|attack",
    "newline\nattack",
    "quote'attack",
    "slash/attack",
    # Trailing newline / CR: a $ anchor matches just before a trailing \n,
    # so "attack\n" would be wrongly accepted (and then keyed into _shares
    # and fed to the sshd ForceCommand). \Z rejects both.
    "attack\n",
    "attack\r",
    "",
])
def test_start_share_rejects_unsafe_session_name(monkeypatch, tmp_path, bad_name):
    svc = _ssh_service_with_mocked_tools(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="unsafe characters"):
        svc.start_share(bad_name)
    # Nothing was registered for the rejected name.
    assert svc._shares == {}


@pytest.mark.parametrize("good_name", [
    "qterm-1",
    "session_2",
    "my.session",
    "ABC123",
    "a-b_c.d",
])
def test_start_share_accepts_safe_session_name(monkeypatch, tmp_path, good_name):
    svc = _ssh_service_with_mocked_tools(monkeypatch, tmp_path)
    share = svc.start_share(good_name)
    try:
        assert share.session == good_name
        assert svc._shares.get(good_name) == [share]
    finally:
        # Mocked proc: stop() is a no-op-ish cleanup.
        svc.stop_all()
