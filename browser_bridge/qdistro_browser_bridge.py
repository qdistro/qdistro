"""qdistro-browser-bridge — native-messaging host (Phase-8 MVP skeleton).

Per doc/browser.md §"Phase-8 MVP scope". Closes the
architectural seam: the browser launches us as a native-messaging
host, we read 4-byte length-prefixed JSON on stdin, we verify our
parent is an allowlisted RPM browser binary, and we round-trip a
single ``qdistro.ping`` op to prove the channel works end-to-end.

Heavier ops (pwd.fill, page.extract, recall.push, MPRIS bridging,
intent tokens, persistent-port heartbeat, cross-uid republish) are
all called out in the spec as Phase-9 deferrals. This module is
deliberately the smallest surface that retires the architecture
risk: parent-chain identity check, length-prefix protocol, JSON
shape, and the dispatch table.

Identity-chain check is the load-bearing piece. Per spec/14
§"Supported-browser matrix" the bridge fails closed if its parent
binary isn't on the allowlist (the RPM Firefox / Chromium / Brave /
Vivaldi / Edge paths). Snap-Firefox / Flatpak-browsers route
through xdg-desktop-portal which strips the parent identity; those
hit the allowlist miss and bail.

The module is pure-python (no Qt, no DBus) so tests drive every
branch in-process by injecting fakes for stdin/stdout, getppid, the
parent-readers, and the dispatch table.
"""
from __future__ import annotations

import json
import os
import struct
import sys
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
    if not argv or len(argv) < 2:
        return ""
    exe = (parent_exe or "").lower()
    if "firefox" in exe:
        return argv[2].strip() if len(argv) >= 3 else ""
    chrome_origin = argv[1].strip()
    if chrome_origin.startswith("chrome-extension://"):
        rest = chrome_origin[len("chrome-extension://"):]
        return rest.rstrip("/").strip()
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


DEFAULT_HANDLERS: dict[str, Callable[[dict, dict], dict]] = {
    "qdistro.ping": _handle_ping,
    "recall.push": _handle_recall_push,
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
    body.setdefault("ok", True)
    body["op"] = op
    return body


# ---- main loop ---------------------------------------------------

def main(stdin=None, stdout=None) -> int:
    """Native-messaging host main loop.

    Reads one message at a time, dispatches, writes the response,
    repeats until stdin closes. Returns process exit code.
    """
    if stdin is None:
        stdin = getattr(sys.stdin, "buffer", sys.stdin)
    if stdout is None:
        stdout = getattr(sys.stdout, "buffer", sys.stdout)
    identity = verify_parent()
    while True:
        try:
            msg = read_message(stdin)
        except ValueError as e:
            write_message(stdout,
                          {"ok": False, "error": "frame_error",
                           "detail": str(e)})
            return 2
        if msg is None:
            return 0
        resp = dispatch(msg, identity)
        write_message(stdout, resp)


if __name__ == "__main__":
    sys.exit(main())
