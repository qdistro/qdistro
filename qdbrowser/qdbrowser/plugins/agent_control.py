"""Agent control plugin — Unix-socket JSON-RPC, same shape as qterminator.

Exposes the browser to external agents (Claude Code, opencode, ...).
Verbs cover tab management, navigation, input injection, DOM
introspection, and full-page screenshots. Authentication is by
``SO_PEERCRED`` (same UID only).

Enable by setting ``$QDBROWSER_AGENT_CONTROL=1`` or
``[plugins] agent_control = true`` in the TOML config.

Wire format: newline-delimited JSON-RPC 2.0. Server-pushed events have
``event`` set and no ``id``.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import math
import os
import signal
import socket
import stat
import struct
import time

log = logging.getLogger("qdbrowser.agent_control")

# Hard caps to defend against same-UID DoS.
_MAX_LINE_BYTES = 4 * 1024 * 1024     # 4 MiB per JSON-RPC line
_MAX_BUFFER_BYTES = 8 * 1024 * 1024   # 8 MiB pending buffer per client

# Default-deny set. These RPCs can exfiltrate data or act on behalf of
# the user without consent (arbitrary JS, synthetic typing/clicking).
# Admin must explicitly re-enable each via [agent_control] allowed_methods.
# See todo/browser/03-agent-guardrails.md §Layer 2.
_DEFAULT_DENIED_METHODS = frozenset({
    "eval_js",
    "type_text",
    "send_keys",
    "click_at",
    "dblclick_at",
    "move_mouse",
})

# Param keys whose values are redacted in audit logs — they're either
# attacker-controllable or sensitive on their face.
_REDACT_PARAM_KEYS = frozenset({
    "script",   # eval_js
    "text",     # type_text
    "keys",     # send_keys
    "png_b64",  # never in request, but defence in depth
})

# URL-bearing params. These are normally kept in the audit log (the audit
# records *where* an agent navigated), but they must be redacted when the
# RPC targets a private (off-the-record) profile/tab — the journal is
# durable on-disk storage and a private URL there is the same class of
# leak as recording it to history.jsonl.
_URL_PARAM_KEYS = frozenset({"url"})

from PyQt6.QtCore import (  # noqa: E402
    QBuffer,
    QByteArray,
    QEvent,
    QIODevice,
    QObject,
    QPoint,
    QPointF,
    QSocketNotifier,
    Qt,
    QTimer,
)
from PyQt6.QtGui import (  # noqa: E402
    QKeyEvent,
    QMouseEvent,
)
from PyQt6.QtWidgets import QApplication  # noqa: E402

from qdbrowser.config import Config  # noqa: E402
from qdbrowser.plugin import Plugin  # noqa: E402
from qdbrowser.webview import WebView  # noqa: E402


def _socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime_dir, f"qdbrowser-agent-{os.getuid()}.sock")


def _safe_unlink_socket(path: str) -> None:
    """Unlink a stale socket path with TOCTOU/symlink protection.

    On hosts where ``XDG_RUNTIME_DIR`` is unset and the fallback is the
    world-writable ``/tmp``, an attacker can race a symlink into the
    predictable socket path. ``os.lstat`` lets us refuse to remove
    anything that isn't a plain unix socket owned by us.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("lstat %r failed; refusing to unlink: %s", path, exc)
        return
    if not stat.S_ISSOCK(st.st_mode):
        log.error(
            "refusing to unlink %r: not a socket (mode=%o) — possible "
            "symlink attack", path, st.st_mode)
        raise PermissionError(
            f"refusing to unlink non-socket at {path}")
    if st.st_uid != os.getuid():
        log.error(
            "refusing to unlink %r: owned by uid=%d, not %d",
            path, st.st_uid, os.getuid())
        raise PermissionError(
            f"refusing to unlink socket owned by another user at {path}")
    os.unlink(path)


def _peer_uid_matches(conn: socket.socket) -> bool:
    try:
        creds = conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid == os.getuid()
    except OSError:
        return False


def _peer_creds(conn: socket.socket) -> tuple[int | None, int | None]:
    """Return ``(pid, uid)`` from SO_PEERCRED, or ``(None, None)`` on
    failure. Split from ``_peer_uid_matches`` so the L6 exe check has
    the pid without re-querying the kernel."""
    try:
        creds = conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", creds)
        return pid, uid
    except OSError:
        return None, None


def _redact_params(params, redact_url=False):
    """Strip sensitive values from RPC params for audit logging.

    Replaces values under _REDACT_PARAM_KEYS with ``<redacted:Nb>`` so the
    audit log records the operation shape without leaking JS source, typed
    text, or key sequences. When ``redact_url`` is set (the RPC targets a
    private/off-the-record profile or tab), URL-bearing params are also
    redacted so a private navigation leaves no durable trace in the
    journal.
    """
    if not isinstance(params, dict):
        return params
    out = {}
    for k, v in params.items():
        redact = k in _REDACT_PARAM_KEYS or (redact_url and k in _URL_PARAM_KEYS)
        if redact and v is not None:
            try:
                size = len(v) if not isinstance(v, (int, float, bool)) else 0
            except TypeError:
                size = 0
            out[k] = f"<redacted:{size}b>"
        else:
            out[k] = v
    return out


def _hostname_match(host: str, pattern: str) -> bool:
    """Match a hostname against a glob.

    Bare names are exact match: ``foo.com`` only matches ``foo.com``.
    Wildcard prefix ``*.foo.com`` matches ``a.foo.com`` and any deeper
    subdomain, but **not** ``foo.com`` itself and **not** ``evilfoo.com``
    — the leading ``*.`` requires a real dot boundary.
    """
    if pattern == host:
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]   # e.g. ".foo.com"
        return host.endswith(suffix) and len(host) > len(suffix)
    return False


def _hostname_match_any(host: str, patterns) -> bool:
    return any(_hostname_match(host, p) for p in patterns)


# -- Layer 4: rate limiting ------------------------------------------------

# Methods that count against the screenshot bucket and eval bucket
# respectively. Kept as module-level frozensets so tests can introspect.
_SCREENSHOT_METHODS = frozenset({"screenshot"})
_EVAL_METHODS = frozenset({"eval_js"})
_OPEN_TAB_METHODS = frozenset({"open_tab"})


class _RateBucket:
    """Sliding-window counter over a fixed window (seconds).

    Records the wall-clock time of each accepted hit and answers
    ``allow(now, limit)`` for the next call. ``limit <= 0`` always
    denies (used for ``eval_rate_limit_per_minute = 0``). ``limit ==
    None`` means uncapped — used as a sentinel by callers that want to
    skip a bucket entirely. Storage is bounded by the limit so a
    misbehaving client can't grow it without bound.
    """

    __slots__ = ("_hits", "_window")

    def __init__(self, window_seconds: float = 60.0):
        self._hits: collections.deque = collections.deque()
        self._window = float(window_seconds)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._hits and self._hits[0] <= cutoff:
            self._hits.popleft()

    def would_allow(self, now: float, limit: int) -> bool:
        """Check whether the bucket would allow without recording."""
        if limit is None:
            return True
        if limit <= 0:
            return False
        self._evict(now)
        return len(self._hits) < limit

    def record(self, now: float) -> None:
        """Record a hit (call after all buckets pass)."""
        self._hits.append(now)

    def allow(self, now: float, limit: int) -> bool:
        if limit is None:
            self._hits.append(now)
            return True
        if limit <= 0:
            return False
        self._evict(now)
        if len(self._hits) >= limit:
            return False
        self._hits.append(now)
        return True

    def retry_after(self, now: float, limit: int) -> float:
        """Seconds until the next slot opens. Returns 0 if a slot is
        available right now."""
        if limit is None or limit <= 0:
            return self._window
        self._evict(now)
        if len(self._hits) < limit:
            return 0.0
        # Oldest hit will expire at oldest + window.
        oldest = self._hits[0]
        wait = (oldest + self._window) - now
        return max(0.0, wait)

    def __len__(self) -> int:
        return len(self._hits)


# -- Layer 5: broker mediation --------------------------------------------

def _broker_check(method: str, params: dict, *, bus_name: str,
                  object_path: str, interface: str,
                  timeout_ms: int) -> tuple[bool, str, bool]:
    """Synchronously ask the broker ``CheckAgentAction(uid, method,
    params_json) → (b ok, s reason)``.

    Returns ``(allowed, reason, reachable)``. ``reachable=False`` means
    the broker is down / not on the bus; the caller decides
    fail-open vs fail-closed from policy state. The broker contract is
    deliberately small (a single boolean + reason) so a Python or C
    broker stub is trivial to implement; ``params`` is passed as
    redacted JSON so the broker never sees script source or typed text.
    """
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
    except Exception as exc:  # pragma: no cover - jeepney always present
        log.warning("broker_check: jeepney import failed: %s", exc)
        return True, "jeepney_missing", False
    addr = DBusAddress(object_path=object_path, bus_name=bus_name,
                       interface=interface)
    redacted = _redact_params(params) if isinstance(params, dict) else {}
    payload = json.dumps(redacted, default=str)
    msg = new_method_call(addr, "CheckAgentAction", "uss",
                          (os.getuid(), method, payload))
    try:
        # Session bus by default — that's where per-user brokers live.
        conn = open_dbus_connection(bus="SESSION")
    except Exception as exc:
        log.warning("broker_check: bus connect failed: %s", exc)
        return True, "bus_unreachable", False
    try:
        try:
            reply = conn.send_and_get_reply(msg, timeout=timeout_ms / 1000.0)
        except Exception as exc:
            log.warning("broker_check: rpc failed: %s", exc)
            return True, "broker_timeout", False
        body = getattr(reply, "body", None) or ()
        if not isinstance(body, tuple) or len(body) < 2:
            log.warning("broker_check: malformed reply body=%r", body)
            return True, "broker_malformed", False
        ok, reason = bool(body[0]), str(body[1])
        return ok, reason, True
    finally:
        try:
            conn.close()
        except Exception:
            pass


# -- Layer 6: client exe identity -----------------------------------------

def _file_sha256(path: str) -> str | None:
    """SHA256 of a file by absolute path. Returns ``None`` on any error
    (file missing, perm denied, race during read). Caller decides whether
    that's fatal."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _proc_exe_digest(pid: int) -> tuple[str | None, str | None]:
    """Return ``(exe_path, sha256_hex)`` for /proc/<pid>/exe.

    Reads the link (so we record what the kernel sees, not what the
    client claimed) and digests the underlying file. Either component
    may be ``None`` if the process or executable has gone away — this
    is best-effort audit/identity, not enforcement against a TOCTOU
    racer (the post-handshake ``execve`` concern from L6 §note still
    applies; we don't promise more than the proc table).
    """
    link = f"/proc/{int(pid)}/exe"
    try:
        target = os.readlink(link)
    except OSError:
        target = None
    digest = _file_sha256(link) if target is not None else None
    return target, digest


def _resolve_allowed_exes(entries) -> set[str]:
    """Resolve config entries (paths or ``sha256:<hex>``) to a set of
    SHA256 digests. Bad/missing path entries are skipped with a warning
    — partial config shouldn't take down the plugin."""
    out: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, str) or not entry:
            continue
        if entry.startswith("sha256:"):
            hex_part = entry[len("sha256:"):].strip().lower()
            if len(hex_part) == 64 and all(c in "0123456789abcdef"
                                            for c in hex_part):
                out.add(hex_part)
            else:
                log.warning("agent_control: bad sha256 entry %r", entry)
            continue
        if not os.path.isabs(entry):
            log.warning("agent_control: non-absolute exe entry %r", entry)
            continue
        digest = _file_sha256(entry)
        if digest is None:
            log.warning(
                "agent_control: cannot digest exe %r (skipping)", entry)
            continue
        out.add(digest)
    return out


# -- key name -> Qt.Key / text translation. Coarser than qterminator's
# PTY-bytes table; we feed QKeyEvents into the QWebEngineView.

_KEYNAME_TO_QT = {
    "enter": (Qt.Key.Key_Return, "\r"),
    "return": (Qt.Key.Key_Return, "\r"),
    "tab": (Qt.Key.Key_Tab, "\t"),
    "backspace": (Qt.Key.Key_Backspace, ""),
    "escape": (Qt.Key.Key_Escape, ""),
    "esc": (Qt.Key.Key_Escape, ""),
    "space": (Qt.Key.Key_Space, " "),
    "up": (Qt.Key.Key_Up, ""),
    "down": (Qt.Key.Key_Down, ""),
    "left": (Qt.Key.Key_Left, ""),
    "right": (Qt.Key.Key_Right, ""),
    "home": (Qt.Key.Key_Home, ""),
    "end": (Qt.Key.Key_End, ""),
    "pageup": (Qt.Key.Key_PageUp, ""),
    "pagedown": (Qt.Key.Key_PageDown, ""),
    "insert": (Qt.Key.Key_Insert, ""),
    "delete": (Qt.Key.Key_Delete, ""),
}
for _i in range(1, 13):
    _KEYNAME_TO_QT[f"f{_i}"] = (getattr(Qt.Key, f"Key_F{_i}"), "")


_MODIFIERS = {
    "ctrl": Qt.KeyboardModifier.ControlModifier,
    "control": Qt.KeyboardModifier.ControlModifier,
    "shift": Qt.KeyboardModifier.ShiftModifier,
    "alt": Qt.KeyboardModifier.AltModifier,
    "meta": Qt.KeyboardModifier.MetaModifier,
    "super": Qt.KeyboardModifier.MetaModifier,
}


def _parse_key(name: str):
    """Return (Qt.Key, text, modifiers) for a symbolic key name like
    'enter', 'ctrl+l', 'shift+tab', 'a'.

    Only printable ASCII single-char keys are accepted; for non-ASCII
    or composed characters the caller should use ``rpc_type_text``
    (which uses ``insertText`` on the focused element) rather than
    synthesising key events with a bogus ``Qt.Key`` enum value.
    """
    parts = name.lower().split("+")
    mods = Qt.KeyboardModifier.NoModifier
    while len(parts) > 1 and parts[0] in _MODIFIERS:
        mods |= _MODIFIERS[parts[0]]
        parts = parts[1:]
    key_name = parts[0]
    if key_name in _KEYNAME_TO_QT:
        qkey, text = _KEYNAME_TO_QT[key_name]
        return qkey, text, mods
    if len(key_name) == 1:
        ch = key_name
        if "a" <= ch <= "z":
            shifted = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            return (Qt.Key(ord(ch.upper())),
                    ch.upper() if shifted else ch,
                    mods)
        if "0" <= ch <= "9":
            return Qt.Key(ord(ch)), ch, mods
        if 0x20 <= ord(ch) <= 0x7E:
            return Qt.Key(ord(ch)), ch, mods
        raise ValueError(
            f"unsupported key {name!r} — use type_text for non-ASCII")
    raise ValueError(f"unknown key name: {name!r}")


# -- RPC error type --

class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


# -- Per-attached-tab state --

class _AttachState:
    def __init__(self):
        self.subscribers: set[int] = set()


# -- Client connection wrapper --

class _Client(QObject):
    def __init__(self, conn: socket.socket, server: _AgentServer,
                 *, pid: int | None = None,
                 exe_path: str | None = None,
                 exe_digest: str | None = None):
        super().__init__()
        self._conn = conn
        self._fd = conn.fileno()
        self._server = server
        self._buf = bytearray()
        self._notifier = QSocketNotifier(
            self._fd, QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._on_readable)
        self.attached_tabs: set[int] = set()
        # L6: identity captured at accept time, used for audit lines.
        self.pid = pid
        self.exe_path = exe_path
        self.exe_digest = exe_digest
        # L6: handshake state. When ``require_handshake`` is True,
        # the client must send a handshake message before any RPC.
        self.handshake_done = False
        self.handshake_exe: str | None = None
        self.handshake_pid: int | None = None
        # L4: per-client sliding-window token buckets, reset on
        # disconnect by virtue of being instance attributes.
        self.bucket_total = _RateBucket()
        self.bucket_screenshot = _RateBucket()
        self.bucket_eval = _RateBucket()
        self.bucket_open_tab = _RateBucket()

    @property
    def fd(self) -> int:
        return self._fd

    def _on_readable(self, *_):
        try:
            chunk = self._conn.recv(8192)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.close()
            return
        if not chunk:
            self.close()
            return
        self._buf.extend(chunk)
        if len(self._buf) > _MAX_BUFFER_BYTES:
            log.warning(
                "client %d buffer over %d bytes without newline; closing",
                self._fd, _MAX_BUFFER_BYTES)
            self.send_obj(_err(None, -32700, "request too large"))
            self.close()
            return
        while b"\n" in self._buf:
            line, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            if len(line) > _MAX_LINE_BYTES:
                self.send_obj(_err(None, -32700, "line too long"))
                self.close()
                return
            if not line.strip():
                continue
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception:
                self.send_obj(_err(None, -32700, "parse error"))
                continue
            if not isinstance(req, dict):
                self.send_obj(_err(None, -32600, "request must be object"))
                continue
            if req.get("op") == "handshake":
                resp = self._server.handle(self, req)
                self.send_obj(resp)
                continue
            method = req.get("method")
            if not isinstance(method, str):
                self.send_obj(_err(req.get("id"), -32600,
                                    "method must be a string"))
                continue
            resp = self._server.handle(self, req)
            self.send_obj(resp)

    def send_obj(self, obj: dict):
        self.send_raw((json.dumps(obj) + "\n").encode("utf-8"))

    def send_raw(self, data: bytes):
        try:
            self._conn.sendall(data)
        except OSError:
            self.close()

    def close(self):
        for tab_id in list(self.attached_tabs):
            try:
                self._server._plugin._detach_client_from_tab(self._fd, tab_id)
            except Exception:
                pass
        self.attached_tabs.clear()
        self._server.remove_client(self._fd)
        if self._notifier:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        try:
            self._conn.close()
        except OSError:
            pass


# -- Server (listener) --

class _AgentServer(QObject):
    def __init__(self, plugin: AgentControlPlugin, window):
        super().__init__()
        self._plugin = plugin
        self._window = window
        self._path = _socket_path()
        self._sock: socket.socket | None = None
        self._notifier: QSocketNotifier | None = None
        self._clients: dict[int, _Client] = {}

    @property
    def socket_path(self) -> str:
        return self._path

    def start(self):
        _safe_unlink_socket(self._path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self._path)
        os.chmod(self._path, 0o600)
        s.listen(8)
        s.setblocking(False)
        self._sock = s
        self._notifier = QSocketNotifier(
            s.fileno(), QSocketNotifier.Type.Read, self)
        self._notifier.activated.connect(self._accept)

    def stop(self):
        if self._notifier:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        for client in list(self._clients.values()):
            client.close()
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None
        try:
            _safe_unlink_socket(self._path)
        except (FileNotFoundError, PermissionError):
            pass

    def _accept(self, *_):
        try:
            conn, _addr = self._sock.accept()
        except BlockingIOError:
            return
        pid, uid = _peer_creds(conn)
        if uid is None or uid != os.getuid():
            conn.close()
            return
        # L6: client exe allowlist. Empty allowlist => any same-UID
        # process is fine (legacy behaviour). Non-empty => the peer's
        # /proc/<pid>/exe digest must be in the resolved set, else
        # reject *before* any RPC bytes are read so the audit trail
        # records the rejection and we never spin up a _Client for it.
        exe_path, exe_digest = (None, None)
        if pid is not None:
            exe_path, exe_digest = _proc_exe_digest(pid)
        allowed_digests = self._plugin._resolved_allowed_exes()
        if allowed_digests:
            if exe_digest is None or exe_digest not in allowed_digests:
                log.warning(
                    "AGENT_RPC_DENY uid=%d fd=%d reason=client_not_allowed "
                    "pid=%s exe=%s digest=%s",
                    os.getuid(), conn.fileno(), pid, exe_path,
                    exe_digest[:16] + "..." if exe_digest else None)
                try:
                    # Best-effort error frame so a polite client sees
                    # *why* it was dropped instead of an opaque EOF.
                    conn.sendall((json.dumps(
                        _err(None, -32002, "client_not_allowed")
                    ) + "\n").encode("utf-8"))
                except OSError:
                    pass
                conn.close()
                return
        conn.setblocking(False)
        client = _Client(conn, self, pid=pid, exe_path=exe_path,
                          exe_digest=exe_digest)
        log.info(
            "AGENT_RPC_CONNECT uid=%d fd=%d pid=%s exe=%s digest=%s",
            os.getuid(), conn.fileno(), pid, exe_path,
            (exe_digest[:16] + "...") if exe_digest else None)
        self._clients[conn.fileno()] = client

    def remove_client(self, fd: int):
        self._clients.pop(fd, None)
        for state in self._plugin.tab_states.values():
            state.subscribers.discard(fd)

    def broadcast_event(self, tab_id: int, event_type: str, payload: dict):
        """Broadcast an event to all subscribers of ``tab_id``.

        Unlike the original implementation, we no longer fall back to
        "broadcast to all clients" when nobody is subscribed — that
        leaks side-channel information (one agent can tell whether
        another agent is attached). For genuinely global events, use
        ``broadcast_global``.
        """
        state = self._plugin.tab_states.get(tab_id)
        if state is None or not state.subscribers:
            return
        msg = {"event": event_type, "tab_id": tab_id, **payload}
        line = (json.dumps(msg) + "\n").encode("utf-8")
        for fd in list(state.subscribers):
            client = self._clients.get(fd)
            if client:
                client.send_raw(line)

    def broadcast_global(self, event_type: str, payload: dict):
        """Send a non-tab-scoped event to every connected client."""
        msg = {"event": event_type, **payload}
        line = (json.dumps(msg) + "\n").encode("utf-8")
        for _fd, client in list(self._clients.items()):
            client.send_raw(line)

    def _targets_off_the_record(self, method, params) -> bool:
        """True when an RPC is bound to a private (off-the-record) profile
        or tab, so its URL params must be kept out of the audit journal.

        Fail closed: if a ``tab_id`` is supplied but can't be resolved,
        treat it as private rather than risk logging a private URL.
        """
        if not isinstance(params, dict):
            return False
        # open_tab carries the profile name directly.
        prof = params.get("profile")
        if isinstance(prof, str) and prof == "private":
            return True
        # Tab-targeted RPCs (navigate, etc.) reference an existing tab.
        if "tab_id" in params:
            try:
                wv = self._plugin._get_webview(params.get("tab_id"))
            except Exception:
                # Unresolvable / invalid tab id — fail closed only if a
                # URL would otherwise be logged.
                return any(k in params for k in _URL_PARAM_KEYS)
            try:
                return bool(wv.is_off_the_record)
            except Exception:
                return True
        return False

    def _off_the_record_denied(self, method, params) -> tuple[bool, str]:
        """02/S9: an external agent must not control a private
        (off-the-record) tab — not drive it, read it, script it, attach to it,
        or open one. Returns (denied, reason).

        This BLOCKS the RPC, unlike :meth:`_targets_off_the_record` (which only
        decides audit-log URL redaction). It resolves the target the same way:
        an ``open_tab`` whose ``profile`` is private, or a ``tab_id`` that
        resolves to an off-the-record webview. Every control verb carries an
        explicit ``tab_id`` (rpc_navigate/click/type/eval/screenshot/...), so a
        tab_id gate covers them all. Fail closed when a resolved tab's privacy
        can't be read; but an unresolvable tab_id is left to the verb so the
        agent gets the real ``no such tab`` error, not a misleading privacy
        denial."""
        if not isinstance(params, dict):
            return (False, "")
        if params.get("profile") == "private":
            return (True, "agents may not open private (off-the-record) tabs")
        if "tab_id" in params:
            try:
                wv = self._plugin._get_webview(params.get("tab_id"))
            except Exception:
                return (False, "")
            try:
                if bool(wv.is_off_the_record):
                    return (True, "agents may not control private "
                                  "(off-the-record) tabs")
            except Exception:
                return (True, "could not verify tab privacy; denying "
                              "(fail closed)")
        return (False, "")

    def handle(self, client: _Client, req: dict) -> dict:
        method = req.get("method")
        params = req.get("params") or {}
        rid = req.get("id")
        # L6 handshake: if the message has ``"op": "handshake"`` it is
        # a handshake frame, not a normal JSON-RPC call. Process it
        # before any other gate so the client can identify itself.
        if req.get("op") == "handshake":
            return self._plugin._handle_handshake(client, req)
        # If handshake is required but not yet done, reject the call.
        if self._plugin._require_handshake() and not client.handshake_done:
            log.warning(
                "AGENT_RPC_DENY uid=%d fd=%d method=%s "
                "reason=handshake_required",
                os.getuid(), client.fd, method)
            return _err(rid, -32007, "handshake_required")
        # Audit log every RPC before policy/dispatch so denied attempts
        # are also captured. Tag matches the logger name for
        # ``journalctl --user -t qdbrowser.agent_control``.
        # Redact URL params when the RPC targets a private (OTR) profile
        # or tab — the journal is durable storage and a private URL there
        # is the same leak as recording it to history.
        redact_url = (self._targets_off_the_record(method, params)
                      if isinstance(params, dict) else False)
        log.info(
            "AGENT_RPC uid=%d fd=%d pid=%s exe=%s method=%s params=%s",
            os.getuid(), client.fd,
            client.handshake_pid or client.pid,
            client.handshake_exe or client.exe_path,
            method,
            _redact_params(params, redact_url=redact_url)
            if isinstance(params, dict) else params)
        if not isinstance(params, dict):
            return _err(rid, -32602, "params must be an object")
        # Don't let an agent pass a positional-conflicting kwarg.
        if "client" in params:
            return _err(rid, -32602, "reserved param name: client")
        # Method-allowlist gate. ``policy_denied`` returns at the standard
        # JSON-RPC application-error code so the MCP/HTTP proxy can
        # propagate it without translation.
        allowed, deny_reason = self._plugin._policy_check_method(method)
        if not allowed:
            log.warning(
                "AGENT_RPC_DENY uid=%d fd=%d method=%s reason=policy_denied "
                "detail=%s",
                os.getuid(), client.fd, method, deny_reason)
            return _err(rid, -32002, f"policy_denied: {deny_reason}")
        # 02/S9: agents may not touch private (off-the-record) tabs at all —
        # not control, read, script, attach, or open. Deny before rate-limit
        # and broker mediation so a private-tab attempt consumes neither quota
        # nor a broker round-trip.
        otr_denied, otr_reason = self._off_the_record_denied(method, params)
        if otr_denied:
            log.warning(
                "AGENT_RPC_DENY uid=%d fd=%d method=%s reason=off_the_record "
                "detail=%s",
                os.getuid(), client.fd, method, otr_reason)
            return _err(rid, -32008, f"off_the_record_denied: {otr_reason}")
        # L4: rate limit. Check category bucket first (cheap to deny a
        # caller who's blown the screenshot quota without consuming a
        # slot in the total bucket) then the total. A denied request
        # does *not* count against either bucket — same shape as
        # 03-agent-guardrails.md §L4 "Rate-limited requests don't count
        # against the quota."
        rl_allowed, rl_reason, rl_retry = self._plugin._rate_check(
            client, method)
        if not rl_allowed:
            log.warning(
                "AGENT_RPC_DENY uid=%d fd=%d method=%s reason=rate_limited "
                "detail=%s retry_after=%.1f",
                os.getuid(), client.fd, method, rl_reason, rl_retry)
            resp = _err(rid, -32005, f"rate_limited: {rl_reason}")
            resp["error"]["retry_after"] = math.ceil(rl_retry)
            return resp
        # L5: broker mediation. Sensitive methods (default-deny set +
        # admin-configured ``broker_mediated_methods``) get a synchronous
        # CheckAgentAction call.
        b_allowed, b_reason = self._plugin._broker_mediate(method, params)
        if not b_allowed:
            log.warning(
                "AGENT_RPC_DENY uid=%d fd=%d method=%s reason=broker_denied "
                "detail=%s",
                os.getuid(), client.fd, method, b_reason)
            return _err(rid, -32006, f"broker_denied: {b_reason}")
        try:
            fn = self._plugin._lookup_method(method)
            if fn is None:
                return _err(rid, -32601, f"unknown method: {method}")
            result = fn(client, **params)
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except _RpcError as e:
            return _err(rid, e.code, e.message)
        except TypeError as e:
            return _err(rid, -32602, f"invalid params: {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("rpc %s failed", method)
            return _err(rid, -32000, f"{type(e).__name__}: {e}")


# -- The plugin --

class AgentControlPlugin(Plugin):
    name = "agent_control"
    description = "Unix-socket JSON-RPC for external agents."
    version = "0.1"
    capabilities = ["agent_control"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._server: _AgentServer | None = None
        self.tab_states: dict[int, _AttachState] = {}
        # Instance-level RPC dispatch — other plugins (PiP, translate)
        # add verbs via ``register_method`` instead of monkey-patching
        # ``AgentControlPlugin.__class__``.
        self._methods: dict[str, callable] = {}
        # L6 cache: resolved at first use, refreshed when config
        # mutates from underneath us (tests do this a lot).
        self._allowed_exes_cache: set[str] | None = None
        self._allowed_exes_signature: tuple | None = None

    @staticmethod
    def _is_enabled() -> bool:
        if os.environ.get("QDBROWSER_AGENT_CONTROL") == "1":
            return True
        try:
            return bool(Config().get("plugins", "agent_control", default=False))
        except Exception:
            return False

    def activate(self, app_controller):
        if not self._is_enabled():
            return
        self._window = app_controller
        app_controller.agent_control = self
        self._server = _AgentServer(self, app_controller)
        self._server.start()
        # Wire global navigation events.
        try:
            app_controller.navigation_event.connect(self._on_navigation)
        except Exception:
            pass
        # Drop a tab's state when its webview goes away — keeps stale
        # ids from acting on a closed (and possibly recycled) WebView.
        try:
            app_controller.webview_removed.connect(self._on_webview_removed)
        except Exception:
            pass
        # SIGHUP triggers a config reload so the admin can update
        # policy without restarting the browser.
        self._install_sighup_handler()

    # -- method registration -------------------------------------------

    _METHOD_NAME_RE = __import__("re").compile(r"\A[A-Za-z][A-Za-z0-9_]*\Z")

    def register_method(self, name: str, fn) -> None:
        """Register an RPC verb. Used by sibling plugins (PiP, translate)
        to expose verbs without monkey-patching the class.

        ``fn`` is called as ``fn(client, **params)`` and must respect
        the same conventions as the plugin's own ``rpc_*`` methods.
        """
        if not isinstance(name, str) or not self._METHOD_NAME_RE.match(name):
            raise ValueError(f"invalid rpc method name: {name!r}")
        self._methods[name] = fn

    def unregister_method(self, name: str) -> None:
        self._methods.pop(name, None)

    def _lookup_method(self, method: str):
        # Strict ASCII-only allowlist. ``isalnum``/``isidentifier``
        # silently accept Unicode characters which could (with a future
        # ``rpc_<unicode>`` attr) reach unintended methods.
        if not isinstance(method, str) or not self._METHOD_NAME_RE.match(method):
            return None
        if method in self._methods:
            return self._methods[method]
        return getattr(self, f"rpc_{method}", None)

    # -- SIGHUP config reload --------------------------------------------

    _prev_sighup_handler = None
    _sighup_pipe_r = -1
    _sighup_pipe_w = -1

    def _install_sighup_handler(self):
        """Install a SIGHUP handler that defers config reload to the
        event loop via a self-pipe (async-signal-safe).
        """
        try:
            prev = signal.getsignal(signal.SIGHUP)
        except (AttributeError, OSError):
            return
        self._prev_sighup_handler = prev

        r, w = os.pipe()
        os.set_blocking(w, False)
        os.set_blocking(r, False)
        self.__class__._sighup_pipe_r = r
        self.__class__._sighup_pipe_w = w

        def _on_sighup(signum, frame):
            try:
                os.write(w, b'\x00')
            except OSError:
                pass
            if callable(prev) and prev not in (signal.SIG_DFL,
                                                signal.SIG_IGN):
                prev(signum, frame)

        try:
            signal.signal(signal.SIGHUP, _on_sighup)
        except (OSError, ValueError):
            pass

        try:
            from PyQt6.QtCore import QSocketNotifier
            self._sighup_notifier = QSocketNotifier(
                r, QSocketNotifier.Type.Read)
            self._sighup_notifier.activated.connect(
                self._check_sighup_pending)
            self._sighup_notifier.setEnabled(True)
        except Exception:
            pass

    def _check_sighup_pending(self):
        try:
            os.read(self.__class__._sighup_pipe_r, 256)
        except OSError:
            pass
        log.info("SIGHUP received — reloading agent_control policy")
        try:
            Config._instance = None
            Config()
        except Exception:
            log.exception("config reload on SIGHUP failed")
        self._allowed_exes_cache = None
        self._allowed_exes_signature = None
        # §6: a reloaded ``[general] user_agent`` must be re-pinned on
        # every live profile, otherwise existing profiles keep the old UA
        # while new ones get the new value — exactly the per-profile drift
        # the single-source-of-truth UA is meant to prevent.
        try:
            from qdbrowser import webview as _wv_mod
            _wv_mod.pin_all_profiles()
        except Exception:
            log.exception("user-agent re-pin on SIGHUP failed")

    # -- Layer 6: handshake protocol ------------------------------------

    def _require_handshake(self) -> bool:
        """Return True if the admin requires a handshake before RPCs."""
        try:
            cfg = Config()
            return bool(cfg.get("agent_control", "require_handshake",
                                default=True))
        except Exception:
            return True

    def _handle_handshake(self, client: _Client, req: dict) -> dict:
        """Process a ``{op: "handshake", exe: "...", pid: N}`` frame.

        Verifies ``/proc/<pid>/exe`` against the claimed path (audit
        only — see Layer 6 TOCTOU caveat in the spec). Logs the
        identity for audit and records it on the client object.
        """
        claimed_exe = req.get("exe", "")
        claimed_pid = req.get("pid")
        rid = req.get("id")
        if not isinstance(claimed_exe, str):
            return _err(rid, -32602, "handshake: exe must be a string")
        if not isinstance(claimed_pid, int) or claimed_pid <= 0:
            return _err(rid, -32602, "handshake: pid must be positive int")
        if client.pid is not None and claimed_pid != client.pid:
            log.warning(
                "AGENT_RPC_HANDSHAKE uid=%d fd=%d claimed_pid=%d "
                "peer_pid=%d — pid mismatch, rejecting",
                os.getuid(), client.fd, claimed_pid, client.pid)
            return _err(rid, -32602,
                        "handshake: pid does not match peer credentials")
        actual_exe, actual_digest = _proc_exe_digest(claimed_pid)
        match = (actual_exe is not None
                 and os.path.realpath(claimed_exe) == os.path.realpath(actual_exe))
        client.handshake_done = match
        client.handshake_exe = claimed_exe
        client.handshake_pid = claimed_pid
        log.info(
            "AGENT_RPC_HANDSHAKE uid=%d fd=%d claimed_exe=%s "
            "claimed_pid=%d actual_exe=%s match=%s digest=%s",
            os.getuid(), client.fd, claimed_exe, claimed_pid,
            actual_exe, match,
            (actual_digest[:16] + "...") if actual_digest else None)
        result = {"ok": True, "verified": match}
        if match:
            result["actual_exe"] = actual_exe
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    # -- policy checks --------------------------------------------------

    def _policy_check_method(self, method) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for an RPC method.

        Policy enforcement is **on by default**. When enforced,
        ``_DEFAULT_DENIED_METHODS`` are denied; admin can subtract from
        that set via ``allowed_methods`` and add to it via
        ``denied_methods``. ``allowed`` wins over ``denied`` when a
        method appears in both, so an admin can explicitly re-enable
        e.g. ``eval_js`` for a development host.

        Set ``[agent_control] policy_enforced = false`` only for a
        local bring-up that needs the historical open socket. The
        secure recipe is in ``todo/browser/03-agent-guardrails.md``.
        """
        try:
            cfg = Config()
            enforced = bool(cfg.get("agent_control", "policy_enforced",
                                    default=True))
        except Exception:
            return True, ""
        if not enforced:
            return True, ""
        try:
            allowed = set(cfg.get("agent_control", "allowed_methods",
                                  default=[]) or [])
            denied = set(cfg.get("agent_control", "denied_methods",
                                 default=[]) or [])
        except Exception:
            allowed, denied = set(), set()
        effective_deny = (_DEFAULT_DENIED_METHODS | denied) - allowed
        if method in effective_deny:
            return False, method
        return True, ""

    def _policy_check_url(self, url) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a navigation URL.

        Empty allowlist+denylist = no restriction. ``about:`` URLs always
        allowed (safe internal pages). Denylist takes precedence over
        allowlist. Glob semantics per ``_hostname_match``.
        """
        if not isinstance(url, str):
            return False, "url not a string"
        if url.startswith("about:"):
            return True, ""
        try:
            cfg = Config()
            allowlist = cfg.get("agent_control", "navigate_allowlist",
                                default=[]) or []
            denylist = cfg.get("agent_control", "navigate_denylist",
                               default=[]) or []
        except Exception:
            return True, ""
        if not allowlist and not denylist:
            return True, ""
        from urllib.parse import urlparse
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return False, "url unparseable"
        if not host:
            return False, "url has no host"
        if denylist and _hostname_match_any(host, denylist):
            return False, f"host {host} in denylist"
        if allowlist and not _hostname_match_any(host, allowlist):
            return False, f"host {host} not in allowlist"
        return True, ""

    # -- Layer 4: rate limiting ----------------------------------------

    def _rate_check(self, client: _Client, method: str
                     ) -> tuple[bool, str, float]:
        """Return ``(allowed, reason, retry_after)`` for an RPC against
        this client's token buckets. Does not mutate buckets on denial.

        Defaults track the §L4 spec: 600/min total (10 req/s),
        5/min screenshot, 3/min eval, 10/min open_tab. When config is
        unreadable, fail open — rate limiting is hardening, not a
        security boundary; the policy gate is.
        """
        try:
            cfg = Config()
            total = int(cfg.get("agent_control", "rate_limit_per_minute",
                                 default=600) or 0)
            shot = int(cfg.get("agent_control",
                                "screenshot_rate_limit_per_minute",
                                default=5) or 0)
            ev = int(cfg.get("agent_control", "eval_rate_limit_per_minute",
                              default=3) or 0)
            ot = int(cfg.get("agent_control",
                              "open_tab_rate_limit_per_minute",
                              default=10) or 0)
        except Exception:
            return True, "", 0.0
        now = time.monotonic()
        bucket_total = getattr(client, "bucket_total", None)
        bucket_shot = getattr(client, "bucket_screenshot", None)
        bucket_eval = getattr(client, "bucket_eval", None)
        bucket_ot = getattr(client, "bucket_open_tab", None)
        if bucket_total is None:
            return True, "", 0.0
        # Two-phase: check all buckets first, record only on success.
        checks: list[tuple[_RateBucket, int, str]] = []
        checks.append((bucket_total, total, f"total {total}/min"))
        if method in _SCREENSHOT_METHODS and bucket_shot is not None:
            checks.append((bucket_shot, shot, f"screenshot {shot}/min"))
        if method in _EVAL_METHODS and bucket_eval is not None:
            checks.append((bucket_eval, ev, f"eval_js {ev}/min"))
        if method in _OPEN_TAB_METHODS and bucket_ot is not None:
            checks.append((bucket_ot, ot, f"open_tab {ot}/min"))
        for bucket, limit, reason in checks:
            if not bucket.would_allow(now, limit):
                retry = bucket.retry_after(now, limit)
                return False, reason, retry
        for bucket, _limit, _reason in checks:
            bucket.record(now)
        return True, "", 0.0

    # -- Layer 5: broker mediation -------------------------------------

    def _broker_mediated_methods(self) -> set[str]:
        try:
            cfg = Config()
            extra = set(cfg.get("agent_control", "broker_mediated_methods",
                                 default=[]) or [])
        except Exception:
            extra = set()
        return set(_DEFAULT_DENIED_METHODS) | extra

    def _broker_mediate(self, method: str, params) -> tuple[bool, str]:
        """Optionally consult the broker before allowing the call.

        Fail-open vs fail-closed rationale (this is the subtle bit
        future-me will second-guess):

        - When ``policy_enforced = true``, the admin has explicitly
          said "I want this thing locked down." A broker that the
          admin enabled but can't reach is then a *configuration
          problem*, not a free pass. We **fail closed**.
        - When ``policy_enforced = false``, ``broker_enabled = true``
          means "audit + soft-deny if the broker says so" — the deploy
          isn't yet committed to the security boundary. A broker
          outage here means we'd silently break working agent
          workflows, which is worse than allowing the call. We
          **fail open** with a warning so the journal still tells the
          admin to look at the broker.

        This matches the principle the rest of the file follows: the
        boundary is the policy gate (L2/L3), with L5 as defence in
        depth.
        """
        try:
            cfg = Config()
            enabled = bool(cfg.get("agent_control", "broker_enabled",
                                    default=False))
            enforced = bool(cfg.get("agent_control", "policy_enforced",
                                     default=False))
            bus_name = cfg.get("agent_control", "broker_bus_name",
                                default="org.qdistro.Broker")
            obj_path = cfg.get("agent_control", "broker_object_path",
                                default="/org/qdistro/Broker")
            iface = cfg.get("agent_control", "broker_interface",
                             default="org.qdistro.Broker")
            timeout = int(cfg.get("agent_control", "broker_timeout_ms",
                                   default=1500) or 1500)
        except Exception:
            return True, ""
        if not enabled:
            return True, ""
        mediated = self._broker_mediated_methods()
        if method not in mediated:
            return True, ""
        ok, reason, reachable = _broker_check(
            method, params if isinstance(params, dict) else {},
            bus_name=bus_name, object_path=obj_path,
            interface=iface, timeout_ms=timeout)
        if reachable:
            return ok, reason
        # Unreachable broker: fail-closed only when the policy gate is
        # already on. See docstring above.
        if enforced:
            log.warning(
                "broker unreachable (%s); failing closed because "
                "policy_enforced=true", reason)
            return False, f"broker unreachable: {reason}"
        log.warning(
            "broker unreachable (%s); failing open because "
            "policy_enforced=false", reason)
        return True, ""

    # -- Layer 6: client exe allowlist ---------------------------------

    def _resolved_allowed_exes(self) -> set[str]:
        """Resolve config entries to a set of SHA256 hexdigests. Cached
        across calls but re-resolved when the underlying config list
        changes (tests mutate it via ``Config().set``)."""
        try:
            cfg = Config()
            entries = cfg.get("agent_control", "allowed_client_exes",
                               default=[]) or []
        except Exception:
            return set()
        signature = tuple(entries)
        if (self._allowed_exes_cache is not None
                and self._allowed_exes_signature == signature):
            return self._allowed_exes_cache
        resolved = _resolve_allowed_exes(entries)
        self._allowed_exes_cache = resolved
        self._allowed_exes_signature = signature
        return resolved

    def _on_webview_removed(self, webview):
        """Forget per-tab state for a vanished webview, so no stale
        ``tab_id`` can ever route to a recycled object."""
        try:
            tid = webview.stable_id
        except AttributeError:
            return
        self.tab_states.pop(tid, None)
        if self._server is not None:
            for client in list(self._server._clients.values()):
                client.attached_tabs.discard(tid)

    def deactivate(self):
        self.tab_states.clear()
        if self._server:
            self._server.stop()
            self._server = None
        if self._window and getattr(self._window, "agent_control", None) is self:
            try:
                del self._window.agent_control
            except AttributeError:
                pass

    @property
    def socket_path(self) -> str | None:
        return self._server.socket_path if self._server else None

    # -- enumeration helpers --

    def _enumerate_webviews(self) -> list:
        out = []
        if not self._window:
            return out
        tabs = getattr(self._window, "_tabs", None)
        if tabs is None:
            return out
        for i in range(tabs.count()):
            split = tabs.widget(i)
            for wv in split.find_webviews():
                out.append(wv)
        return out

    def _get_webview(self, tab_id: int) -> WebView:
        # Use the WebView's stable_id (monotonic counter assigned at
        # construction). id() is unsafe because Python may recycle the
        # memory address after a tab is closed.
        if not isinstance(tab_id, int):
            raise _RpcError(-32602, "tab_id must be int")
        for wv in self._enumerate_webviews():
            if wv.stable_id == tab_id:
                return wv
        raise _RpcError(-32004, f"no such tab: {tab_id}")

    def _attach_client_to_tab(self, fd: int, tab_id: int):
        state = self.tab_states.get(tab_id)
        if state is None:
            self._get_webview(tab_id)
            state = _AttachState()
            self.tab_states[tab_id] = state
        state.subscribers.add(fd)

    def _detach_client_from_tab(self, fd: int, tab_id: int):
        state = self.tab_states.get(tab_id)
        if state is None:
            return
        state.subscribers.discard(fd)
        if not state.subscribers:
            self.tab_states.pop(tab_id, None)

    # -- navigation broadcaster (called by window) --

    def _on_navigation(self, webview, url):
        if not self._server:
            return
        try:
            tab_id = webview.stable_id
        except AttributeError:
            return
        self._server.broadcast_event(tab_id, "navigation",
                                      {"url": url, "title": webview.title()})

    # -- helpers for JS-based actions: synchronous wait for callback --

    def _run_js(self, webview: WebView, script: str, timeout: float = 5.0):
        """Run JS and synchronously return the result.

        Uses a nested ``QEventLoop`` with a deadline so we don't pump
        every queued event in the main loop (which would re-enter RPC
        handling from a QSocketNotifier callback and corrupt our own
        state). The local event loop processes WebEngine IPC and Qt
        timers, but it doesn't re-enter ``handle()`` on the agent
        socket — that fd's notifier is the outer loop's.
        """
        from PyQt6.QtCore import QEventLoop

        result: dict = {"value": None, "done": False}
        loop = QEventLoop()

        def _cb(v):
            result["value"] = v
            result["done"] = True
            loop.quit()

        try:
            webview.view.page().runJavaScript(script, _cb)
        except Exception as exc:
            log.warning("runJavaScript dispatch failed: %s", exc)
            return None

        # Hard deadline so a broken page can't wedge the RPC.
        QTimer.singleShot(int(timeout * 1000), loop.quit)
        loop.exec()
        return result["value"]

    # -- RPC: tab management --

    def rpc_list_tabs(self, _client):
        out = []
        for wv in self._enumerate_webviews():
            # 02/S9: never enumerate private (off-the-record) tabs to an agent.
            # list_tabs carries no tab_id, so the handle() OTR deny gate can't
            # catch it — filter here, the agent-control parallel to the bridge's
            # TabsProxy.list. Fail closed: if privacy can't be read, hide it.
            try:
                if bool(wv.is_off_the_record):
                    continue
            except Exception:
                continue
            tid = wv.stable_id
            out.append({
                "id": tid,
                "title": wv.title(),
                "url": wv.url(),
                "attached": tid in self.tab_states,
                "can_go_back": wv.can_go_back(),
                "can_go_forward": wv.can_go_forward(),
                "loading": wv.is_loading(),
                "muted": wv.muted,
                "pinned": wv.pinned,
                "group": wv.group,
                "profile": wv.profile_name,
                "zoom": wv.zoom(),
                "page_load_seq": wv.page_load_seq(),
            })
        return out

    def rpc_attach(self, client, tab_id: int):
        wv = self._get_webview(tab_id)
        self._attach_client_to_tab(client.fd, tab_id)
        client.attached_tabs.add(tab_id)
        return {"ok": True, "page_load_seq": wv.page_load_seq()}

    def rpc_detach(self, client, tab_id: int):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        self._detach_client_from_tab(client.fd, tab_id)
        client.attached_tabs.discard(tab_id)
        return {"ok": True}

    def rpc_open_tab(self, _client, url: str | None = None,
                     background: bool = False,
                     profile: str = "default"):
        if not self._window:
            raise _RpcError(-32003, "no window")
        if url is not None:
            allowed, reason = self._policy_check_url(url)
            if not allowed:
                raise _RpcError(-32002, f"policy_denied: {reason}")
        wv = self._window.new_tab(url=url, profile_name=profile,
                                  background=background)
        return {"id": wv.stable_id}

    def rpc_close_tab(self, _client, tab_id: int):
        wv = self._get_webview(tab_id)
        idx, _ = self._window._find_tab_for_webview(wv)
        if idx < 0:
            raise _RpcError(-32004, "tab not in any index")
        self._window._on_tab_close_requested(idx)
        self.tab_states.pop(tab_id, None)
        return {"ok": True}

    def rpc_focus_tab(self, _client, tab_id: int):
        wv = self._get_webview(tab_id)
        idx, _ = self._window._find_tab_for_webview(wv)
        if idx >= 0:
            self._window._tabs.setCurrentIndex(idx)
            self._window._set_active_webview(wv)
        return {"ok": True}

    # -- RPC: navigation --

    def rpc_navigate(self, client, tab_id: int, url: str):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        allowed, reason = self._policy_check_url(url)
        if not allowed:
            raise _RpcError(-32002, f"policy_denied: {reason}")
        wv = self._get_webview(tab_id)
        wv.navigate(url)
        return {"ok": True}

    def rpc_reload(self, client, tab_id: int):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        self._get_webview(tab_id).reload()
        return {"ok": True}

    def rpc_stop(self, client, tab_id: int):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        self._get_webview(tab_id).stop()
        return {"ok": True}

    def rpc_go_back(self, client, tab_id: int):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        self._get_webview(tab_id).go_back()
        return {"ok": True}

    def rpc_go_forward(self, client, tab_id: int):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        self._get_webview(tab_id).go_forward()
        return {"ok": True}

    def rpc_get_url(self, _client, tab_id: int):
        wv = self._get_webview(tab_id)
        return {"url": wv.url(), "title": wv.title(),
                "loading": wv.is_loading()}

    def rpc_wait_for_load(self, client, tab_id: int, timeout: float = 10.0):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        start_seq = wv.page_load_seq()
        result = {"done": False, "ok": False}

        def _on_finish(_wv, ok):
            result["done"] = True
            result["ok"] = ok

        wv.load_finished.connect(_on_finish)
        try:
            deadline = time.time() + timeout
            app = QApplication.instance()
            while not result["done"] and time.time() < deadline:
                if wv.page_load_seq() > start_seq:
                    result["done"] = True
                    result["ok"] = True
                    break
                app.processEvents()
        finally:
            try:
                wv.load_finished.disconnect(_on_finish)
            except (RuntimeError, TypeError):
                pass
        return {"ok": result["ok"], "url": wv.url(),
                "timed_out": not result["done"]}

    # -- RPC: input injection --

    def rpc_click_at(self, client, tab_id: int, x: float, y: float,
                     button: str = "left", modifiers: list | None = None):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        # JS-based dispatch is far more reliable than QMouseEvent into
        # QWebEngineView's renderer proxy. We synthesize a real
        # MouseEvent on document.elementFromPoint(x, y).
        btn_idx = {"left": 0, "middle": 1, "right": 2}.get(button.lower(), 0)
        mods = modifiers or []
        js = (
            "(function(){"
            f"const x={float(x)},y={float(y)};"
            "const el=document.elementFromPoint(x,y);"
            "if(!el) return false;"
            "const opts={bubbles:true,cancelable:true,view:window,"
            "clientX:x,clientY:y,"
            f"button:{btn_idx},"
            f"ctrlKey:{'true' if 'ctrl' in [m.lower() for m in mods] else 'false'},"
            f"shiftKey:{'true' if 'shift' in [m.lower() for m in mods] else 'false'},"
            f"altKey:{'true' if 'alt' in [m.lower() for m in mods] else 'false'},"
            f"metaKey:{'true' if 'meta' in [m.lower() for m in mods] else 'false'}"
            "};"
            "el.dispatchEvent(new MouseEvent('mousedown',opts));"
            "el.dispatchEvent(new MouseEvent('mouseup',opts));"
            "el.dispatchEvent(new MouseEvent('click',opts));"
            "if(el.focus) el.focus();"
            "return true;"
            "})()"
        )
        ok = self._run_js(wv, js)
        return {"ok": bool(ok)}

    def rpc_dblclick_at(self, client, tab_id: int, x: float, y: float):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        target = wv.view.focusProxy() or wv.view
        pos = QPointF(float(x), float(y))
        global_pos = QPointF(target.mapToGlobal(QPoint(int(x), int(y))))
        for evt_type in (QEvent.Type.MouseButtonPress,
                         QEvent.Type.MouseButtonRelease,
                         QEvent.Type.MouseButtonDblClick,
                         QEvent.Type.MouseButtonRelease):
            QApplication.sendEvent(target, QMouseEvent(
                evt_type, pos, global_pos,
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier))
        return {"ok": True}

    def rpc_move_mouse(self, client, tab_id: int, x: float, y: float):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        target = wv.view.focusProxy() or wv.view
        pos = QPointF(float(x), float(y))
        global_pos = QPointF(target.mapToGlobal(QPoint(int(x), int(y))))
        QApplication.sendEvent(target, QMouseEvent(
            QEvent.Type.MouseMove, pos, global_pos,
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))
        return {"ok": True}

    def rpc_scroll(self, client, tab_id: int, dx: float = 0, dy: float = 0):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        # Most reliable cross-version path: scroll via JS.
        wv.view.page().runJavaScript(
            f"window.scrollBy({float(dx)}, {float(dy)});")
        return {"ok": True}

    def rpc_type_text(self, client, tab_id: int, text: str):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        # JS path: set value on the active element if it's an input/
        # textarea/contenteditable, then dispatch input/change events so
        # frameworks (React, Vue) notice.
        js = (
            "(function(t){"
            "const el=document.activeElement;"
            "if(!el) return false;"
            "if(el.tagName==='INPUT'||el.tagName==='TEXTAREA'){"
            "  const proto=Object.getPrototypeOf(el);"
            "  const setter=Object.getOwnPropertyDescriptor(proto,'value');"
            "  if(setter&&setter.set){setter.set.call(el,(el.value||'')+t);}"
            "  else{el.value=(el.value||'')+t;}"
            "  el.dispatchEvent(new Event('input',{bubbles:true}));"
            "  el.dispatchEvent(new Event('change',{bubbles:true}));"
            "  return true;"
            "}"
            "if(el.isContentEditable){"
            "  document.execCommand('insertText',false,t);"
            "  return true;"
            "}"
            "return false;"
            f"}})({json.dumps(text)})"
        )
        ok = self._run_js(wv, js)
        return {"ok": bool(ok)}

    def rpc_send_keys(self, client, tab_id: int, keys: list):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        target = wv.view.focusProxy() or wv.view
        for k in keys:
            qkey, text, mods = _parse_key(k)
            press = QKeyEvent(QEvent.Type.KeyPress, qkey, mods, text)
            release = QKeyEvent(QEvent.Type.KeyRelease, qkey, mods, text)
            QApplication.sendEvent(target, press)
            QApplication.sendEvent(target, release)
        return {"ok": True}

    # -- RPC: introspection --

    def rpc_screenshot(self, _client, tab_id: int, full_page: bool = False):
        wv = self._get_webview(tab_id)
        if full_page:
            # Best-effort full-page: scale viewport down via Qt grab is
            # not feasible. We use page().runJavaScript to scroll and
            # stitch — for v0 we return what's visible.
            pass
        pixmap = wv.view.grab()
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buf, "PNG")
        buf.close()
        return {
            "width": pixmap.width(),
            "height": pixmap.height(),
            "png_b64": bytes(ba.toBase64()).decode("ascii"),
        }

    def rpc_get_dom(self, client, tab_id: int):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        html = self._run_js(wv, "document.documentElement.outerHTML")
        return {"html": html or ""}

    def rpc_get_visible_text(self, client, tab_id: int):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        text = self._run_js(wv, "document.body ? document.body.innerText : ''")
        return {"text": text or ""}

    def rpc_eval_js(self, client, tab_id: int, script: str,
                    timeout: float = 5.0):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        return {"result": self._run_js(wv, script, timeout=timeout)}

    def rpc_query_selector(self, client, tab_id: int, selector: str):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        js = (
            "(()=>{"
            f"const el=document.querySelector({json.dumps(selector)});"
            "if(!el)return null;"
            "const r=el.getBoundingClientRect();"
            "return {x:r.left,y:r.top,w:r.width,h:r.height,"
            "text:(el.innerText||'').slice(0,200)};"
            "})()"
        )
        res = self._run_js(wv, js)
        return {"found": res is not None, "rect": res}

    def rpc_wait_for_selector(self, client, tab_id: int, selector: str,
                              timeout: float = 5.0):
        if tab_id not in client.attached_tabs:
            raise _RpcError(-32001, "not attached")
        wv = self._get_webview(tab_id)
        deadline = time.time() + timeout
        app = QApplication.instance()
        while time.time() < deadline:
            res = self._run_js(
                wv,
                f"!!document.querySelector({json.dumps(selector)})",
                timeout=1.0,
            )
            if res:
                return {"ok": True, "found": True}
            app.processEvents()
            time.sleep(0.1)
        return {"ok": False, "found": False, "timed_out": True}


def _button_to_qt(name: str):
    name = name.lower()
    return {
        "left": Qt.MouseButton.LeftButton,
        "right": Qt.MouseButton.RightButton,
        "middle": Qt.MouseButton.MiddleButton,
    }.get(name, Qt.MouseButton.LeftButton)


def _modifiers_to_qt(names: list):
    mods = Qt.KeyboardModifier.NoModifier
    for n in names:
        m = _MODIFIERS.get(n.lower())
        if m:
            mods |= m
    return mods
