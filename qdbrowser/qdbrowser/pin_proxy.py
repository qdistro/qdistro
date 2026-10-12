"""Success-path certificate pin check: a loopback CONNECT proxy.

``cert_policy``'s ``certificateError`` hook only sees chains the trust
store already refused (iso2 `13` E2): QtWebEngine has no signal carrying
the peer chain of a *successful* handshake. This module closes most of
that gap without a new Qt API.

How it works:

  - ``__main__`` starts :class:`PinProxy` on ``127.0.0.1:<ephemeral>``
    before ``QApplication`` exists, and hands Chromium a PAC script
    (``--proxy-pac-url=data:...``, :func:`compose_pac`) that routes
    ``https:``/``wss:`` requests for pinned hosts — and only those — to
    the proxy. There is no ``DIRECT`` fallback in that rule, so a dead
    proxy fails the load instead of bypassing the check.
  - For each ``CONNECT host:port`` the proxy resolves ``host`` once, then
    per address: runs its own TLS handshake to that address (system
    trust + hostname check), evaluates the *verified* chain with
    ``PinStore.evaluate``, closes the probe, and opens the tunnel to the
    **same address**. A mismatch, an unverifiable chain or a probe error
    refuses the tunnel (``502``) and journals
    ``qdbrowser.cert reject host=<h> reason=<r> path=probe``.
  - Chromium still does its own normal validation inside the tunnel.

Not an open relay: only ``CONNECT`` to a currently pinned, non-overridden
host is served, and only to a client socket owned by this uid
(``/proc/net/tcp*`` lookup; unknown owner = refused).

Matching uses the chain OpenSSL *verified*, never the presented one: the
presented chain is attacker-controlled and pinned certificates are
public, so matching on it could be satisfied by appending the pinned
intermediate to a forged chain.

KNOWN RESIDUALS (keep these when editing docs; see cert_policy):

  - The probe and Chromium's handshake are separate TLS sessions to the
    same address. An on-path attacker that can tell them apart (e.g. by
    ClientHello fingerprint) can pass the probe and intercept only
    Chromium's session. Interception that applies to every connection —
    a TLS-inspecting middlebox, a mis-issued certificate served by an
    on-path attacker — is caught.
  - The probe trusts OpenSSL's store (``SSL_CERT_FILE``/system bundle),
    not Chromium's NSS DB. A pinned host chaining to a CA present only in
    NSS fails the probe (fail closed), so its CA must be in both.
  - Pins and the PAC host list are read once at startup.
  - A user-supplied ``--proxy-*`` Chromium flag disables the proxy (a
    second proxy config would be ambiguous); ``__main__`` logs it at
    ERROR.
"""

from __future__ import annotations

import json
import logging
import os
import re
import selectors
import socket
import ssl
import threading
from collections.abc import Callable
from urllib.parse import quote

from .cert_policy import PinStore

log = logging.getLogger("qdbrowser.cert")

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
_MAX_HEAD = 8192
_HEAD_TIMEOUT_S = 10.0
_PROBE_TIMEOUT_S = 10.0
_MAX_CLIENTS = 64
# Chromium flags that already configure a proxy. With one present the PAC
# is not added (two configs would race) and the success path is off.
PROXY_FLAGS = ("--proxy-server", "--proxy-pac-url", "--no-proxy-server",
               "--proxy-auto-detect")


def proxied_hosts(store: PinStore) -> list:
    """Pinned, non-overridden hosts with a PAC-safe name."""
    out = []
    for host in store.pinned_hosts:
        if store.is_overridden(host):
            continue
        if not _HOST_RE.match(host):
            log.warning("qdbrowser.cert pin host %r not routable through "
                        "the pin proxy (invalid name)", host)
            continue
        out.append(host)
    return out


def compose_pac(hosts: list, port: int) -> str:
    """PAC script: ``https:``/``wss:`` for ``hosts`` go to the proxy,
    everything else ``DIRECT``. Host names are JSON-embedded (they were
    already restricted to ``[a-z0-9.-]``)."""
    return (
        "function FindProxyForURL(url, host) {\n"
        f"  var pinned = {json.dumps(sorted(hosts))};\n"
        "  var h = host.toLowerCase();\n"
        "  if (h.charAt(h.length - 1) == '.') h = h.substring(0, h.length - 1);\n"
        "  var s = url.substring(0, 6).toLowerCase();\n"
        "  if ((s == 'https:' || s.substring(0, 4) == 'wss:')\n"
        "      && pinned.indexOf(h) >= 0)\n"
        f"    return 'PROXY 127.0.0.1:{int(port)}';\n"
        "  return 'DIRECT';\n"
        "}\n")


def pac_flag(hosts: list, port: int) -> str:
    """The ``--proxy-pac-url`` Chromium flag carrying :func:`compose_pac`."""
    return ("--proxy-pac-url=data:application/x-ns-proxy-autoconfig,"
            + quote(compose_pac(hosts, port), safe=""))


def peer_uid(conn: socket.socket) -> int | None:
    """uid owning the client end of a loopback TCP connection, or None.

    The client's socket is the ``/proc/net/tcp*`` row whose local address
    is our peer address and whose remote address is our local address.
    """
    try:
        peer = conn.getpeername()
        local = conn.getsockname()
    except OSError:
        return None
    want_l = peer[1]
    want_r = local[1]
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="ascii") as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) < 8:
                        continue
                    lport = int(parts[1].rsplit(":", 1)[1], 16)
                    rport = int(parts[2].rsplit(":", 1)[1], 16)
                    if lport == want_l and rport == want_r:
                        return int(parts[7])
        except (OSError, ValueError, IndexError):
            continue
    return None


def _default_context() -> ssl.SSLContext:
    # Honors SSL_CERT_FILE / SSL_CERT_DIR, which __main__'s CA-bundle
    # step may have exported for the launch profile.
    return ssl.create_default_context()


class PinProxy:
    """Loopback CONNECT proxy that pin-checks each tunnel before opening it."""

    def __init__(self,
                 store_getter: Callable[[], PinStore | None],
                 *,
                 resolver: Callable = socket.getaddrinfo,
                 context_factory: Callable[[], ssl.SSLContext] = _default_context,
                 uid: int | None = None,
                 probe_timeout: float = _PROBE_TIMEOUT_S):
        self._store_getter = store_getter
        self._resolve = resolver
        self._context_factory = context_factory
        self._uid = os.getuid() if uid is None else uid
        self._probe_timeout = probe_timeout
        self._slots = threading.BoundedSemaphore(_MAX_CLIENTS)
        self._sock: socket.socket | None = None
        self._stopped = threading.Event()

    @property
    def port(self) -> int:
        assert self._sock is not None, "PinProxy not started"
        return self._sock.getsockname()[1]

    def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(32)
        self._sock = sock
        threading.Thread(target=self._accept_loop, name="qdbrowser-pin-proxy",
                         daemon=True).start()
        log.info("qdbrowser.cert pin proxy listening port=%d", self.port)
        return self.port

    def stop(self) -> None:
        self._stopped.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    # -- accept / dispatch ------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                if self._stopped.is_set():
                    return
                continue
            if not self._slots.acquire(blocking=False):
                _reply(conn, 503, "busy")
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            self._handle(conn)
        except Exception as exc:
            log.error("qdbrowser.cert pin proxy error: %s", exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._slots.release()

    def _handle(self, conn: socket.socket) -> None:
        owner = peer_uid(conn)
        if owner != self._uid:
            log.warning("qdbrowser.cert pin proxy refused client uid=%s",
                        owner)
            return
        conn.settimeout(_HEAD_TIMEOUT_S)
        head = _read_head(conn)
        if head is None:
            return
        target = _parse_connect(head)
        if target is None:
            _reply(conn, 405, "only CONNECT")
            return
        host, port = target
        store = self._store_getter()
        if store is None or not store.is_pinned(host) or store.is_overridden(host):
            # Not an open relay: the PAC only sends pinned hosts here.
            log.warning("qdbrowser.cert pin proxy refused unpinned host=%s",
                        host)
            _reply(conn, 403, "not a pinned host")
            return
        upstream = self._checked_upstream(store, host, port)
        if upstream is None:
            _reply(conn, 502, "certificate pin check failed")
            return
        try:
            conn.settimeout(None)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _splice(conn, upstream)
        finally:
            upstream.close()

    # -- the check ------------------------------------------------------------

    def _checked_upstream(self, store: PinStore, host: str,
                          port: int) -> socket.socket | None:
        """Probe each resolved address; return a plain socket to the first
        address whose verified chain matches a pin, else None."""
        try:
            addrs = self._resolve(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            log.warning("qdbrowser.cert reject host=%s reason=resolve_failed "
                        "path=probe detail=%s", host, exc)
            return None
        last = "no_address"
        for family, socktype, proto, _canon, sockaddr in addrs:
            verdict = self._probe(store, host, family, socktype, proto,
                                  sockaddr)
            if verdict is None:
                try:
                    up = socket.socket(family, socktype, proto)
                    up.settimeout(self._probe_timeout)
                    up.connect(sockaddr)
                    up.settimeout(None)
                except OSError as exc:
                    last = f"connect_failed:{exc}"
                    continue
                log.info("qdbrowser.cert pin ok host=%s addr=%s path=probe",
                         host, sockaddr[0])
                return up
            if verdict.startswith("connect_failed"):
                last = verdict
                continue
            # A reachable address presented a bad chain: refuse outright
            # rather than trying the next address.
            log.warning("qdbrowser.cert reject host=%s reason=%s path=probe "
                        "addr=%s", host, verdict, sockaddr[0])
            return None
        log.warning("qdbrowser.cert reject host=%s reason=%s path=probe",
                    host, last)
        return None

    def _probe(self, store: PinStore, host: str, family, socktype, proto,
               sockaddr) -> str | None:
        """None if the address's verified chain matches a pin, else a reason."""
        raw = socket.socket(family, socktype, proto)
        raw.settimeout(self._probe_timeout)
        try:
            try:
                raw.connect(sockaddr)
            except OSError as exc:
                return f"connect_failed:{exc}"
            ctx = self._context_factory()
            try:
                tls = ctx.wrap_socket(raw, server_hostname=host)
            except ssl.SSLCertVerificationError as exc:
                return f"probe_unverified:{exc.verify_message}"
            except (ssl.SSLError, OSError) as exc:
                return f"probe_tls_error:{exc}"
            try:
                # Python >= 3.13: a list of DER bytes.
                chain = [bytes(c) for c in tls.get_verified_chain()]
            finally:
                tls.close()
        finally:
            raw.close()
        decision = store.evaluate(host, chain)
        return None if decision.allow else decision.kind


def _read_head(conn: socket.socket) -> bytes | None:
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > _MAX_HEAD:
            return None
        try:
            chunk = conn.recv(4096)
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf.split(b"\r\n\r\n", 1)[0]


def _parse_connect(head: bytes) -> tuple[str, int] | None:
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = line.split()
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        return None
    hostport = parts[1]
    if hostport.startswith("["):
        return None  # IP literals are never pinned names
    host, sep, port_s = hostport.rpartition(":")
    if not sep or not port_s.isdigit():
        return None
    port = int(port_s)
    if not 0 < port < 65536:
        return None
    host = host.lower().rstrip(".")
    if not _HOST_RE.match(host):
        return None
    return host, port


def _reply(conn: socket.socket, code: int, reason: str) -> None:
    try:
        conn.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Length: 0\r\n"
                     "Connection: close\r\n\r\n".encode("ascii"))
    except OSError:
        pass


def _splice(a: socket.socket, b: socket.socket) -> None:
    """Copy bytes both ways until either side closes."""
    sel = selectors.DefaultSelector()
    sel.register(a, selectors.EVENT_READ, b)
    sel.register(b, selectors.EVENT_READ, a)
    try:
        while True:
            for key, _ in sel.select():
                src, dst = key.fileobj, key.data
                try:
                    data = src.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return
    finally:
        sel.close()
