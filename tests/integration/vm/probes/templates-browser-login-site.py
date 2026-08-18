#!/usr/bin/env python3
"""templates-browser-login-site.py — the test-local login site for the
fableplan2 task 06 browser-rollback demo (doc/06-integration-tests.md).

A single stdlib http.server (no third-party deps) that simulates a real site
the silo's user depends on: a navigation/form login flow that sets a
persistent auth cookie, plus a test-control endpoint that fakes a breakage
KEYED ON THE GENERATION MARKER carried in the browser's User-Agent
(`... qdistro-mk/<marker>`). Gen A keeps working while gen B "broke", so the
value of the snapshotted profile (its cookie) is testable.

Routes
  GET  /healthz            liveness sentinel (HEALTHZ-OK) — reachability probe
  GET  /login              a real POST form + a script that submits it after
                           DOMContentLoaded (cookies set on navigation are what
                           the profile persists; NOT a fetch()-only login)
  POST /auth               checks creds, issues a persistent auth cookie, 302s
                           to /home — UNLESS the request's marker is "broken"
  GET  /home               requires the cookie -> LOGIN-OK-<nonce>, else
                           LOGIN-FAILED-<reason>
  POST /__break            test control: set breakage mode + the broken marker
  POST /__slow_auth_status test control: report whether the selected marker's
                           /auth request entered the deliberate stall
  POST /__reset            clear breakage + forget issued sessions

Breakage modes (apply ONLY to a request whose UA carries the broken marker;
an old session's cookie at /home is honoured in EVERY mode — the breakage is
the login flow, not the session):
  reject-login   /auth refuses to issue a cookie -> fresh login lands on /home
                 with LOGIN-FAILED (the classic "site rejects the updated
                 browser")
  js-break       /login serves a script that throws before submitting -> the
                 page renders but the flow never navigates (only the DOM
                 sentinel, not a screenshot, catches this)
  slow-auth      /auth stalls past the check timeout -> the harness must detect
                 the timeout and leave no containers behind

Usage: templates-browser-login-site.py --port N [--bind ADDR]
The DOM sentinels are the contract; every assertion in the probe is
sentinel-based, never exit-code-only.
"""
from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import threading
import time
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

# Baked-in test credentials (a fixed, test-only secret — this is a fixture).
TEST_USER = "demo"
TEST_PASS = "s3cr3t-demo"
COOKIE_NAME = "qdistro_session"
REJECT_COOKIE = "qdistro_reject"   # set by /auth when it rejects an updated browser
MARKER_PREFIX = "qdistro-mk/"
SLOW_AUTH_STALL_S = 120  # longer than any check timeout -> the check times out

# Mutable, mutex-guarded server state shared across worker threads.
_LOCK = threading.Lock()
_ISSUED: set[str] = set()        # nonces this server has handed out
_BREAK_MODE: str | None = None   # None | reject-login | js-break | slow-auth
_BREAK_MARKER: str | None = None # the generation marker that is "broken"
_SLOW_AUTH_ENTERED: set[str] = set()  # markers whose /auth reached the stall


def _marker_from_ua(ua: str) -> str | None:
    """Extract <marker> from a `... qdistro-mk/<marker>` User-Agent suffix."""
    idx = ua.rfind(MARKER_PREFIX)
    if idx < 0:
        return None
    tail = ua[idx + len(MARKER_PREFIX):].strip()
    # the marker is a single token (a UUID in practice); stop at whitespace
    return tail.split()[0] if tail else None


def _is_broken(ua: str, mode: str) -> bool:
    with _LOCK:
        return _BREAK_MODE == mode and _BREAK_MARKER is not None \
            and _marker_from_ua(ua) == _BREAK_MARKER


LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>qdistro login</title><style>
html,body{{margin:0;padding:0;height:100%;background:#0b3d2e;color:#f5f5f5;
font-family:'DejaVu Sans',sans-serif;}}
.banner{{font-size:48px;font-weight:bold;padding:40px;color:#ffd166;}}
</style></head><body>
<div class="banner">QDISTRO-LOGIN-FORM</div>
<form id="loginform" method="POST" action="/auth">
  <input type="hidden" name="username" value="{user}">
  <input type="hidden" name="password" value="{password}">
  <button type="submit">sign in</button>
</form>
<div id="flow-status">LOGIN-PENDING</div>
<script>
{script}
</script>
</body></html>
"""

# Normal flow: submit the real form after the DOM is ready (a navigation, so
# the Set-Cookie on /auth's 302 is persisted by the profile).
SUBMIT_SCRIPT = """
document.addEventListener('DOMContentLoaded', function () {
  document.getElementById('flow-status').textContent = 'LOGIN-SUBMITTING';
  document.getElementById('loginform').submit();
});
"""

# js-break: throw before wiring the submit. The banner still renders (so a
# screenshot looks fine), but the flow never navigates -> no LOGIN-OK.
JSBREAK_SCRIPT = """
throw new Error('qdistro-jsbreak: updated browser tripped a site script bug');
document.addEventListener('DOMContentLoaded', function () {
  document.getElementById('loginform').submit();
});
"""


def _home_html(body_sentinel: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>qdistro home</title><style>"
        "html,body{margin:0;padding:0;height:100%;background:#1d4ed8;"
        "color:#fde047;font-family:'DejaVu Sans',sans-serif;}"
        ".banner{font-size:64px;font-weight:bold;padding:48px;}"
        "</style></head><body>"
        f"<div class=\"banner\">{body_sentinel}</div>"
        "</body></html>"
    ).encode()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "qdistro-login-site/1"

    # quieter logs (still to stderr) so the journal stays readable
    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("[login-site] " + (fmt % args) + "\n")

    def _ua(self) -> str:
        return self.headers.get("User-Agent", "")

    def _cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    def _send_html(self, body: bytes, status=HTTPStatus.OK, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---- GET -----------------------------------------------------------
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send_html(b"<!doctype html><html><body>"
                            b"<div id=\"h\">HEALTHZ-OK</div></body></html>")
            return
        if path == "/login":
            script = JSBREAK_SCRIPT if _is_broken(self._ua(), "js-break") \
                else SUBMIT_SCRIPT
            body = LOGIN_HTML.format(
                user=TEST_USER, password=TEST_PASS, script=script).encode()
            self._send_html(body)
            return
        if path == "/home":
            self._render_home()
            return
        self._send_html(b"<!doctype html><html><body>NOT-FOUND</body></html>",
                        status=HTTPStatus.NOT_FOUND)

    # ---- POST ----------------------------------------------------------
    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/auth":
            self._do_auth()
            return
        if path == "/__break":
            self._do_break()
            return
        if path == "/__slow_auth_status":
            self._do_slow_auth_status()
            return
        if path == "/__clearbreak":
            self._do_clearbreak()
            return
        if path == "/__reset":
            self._do_reset()
            return
        self._send_html(b"<!doctype html><html><body>NOT-FOUND</body></html>",
                        status=HTTPStatus.NOT_FOUND)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def _do_auth(self):
        form = parse_qs(self._read_body().decode("utf-8", "replace"))
        user = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]
        ua = self._ua()

        # slow-auth: stall past the harness timeout for the broken marker. A
        # good-marker request authenticates normally.
        if _is_broken(ua, "slow-auth"):
            marker = _marker_from_ua(ua)
            if marker is not None:
                with _LOCK:
                    _SLOW_AUTH_ENTERED.add(marker)
            time.sleep(SLOW_AUTH_STALL_S)

        creds_ok = (user == TEST_USER and password == TEST_PASS)
        # reject-login: the site refuses to issue a cookie to the updated
        # browser even though the credentials are correct.
        rejected = _is_broken(ua, "reject-login")

        if creds_ok and not rejected:
            nonce = secrets.token_hex(16)
            with _LOCK:
                _ISSUED.add(nonce)
            cookie = (f"{COOKIE_NAME}={nonce}; Path=/; HttpOnly; "
                      f"SameSite=Lax; Max-Age=86400")
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/home")
            self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # No session cookie issued. When the cause is a marker-keyed REJECTION
        # (not a generic failure), plant a distinct reject cookie so /home can
        # render a reject-specific sentinel — this proves /auth ran and refused
        # the updated browser, not that the flow merely never reached /auth.
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/home")
        if rejected:
            self.send_header("Set-Cookie",
                             f"{REJECT_COOKIE}=1; Path=/; SameSite=Lax; Max-Age=86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _render_home(self):
        nonce = self._cookie(COOKIE_NAME)
        with _LOCK:
            valid = bool(nonce) and nonce in _ISSUED
        if valid:
            self._send_html(_home_html(f"LOGIN-OK-{nonce}"))
        elif self._cookie(REJECT_COOKIE):
            # /auth rejected this updated browser's fresh login (reject-login).
            self._send_html(_home_html("LOGIN-FAILED-reject-login"))
        else:
            reason = "no-session" if not nonce else "unknown-session"
            self._send_html(_home_html(f"LOGIN-FAILED-{reason}"))

    def _do_break(self):
        self._read_body()
        q = parse_qs(urlsplit(self.path).query)
        mode = (q.get("mode") or [""])[0]
        marker = (q.get("marker") or [""])[0]
        if mode not in ("reject-login", "js-break", "slow-auth"):
            self._send_html(b"BREAK-BAD-MODE", status=HTTPStatus.BAD_REQUEST)
            return
        if not marker:
            self._send_html(b"BREAK-NO-MARKER", status=HTTPStatus.BAD_REQUEST)
            return
        global _BREAK_MODE, _BREAK_MARKER
        with _LOCK:
            _BREAK_MODE = mode
            _BREAK_MARKER = marker
            # Re-arming a marker starts a new observation window. This keeps a
            # reused fixture from satisfying readiness with an older request.
            _SLOW_AUTH_ENTERED.discard(marker)
        self._send_html(f"BREAK-SET-{mode}".encode())

    def _do_slow_auth_status(self):
        self._read_body()
        q = parse_qs(urlsplit(self.path).query)
        marker = (q.get("marker") or [""])[0]
        with _LOCK:
            entered = bool(marker) and marker in _SLOW_AUTH_ENTERED
        sentinel = "SLOW-AUTH-ENTERED" if entered else "SLOW-AUTH-WAITING"
        self._send_html(f"{sentinel}-{marker}".encode())

    def _do_clearbreak(self):
        # Clear ONLY the breakage; KEEP issued sessions so an already-issued
        # cookie (e.g. the A-era session the rollback scenario proves) stays
        # valid across scenarios.
        self._read_body()
        global _BREAK_MODE, _BREAK_MARKER
        with _LOCK:
            _BREAK_MODE = None
            _BREAK_MARKER = None
        self._send_html(b"CLEARBREAK-OK")

    def _do_reset(self):
        # Full reset: clears breakage AND forgets every issued session. Only for
        # initial setup / teardown — NEVER between scenarios that rely on a
        # previously-issued cookie still being honoured.
        self._read_body()
        global _BREAK_MODE, _BREAK_MARKER
        with _LOCK:
            _BREAK_MODE = None
            _BREAK_MARKER = None
            _ISSUED.clear()
            _SLOW_AUTH_ENTERED.clear()
        self._send_html(b"RESET-OK")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="templates-browser-login-site")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args(argv)
    httpd = http.server.ThreadingHTTPServer(
        (args.bind, args.port), Handler)
    sys.stderr.write(f"[login-site] listening on {args.bind}:{args.port}\n")
    sys.stderr.flush()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
