"""qdistro-browser-bridge — native-messaging host (Phase-8 + Phase-9).

Per doc/browser.md §"Phase-8 MVP scope" + todo/browser/01-bridge-phase9.md.

Phase-8 closed the architectural seam: the browser launches us as a
native-messaging host, we read 4-byte length-prefixed JSON on stdin, we
verify our parent is an allowlisted RPM browser binary, and we
round-trip ``qdistro.ping`` and ``recall.push``.

Phase-9 (this module) adds the richer dispatch surface described in
``todo/browser/01-bridge-phase9.md``:

* **9a** ``pwd.fill`` / ``pwd.save`` — forwarded to ``qdistro-pwd``
  daemon via D-Bus. The D-Bus client is injectable so tests can mock
  the daemon.
* **9b** ``tabs.list`` / ``tabs.open`` / ``tabs.close`` — these flow
  *inbound* from a desktop daemon, into the bridge, down to the
  extension over the persistent native-messaging port, back to the
  daemon. The bridge correlates request/reply via ``request_id``.
  An MV3 heartbeat (``qdistro.heartbeat`` ↔ ``qdistro.heartbeat.ack``)
  keeps the Chromium service worker alive: every
  :data:`HEARTBEAT_INTERVAL_S` (25s) the bridge sends a heartbeat; if
  three in a row miss their :data:`HEARTBEAT_DEADLINE_S` (50s) reply
  window, the bridge exits with :data:`EXIT_HEARTBEAT_LOST`.
* **9c** ``page.extract`` — forwarded to the ``qbus-admin`` cross-user
  broker via D-Bus (also mocked through the injectable client).
* **9d** ``qdistro.handshake`` + ``cookies.export`` — handshake
  exchanges a per-session HMAC secret with the extension; cookies and
  any other sensitive op require an intent-token of the shape
  ``{request_id, ts, op, hmac}`` with a 5-second TTL and single-use
  semantics. The verifier lives here so the bridge can fast-fail; a
  daemon-side verifier is stubbed for later replacement.
* **9e** ``mpris.publish`` / ``downloads.notify`` /
  ``notifications.show`` / ``screenlock.inhibit`` /
  ``screenlock.release`` — forwarded to the relevant daemons via the
  same injectable D-Bus client; daemons themselves are not yet shipped
  in qdistro, so the handlers currently land at the mock and return
  ``{"ok": True, "stub": True}`` in production until the daemons land.

Inbound D-Bus surface
---------------------

The bridge registers a session-bus name of the form
``org.qdistro.BrowserBridge.<ppid>`` so co-resident daemons can
initiate browser-bound operations. The object lives at
``/org/qdistro/BrowserBridge`` and exposes a single method:

* ``RequestTabs(s op, s args_json) -> s reply_json``

  ``op`` is one of ``tabs.list``, ``tabs.open``, ``tabs.close``,
  ``page.extract``, ``page.extract.request`` (bridge → ext read of
  the page content; modes ``selection`` / ``visible_text`` /
  ``full_text`` / ``outer_html`` / ``by_selector`` / ``title``),
  ``cookies.export``, ``mpris.publish``, ``downloads.notify``,
  ``notifications.show``, ``screenlock.inhibit``,
  ``screenlock.release``. ``args_json`` is a JSON object with the
  per-op arguments. The reply is a JSON object
  ``{"ok": bool, ...}``.

  Note that the RequestTabs method body does not gate ``op`` against
  this list — anything the extension's dispatcher knows how to
  handle will round-trip. New inbound ops can be added on the
  extension side and the bridge will route them transparently.

Tests do not need a real bus running — they call
:func:`enqueue_inbound_request` directly and read the matching
``request_id`` off the stdio pipe, then call :func:`deliver_reply`
to unblock the simulated daemon caller.

Identity-chain check (Phase-8) is still load-bearing. Every Phase-9
handler receives the ``identity`` dict and the central
:func:`dispatch` enforces ``parent_not_allowed`` before any handler
runs. Handlers strip ``identity`` from their reply bodies so the
extension never sees the kernel-attested fields it might otherwise
spoof on the next message.

The module is pure-python (no Qt, no GTK). The jeepney D-Bus library
is used at runtime, but every D-Bus call routes through the injectable
:data:`_dbus_client` so unit tests run without a session bus.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import struct
import sys
import threading
import time
from typing import Any, Callable

# Allowlist of acceptable parent-process exe paths. Per spec/14:
# RPM-only support matrix; sandboxed-browser support is explicitly
# out for v1. Tests override via ALLOWED_PARENT_EXES_TEST_OVERRIDE
# (read by _resolve_allowlist below).
ALLOWED_PARENT_EXES: tuple[str, ...] = (
    "/usr/lib64/firefox/firefox",
    "/usr/lib/firefox/firefox",   # 32-bit / non-/lib64 distros
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/brave",
    "/usr/bin/brave-browser",
    "/usr/bin/vivaldi",
    "/usr/bin/vivaldi-stable",
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
)


# ---- length-prefix framing ---------------------------------------

def read_message(stream) -> dict | None:
    """Read one 4-byte-length-prefixed JSON message from `stream`.

    Returns None on EOF (the browser closed our stdin = bridge
    should exit). Raises ValueError on length-overflow per the
    Chrome native-messaging cap (1 MiB inbound to the host on
    Chromium; we accept up to 4 MiB so Firefox's larger payloads
    don't trip an early-exit).
    """
    raw_len = stream.read(4)
    if not raw_len:
        return None
    if len(raw_len) != 4:
        raise ValueError(
            f"short length prefix: got {len(raw_len)} bytes")
    (n,) = struct.unpack("<I", raw_len)
    if n > 4 * 1024 * 1024:
        raise ValueError(f"message too large: {n} bytes")
    body = stream.read(n)
    if len(body) != n:
        raise ValueError(
            f"short body: expected {n}, got {len(body)}")
    return json.loads(body.decode("utf-8"))


def write_message(stream, payload: dict) -> None:
    """Write `payload` as a 4-byte-length-prefixed JSON message."""
    body = json.dumps(payload).encode("utf-8")
    stream.write(struct.pack("<I", len(body)))
    stream.write(body)
    stream.flush()


# ---- parent-chain identity check ---------------------------------

def _resolve_allowlist() -> tuple[str, ...]:
    """Return the effective parent-exe allowlist.

    The unsuffixed ``QDISTRO_BROWSER_BRIDGE_ALLOWLIST`` env var was a
    historical escape hatch that production builds also honored — any
    process in the bridge's launch environment could replace the trust
    boundary. P0-2 closes that: only ``..._ALLOWLIST_TEST`` is
    accepted, and only under ``QDISTRO_TEST_MODE=1``. The legacy name
    is now an explicit hard error rather than a silent override so
    nothing accidentally reaches production with the old behaviour.
    """
    legacy = os.environ.get(
        "QDISTRO_BROWSER_BRIDGE_ALLOWLIST", "").strip()
    if legacy:
        raise RuntimeError(
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST is rejected; use "
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST under "
            "QDISTRO_TEST_MODE=1")
    override = os.environ.get(
        "QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST", "").strip()
    if override:
        if os.environ.get("QDISTRO_TEST_MODE", "").strip() != "1":
            raise RuntimeError(
                "QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST requires "
                "QDISTRO_TEST_MODE=1")
        return tuple(p for p in override.split(":") if p)
    return ALLOWED_PARENT_EXES


# ---- extension-identity from kernel-attested argv (P0-1) ----------

def parse_extension_id_from_argv(
        argv: list[str] | None, parent_exe: str) -> str:
    """Return the authentic extension ID for the connecting WebExtension.

    The browser passes the calling extension's identity as a command-line
    argument when it spawns a native-messaging host; this value is set
    by the browser at exec time and is not forgeable by extension JS.

    - **Chrome / Chromium-family (Linux, macOS):** ``argv[1]`` is the
      caller origin in the form ``chrome-extension://<ID>/``.
    - **Firefox** (since Firefox 55): ``argv[1]`` is the path to the
      host manifest; ``argv[2]`` is the extension ID as declared in
      ``browser_specific_settings.gecko.id``.

    Returns "" if argv shape doesn't match either form — caller treats
    empty as "unknown extension," not "trusted to be anything." The
    bridge no longer accepts a stdio-supplied extension_id field; only
    this argv-derived value is authoritative.
    """
    import re
    if not argv or len(argv) < 2:
        return ""
    exe = (parent_exe or "").lower()
    if "firefox" in exe:
        if len(argv) < 3:
            return ""
        ext_id = argv[2].strip()
        # Firefox: UUID-in-braces ``{...}`` or ``name@host`` style.
        if re.fullmatch(
                r"\{[0-9a-fA-F-]+\}|[A-Za-z0-9_.+-]+@[A-Za-z0-9_.-]+",
                ext_id):
            return ext_id
        return ""
    chrome_origin = argv[1].strip()
    if chrome_origin.startswith("chrome-extension://"):
        rest = chrome_origin[len("chrome-extension://"):]
        rest = rest.rstrip("/").strip()
        # Chrome / Chromium: extension IDs are exactly 32 a-p chars.
        if re.fullmatch(r"[a-p]{32}", rest):
            return rest
        return ""
    return ""


def read_parent_exe(ppid: int) -> str:
    """Resolve /proc/<ppid>/exe to the on-disk path. Returns "" on
    any failure — caller treats empty string as "unknown" + denies.
    """
    try:
        return os.readlink(f"/proc/{ppid}/exe")
    except OSError:
        return ""


def read_parent_selinux(ppid: int) -> str:
    """Read /proc/<ppid>/attr/current. Empty string if SELinux is
    disabled or the file is unreadable.
    """
    try:
        with open(f"/proc/{ppid}/attr/current", "r",
                  encoding="utf-8") as f:
            return f.read().strip("\x00 \n")
    except OSError:
        return ""


def verify_parent(
        ppid_fn: Callable[[], int] = os.getppid,
        exe_reader: Callable[[int], str] = read_parent_exe,
        selinux_reader: Callable[[int], str] = read_parent_selinux,
        allowlist: tuple[str, ...] | None = None,
        argv: list[str] | None = None,
) -> dict:
    """Build an identity dict for the parent process.

    Always returns a dict; ``allowed`` is the gate the dispatcher
    checks. Tests inject the callables (and optionally argv) to drive
    every branch.

    The ``extension_id`` field is derived from ``argv`` via
    :func:`parse_extension_id_from_argv` — kernel-attested at the
    browser's ``execve`` time. The bridge never trusts a stdio-supplied
    ``extension_id`` field (P0-1).
    """
    allow = allowlist if allowlist is not None else _resolve_allowlist()
    ppid = int(ppid_fn())
    exe = exe_reader(ppid)
    selinux = selinux_reader(ppid)
    allowed = bool(exe) and exe in allow
    if argv is None:
        argv = sys.argv
    extension_id = parse_extension_id_from_argv(argv, exe)
    return {
        "ppid": ppid,
        "parent_exe": exe,
        "parent_selinux": selinux,
        "extension_id": extension_id,
        "allowed": allowed,
    }


# ---- dispatch table ----------------------------------------------
# Handlers receive (msg, identity) and return the response dict
# (NOT length-framed; the caller wraps).

def _handle_ping(msg: dict, identity: dict) -> dict:
    """qdistro.ping — round-trip echo with identity confirmation.

    ``extension_id`` is read from the identity dict (derived from argv
    by :func:`parse_extension_id_from_argv`), **not** from the stdio
    payload. The stdio field is ignored entirely.
    """
    return {
        "pong": True,
        "echo": msg.get("echo"),
        "ppid": identity["ppid"],
        "parent_exe": identity["parent_exe"],
        "parent_selinux": identity["parent_selinux"],
        "extension_id": str(identity.get("extension_id") or ""),
    }


def _handle_recall_push(msg: dict, identity: dict) -> dict:
    """recall.push — text snapshot ingest from the WebExtension.

    Spec/17 §step 0 MVP: extension captures page text + URL on
    meaningful navigation/content change and ships it through the
    bridge. The bridge rejects pwd-domain pushes locally (advisory
    layer per spec/17 §"Pwd-manager exclusion") and otherwise
    inserts via the recall engine into the calling user's per-day DB.

    Optional ``QDISTRO_RECALL_ROOT`` env var override is honored so
    bats can drive this against a tmpdir.
    """
    text = msg.get("text") or ""
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "missing_text"}
    # Defer the import: the bridge module is in the
    # qdistro_browser_bridge package; the recall engine + SDK live
    # under a separate prefix. Tests can monkey-patch
    # `_recall_push_impl` to skip the engine.
    impl = _recall_push_impl
    if impl is None:
        impl = _default_recall_push_impl
    return impl(msg, identity, text)


def _default_recall_push_impl(msg: dict, identity: dict,
                              text: str) -> dict:
    """Default recall-push backend: write to the per-day SQLite DB
    via qdistro_recall_ingest. Imported lazily so the bridge can
    run on hosts where recall isn't installed (the qdistro.ping op
    still works).

    The destination user is **always** the bridge process's own UID
    via ``getpass.getuser()``. The bridge inherits the browser's UID,
    so this is the kernel-attested caller. P0-3 removed the previous
    ``msg.get("user")`` field — accepting that from the extension
    payload was a cross-silo write primitive (a compromised extension
    in user A's browser could write rows tagged as user B).
    """
    import getpass
    candidates = [
        "/usr/libexec/qdistro/qdistro_recall_ingest.py",
        os.path.join(os.path.dirname(__file__),
                     "..", "recall",
                     "qdistro_recall_ingest.py"),
    ]
    eng = None
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.isfile(path):
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "qdistro_recall_ingest", path)
            eng = importlib.util.module_from_spec(spec)
            sys.modules["qdistro_recall_ingest"] = eng
            spec.loader.exec_module(eng)
            break
    if eng is None:
        return {"ok": False, "error": "recall_engine_missing"}
    user = getpass.getuser()
    root = (os.environ.get("QDISTRO_RECALL_ROOT", "").strip()
            or "/var/lib/qdistro/recall")
    db_path = eng.db_path_for(root, user)
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = eng.open_db(db_path)
    try:
        try:
            rowid = eng.push_text(
                conn, user=user, text=text, source="bridge",
                app_id=str(msg.get("app_id") or "") or None,
                exe=identity.get("parent_exe") or None,
                secctx=str(msg.get("secctx") or "") or None,
                url=str(msg.get("url") or "") or None,
                title=str(msg.get("title") or "") or None,
            )
        except eng.PwdDomainRefused as refused:
            return {"ok": False, "error": "pwd_domain_refused",
                    "detail": str(refused)[:200]}
    finally:
        conn.close()
    return {"ok": True, "row_id": int(rowid),
            "user": user, "db": db_path}


# Tests override this by assigning a callable; production paths
# leave it None so the lazy default impl runs.
_recall_push_impl: Callable[[dict, dict, str], dict] | None = None


# =====================================================================
# Phase-9 infrastructure
# =====================================================================

# ---- exit codes --------------------------------------------------
EXIT_OK = 0
EXIT_FRAME_ERROR = 2
EXIT_HEARTBEAT_LOST = 3

# ---- heartbeat tunables (overridable for tests) ------------------
# Default per todo/browser/01-bridge-phase9.md §9b: 25s interval,
# 50s reply deadline, 3 misses → exit. The Chromium MV3 service
# worker is killed after 30s of idle, so 25s keeps it alive.
HEARTBEAT_INTERVAL_S: float = 25.0
HEARTBEAT_DEADLINE_S: float = 50.0
HEARTBEAT_MAX_MISSES: int = 3

# ---- intent-token tunables ---------------------------------------
INTENT_TOKEN_TTL_S: float = 5.0
# Ops that REQUIRE a valid intent token before they will be served.
# Anything not in this set bypasses token validation (e.g. ping,
# recall.push, heartbeat ack).
INTENT_TOKEN_REQUIRED_OPS: frozenset[str] = frozenset({
    "pwd.fill", "pwd.fill_confirm", "pwd.save", "cookies.export",
    "page.extract",
})


# ---- D-Bus client abstraction ------------------------------------
# All outbound D-Bus calls go through a single small interface so
# tests can mock the daemon side without a running bus. Production
# binds the jeepney-backed default at first use.

class _BaseDBusClient:
    """Outbound D-Bus client surface used by Phase-9 handlers.

    A single ``call`` entry point — daemons named here are
    ``qdistro-pwd`` (SYSTEM bus), ``qbus-admin`` (SYSTEM bus),
    ``qdistro-downloads``, ``qdistro-notifications``, ``qdistro-mpris``,
    ``qdistro-compositor`` (SESSION bus). The ``bus`` argument
    (``"SESSION"`` or ``"SYSTEM"``) is REQUIRED — there is no implicit
    default — because mismatched-bus drift between the bridge and the
    daemon it's supposed to reach silently breaks production
    (see plan2/reviews/P04 H3 / HIGH-1 review findings).

    Any daemon may not yet exist; in that case the default jeepney
    client returns ``{"ok": False, "error": "daemon_missing"}`` and
    the handler propagates it.
    """

    def call(self, bus: str, service: str, object_path: str,
             interface: str, method: str, signature: str,
             body: tuple) -> dict:
        raise NotImplementedError


class _JeepneyDBusClient(_BaseDBusClient):
    """Default production client. jeepney is pure-Python so it never
    pulls a C extension into the bridge process.

    The ``bus`` argument selects ``"SESSION"`` or ``"SYSTEM"``. The
    pwd / broker daemons live on SYSTEM; mpris / compositor / downloads
    on SESSION. See module docstring on `_BaseDBusClient` for the
    rationale.
    """

    def call(self, bus: str, service: str, object_path: str,
             interface: str, method: str, signature: str,
             body: tuple) -> dict:
        if bus not in ("SESSION", "SYSTEM"):
            return {"ok": False, "error": "bad_bus", "bus": bus}
        try:
            from jeepney import DBusAddress, new_method_call
            from jeepney.io.blocking import open_dbus_connection
        except ImportError:
            return {"ok": False, "error": "jeepney_missing"}
        try:
            addr = DBusAddress(
                object_path=object_path,
                bus_name=service,
                interface=interface)
            msg = new_method_call(addr, method, signature, body)
            conn = open_dbus_connection(bus=bus)
            try:
                reply = conn.send_and_get_reply(msg, timeout=5.0)
            finally:
                conn.close()
            if reply.header.message_type.name == "ERROR":
                return {"ok": False, "error": "dbus_error",
                        "detail": str(reply.body)[:200]}
            # Daemons return a single JSON-string reply by convention;
            # decode it. Any other shape is surfaced as-is.
            if reply.body and isinstance(reply.body[0], str):
                try:
                    return json.loads(reply.body[0])
                except json.JSONDecodeError:
                    return {"ok": True, "raw": reply.body[0]}
            return {"ok": True, "body": list(reply.body)}
        except Exception as e:
            return {"ok": False, "error": "dbus_call_failed",
                    "detail": str(e)[:200]}


# Tests override by assigning a _BaseDBusClient instance. Production
# leaves it None so the jeepney default binds lazily on first use.
_dbus_client: _BaseDBusClient | None = None


def _get_dbus_client() -> _BaseDBusClient:
    global _dbus_client
    if _dbus_client is None:
        _dbus_client = _JeepneyDBusClient()
    return _dbus_client


# ---- intent-token store ------------------------------------------
# Per-process, in-memory. Each bridge launch rotates the session
# secret. The store is keyed by request_id so single-use is trivial:
# verification pops the entry. Expired entries are swept on every
# verify call.

_SESSION_SECRET: bytes = secrets.token_bytes(32)
_USED_TOKENS: dict[str, float] = {}
_TOKEN_LOCK = threading.Lock()


def reset_session_secret() -> bytes:
    """Rotate the per-session HMAC secret. Called on bridge startup
    and exposed for tests.

    Also clears the per-extension secret registry — a master rotation
    invalidates every previously-handed-out derived secret.
    """
    global _SESSION_SECRET, _USED_TOKENS
    with _TOKEN_LOCK:
        _SESSION_SECRET = secrets.token_bytes(32)
        _USED_TOKENS = {}
    with _EXT_SECRETS_LOCK:
        _EXT_SECRETS.clear()
    return _SESSION_SECRET


def _compute_token_hmac(request_id: str, ts: float, op: str,
                        secret: bytes | None = None) -> str:
    """Compute the canonical HMAC for an intent token.

    Pass ``secret`` to compute against an explicit derived per-extension
    secret. With ``secret=None`` the bridge's master is used — this is
    only correct for the no-extension test path; production handshakes
    return :func:`derive_extension_session_secret(master, extension_id)`
    so an attacker who steals one extension's secret can't forge tokens
    for another extension.
    """
    s = secret if secret is not None else _SESSION_SECRET
    msg = f"{request_id}|{ts}|{op}".encode("utf-8")
    return hmac.new(s, msg, hashlib.sha256).hexdigest()


def _sweep_used_tokens(now: float | None = None) -> None:
    """Drop tokens whose TTL has elapsed. Called under _TOKEN_LOCK."""
    now = time.time() if now is None else now
    cutoff = now - INTENT_TOKEN_TTL_S
    dead = [k for k, ts in _USED_TOKENS.items() if ts < cutoff]
    for k in dead:
        _USED_TOKENS.pop(k, None)


def verify_intent_token(token: dict | None, op: str,
                        now_fn: Callable[[], float] = time.time,
                        *,
                        extension_id: str | None = None,
                        ) -> tuple[bool, str]:
    """Return ``(ok, error)``. Single-use: a successful verify marks
    the request_id as consumed.

    ``extension_id`` selects the per-extension derived secret recorded
    on the prior :func:`_handle_handshake` call. When ``None`` (legacy
    path / direct tests), the master secret is used so the existing
    unit suite keeps working unchanged.

    Rejects:
      * missing / malformed token → ``"missing_intent_token"``
      * wrong op → ``"intent_token_op_mismatch"``
      * expired (ts older than INTENT_TOKEN_TTL_S) → ``"intent_token_expired"``
      * future-dated by > 1s → ``"intent_token_future"``
      * bad HMAC → ``"intent_token_bad_hmac"``
      * replay (request_id already used) → ``"intent_token_replay"``
    """
    if not isinstance(token, dict):
        return False, "missing_intent_token"
    request_id = token.get("request_id")
    ts = token.get("ts")
    tok_op = token.get("op")
    mac = token.get("hmac")
    if not (isinstance(request_id, str) and request_id
            and isinstance(ts, (int, float))
            and isinstance(tok_op, str)
            and isinstance(mac, str) and mac):
        return False, "missing_intent_token"
    if tok_op != op:
        return False, "intent_token_op_mismatch"
    now = now_fn()
    if ts > now + 1.0:
        return False, "intent_token_future"
    if now - ts > INTENT_TOKEN_TTL_S:
        return False, "intent_token_expired"
    if extension_id is not None:
        secret = _lookup_extension_secret(extension_id)
        if secret is None:
            # No handshake on record for this extension — fall back
            # to master so the legacy stdio test path still works,
            # but production callers MUST handshake first.
            secret = _SESSION_SECRET
    else:
        secret = _SESSION_SECRET
    expected = _compute_token_hmac(request_id, ts, tok_op, secret=secret)
    if not hmac.compare_digest(expected, mac):
        return False, "intent_token_bad_hmac"
    with _TOKEN_LOCK:
        _sweep_used_tokens(now)
        if request_id in _USED_TOKENS:
            return False, "intent_token_replay"
        _USED_TOKENS[request_id] = now
    return True, ""


# ---- pending-request correlation (Phase-9b inbound flow) --------
# When a daemon calls into the bridge (via D-Bus), the bridge mints
# a request_id, parks the caller on a threading.Event, and pushes
# ``{op, request_id, ...}`` down the stdio pipe to the extension.
# When the extension replies with ``{op: '<op>.reply', request_id}``
# we set the Event and the caller wakes up to read the reply.

class _PendingRequest:
    __slots__ = ("event", "reply", "op", "created")

    def __init__(self, op: str) -> None:
        self.event = threading.Event()
        self.reply: dict | None = None
        self.op = op
        self.created = time.time()


_pending: dict[str, _PendingRequest] = {}
_pending_lock = threading.Lock()
_request_seq: int = 0


def _next_request_id() -> str:
    global _request_seq
    with _pending_lock:
        _request_seq += 1
        # Mix in a few random bits so an extension that observes one
        # request_id can't trivially predict the next one and inject a
        # spoofed reply.
        return f"r{_request_seq}-{secrets.token_hex(4)}"


def enqueue_inbound_request(op: str, args: dict | None,
                            out_stream,
                            timeout_s: float = 75.0) -> dict:
    """Push an inbound op down the stdio pipe and block for the reply.

    Used by the D-Bus surface (and by tests). Returns the reply dict
    when the extension answers, or ``{"ok": False, "error":
    "request_timeout"}`` on no reply within ``timeout_s``.
    """
    if not isinstance(args, dict):
        args = {}
    req_id = _next_request_id()
    pending = _PendingRequest(op)
    with _pending_lock:
        _pending[req_id] = pending
    payload = {"op": op, "request_id": req_id, **args}
    try:
        write_message(out_stream, payload)
    except Exception as e:
        with _pending_lock:
            _pending.pop(req_id, None)
        return {"ok": False, "error": "stdio_write_failed",
                "detail": str(e)[:200]}
    ok = pending.event.wait(timeout=timeout_s)
    with _pending_lock:
        _pending.pop(req_id, None)
    if not ok:
        return {"ok": False, "error": "request_timeout",
                "op": op, "request_id": req_id}
    return pending.reply or {"ok": False, "error": "empty_reply"}


def deliver_reply(msg: dict) -> bool:
    """Try to match ``msg`` as a reply to a pending inbound request.

    Returns True if it matched (caller is unblocked and should NOT
    dispatch the message further); False if there's no pending entry
    with that request_id (in which case dispatch continues normally).
    """
    req_id = msg.get("request_id")
    if not isinstance(req_id, str) or not req_id:
        return False
    with _pending_lock:
        pending = _pending.get(req_id)
    if pending is None:
        return False
    pending.reply = msg
    pending.event.set()
    return True


# ---- identity rejection helper -----------------------------------

def _identity_gate(identity: dict) -> dict | None:
    """Return an error dict if identity is not allowed, else None.

    Called at the top of every Phase-9 handler. ``dispatch`` already
    gates inbound stdio messages, but inbound D-Bus paths call handlers
    via :func:`enqueue_inbound_request` which doesn't pass through
    ``dispatch``. We re-check defensively.
    """
    if not identity.get("allowed"):
        return {"ok": False, "error": "parent_not_allowed",
                "parent_exe": identity.get("parent_exe", "")}
    return None


# =====================================================================
# Phase-9 handlers
# =====================================================================

_IDENTITY_FIELDS = frozenset({
    "ppid", "parent_exe", "parent_selinux", "extension_id", "allowed",
})

# Ops whose reply intentionally surfaces (a subset of) identity
# fields. Dispatch strips identity universally and then re-stamps
# these specific fields from the bridge's view of the caller.
# Adding a new op here requires a security review.
_IDENTITY_REFLECTING_OPS = frozenset({
    "qdistro.ping",
    "qdistro.handshake",
})


def _strip_identity(reply: dict) -> dict:
    """Defensive: handlers must never leak identity fields back to
    the extension on the stdio reply path. Kernel-attested fields
    on the identity dict (parent_exe, ppid, parent_selinux,
    extension_id) belong to the bridge, not to the daemon's reply.

    This is now applied centrally by :func:`dispatch` so handlers
    don't need to remember; kept exposed for tests + back-compat.
    """
    for k in _IDENTITY_FIELDS:
        reply.pop(k, None)
    return reply


# ---- 9a: pwd.fill / pwd.save -------------------------------------
#
# CANONICAL NAMES — must match ``qdistro/pwd/qdistro_pwd_daemon.py``:
#   well-known name : ``org.qdistro.Pwd1`` on the SYSTEM bus
#   object path     : ``/org/qdistro/Pwd1``
#   interface       : ``org.qdistro.Pwd1``
# Three previous review angles flagged the same bus-name drift; we
# now keep one set of constants and import them everywhere.

_PWD_BUS_KIND = "SYSTEM"
_PWD_BUS = "org.qdistro.Pwd1"
_PWD_PATH = "/org/qdistro/Pwd1"
_PWD_IFACE = "org.qdistro.Pwd1"


def _handle_pwd_fill(msg: dict, identity: dict) -> dict:
    """pwd.fill — fetch credentials for a URL.

    Forwards to ``qdistro-pwd`` (org.qdistro.Pwd1 / Pwd1.Fill on the
    SYSTEM bus). Requires an intent token (see
    :data:`INTENT_TOKEN_REQUIRED_OPS`).
    """
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    ok, err = verify_intent_token(
        msg.get("intent_token"), "pwd.fill",
        extension_id=identity.get("extension_id"))
    if not ok:
        return {"ok": False, "error": err}
    url = msg.get("url")
    if not isinstance(url, str) or not url:
        return {"ok": False, "error": "missing_url"}
    body = json.dumps({
        "url": url,
        "username": str(msg.get("username") or "") or None,
        "extension_id": identity.get("extension_id") or "",
        "parent_exe": identity.get("parent_exe") or "",
    })
    reply = _get_dbus_client().call(
        _PWD_BUS_KIND, _PWD_BUS, _PWD_PATH, _PWD_IFACE,
        "Fill", "s", (body,))
    return dict(reply)


def _handle_pwd_fill_confirm(msg: dict, identity: dict) -> dict:
    """pwd.fill_confirm — retrieve the actual password after the user
    picks a credential from the Fill list.
    """
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    ok, err = verify_intent_token(
        msg.get("intent_token"), "pwd.fill_confirm",
        extension_id=identity.get("extension_id"))
    if not ok:
        return {"ok": False, "error": err}
    url = msg.get("url")
    username = msg.get("username")
    if not (isinstance(url, str) and url
            and isinstance(username, str) and username):
        return {"ok": False, "error": "missing_credentials"}
    body = json.dumps({
        "url": url, "username": username,
        "extension_id": identity.get("extension_id") or "",
        "parent_exe": identity.get("parent_exe") or "",
    })
    reply = _get_dbus_client().call(
        _PWD_BUS_KIND, _PWD_BUS, _PWD_PATH, _PWD_IFACE,
        "FillConfirm", "s", (body,))
    return dict(reply)


def _handle_pwd_save(msg: dict, identity: dict) -> dict:
    """pwd.save — persist credentials for a URL.

    Forwards to ``qdistro-pwd`` (org.qdistro.Pwd1 / Pwd1.Save on the
    SYSTEM bus).
    """
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    ok, err = verify_intent_token(
        msg.get("intent_token"), "pwd.save",
        extension_id=identity.get("extension_id"))
    if not ok:
        return {"ok": False, "error": err}
    url = msg.get("url")
    username = msg.get("username")
    password = msg.get("password")
    if not (isinstance(url, str) and url
            and isinstance(username, str) and username
            and isinstance(password, str) and password):
        return {"ok": False, "error": "missing_credentials"}
    body = json.dumps({
        "url": url, "username": username, "password": password,
        "extension_id": identity.get("extension_id") or "",
        "parent_exe": identity.get("parent_exe") or "",
    })
    reply = _get_dbus_client().call(
        _PWD_BUS_KIND, _PWD_BUS, _PWD_PATH, _PWD_IFACE,
        "Save", "s", (body,))
    return dict(reply)


# ---- 9b: tabs.* (extension-side replies) -------------------------
# The tabs ops normally arrive *inbound* via D-Bus (see
# :func:`enqueue_inbound_request`), but the bridge also accepts them
# from the extension as a fallback for development / testing.
# In that case there's no daemon waiting, so we just echo back.

def _handle_tabs_reply(msg: dict, identity: dict) -> dict:
    """Generic *.reply landing for tabs ops the extension answers.

    When the extension replies to an inbound tabs.list/open/close
    request, deliver_reply() unblocks the parked daemon thread.
    This handler is also reached if the extension sends a *.reply
    without a matching request_id (no-op).
    """
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    delivered = deliver_reply(msg)
    return {"ok": True, "delivered": delivered}


# ---- 9b: heartbeat ack -------------------------------------------

class _HeartbeatState:
    """Track outstanding heartbeats. Thread-safe."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outstanding: dict[str, float] = {}
        self._misses = 0

    def sent(self, req_id: str, now: float) -> None:
        with self._lock:
            self._outstanding[req_id] = now

    def acked(self, req_id: str) -> bool:
        with self._lock:
            if req_id in self._outstanding:
                self._outstanding.pop(req_id, None)
                self._misses = 0
                return True
            return False

    def sweep(self, now: float, deadline_s: float) -> int:
        """Drop heartbeats older than deadline; increment misses for
        each. Returns current consecutive-miss count."""
        with self._lock:
            dead = [k for k, t in self._outstanding.items()
                    if now - t > deadline_s]
            for k in dead:
                self._outstanding.pop(k, None)
                self._misses += 1
            return self._misses


_heartbeat = _HeartbeatState()


def _handle_heartbeat_ack(msg: dict, identity: dict) -> dict:
    """qdistro.heartbeat.ack — extension confirms it's still alive."""
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    req_id = str(msg.get("request_id") or "")
    matched = _heartbeat.acked(req_id) if req_id else False
    return {"ok": True, "matched": matched}


# ---- 9c: page.extract --------------------------------------------

_BROKER_BUS_KIND = "SYSTEM"
_BROKER_BUS = "org.qdistro.AdminBroker1"
_BROKER_PATH = "/org/qdistro/AdminBroker1"
_BROKER_IFACE = "org.qdistro.AdminBroker1"


def _handle_page_extract(msg: dict, identity: dict) -> dict:
    """page.extract — share a page snippet via the qbus-admin broker.

    The broker lives on the SYSTEM bus
    (``org.qdistro.AdminBroker1``).
    """
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    ok, err = verify_intent_token(
        msg.get("intent_token"), "page.extract",
        extension_id=identity.get("extension_id"))
    if not ok:
        return {"ok": False, "error": err}
    url = msg.get("url")
    if not isinstance(url, str) or not url:
        return {"ok": False, "error": "missing_url"}
    body = json.dumps({
        "url": url,
        "title": str(msg.get("title") or ""),
        "selected_text": str(msg.get("selected_text") or ""),
        "dest_uid": str(msg.get("dest_uid") or ""),
        "content_type": str(msg.get("content_type") or "url"),
        "parent_exe": identity.get("parent_exe") or "",
        "extension_id": identity.get("extension_id") or "",
    })
    reply = _get_dbus_client().call(
        _BROKER_BUS_KIND, _BROKER_BUS, _BROKER_PATH, _BROKER_IFACE,
        "PageExtract", "s", (body,))
    return dict(reply)


# ---- 9d: handshake + cookies.export ------------------------------

def derive_extension_session_secret(master: bytes,
                                    extension_id: str) -> bytes:
    """Derive a per-extension session secret from the bridge's master.

    The bridge process keeps a single per-launch master secret in
    :data:`_SESSION_SECRET`. Each connecting extension gets a derived
    secret bound to its kernel-attested ``extension_id`` (from the
    browser's exec-time argv, NOT from stdio) so that:

      * a captured secret only forges tokens for ONE extension_id;
      * two extensions sharing the same bridge process can't replay
        each other's intent tokens even if one is compromised;
      * an extension whose argv-attested id is ``""`` (e.g. someone
        running the bridge manually for development) gets an
        unforgeable but un-shareable derived value.

    Tests can drive the derivation directly to assert that
    handshake-reply secrets for two different extension_ids differ.
    """
    msg = ("extension|" + (extension_id or "")).encode("utf-8")
    return hmac.new(master, msg, hashlib.sha256).digest()


def _handle_handshake(msg: dict, identity: dict) -> dict:
    """qdistro.handshake — return a per-extension session HMAC secret.

    The extension uses the returned secret to mint intent tokens.
    The bridge derives it from the per-launch master secret bound
    to the caller's kernel-attested ``extension_id`` (from argv at
    browser exec time, not from the stdio payload). This means:

    * Two extensions reaching the same bridge process get distinct
      secrets — even if one leaks, the other's intent-token chain is
      not forged.
    * The bridge process restarts → new master → new derived secret;
      any previously-stored secret in a qdbrowser orchestrator
      becomes invalid and the orchestrator must re-handshake.

    Trust model: the extension is trusted within the browser
    sandbox. A compromised extension can still mint valid intent
    tokens for itself — intent tokens defend against
    web-page-triggered calls and replays, not against extension
    compromise (see todo/browser/01-bridge-phase9.md §9d).
    """
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    extension_id = str(identity.get("extension_id") or "")
    derived = derive_extension_session_secret(
        _SESSION_SECRET, extension_id)
    # Also pin verify_intent_token to expect the derived secret for
    # subsequent calls from this extension. The verifier looks up
    # the per-extension secret by extension_id; see _per_ext_secret.
    _remember_extension_secret(extension_id, derived)
    return {
        "ok": True,
        "session_secret_hex": derived.hex(),
        "token_ttl_s": INTENT_TOKEN_TTL_S,
        "hmac_algo": "sha256",
        "token_canonical": "request_id|ts|op",
        "extension_id": extension_id,
    }


# Per-extension secret registry. Populated by `_handle_handshake`.
# verify_intent_token() consults this so a token minted by extension A
# can't be replayed by extension B (different derived secret).
_EXT_SECRETS: dict[str, bytes] = {}
_EXT_SECRETS_LOCK = threading.Lock()


def _remember_extension_secret(extension_id: str, secret: bytes) -> None:
    with _EXT_SECRETS_LOCK:
        _EXT_SECRETS[extension_id] = secret


def _lookup_extension_secret(extension_id: str) -> bytes | None:
    with _EXT_SECRETS_LOCK:
        return _EXT_SECRETS.get(extension_id)


_COOKIES_BUS_KIND = "SYSTEM"
_COOKIES_BUS = "org.qdistro.Pwd1"  # cookies live with pwd daemon (SYSTEM)
_COOKIES_PATH = "/org/qdistro/Pwd1"
_COOKIES_IFACE = "org.qdistro.Pwd1"


def _handle_cookies_export(msg: dict, identity: dict) -> dict:
    """cookies.export — audit-logged TTL-limited cookie export.

    Cookies live with the pwd daemon at
    ``org.qdistro.Pwd1`` (SYSTEM bus).
    """
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    ok, err = verify_intent_token(msg.get("intent_token"),
                                  "cookies.export",
                                  extension_id=identity.get("extension_id"))
    if not ok:
        return {"ok": False, "error": err}
    domain = msg.get("domain") or msg.get("url")
    if not isinstance(domain, str) or not domain:
        return {"ok": False, "error": "missing_domain"}
    cookies = msg.get("cookies") or []
    if not isinstance(cookies, list):
        return {"ok": False, "error": "bad_cookies"}
    body = json.dumps({
        "domain": domain,
        "cookies": cookies,
        "extension_id": identity.get("extension_id") or "",
        "parent_exe": identity.get("parent_exe") or "",
    })
    reply = _get_dbus_client().call(
        _COOKIES_BUS_KIND, _COOKIES_BUS, _COOKIES_PATH, _COOKIES_IFACE,
        "ExportCookies", "s", (body,))
    return dict(reply)


# ---- 9e: MPRIS / downloads / notifications / screenlock ----------

_MPRIS_BUS_KIND = "SESSION"
_MPRIS_BUS = "org.qdistro.Mpris"
_MPRIS_PATH = "/org/qdistro/Mpris"
_MPRIS_IFACE = "org.qdistro.Mpris1"

_DOWNLOADS_BUS_KIND = "SESSION"
_DOWNLOADS_BUS = "org.qdistro.Downloads"
_DOWNLOADS_PATH = "/org/qdistro/Downloads"
_DOWNLOADS_IFACE = "org.qdistro.Downloads1"

_NOTIF_BUS_KIND = "SESSION"
_NOTIF_BUS = "org.qdistro.Notifications"
_NOTIF_PATH = "/org/qdistro/Notifications"
_NOTIF_IFACE = "org.qdistro.Notifications1"

_COMPOSITOR_BUS_KIND = "SESSION"
_COMPOSITOR_BUS = "org.qdistro.Compositor"
_COMPOSITOR_PATH = "/org/qdistro/Compositor"
_COMPOSITOR_IFACE = "org.qdistro.Compositor1"


def _forward(msg: dict, identity: dict,
             bus_kind: str, bus: str, path: str, iface: str, method: str,
             fields: tuple[str, ...]) -> dict:
    body = {f: msg.get(f) for f in fields}
    body["extension_id"] = identity.get("extension_id") or ""
    body["parent_exe"] = identity.get("parent_exe") or ""
    reply = _get_dbus_client().call(
        bus_kind, bus, path, iface, method, "s",
        (json.dumps(body),))
    return dict(reply)


def _handle_mpris_publish(msg: dict, identity: dict) -> dict:
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    return _forward(msg, identity,
                    _MPRIS_BUS_KIND, _MPRIS_BUS, _MPRIS_PATH,
                    _MPRIS_IFACE, "Publish",
                    ("title", "artist", "album", "playback_status",
                     "position_us", "tab_id"))


def _handle_downloads_notify(msg: dict, identity: dict) -> dict:
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    return _forward(msg, identity,
                    _DOWNLOADS_BUS_KIND, _DOWNLOADS_BUS,
                    _DOWNLOADS_PATH, _DOWNLOADS_IFACE,
                    "Notify",
                    ("download_id", "filename", "state",
                     "bytes_received", "total_bytes", "url", "mime"))


def _handle_notifications_show(msg: dict, identity: dict) -> dict:
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    return _forward(msg, identity,
                    _NOTIF_BUS_KIND, _NOTIF_BUS, _NOTIF_PATH,
                    _NOTIF_IFACE, "Show",
                    ("title", "body", "icon_url", "origin", "tag"))


def _handle_screenlock_inhibit(msg: dict, identity: dict) -> dict:
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    return _forward(msg, identity,
                    _COMPOSITOR_BUS_KIND, _COMPOSITOR_BUS,
                    _COMPOSITOR_PATH,
                    _COMPOSITOR_IFACE, "ScreenlockInhibit",
                    ("reason", "tab_id", "url"))


def _handle_screenlock_release(msg: dict, identity: dict) -> dict:
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    return _forward(msg, identity,
                    _COMPOSITOR_BUS_KIND, _COMPOSITOR_BUS,
                    _COMPOSITOR_PATH,
                    _COMPOSITOR_IFACE, "ScreenlockRelease",
                    ("tab_id", "url"))


# =====================================================================
# heartbeat loop (Phase-9b)
# =====================================================================

def heartbeat_loop(out_stream,
                   stop_event: threading.Event,
                   interval_s: float | None = None,
                   deadline_s: float | None = None,
                   max_misses: int | None = None,
                   on_exit: Callable[[int], None] | None = None,
                   now_fn: Callable[[], float] = time.time,
                   ) -> None:
    """Send periodic heartbeats; exit if too many miss their deadline.

    Runs in a daemon thread. ``stop_event`` is set when the bridge
    main loop is exiting, so we don't keep tripping after EOF on
    stdin. On too-many misses, calls ``on_exit`` (default os._exit
    with EXIT_HEARTBEAT_LOST) so the process tears down even though
    main may be blocked on stdin.read().
    """
    interval = interval_s if interval_s is not None else HEARTBEAT_INTERVAL_S
    deadline = deadline_s if deadline_s is not None else HEARTBEAT_DEADLINE_S
    misses_cap = max_misses if max_misses is not None else HEARTBEAT_MAX_MISSES
    exit_fn = on_exit if on_exit is not None else (
        lambda rc: os._exit(rc))
    while not stop_event.is_set():
        now = now_fn()
        req_id = _next_request_id()
        _heartbeat.sent(req_id, now)
        try:
            write_message(out_stream,
                          {"op": "qdistro.heartbeat",
                           "request_id": req_id, "ts": now})
        except Exception:
            # stdio closed — main loop will exit on its own.
            return
        # Wait for the next tick OR for the stop signal. Using
        # stop_event.wait lets tests unblock the thread promptly.
        stop_event.wait(timeout=interval)
        misses = _heartbeat.sweep(now_fn(), deadline)
        if misses >= misses_cap:
            exit_fn(EXIT_HEARTBEAT_LOST)
            return


# =====================================================================
# inbound D-Bus surface (Phase-9b)
# =====================================================================

def inbound_dbus_serve(ppid: int, out_stream,
                       stop_event: threading.Event,
                       request_timeout_s: float = 75.0,
                       ) -> None:
    """Register ``org.qdistro.BrowserBridge.<ppid>`` and dispatch.

    Pulled into its own function so unit tests can skip it. The
    implementation uses jeepney's blocking router; if jeepney isn't
    available (e.g. inside a minimal test image) we log and return.

    Method exported:

      ``RequestTabs(s op, s args_json) -> s reply_json``
    """
    try:
        from jeepney import (DBusAddress, MessageType,
                             new_method_return, new_error,
                             new_signal)
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import open_dbus_connection
        from jeepney.wrappers import DBusErrorResponse
    except ImportError:
        sys.stderr.write(
            "qdistro-browser-bridge: jeepney missing; inbound "
            "D-Bus surface disabled\n")
        return
    bus_name = f"org.qdistro.BrowserBridge.{ppid}"
    try:
        conn = open_dbus_connection(bus="SESSION")
    except Exception as e:
        sys.stderr.write(
            f"qdistro-browser-bridge: SESSION bus unavailable: {e}\n")
        return
    try:
        # Flags: 0x04 = DO_NOT_QUEUE; without it the bridge silently
        # queues behind a same-uid impostor (M2 review finding).
        reply = conn.send_and_get_reply(
            message_bus.RequestName(bus_name, 4))
        # 1 = PRIMARY_OWNER, the only acceptable outcome. Anything
        # else (EXISTS / ALREADY_OWNER / IN_QUEUE) is fatal — a
        # same-uid attacker may already own the name and would
        # otherwise harvest the intent-token chain.
        request_name_result = (reply.body[0]
                               if reply.body else 0)
        sys.stderr.write(
            f"qdistro-browser-bridge: requested name {bus_name} "
            f"result={request_name_result}\n")
        if request_name_result != 1:
            sys.stderr.write(
                f"qdistro-browser-bridge: refusing to serve — "
                f"RequestName({bus_name}) returned "
                f"{request_name_result} (1=PRIMARY_OWNER required)\n")
            return
        while not stop_event.is_set():
            # jeepney blocking receive with a short timeout so we can
            # check stop_event regularly.
            try:
                msg = conn.receive(timeout=1.0)
            except Exception:
                continue
            if msg is None:
                continue
            if msg.header.message_type != MessageType.method_call:
                continue
            member = msg.header.fields.get(3)  # MEMBER
            if member != "RequestTabs":
                conn.send(new_error(
                    msg, "org.freedesktop.DBus.Error.UnknownMethod",
                    "s", ("unknown method",)))
                continue
            try:
                op, args_json = msg.body
                args = json.loads(args_json) if args_json else {}
            except Exception as e:
                conn.send(new_error(
                    msg, "org.freedesktop.DBus.Error.InvalidArgs",
                    "s", (str(e),)))
                continue
            reply_body = enqueue_inbound_request(
                op, args, out_stream, timeout_s=request_timeout_s)
            conn.send(new_method_return(
                msg, "s", (json.dumps(reply_body),)))
    finally:
        try:
            conn.close()
        except Exception:
            pass


DEFAULT_HANDLERS: dict[str, Callable[[dict, dict], dict]] = {
    "qdistro.ping": _handle_ping,
    "recall.push": _handle_recall_push,
    # 9a
    "pwd.fill": _handle_pwd_fill,
    "pwd.fill_confirm": _handle_pwd_fill_confirm,
    "pwd.save": _handle_pwd_save,
    # 9b — extension-initiated *.reply landings
    "tabs.list.reply": _handle_tabs_reply,
    "tabs.open.reply": _handle_tabs_reply,
    "tabs.close.reply": _handle_tabs_reply,
    "page.extract.reply": _handle_tabs_reply,
    "page.extract.request.reply": _handle_tabs_reply,
    "cookies.export.reply": _handle_tabs_reply,
    "mpris.publish.reply": _handle_tabs_reply,
    "downloads.notify.reply": _handle_tabs_reply,
    "notifications.show.reply": _handle_tabs_reply,
    "screenlock.inhibit.reply": _handle_tabs_reply,
    "screenlock.release.reply": _handle_tabs_reply,
    # Firefox containers (contextual identities). Bridge → ext only;
    # see qdistro/doc/firefox-containers.md. The qdfirefox-extension
    # answers; Chromium replies contextualIdentities_unavailable.
    "containers.list.reply": _handle_tabs_reply,
    "containers.create.reply": _handle_tabs_reply,
    "containers.remove.reply": _handle_tabs_reply,
    "qdistro.heartbeat.ack": _handle_heartbeat_ack,
    # 9c
    "page.extract": _handle_page_extract,
    # 9d
    "qdistro.handshake": _handle_handshake,
    "cookies.export": _handle_cookies_export,
    # 9e
    "mpris.publish": _handle_mpris_publish,
    "downloads.notify": _handle_downloads_notify,
    "notifications.show": _handle_notifications_show,
    "screenlock.inhibit": _handle_screenlock_inhibit,
    "screenlock.release": _handle_screenlock_release,
}


def dispatch(
        msg: dict,
        identity: dict,
        handlers: dict[str, Callable[[dict, dict], dict]] | None = None,
) -> dict:
    """Route `msg` through the dispatch table.

    Always returns a JSON-able dict. Identity-deny is enforced
    here, NOT inside individual handlers, so future ops don't have
    to redo the gate.
    """
    if not identity.get("allowed"):
        return {
            "ok": False,
            "error": "parent_not_allowed",
            "parent_exe": identity.get("parent_exe", ""),
        }
    op = str(msg.get("op", "")).strip()
    if not op:
        return {"ok": False, "error": "missing_op"}
    h = (handlers or DEFAULT_HANDLERS).get(op)
    if h is None:
        return {"ok": False, "error": "unknown_op", "op": op}
    try:
        body = h(msg, identity)
    except Exception as e:
        return {"ok": False, "error": "handler_raised",
                "op": op, "detail": str(e)[:200]}
    # Central identity-strip — handlers don't have to remember (L4
    # review). Any new field a future op adds is automatically
    # guarded. Handlers that deliberately reflect identity back to
    # the extension (currently only ``qdistro.ping`` and
    # ``qdistro.handshake``) annotate themselves on
    # :data:`_IDENTITY_REFLECTING_OPS` and run a re-stamp pass
    # after the strip.
    _strip_identity(body)
    if op in _IDENTITY_REFLECTING_OPS:
        body["extension_id"] = str(identity.get("extension_id") or "")
        if op == "qdistro.ping":
            body["ppid"] = identity.get("ppid")
            body["parent_exe"] = identity.get("parent_exe", "")
            body["parent_selinux"] = identity.get("parent_selinux", "")
    body.setdefault("ok", True)
    body["op"] = op
    return body


# ---- main loop ---------------------------------------------------

def main(stdin=None, stdout=None,
         enable_heartbeat: bool | None = None,
         enable_dbus: bool | None = None) -> int:
    """Native-messaging host main loop.

    Reads one message at a time, dispatches, writes the response,
    repeats until stdin closes. Returns process exit code.

    Phase-9 additions:

    * Rotates the per-session HMAC secret on entry (intent tokens).
    * Optionally starts the heartbeat thread (Chromium MV3 keep-alive)
      — only after the extension performs ``qdistro.handshake`` so
      that bare ``qdistro.ping`` clients (Phase-8 tests, manual
      smoke-checks) keep their single-frame reply shape.
    * Optionally registers the inbound D-Bus surface
      ``org.qdistro.BrowserBridge.<ppid>`` — disabled by default;
      enable in production installs by setting
      ``QDISTRO_BRIDGE_ENABLE_DBUS=1``.

    Heartbeat starts on the first ``qdistro.handshake`` op, NOT
    unconditionally on startup. This keeps the Phase-8 single-message
    test shape working — a bridge that only sees ``qdistro.ping`` and
    then EOF emits exactly one frame in reply.
    """
    if stdin is None:
        stdin = getattr(sys.stdin, "buffer", sys.stdin)
    if stdout is None:
        stdout = getattr(sys.stdout, "buffer", sys.stdout)
    identity = verify_parent()
    reset_session_secret()
    stop_event = threading.Event()
    heartbeat_started = False

    def _start_heartbeat() -> None:
        nonlocal heartbeat_started
        if heartbeat_started:
            return
        if not identity.get("allowed", False):
            return
        if enable_heartbeat is False:
            return
        if os.environ.get(
                "QDISTRO_BRIDGE_DISABLE_HEARTBEAT", "") == "1":
            return
        threading.Thread(
            target=heartbeat_loop,
            args=(stdout, stop_event),
            name="qdistro-bridge-heartbeat",
            daemon=True).start()
        heartbeat_started = True

    if enable_heartbeat is True:
        _start_heartbeat()
    want_dbus = (enable_dbus is True
                 or (enable_dbus is None
                     and identity.get("allowed", False)
                     and os.environ.get(
                         "QDISTRO_BRIDGE_ENABLE_DBUS", "") == "1"))
    if want_dbus:
        threading.Thread(
            target=inbound_dbus_serve,
            args=(int(identity.get("ppid") or 0), stdout, stop_event),
            name="qdistro-bridge-dbus",
            daemon=True).start()
    try:
        while True:
            try:
                msg = read_message(stdin)
            except ValueError as e:
                write_message(stdout,
                              {"ok": False, "error": "frame_error",
                               "detail": str(e)})
                return EXIT_FRAME_ERROR
            if msg is None:
                return EXIT_OK
            # Inbound-reply correlation: if the extension is answering
            # a daemon-initiated request, deliver_reply unblocks the
            # waiter and we skip the normal dispatch path so we don't
            # echo a duplicate frame back at the extension.
            if deliver_reply(msg):
                continue
            resp = dispatch(msg, identity)
            write_message(stdout, resp)
            # Start heartbeat after a successful handshake. Extensions
            # that never handshake (Phase-8 ping-only clients) get the
            # single-frame reply shape they were built against.
            if (resp.get("ok")
                    and msg.get("op") == "qdistro.handshake"):
                _start_heartbeat()
    finally:
        stop_event.set()


if __name__ == "__main__":
    sys.exit(main())
