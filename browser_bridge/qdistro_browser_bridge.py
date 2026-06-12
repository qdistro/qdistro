"""qdistro-browser-bridge — native-messaging host (Phase-8 + Phase-9).

Per doc/browser.md §"Phase-8 MVP scope" + todo/browser/01-bridge-phase9.md.

Phase-8 closed the architectural seam: the browser launches us as a
native-messaging host, we read 4-byte length-prefixed JSON on stdin, we
verify our parent is an allowlisted RPM browser binary, and we
round-trip ``qdistro.ping``. ``recall.push`` is deliberately absent from
the v1 dispatch table because Recall capture is cut from v1.

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

  RequestTabs is authenticated and authorized before anything is
  forwarded to the extension (fail closed):

  * The D-Bus sender's uid/pid are resolved via
    ``GetConnectionUnixUser`` / ``GetConnectionUnixProcessID`` and the
    caller must be same-uid AND be one of the *specific legitimate
    qdistro daemons* that drive this surface — authorized by its
    resolved executable identity (the python interpreter behind
    ``/proc/<pid>/exe`` plus the daemon SCRIPT path it is running),
    anchored against PID reuse by the process starttime
    (:func:`_inbound_caller_authorized`). Same-uid is necessary but NOT
    sufficient: an arbitrary same-session process (which IS the desktop
    user) is denied.
  * ``op`` is checked against :data:`INBOUND_OP_ALLOWLIST`
    (:func:`_inbound_op_allowed`). Page-extraction-class ops
    (:data:`INBOUND_EXTRACT_OPS`) are denied by default and only
    reachable with ``QDISTRO_BRIDGE_INBOUND_ALLOW_EXTRACT=1``.

  Both decisions are audited to stderr/journal.

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
import stat
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

# Shared /proc identity readers (anti-PID-reuse starttime anchor, exe
# resolution). Lives under broker/; add it to sys.path so the inbound
# RequestTabs gate can attest the *real* caller (a specific qdistro
# daemon) rather than trusting any same-uid session-bus process.
_BROKER_DIR = Path(__file__).resolve().parent.parent / "broker"
if str(_BROKER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROKER_DIR))
try:
    import qdistro_proc_identity as _pi  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 — fail closed if readers are unavailable
    _pi = None  # type: ignore[assignment]

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
    don't trip an early-exit), and on any frame whose top-level
    JSON value is not an object (the wire contract is dict-shaped;
    a bare scalar/array/null would otherwise raise AttributeError
    deeper in deliver_reply/dispatch and crash the stdio loop).
    json.loads's own JSONDecodeError is a ValueError subclass, so
    malformed JSON surfaces on the same fail-closed path.
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
    msg = json.loads(body.decode("utf-8"))
    if not isinstance(msg, dict):
        raise ValueError(
            f"top-level JSON value is not an object: {type(msg).__name__}")
    return msg


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
# heartbeat ack).
INTENT_TOKEN_REQUIRED_OPS: frozenset[str] = frozenset({
    "pwd.fill", "pwd.fill_confirm", "pwd.save", "cookies.export",
    "page.extract", "clipboard.set",
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
            # No handshake on record for this attested extension. The
            # master secret is NOT a production fallback here: validating
            # a master-HMAC token without a handshake would be a
            # master/shared-secret bypass of the per-extension binding
            # (a token minted against the raw master would serve any
            # sensitive op). Production callers MUST handshake first, so
            # we reject. The master fallback survives ONLY as a
            # test-path convenience, gated behind QDISTRO_TEST_MODE=1
            # (cf. the parent-exe allowlist _TEST override, P0-2).
            if os.environ.get("QDISTRO_TEST_MODE", "").strip() == "1":
                secret = _SESSION_SECRET
            else:
                return False, "intent_token_no_handshake"
    else:
        # extension_id=None is the legacy direct-call path (no attested
        # identity, e.g. unit tests calling verify_intent_token directly);
        # it uses the master and is never reached from production dispatch,
        # which always passes the kernel-attested extension_id.
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


# =====================================================================
# Outbound reply allowlist (response schema, per op)
# =====================================================================
#
# Security model: the daemons the bridge talks to are MORE trusted than
# the browser extension. A denylist (``_strip_identity``) fails OPEN —
# any new/unexpected field a daemon starts returning is forwarded to the
# extension unless someone remembers to add it to the strip list. We
# instead forward replies through a per-op ALLOWLIST and copy ONLY the
# fields named here; anything else is dropped (fail CLOSED). Per
# todo/security-hardening-carryforward.md §"Clipboard".
#
# The fields below are derived from the actual handlers / daemon reply
# shapes:
#   * qdistro.ping       — _handle_ping(); identity fields re-stamped by
#                          dispatch (pong/echo are the handler's own).
#   * qdistro.handshake  — _handle_handshake() return dict.
#   * pwd.fill           — qdistro-pwd Fill: credentials + fill_token.
#   * pwd.fill_confirm   — qdistro-pwd FillConfirm: credentials.
#   * pwd.save           — qdistro-pwd Save: envelope only.
#   * page.extract       — qbus-admin PageExtract: envelope only.
#   * cookies.export     — qdistro-pwd ExportCookies: exported count.
#   * qdistro.heartbeat.ack — _handle_heartbeat_ack(): matched.
#   * *.reply landings   — _handle_tabs_reply(): delivered.
#   * 9e desktop ops     — daemon/stub: stub flag.
#
# The envelope keys (ok/op/error/detail/request_id) are ALWAYS allowed
# (see _ENVELOPE_FIELDS); they carry status and error reporting and are
# never daemon-identity material. The identity fields that the two
# identity-reflecting ops surface (extension_id/ppid/parent_exe/
# parent_selinux) are re-stamped by dispatch AFTER projection from the
# bridge's kernel-attested view, so they don't need to be allowlisted
# from the handler body here.

# Status / error envelope. Always forwarded for every op.
_ENVELOPE_FIELDS: frozenset[str] = frozenset({
    "ok", "op", "error", "detail", "request_id",
})

# Per-op success-reply field allowlist (excluding the envelope). An op
# absent from this table is "no registered schema" → forward envelope
# only (fail closed), NOT the raw daemon reply.
_REPLY_SCHEMA: dict[str, frozenset[str]] = {
    "qdistro.ping": frozenset({"pong", "echo"}),
    "qdistro.handshake": frozenset({
        "session_secret_hex", "token_ttl_s", "hmac_algo",
        "token_canonical",
    }),
    "pwd.fill": frozenset({"credentials", "fill_token"}),
    "pwd.fill_confirm": frozenset({"credentials"}),
    "pwd.save": frozenset(),
    "page.extract": frozenset(),
    "cookies.export": frozenset({"exported"}),
    "qdistro.heartbeat.ack": frozenset({"matched"}),
    "clipboard.set": frozenset({"stub"}),
    # 9b *.reply landings — the bridge only acks delivery to the ext.
    "tabs.list.reply": frozenset({"delivered"}),
    "tabs.open.reply": frozenset({"delivered"}),
    "tabs.close.reply": frozenset({"delivered"}),
    "page.extract.reply": frozenset({"delivered"}),
    "page.extract.request.reply": frozenset({"delivered"}),
    "cookies.export.reply": frozenset({"delivered"}),
    "mpris.publish.reply": frozenset({"delivered"}),
    "downloads.notify.reply": frozenset({"delivered"}),
    "notifications.show.reply": frozenset({"delivered"}),
    "screenlock.inhibit.reply": frozenset({"delivered"}),
    "screenlock.release.reply": frozenset({"delivered"}),
    "containers.list.reply": frozenset({"delivered"}),
    "containers.create.reply": frozenset({"delivered"}),
    "containers.remove.reply": frozenset({"delivered"}),
    # 9e desktop integrations — daemons not yet shipped; stub flag.
    "mpris.publish": frozenset({"stub"}),
    "downloads.notify": frozenset({"stub"}),
    "notifications.show": frozenset({"stub"}),
    "screenlock.inhibit": frozenset({"stub"}),
    "screenlock.release": frozenset({"stub"}),
}


def _project_reply(op: str, reply: dict) -> dict:
    """Build the outbound reply by ALLOWLIST: keep only the envelope
    keys plus the per-op schema fields registered in
    :data:`_REPLY_SCHEMA`. Everything else a (more-trusted) daemon
    returned is DROPPED before it reaches the (less-trusted) extension.

    Fail-closed: an op with no registered schema forwards ONLY the
    envelope (ok/op/error/detail/request_id) — never the raw daemon
    reply body.

    This is the primary outbound filter; :func:`_strip_identity` is
    retained as a defense-in-depth second pass at the call site.
    """
    allowed = _ENVELOPE_FIELDS | _REPLY_SCHEMA.get(op, frozenset())
    return {k: v for k, v in reply.items() if k in allowed}


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
    fill_token = msg.get("fill_token")
    if not isinstance(fill_token, str) or not fill_token:
        return {"ok": False, "error": "missing_fill_token"}
    body = json.dumps({
        "url": url, "username": username,
        "fill_token": fill_token,
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
    body_obj = {
        "domain": domain,
        "cookies": cookies,
        "extension_id": identity.get("extension_id") or "",
        "parent_exe": identity.get("parent_exe") or "",
    }
    cookie_store_id = msg.get("cookie_store_id")
    if isinstance(cookie_store_id, str) and cookie_store_id:
        body_obj["cookie_store_id"] = cookie_store_id
    body = json.dumps(body_obj)
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


def _string_list(value: Any, *, item_limit: int, char_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        s = item.strip()[:char_limit]
        if s and s not in out:
            out.append(s)
        if len(out) >= item_limit:
            break
    return out


def _element_context(value: Any) -> dict[str, str | bool]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "target", "selection_anchor", "active",
        "is_password_field", "is_contenteditable", "is_code",
    }
    out: dict[str, str | bool] = {}
    for key in allowed:
        v = value.get(key)
        if isinstance(v, bool):
            out[key] = v
        elif isinstance(v, str):
            out[key] = v[:80]
    return out


def _handle_clipboard_set(msg: dict, identity: dict) -> dict:
    """clipboard.set — forward extension-captured copy metadata.

    This is intentionally metadata-only. Clipboard bytes stay in the
    browser/compositor clipboard path; enforcement and VM validation are
    compositor-side follow-ups.
    """
    gate = _identity_gate(identity)
    if gate is not None:
        return gate
    source_url = msg.get("source_url")
    if not isinstance(source_url, str) or not source_url:
        return {"ok": False, "error": "missing_source_url"}
    ok, err = verify_intent_token(
        msg.get("intent_token"), "clipboard.set",
        extension_id=identity.get("extension_id"))
    if not ok:
        return {"ok": False, "error": err}
    selected_len = msg.get("selected_text_length")
    if isinstance(selected_len, bool) or not isinstance(selected_len, int):
        selected_len = 0
    body = json.dumps({
        "operation": "cut" if msg.get("operation") == "cut" else "copy",
        "source_url": source_url[:4096],
        "title": str(msg.get("title") or "")[:512],
        "tab_id": msg.get("tab_id"),
        "mime_types": _string_list(
            msg.get("mime_types"), item_limit=32, char_limit=120),
        "content_tags": _string_list(
            msg.get("content_tags"), item_limit=32, char_limit=80),
        "sensitive_fields_nearby": bool(
            msg.get("sensitive_fields_nearby")),
        "element_context": _element_context(msg.get("element_context")),
        "selected_text_length": max(0, selected_len),
        "extension_id": identity.get("extension_id") or "",
        "parent_exe": identity.get("parent_exe") or "",
    })
    reply = _get_dbus_client().call(
        _COMPOSITOR_BUS_KIND, _COMPOSITOR_BUS, _COMPOSITOR_PATH,
        _COMPOSITOR_IFACE, "ClipboardSet", "s", (body,))
    return dict(reply)


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

# Inbound op allowlist. The RequestTabs surface forwards *only* these
# ops to the extension; anything else is denied before
# enqueue_inbound_request runs (fail closed).
#
# This set is derived from the extension's REAL inbound dispatcher —
# ``handleInboundRequest``'s ``switch (op)`` in
# ``browser_bridge/extension/background.js``. Only ops the extension
# actually handles on the inbound path belong here. In particular:
#
#   * ``mpris.control`` IS a real inbound op (admin media widget ->
#     originating tab; sent by qdistro_mpris_daemon via RequestTabs and
#     handled by ``handleMprisControlInbound``). Omitting it broke
#     legitimate media Play/Pause/Next — it MUST be allowlisted.
#   * Outbound-only ops the extension *initiates* (``mpris.publish``,
#     ``downloads.notify``, ``screenlock.inhibit``/``release``,
#     ``cookies.export``) are NOT handled by the inbound switch, so they
#     must NOT appear in this inbound allowlist.
#
# Page-extraction-class ops are NOT in the default set — they read
# arbitrary page content and are only reachable when explicitly enabled
# via QDISTRO_BRIDGE_INBOUND_ALLOW_EXTRACT=1 (see INBOUND_EXTRACT_OPS).
INBOUND_OP_ALLOWLIST: frozenset[str] = frozenset({
    "tabs.list",
    "tabs.open",
    "tabs.close",
    "containers.list",
    "containers.create",
    "containers.remove",
    "notifications.show",
    "mpris.control",
})

# Page-extraction-class op: high-risk (exfiltrates arbitrary page
# content) and therefore opt-in only. ``page.extract.request`` is the
# only extraction op the extension's inbound switch handles;
# ``cookies.export`` is deliberately NOT inbound-dispatchable (it must be
# extension-initiated with intent-token validation), so it is neither
# allowlisted nor in this extract set.
INBOUND_EXTRACT_OPS: frozenset[str] = frozenset({
    "page.extract.request",
})


# ---- trusted inbound CALLER identity (finding #7) --------------------
#
# The RequestTabs surface is a per-session bus name reachable by *any*
# same-uid process — and the attacker class here IS the desktop user, so
# a same-uid gate alone authorizes the whole attacker class. We therefore
# authorize only the SPECIFIC legitimate qdistro daemons that drive this
# surface, by their resolved executable identity (anti-PID-reuse anchored
# on starttime). Same-uid remains a necessary precondition but is no
# longer sufficient.
#
# In-tree callers of ``RequestTabs(op, args_json)`` (grep: "RequestTabs"):
#   * browser_daemons/qdistro_mpris_daemon.py  (mpris.control -> tab)
#   * user_relay/qdistro_user_relay.py         (broker -> bridge relay)
# Both are launched by systemd as ``/usr/bin/python3 <script.py>`` (see
# their .service ExecStart), so ``/proc/<pid>/exe`` is the python
# interpreter and the DAEMON identity lives in the script PATH.
#
# We authorize on the FULL RESOLVED SCRIPT PATH (argv[1]) — required to be
# one of the canonical, root-owned, non-writable daemon scripts below — NOT
# on the basename. Pinning to the basename alone was forgeable: a same-uid
# attacker could run ``/usr/bin/python3 /tmp/qdistro_mpris_daemon.py`` and
# the basename matched (finding #7, second review). Requiring the realpath
# to be a root-owned file at a canonical install location defeats that: the
# attacker cannot place a root-owned file under /usr, and a /tmp/... or
# ~/... script fails both the path allowlist and the root-owned stat.
#
# (Residual: argv is process-writable, so a process could in principle run
# attacker code yet present argv[1]=<the real root-owned script>. That is
# the same residual as the qdwin locker entrypoint check; closing it fully
# needs a non-forgeable launch credential — systemd cgroup unit match or a
# broker registration token — tracked as future hardening. The realpath +
# root-owned gate removes the trivial filename-spoof attack the review
# demonstrated.)
#
# Canonical install paths (see the daemons' .service ExecStart and
# scripts/install/*): mpris -> /usr/libexec/qdistro, user-relay ->
# /usr/local/lib/qdistro. Plausible alternate prefixes are included; every
# entry is still gated by the root-owned/non-writable stat, so listing a
# path that an unprivileged user could write would not weaken the check.
# ``QDISTRO_BRIDGE_INBOUND_TRUSTED_SCRIPTS`` may NARROW this set (replace it
# with a comma-separated allowlist of absolute paths); it can never widen
# to an arbitrary script.
TRUSTED_INBOUND_SCRIPTS: frozenset[str] = frozenset({
    "/usr/libexec/qdistro/qdistro_mpris_daemon.py",
    "/usr/lib/qdistro/qdistro_mpris_daemon.py",
    "/usr/local/lib/qdistro/qdistro_mpris_daemon.py",
    "/usr/local/libexec/qdistro/qdistro_mpris_daemon.py",
    "/usr/local/lib/qdistro/qdistro_user_relay.py",
    "/usr/lib/qdistro/qdistro_user_relay.py",
    "/usr/libexec/qdistro/qdistro_user_relay.py",
})

# Basenames accepted as the python interpreter behind a trusted caller's
# ``/proc/<pid>/exe``. Anchors the "this is a packaged python daemon"
# half of the identity so a random ELF named like a script can't pass.
_TRUSTED_INTERP_BASENAMES: tuple[str, ...] = (
    "python3", "python", "python3.10", "python3.11", "python3.12",
    "python3.13",
)


def _trusted_inbound_scripts() -> frozenset[str]:
    """Resolve the trusted-caller script-PATH allowlist.

    Defaults to :data:`TRUSTED_INBOUND_SCRIPTS` (canonical absolute paths).
    The env var ``QDISTRO_BRIDGE_INBOUND_TRUSTED_SCRIPTS`` (comma-separated
    absolute paths) REPLACES the default only with a NARROWER set: any path
    it lists that is not already a built-in trusted path is ignored, so the
    override can never widen the allowlist to an arbitrary script.
    """
    raw = os.environ.get("QDISTRO_BRIDGE_INBOUND_TRUSTED_SCRIPTS", "")
    requested = {tok.strip() for tok in raw.split(",") if tok.strip()}
    if not requested:
        return TRUSTED_INBOUND_SCRIPTS
    return frozenset(requested & TRUSTED_INBOUND_SCRIPTS)


def _is_trusted_root_file(path: str, allowed: frozenset) -> bool:
    """True iff ``path`` realpaths to an absolute path on ``allowed`` AND the
    resolved file is a regular file owned by root and not group/other
    writable. Fail closed on any error. Shared "canonical, system-managed
    file" predicate (see the TRUSTED_INBOUND_SCRIPTS rationale).

    The input must already be ABSOLUTE — a relative argv[1] would otherwise
    be realpath()'d against the *daemon's* cwd, which is not the attacker's
    file but is also not a meaningful identity; reject it outright."""
    if not path or not os.path.isabs(path):
        return False
    try:
        real = os.path.realpath(path)
        if real not in allowed:
            return False
        st = os.stat(real)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_uid != 0:
        return False
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    return True


# Trusted system bindirs the python interpreter behind a daemon must live
# under. /proc/<pid>/exe is kernel-set (non-forgeable), so requiring the exe
# to be a root-owned, non-writable interpreter at a canonical bindir removes
# the weaker "basename python3 from anywhere" signal.
_TRUSTED_INTERP_BINDIRS: tuple[str, ...] = (
    "/usr/bin/", "/bin/", "/usr/local/bin/",
)


def _is_trusted_interpreter(exe: str) -> bool:
    """True iff ``exe`` (a resolved /proc/<pid>/exe) is a root-owned,
    non-writable system python interpreter at a canonical bindir. Fail
    closed on any error."""
    if not exe or not os.path.isabs(exe):
        return False
    base = os.path.basename(exe)
    if base not in _TRUSTED_INTERP_BASENAMES:
        return False
    if not any(exe.startswith(d) and "/" not in exe[len(d):]
               for d in _TRUSTED_INTERP_BINDIRS):
        return False
    try:
        st = os.stat(exe)
    except OSError:
        return False
    return (stat.S_ISREG(st.st_mode) and st.st_uid == 0
            and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def _proc_cmdline(pid: int) -> list[str]:
    """NUL-split ``/proc/<pid>/cmdline`` argv, or ``[]`` (fail closed)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [a.decode("utf-8", "replace")
            for a in raw.split(b"\x00") if a]


def _caller_script_path(argv: list[str]) -> str:
    """Best-effort: the python SCRIPT PATH (argv[1]) a python daemon runs.

    For ``python3 /usr/libexec/qdistro/qdistro_mpris_daemon.py [args...]``
    this is ``/usr/libexec/qdistro/qdistro_mpris_daemon.py`` — the FULL
    path, NOT the basename, so the caller can require it to resolve to a
    canonical root-owned file. Skips interpreter flags (``-X..``, ``-E``,
    ``-S``, ``-O`` ...) and ``-m module`` / ``-c prog`` are treated as no
    script (returns ``""`` -> denied; the daemons are launched by path).
    Returns ``""`` when no script argument is present.
    """
    if not argv:
        return ""
    args = argv[1:]  # skip argv[0] (the interpreter itself)
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "-m":
            return ""  # module launch: not the by-path daemon form
        if tok == "-c":
            return ""  # inline program: no script file
        if tok.startswith("-"):
            i += 1
            continue
        return tok
    return ""


def _attest_inbound_caller(pid: int) -> tuple[bool, str]:
    """Attest that ``pid`` is one of the trusted qdistro daemons.

    Anti-PID-reuse: requires a non-zero starttime (the kernel's recycle
    anchor). Requires ``/proc/<pid>/exe`` to resolve to a system python
    interpreter AND the running script's basename to be in the (possibly
    env-narrowed) trusted-script allowlist. Fail closed on any unreadable
    field. Returns ``(ok, detail)``; ``detail`` feeds the audit log.
    """
    if _pi is None:
        return False, "no-proc-identity"
    if pid <= 0:
        return False, "bad-pid"
    starttime = _pi.read_starttime(pid)
    if not starttime:  # 0 -> gone / unreadable: anti-PID-reuse fail-closed
        return False, "zero-starttime"
    exe = _pi.read_exe(pid)
    if not exe or exe == "?":
        return False, "exe-unreadable"
    # /proc/<pid>/exe is kernel-set and non-forgeable: require it to be a
    # root-owned, non-writable system python at a canonical bindir, not just
    # the right basename from anywhere.
    if not _is_trusted_interpreter(exe):
        return False, f"non-system-interp exe={exe!r}"
    interp = os.path.basename(exe).strip()
    script = _caller_script_path(_proc_cmdline(pid))
    if not script:
        return False, "no-script"
    # Require the FULL resolved script path to be a canonical, root-owned,
    # non-writable daemon script — not merely the right basename. This
    # rejects a same-uid `/usr/bin/python3 /tmp/qdistro_mpris_daemon.py`
    # (finding #7): /tmp/... is neither on the path allowlist nor root-owned.
    if not _is_trusted_root_file(script, _trusted_inbound_scripts()):
        return False, f"script-not-trusted script={script!r}"
    return True, f"script={script} interp={interp} starttime={starttime}"


def _inbound_op_allowed(op: str) -> tuple[bool, str]:
    """Authorize an inbound op against the allowlist. Returns
    (allowed, reason). Fail closed: unknown ops are denied."""
    op = str(op or "")
    if op in INBOUND_OP_ALLOWLIST:
        return True, "op-allowed"
    if op in INBOUND_EXTRACT_OPS:
        if os.environ.get(
                "QDISTRO_BRIDGE_INBOUND_ALLOW_EXTRACT", "") == "1":
            return True, "op-extract-enabled"
        return False, "op-extract-disabled"
    return False, "op-not-allowlisted"


def _inbound_caller_authorized(conn, sender: str) -> tuple[bool, str]:
    """Authenticate + authorize the D-Bus caller of an inbound RequestTabs.

    Resolves the sender's uid + pid via
    ``org.freedesktop.DBus.GetConnectionUnixUser`` /
    ``GetConnectionUnixProcessID`` and authorizes against a NARROW
    trusted-caller identity:

      * The caller MUST be the same uid as the bridge (cross-uid callers
        are always denied). This is *necessary but NOT sufficient* — the
        attacker class is the desktop user itself, so a same-uid gate
        alone would authorize every same-session process (finding #7).
      * AND the resolved pid must attest as one of the SPECIFIC
        legitimate qdistro daemons that drive this surface — a packaged
        python interpreter running a known daemon script
        (:func:`_attest_inbound_caller`), anchored against PID reuse by
        the process starttime. An arbitrary same-uid process (random exe
        / random script) is DENIED.

    The env-var narrowing
    (``QDISTRO_BRIDGE_INBOUND_TRUSTED_SCRIPTS``) can only *restrict* the
    daemon-script allowlist, never authorize an arbitrary process.

    Fail closed: a sender we cannot resolve (missing sender, bus error,
    unreadable ``/proc``) is denied. Returns ``(allowed, reason)``;
    ``reason`` includes the resolved uid/pid + attestation detail for the
    audit log.
    """
    if not sender:
        return False, "no-sender"
    try:
        from jeepney import new_method_call, MessageType, DBusAddress
    except ImportError:
        return False, "jeepney-missing"

    dbus_daemon = DBusAddress(
        "/org/freedesktop/DBus",
        bus_name="org.freedesktop.DBus",
        interface="org.freedesktop.DBus")

    def _dbus_call(member: str) -> int | None:
        try:
            call = new_method_call(dbus_daemon, member, "s", (sender,))
            reply = conn.send_and_get_reply(call)
            if reply.header.message_type == MessageType.error:
                return None
            return int(reply.body[0])
        except Exception:  # noqa: BLE001
            return None

    uid = _dbus_call("GetConnectionUnixUser")
    pid = _dbus_call("GetConnectionUnixProcessID")
    if uid is None:
        return False, "uid-unresolved"
    my_uid = os.getuid()
    if uid != my_uid:
        return False, f"cross-uid uid={uid} pid={pid}"
    if pid is None or pid <= 0:
        return False, f"pid-unresolved uid={uid}"
    ok, detail = _attest_inbound_caller(pid)
    if not ok:
        return False, f"caller-not-trusted uid={uid} pid={pid} {detail}"
    return True, f"caller-ok uid={uid} pid={pid} {detail}"


def _audit_inbound(member: str, op: str, decision: str,
                   reason: str) -> None:
    """Audit an inbound D-Bus decision to stderr (journal)."""
    sys.stderr.write(
        f"qdistro-browser-bridge: inbound {member} op={op!r} "
        f"decision={decision} reason={reason}\n")
    sys.stderr.flush()


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
        from jeepney import (MessageType, new_method_return, new_error)
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import open_dbus_connection
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
            # ---- caller authorization (fail closed) -----------------
            # Authenticate the D-Bus sender BEFORE forwarding anything to
            # the extension. A bare per-session bus name is reachable by
            # any same-session process; without this gate any of them
            # could drive privileged browser ops.
            sender = msg.header.fields.get(7)  # SENDER
            allowed, why = _inbound_caller_authorized(conn, sender)
            if not allowed:
                _audit_inbound("RequestTabs", op, "deny-caller", why)
                conn.send(new_error(
                    msg, "org.freedesktop.DBus.Error.AccessDenied",
                    "s", ("caller not authorized",)))
                continue
            # ---- op allowlist (fail closed) -------------------------
            op_ok, op_why = _inbound_op_allowed(op)
            if not op_ok:
                _audit_inbound("RequestTabs", op, "deny-op", op_why)
                conn.send(new_error(
                    msg, "org.freedesktop.DBus.Error.AccessDenied",
                    "s", (f"op not allowed: {op_why}",)))
                continue
            _audit_inbound("RequestTabs", op, "allow",
                           f"{why}; {op_why}")
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
    "clipboard.set": _handle_clipboard_set,
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
    # PRIMARY outbound filter: allowlist by response schema. We copy
    # ONLY the envelope keys + the per-op fields registered in
    # :data:`_REPLY_SCHEMA`; anything else a (more-trusted) daemon
    # returned is dropped before it reaches the (less-trusted)
    # extension. Fail-closed: an op with no registered schema forwards
    # the envelope only. This supersedes the old denylist
    # ``_strip_identity`` (which failed OPEN on unexpected fields).
    #
    # Projection applies to the production op surface (DEFAULT_HANDLERS,
    # i.e. ``handlers is None``). When a caller injects a custom
    # ``handlers`` table (test harnesses), there is no registered
    # schema for those ops, so we leave the body unfiltered and rely on
    # the identity-strip defense-in-depth pass below — projecting an
    # unknown injected op to the bare envelope would be a surprising
    # behavior change for that escape hatch.
    if handlers is None:
        body = _project_reply(op, body)
    # Defense-in-depth: even after the allowlist, run the legacy
    # identity-strip so the kernel-attested fields can never leak (e.g.
    # for the custom-handler escape hatch above, which skips projection).
    # Handlers that deliberately reflect identity back to the extension
    # (currently only ``qdistro.ping`` and ``qdistro.handshake``)
    # annotate themselves on :data:`_IDENTITY_REFLECTING_OPS` and run a
    # re-stamp pass below from the bridge's own view of the caller.
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
