"""qdistro-phone daemon — Phase-8 MVP skeleton.

Owns three responsibilities:

1. **Config**: reads ``/etc/qdistro/phone/*.conf`` (one file per
   paired phone — the qdistro-phone CLI writes these).
2. **HTTP callback listener**: small HTTP server that the phone's
   ntfy push-action button calls back to. The path is::

        POST /v1/decision/<request_id>/<allow|deny>?sig=...&exp=...

   Body is ignored. The handler verifies the HMAC + TTL via
   ``verify_callback_signature`` and writes the decision to a tiny
   sqlite queue at /var/lib/qdistro/phone/decisions.sqlite. The
   broker (or admin-app) polls / subscribes for new rows. (Phase-9
   replaces the SQLite queue with a D-Bus signal.)
3. **BLE presence scanner stub**: a placeholder that PresenceTracker
   subscribers can plug into. BlueZ wire-up is Phase-9; the daemon
   currently never observes anything (PresenceTracker.state ==
   'unknown') unless tests inject samples via the test API.

Refuses to start without ``QDISTRO_PHONE_NTFY_URL`` set — the user
explicitly chose "no default; admin must configure" for Phase-8
MVP.
"""
from __future__ import annotations

import argparse
import getpass
import http.server
import json
import os
import socket
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.parse

# Engine import — try installed path first, fall back to in-tree.
def _load_phone():
    try:
        import qdistro_phone as ph  # type: ignore
        return ph
    except ImportError:
        pass
    candidates = [
        "/usr/libexec/qdistro/qdistro_phone.py",
        os.path.join(os.path.dirname(__file__), "qdistro_phone.py"),
    ]
    import importlib.util
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(
                "qdistro_phone", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["qdistro_phone"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("qdistro_phone not found")


DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 7867
DEFAULT_CONFIG_DIR = "/etc/qdistro/phone"
DEFAULT_QUEUE_PATH = "/var/lib/qdistro/phone/decisions.sqlite"


# ---- config ------------------------------------------------------

def load_phone_configs(config_dir: str) -> dict[str, dict]:
    """Read every ``*.conf`` under ``config_dir``. Returns
    ``{phone_id: trust_dict}``. Bad files are silently skipped
    (the admin app surfaces parse errors).
    """
    ph = _load_phone()
    out: dict[str, dict] = {}
    if not os.path.isdir(config_dir):
        return out
    for name in sorted(os.listdir(config_dir)):
        if not name.endswith(".conf"):
            continue
        path = os.path.join(config_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                parsed = ph.parse_phone_trust_config(f.read())
        except OSError:
            continue
        for phone_id, body in parsed.items():
            out[phone_id] = body
    return out


def render_phone_conf(phone_id: str, trust: dict) -> str:
    """Render one ``[<phone_id>]`` config section to text."""
    if not phone_id:
        raise ValueError("phone_id required")
    lines = [f"[{phone_id}]"]
    for k, v in sorted(trust.items()):
        lines.append(f"{k} = {v}")
    return "\n".join(lines) + "\n"


def save_phone_conf(config_dir: str, phone_id: str,
                    trust: dict) -> str:
    """Atomic write of ``<config_dir>/<phone_id>.conf``. Returns
    the path written. Creates parent at 0o755; final file 0o600
    (the HMAC secret can live here in Phase-9).
    """
    if "/" in phone_id or phone_id.startswith("."):
        raise ValueError("phone_id must be a simple slug")
    os.makedirs(config_dir, mode=0o755, exist_ok=True)
    path = os.path.join(config_dir, f"{phone_id}.conf")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render_phone_conf(phone_id, trust))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


# ---- decisions queue --------------------------------------------

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_decisions (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    request_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    expires_at INTEGER,
    phone_id TEXT,
    UNIQUE(request_id, decision)
);
"""


def open_queue(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    # check_same_thread=False — the HTTP listener handles requests on
    # worker threads (ThreadingMixIn). A module-level lock around
    # writes keeps SQLite happy.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(QUEUE_SCHEMA)
    conn.commit()
    return conn


_QUEUE_LOCK = threading.Lock()


def record_decision(conn: sqlite3.Connection, *,
                    request_id: str, decision: str,
                    expires_at: int,
                    phone_id: str | None = None,
                    ts: float | None = None) -> int:
    """Insert a decision row; returns the new id, or -1 on dup.

    Threadsafe — the global ``_QUEUE_LOCK`` serialises writes so the
    HTTP listener's worker threads share one connection safely.
    """
    with _QUEUE_LOCK:
        try:
            cur = conn.execute(
                "INSERT INTO phone_decisions "
                "(ts, request_id, decision, expires_at, phone_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (float(ts if ts is not None else time.time()),
                 str(request_id), str(decision),
                 int(expires_at), phone_id))
            conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return -1


# ---- HTTP listener ----------------------------------------------

class _DecisionHandler(http.server.BaseHTTPRequestHandler):
    """Handle the phone's POST callbacks."""

    server_version = "qdistro-phone/0.1"

    def _bad(self, code: int, msg: str):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(
            {"ok": False, "error": msg}).encode())

    def do_POST(self):
        path = self.path or ""
        prefix = "/v1/decision/"
        if not path.startswith(prefix):
            self._bad(404, "not found")
            return
        rest = path[len(prefix):]
        head, _, tail = rest.partition("?")
        parts = head.split("/")
        if len(parts) != 2:
            self._bad(400, "bad path")
            return
        request_id, decision = parts
        params = urllib.parse.parse_qs(tail)
        sig = (params.get("sig") or [""])[0]
        try:
            exp = int((params.get("exp") or ["0"])[0])
        except ValueError:
            self._bad(400, "bad exp")
            return
        ph = _load_phone()
        ok = ph.verify_callback_signature(
            request_id=request_id, decision=decision,
            expires_at=exp, sig=sig,
            callback_secret=self.server.callback_secret)
        if not ok:
            self._bad(403, "signature or expiry")
            return
        rid = record_decision(
            self.server.queue_conn,
            request_id=request_id, decision=decision,
            expires_at=exp,
            phone_id=self.headers.get("X-Qdistro-Phone-Id"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(
            {"ok": True, "row_id": rid}).encode())

    def log_message(self, fmt, *args):  # silence the BaseHTTPServer noise
        pass


class DecisionServer(socketserver.ThreadingMixIn,
                     http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(*, host: str, port: int,
                 callback_secret: bytes,
                 queue_path: str) -> DecisionServer:
    """Build a DecisionServer with the secret + queue wired in."""
    if not callback_secret:
        raise ValueError("callback_secret required")
    s = DecisionServer((host, port), _DecisionHandler)
    s.callback_secret = callback_secret  # type: ignore[attr-defined]
    s.queue_conn = open_queue(queue_path)  # type: ignore[attr-defined]
    return s


# ---- main loop ---------------------------------------------------

def _resolve_secret(arg: str | None,
                    env_name: str = "QDISTRO_PHONE_SECRET",
                    file_env: str = "QDISTRO_PHONE_SECRET_FILE",
                    ) -> bytes:
    if arg:
        return arg.encode("utf-8")
    raw = os.environ.get(env_name, "").strip()
    if raw:
        return raw.encode("utf-8")
    path = os.environ.get(file_env, "").strip()
    if path:
        with open(path, "rb") as f:
            return f.read().strip()
    raise SystemExit(
        f"phone callback secret missing — set ${env_name} or "
        f"${file_env} or pass --secret")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qdistro-phone-daemon")
    p.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR)
    p.add_argument("--queue-path", default=DEFAULT_QUEUE_PATH)
    p.add_argument("--listen-host", default=DEFAULT_LISTEN_HOST)
    p.add_argument("--listen-port", type=int,
                   default=DEFAULT_LISTEN_PORT)
    p.add_argument("--ntfy-url", default=None,
                   help="ntfy server URL "
                        "(default $QDISTRO_PHONE_NTFY_URL; required)")
    p.add_argument("--secret", default=None,
                   help="HMAC callback secret "
                        "(default $QDISTRO_PHONE_SECRET or _FILE)")
    p.add_argument("--check-only", action="store_true",
                   help="resolve config + secret + URL then exit "
                        "(used by the systemd ExecStartPre).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    ntfy = (args.ntfy_url
            or os.environ.get("QDISTRO_PHONE_NTFY_URL", "").strip())
    if not ntfy:
        # Per the Phase-8 MVP scope decision: no default. Admin must
        # configure an explicit URL before the daemon starts.
        sys.stderr.write(
            "qdistro-phone: refusing to start without an ntfy URL.\n"
            "Set QDISTRO_PHONE_NTFY_URL or pass --ntfy-url.\n"
            "(Phase-8 MVP scope: per-deployment URL, no default.)\n")
        return 2
    secret = _resolve_secret(args.secret)
    configs = load_phone_configs(args.config_dir)
    print(f"[phone] starting on {args.listen_host}:{args.listen_port}",
          flush=True)
    print(f"[phone] ntfy={ntfy}", flush=True)
    print(f"[phone] paired phones: {sorted(configs.keys())}",
          flush=True)
    if args.check_only:
        return 0
    srv = build_server(
        host=args.listen_host, port=args.listen_port,
        callback_secret=secret, queue_path=args.queue_path)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
