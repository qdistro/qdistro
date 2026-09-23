"""Browser/WebSocket sharing for tmux sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import select
import socket
import struct
import subprocess
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from PyQt6.QtWidgets import QMessageBox

from qterminator.config import CONFIG_DIR, Config
from qterminator.plugin import MenuProvider

_AUTHORIZED_KEYS_PATH = os.path.join(CONFIG_DIR, "authorized_keys")


def _parse_ssh_ed25519_blob(blob: bytes) -> Ed25519PublicKey | None:
    """Validate and extract an Ed25519 public key from an SSH wire-format blob.

    The expected structure is:
        uint32(11) + "ssh-ed25519" + uint32(32) + <32-byte-key>
    Total: 51 bytes exactly.
    Returns None if the blob is malformed.
    """
    if len(blob) != 51:
        return None
    # Validate key-type length field
    type_len = struct.unpack("!I", blob[0:4])[0]
    if type_len != 11:
        return None
    # Validate key-type string
    if blob[4:15] != b"ssh-ed25519":
        return None
    # Validate key-data length field
    key_len = struct.unpack("!I", blob[15:19])[0]
    if key_len != 32:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(blob[19:51])
    except Exception:
        return None


def _load_authorized_keys(path: str) -> list[Ed25519PublicKey]:
    """Parse an ``authorized_keys`` file and return Ed25519 public keys.

    Supports the standard OpenSSH format: optional leading options,
    then ``ssh-ed25519 <base64-key> [comment]``.  Lines with options
    like ``from="..." ssh-ed25519 AAAA... user`` are handled by
    scanning for the ``ssh-ed25519`` token.

    Blank lines and lines starting with ``#`` are skipped.
    Non-Ed25519 key types are silently ignored.
    """
    keys: list[Ed25519PublicKey] = []
    if not os.path.isfile(path):
        return keys
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            # Find the ssh-ed25519 token (may be preceded by options)
            try:
                idx = parts.index("ssh-ed25519")
            except ValueError:
                continue
            if idx + 1 >= len(parts):
                continue
            b64_token = parts[idx + 1]
            try:
                raw = base64.b64decode(b64_token, validate=True)
            except Exception:
                continue
            key = _parse_ssh_ed25519_blob(raw)
            if key is not None:
                keys.append(key)
    return keys


def _verify_ed25519(
    authorized_keys: list[Ed25519PublicKey],
    challenge: bytes,
    pubkey_bytes: bytes,
    signature: bytes,
) -> bool:
    """Verify *signature* over *challenge* using *pubkey_bytes*.

    Returns True only if *pubkey_bytes* matches one of the
    *authorized_keys* and the signature is valid.
    """
    try:
        client_key = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
    except Exception:
        return False

    # Check if the client's public key matches any authorized key.
    # We iterate all keys and use hmac.compare_digest for each comparison
    # to avoid leaking which key (if any) matched via timing.
    client_raw = client_key.public_bytes_raw()
    matched = False
    for ak in authorized_keys:
        if hmac.compare_digest(ak.public_bytes_raw(), client_raw):
            matched = True
            # Do not break — iterate all keys to avoid timing side-channel

    # Always verify the signature regardless of whether the key matched,
    # so that the timing does not leak key-membership information.
    try:
        client_key.verify(signature, challenge)
        sig_ok = True
    except InvalidSignature:
        sig_ok = False

    return matched and sig_ok


_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>QTerminator tmux share</title>
<style>
body { margin: 0; background: #111; color: #ddd; font: 14px/1.25 monospace; }
#term { white-space: pre; padding: 12px; outline: none; min-height: 100vh; }
#auth-msg { padding: 24px; color: #f88; font-size: 16px; }
</style>
</head>
<body>
<div id="auth-msg" style="display:none"></div>
<pre id="term" tabindex="0"></pre>
<script>
const term = document.getElementById("term");
const authMsg = document.getElementById("auth-msg");
// Carry the per-share token (in this page's query string) onto the WS
// upgrade — the server rejects a /ws upgrade without it.
const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws${location.search}`);
let authenticated = false;
let authRequired = false;
ws.onmessage = (ev) => {
  const data = ev.data;
  if (!authenticated && data.startsWith("AUTH_CHALLENGE ")) {
    authRequired = true;
    term.style.display = "none";
    authMsg.style.display = "block";
    authMsg.textContent = "Authentication required. Use the qterminator CLI to set up key-based auth. " +
      "Browser-side key signing is not yet supported.";
    return;
  }
  if (!authenticated && data === "AUTH_OK") {
    authenticated = true;
    authMsg.style.display = "none";
    term.style.display = "block";
    term.focus();
    return;
  }
  if (!authenticated && data === "AUTH_FAIL") {
    authMsg.textContent = "Authentication failed. Connection closed.";
    return;
  }
  if (!authRequired) { authenticated = true; }
  term.textContent = data;
};
term.focus();
term.addEventListener("keydown", (ev) => {
  if (!authenticated) return;
  const special = {Enter:"\\r", Backspace:"\\x7f", Tab:"\\t",
    ArrowUp:"\\x1b[A", ArrowDown:"\\x1b[B", ArrowRight:"\\x1b[C", ArrowLeft:"\\x1b[D"};
  let text = special[ev.key] || (ev.key.length === 1 ? ev.key : "");
  if (ev.ctrlKey && ev.key.length === 1) text = String.fromCharCode(ev.key.toLowerCase().charCodeAt(0) - 96);
  if (text) { ws.send(text); ev.preventDefault(); }
});
</script></body></html>
"""


def _free_tcp_port(bind: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((bind, 0))
        return int(sock.getsockname()[1])


def _ws_accept(key: str) -> str:
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    raw = hashlib.sha1((key + magic).encode("ascii")).digest()
    return base64.b64encode(raw).decode("ascii")


def _ws_send_text(sock: socket.socket, text: str):
    data = text.encode("utf-8", errors="replace")
    header = bytearray([0x81])
    if len(data) < 126:
        header.append(len(data))
    elif len(data) < 65536:
        header.append(126)
        header.extend(struct.pack("!H", len(data)))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", len(data)))
    sock.sendall(bytes(header) + data)


_WS_MAX_PAYLOAD = 1_048_576  # 1 MiB — reject frames larger than this


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes, blocking until complete or EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("connection closed during recv_exact")
        buf.extend(chunk)
    return bytes(buf)


def _ws_recv_text(sock: socket.socket) -> str | None:
    first = _recv_exact(sock, 2)
    opcode = first[0] & 0x0F
    if opcode == 0x8:  # close
        return None
    if opcode == 0x9:  # ping → send pong
        return ""
    if opcode not in (0x1, 0x0):  # only text and continuation
        return ""
    length = first[1] & 0x7F
    masked = bool(first[1] & 0x80)
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > _WS_MAX_PAYLOAD:
        return None
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return payload.decode("utf-8", errors="replace")


def _capture(session: str) -> str:
    proc = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", session],
        capture_output=True,
        text=True,
        timeout=1,
    )
    if proc.returncode != 0:
        return proc.stderr.strip()
    return proc.stdout.rstrip("\n")


def _send_text(session: str, text: str):
    if text:
        subprocess.run(
            ["tmux", "send-keys", "-t", session, "-l", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
        )


class _WebHandler(BaseHTTPRequestHandler):
    server_version = "QTerminatorTmuxWeb/0.1"

    def log_message(self, _fmt, *_args):
        return

    def _token_ok(self) -> bool:
        """True if the request carries the share's per-URL token.

        The token is an unguessable capability minted per share
        (``secrets.token_urlsafe``). It gates BOTH serving the page and the
        WS upgrade, including on loopback — so a drive-by page that guesses
        the ephemeral port still cannot open a live terminal stream (read)
        nor inject keystrokes (write). Compared in constant time."""
        want = getattr(self.server, "token", "")
        if not want:
            return True
        got = parse_qs(urlsplit(self.path).query).get("t", [""])[0]
        # The token is urlsafe-base64 ASCII; a non-ASCII ``t`` (e.g. a
        # percent-escaped ``%ff``) can never match, and hmac.compare_digest
        # raises TypeError on non-ASCII str — so reject early, fail closed.
        if not got.isascii():
            return False
        return hmac.compare_digest(got, want)

    def _origin_ok(self) -> bool:
        """Reject a WS upgrade carrying a *cross-origin* ``Origin`` header.

        WebSocket connections are NOT covered by the same-origin policy, so
        a malicious page in any browser can open ``ws://127.0.0.1:<port>/ws``
        and read live terminal frames. Browsers always attach an ``Origin``
        on the WS handshake, so we require it to match our own ``Host``. A
        missing ``Origin`` means a non-browser client (CLI/programmatic),
        which the token already gates.

        This is defense-in-depth; the per-share token is the load-bearing
        control (a DNS-rebinding attacker can forge a matching Origin/Host
        pair but still lacks the token). We compare the *authority* only
        (not the scheme) on purpose, so the share keeps working behind a
        TLS-terminating reverse proxy (browser Origin ``https://host`` vs the
        proxied ``Host``). An empty authority — e.g. ``Origin: null`` from a
        sandboxed iframe or a redirect-laundered origin — is rejected."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        netloc = urlsplit(origin).netloc
        return bool(netloc) and netloc.lower() == self.headers.get(
            "Host", "").lower()

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            # Two independent barriers before any upgrade: same-origin
            # (blocks drive-by browser CSRF) AND the per-share token.
            if not self._origin_ok() or not self._token_ok():
                self.send_error(403)
                return
            self._websocket()
            return
        # The page itself is a capability: without the token we don't even
        # confirm a share exists here.
        if not self._token_ok():
            self.send_error(404)
            return
        body = _HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # The URL carries the bearer token — keep it out of Referer headers
        # and shared caches (defense-in-depth for the capability token).
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _do_auth(self, sock: socket.socket) -> bool:
        """Run the Ed25519 challenge-response handshake.

        Returns True if the client is authenticated, False otherwise.
        The caller should close the connection on False.

        NOTE: This auth prevents unauthorized access but does not protect
        against active network attackers (MITM).  For that, TLS should
        be layered on top (e.g. via a reverse proxy).  The challenge is
        a fresh 32-byte nonce from os.urandom, so passive replay is
        not possible.
        """
        authorized_keys = self.server.authorized_keys
        challenge = os.urandom(32)
        challenge_b64 = base64.b64encode(challenge).decode("ascii")
        try:
            _ws_send_text(sock, f"AUTH_CHALLENGE {challenge_b64}")
        except OSError:
            return False

        # Wait for AUTH_RESPONSE (timeout 10 s)
        sock.settimeout(10.0)
        try:
            msg = _ws_recv_text(sock)
        except (OSError, TimeoutError):
            return False
        finally:
            sock.settimeout(None)

        if msg is None or not msg.startswith("AUTH_RESPONSE "):
            try:
                _ws_send_text(sock, "AUTH_FAIL")
            except OSError:
                pass
            return False

        parts = msg.split(" ", 2)
        if len(parts) != 3:
            try:
                _ws_send_text(sock, "AUTH_FAIL")
            except OSError:
                pass
            return False

        try:
            pubkey_bytes = base64.b64decode(parts[1], validate=True)
            signature = base64.b64decode(parts[2], validate=True)
        except Exception:
            try:
                _ws_send_text(sock, "AUTH_FAIL")
            except OSError:
                pass
            return False

        if _verify_ed25519(authorized_keys, challenge, pubkey_bytes, signature):
            try:
                _ws_send_text(sock, "AUTH_OK")
            except OSError:
                return False
            return True
        else:
            try:
                _ws_send_text(sock, "AUTH_FAIL")
            except OSError:
                pass
            return False

    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400)
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", _ws_accept(key))
        self.end_headers()
        sock = self.connection
        sock.setblocking(True)
        session = self.server.share.session
        read_only = self.server.share.read_only

        # Auth required for non-loopback, non-read-only shares
        if self.server.auth_required:
            if not self._do_auth(sock):
                try:
                    sock.close()
                except OSError:
                    pass
                return

        while not self.server.share.stopped:
            try:
                _ws_send_text(sock, _capture(session))
            except OSError:
                return
            ready, _, _ = select.select([sock], [], [], 0.25)
            if ready:
                try:
                    sock.settimeout(2.0)
                    text = _ws_recv_text(sock)
                    sock.settimeout(None)
                except OSError:
                    return
                if text is None:
                    return
                if text and not read_only:
                    _send_text(session, text)


@dataclass
class WebShare:
    session: str
    bind: str
    port: int
    read_only: bool
    server: ThreadingHTTPServer
    thread: threading.Thread
    token: str = ""
    stopped: bool = False

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.bind == "0.0.0.0" else self.bind
        base = f"http://{host}:{self.port}/"
        return f"{base}?t={self.token}" if self.token else base

    def stop(self):
        self.stopped = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class _ShareHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, share, *,
                 auth_required: bool = False,
                 authorized_keys: list[Ed25519PublicKey] | None = None,
                 token: str = ""):
        super().__init__(addr, handler)
        self.share = share
        self.auth_required = auth_required
        self.authorized_keys: list[Ed25519PublicKey] = authorized_keys or []
        self.token = token


_LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})


class TmuxWebShareService:
    def __init__(self, bind: str = "127.0.0.1", read_only: bool = True,
                 authorized_keys_path: str | None = None):
        self.bind = bind
        self.authorized_keys_path = authorized_keys_path or _AUTHORIZED_KEYS_PATH
        self._auth_required = False

        if bind not in _LOOPBACK_BINDS:
            # Non-loopback shares expose terminal contents even when
            # read-only, so every public bind requires client auth.
            keys = _load_authorized_keys(self.authorized_keys_path)
            if not keys:
                raise RuntimeError(
                    f"Non-loopback web share requires at least one "
                    f"Ed25519 key in {self.authorized_keys_path}"
                )
            self._authorized_keys = keys
            self._auth_required = True
        else:
            self._authorized_keys: list[Ed25519PublicKey] = []

        self.read_only = read_only
        self._shares: dict[str, list[WebShare]] = {}

    def start_share(self, session: str, port: int = 0) -> WebShare:
        port = port or _free_tcp_port(self.bind)
        # Per-share capability token: required to fetch the page and to open
        # the WS upgrade, even on loopback (closes the unauthenticated
        # drive-by read/keystroke-injection vector on 127.0.0.1).
        token = secrets.token_urlsafe(32)
        placeholder = type("_PendingShare", (), {
            "session": session,
            "bind": self.bind,
            "port": port,
            "read_only": self.read_only,
            "stopped": False,
        })()
        server = _ShareHTTPServer(
            (self.bind, port), _WebHandler, placeholder,
            auth_required=self._auth_required,
            authorized_keys=self._authorized_keys,
            token=token,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        share = WebShare(
            session=session,
            bind=self.bind,
            port=port,
            read_only=self.read_only,
            server=server,
            thread=thread,
            token=token,
        )
        server.share = share
        thread.start()
        self._shares.setdefault(session, []).append(share)
        return share

    def shares_for(self, session: str) -> list[WebShare]:
        live = [s for s in self._shares.get(session, []) if not s.stopped]
        if live:
            self._shares[session] = live
        else:
            self._shares.pop(session, None)
        return live

    def ports_for(self, session: str) -> list[int]:
        return [s.port for s in self.shares_for(session)]

    def stop_all_for(self, session: str):
        for share in self._shares.get(session, []):
            share.stop()
        self._shares.pop(session, None)

    def stop_all(self):
        for session in list(self._shares):
            self.stop_all_for(session)


class TmuxWebSharePlugin(MenuProvider):
    name = "tmux_web_share"
    description = "Share tmux sessions in a browser"
    version = "0.1"
    category = "Workspace"
    capabilities = ["menu_provider", "tmux_web_share"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._service: TmuxWebShareService | None = None

    def activate(self, app_controller):
        self._window = app_controller
        cfg = Config()
        bind = cfg.get("plugins", "tmux_web_share", "bind", default="127.0.0.1")
        read_only = bool(cfg.get(
            "plugins", "tmux_web_share", "read_only", default=True,
        ))
        self._service = TmuxWebShareService(bind=bind, read_only=read_only)
        if not hasattr(app_controller, "tmux_web_share"):
            app_controller.tmux_web_share = self._service

    def deactivate(self):
        if self._service is not None:
            self._service.stop_all()
        if (self._window is not None
                and getattr(self._window, "tmux_web_share", None) is self._service):
            del self._window.tmux_web_share
        self._service = None
        self._window = None

    def get_menu_items(self, terminal):
        tmux_mode = getattr(self._window, "tmux_mode", None)
        if self._service is None or tmux_mode is None:
            return []
        session = tmux_mode.get_session_for_terminal(terminal)
        if not session:
            return []
        items = [(f"Share in Browser... ({session})", lambda s=session: self._start(s))]
        ports = self._service.ports_for(session)
        if ports:
            items.append((
                f"  Stop browser sharing ({len(ports)} active)",
                lambda s=session: self._service.stop_all_for(s),
            ))
        return items

    def _start(self, session: str):
        try:
            share = self._service.start_share(session)
        except Exception as e:
            QMessageBox.critical(self._window, "Browser share failed", str(e))
            return
        QMessageBox.information(
            self._window,
            "Browser share started",
            share.url,
        )
