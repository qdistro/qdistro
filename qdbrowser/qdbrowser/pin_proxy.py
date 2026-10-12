"""Best-effort success-path certificate pin check: a loopback CONNECT proxy.

``cert_policy``'s ``certificateError`` hook only sees chains the trust
store already refused (iso2 `13` E2): QtWebEngine has no signal carrying
the peer chain of a *successful* handshake. This module narrows that gap
without a new Qt API. It is a probe, not pinning enforced on Chromium's
own connection — read the residuals below before describing it.

How it works:

  - ``__main__`` starts :class:`PinProxy` on ``127.0.0.1:<ephemeral>``
    before ``QApplication`` exists and points Chromium's fixed proxy for
    the ``https`` scheme at it (:func:`chromium_flags`). That covers
    ``https:`` and ``wss:`` (and ``ws:``, tunnelled to port 80); ``http:``
    stays direct. A fixed proxy, not a PAC script: Chromium falls back to
    ``DIRECT`` when a PAC script fails, but never when a fixed proxy is
    unreachable, so a dead proxy fails every https load instead of
    skipping the check. ``--proxy-bypass-list=<-loopback>`` removes
    Chromium's implicit ``localhost``/loopback/link-local bypass, so
    those names are proxied too.
  - ``CONNECT`` to a host that is not pinned (or is overridden) is a plain
    tunnel. For a pinned host the proxy resolves it once, then per
    address: runs its own TLS handshake to that address (system trust +
    hostname check), evaluates the *verified* chain with
    ``PinStore.evaluate``, closes the probe, and opens the tunnel to the
    **same address**. A mismatch, an unverifiable chain or a probe error
    refuses the tunnel (``502``) and journals
    ``qdbrowser.cert reject host=<h> reason=<r> path=probe``.
  - Chromium still does its own normal validation inside the tunnel.

Only clients owned by this uid are served (full TCP 4-tuple looked up in
``/proc/net/tcp*``; unknown or ambiguous owner = refused).

Matching uses the chain OpenSSL *verified*, never the presented one: the
presented chain is attacker-controlled and pinned certificates are
public, so matching on it could be satisfied by appending the pinned
intermediate to a forged chain.

KNOWN RESIDUALS (keep these when editing docs; see cert_policy):

  - NOT BOUND TO CHROMIUM'S CONNECTION. The probe and Chromium's
    handshake are separate TLS sessions to the same address. An on-path
    attacker that can tell them apart (ClientHello fingerprint, ALPN,
    timing) can let the probe through to the real server and present its
    CA-valid wrong-key certificate only to Chromium. What this catches is
    interception applied to every connection: a TLS-inspecting middlebox,
    a mis-issued certificate served indiscriminately by an on-path
    attacker. Enforcing pins on Chromium's own handshake needs a Qt API
    that does not exist (6.11).
  - The probe trusts OpenSSL's store (``SSL_CERT_FILE``/system bundle),
    not Chromium's NSS DB. A pinned host chaining to a CA present only in
    NSS fails the probe (fail closed), so its CA must be in both.
  - All https traffic goes through this proxy while any pin is
    configured, and its host names are resolved here (system resolver),
    not by Chromium.
  - Pins are read once at startup.
  - A user-supplied ``--proxy-*`` Chromium flag disables the proxy (a
    second proxy config would be ambiguous); ``__main__`` logs it at
    ERROR.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable

from .cert_policy import PinStore, canonical_host

log = logging.getLogger("qdbrowser.cert")

_HOST_RE = re.compile(r"^[a-z0-9_]([a-z0-9_.-]{0,251}[a-z0-9_])?$")
_MAX_HEAD = 8192
_HEAD_TIMEOUT_S = 10.0
_CONNECT_TIMEOUT_S = 10.0
_MAX_CLIENTS = 256
# Chromium flags that already configure a proxy. With one present ours is
# not added (two configs would race) and the success path is off.
PROXY_FLAGS = ("--proxy-server", "--proxy-pac-url", "--no-proxy-server",
               "--proxy-auto-detect", "--proxy-bypass-list")


def chromium_flags(port: int) -> list:
    """Flags routing every https/wss request through the proxy on ``port``,
    loopback names included. No spaces inside a flag: QtWebEngine splits
    ``QTWEBENGINE_CHROMIUM_FLAGS`` on whitespace."""
    return [f"--proxy-server=https=127.0.0.1:{int(port)}",
            "--proxy-bypass-list=<-loopback>"]


def peer_uid(conn: socket.socket) -> int | None:
    """uid owning the client end of a loopback TCP connection, or None.

    The client's socket is the ``/proc/net/tcp*`` row whose local endpoint
    is our peer and whose remote endpoint is us — all four of address and
    port, so another uid binding the same source port on a different
    loopback address cannot borrow the browser's row. Only ESTABLISHED
    rows count; more than one owner for the tuple is ambiguous and also
    returns None.
    """
    try:
        peer = conn.getpeername()
        local = conn.getsockname()
    except OSError:
        return None
    want = (_norm_ip(peer[0]), peer[1], _norm_ip(local[0]), local[1])
    owners = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="ascii") as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) < 8 or parts[3] != "01":  # ESTABLISHED
                        continue
                    try:
                        row = (*_proc_endpoint(parts[1]),
                               *_proc_endpoint(parts[2]))
                    except ValueError:
                        continue
                    if row == want:
                        owners.add(int(parts[7]))
        except OSError:
            continue
    return owners.pop() if len(owners) == 1 else None


def _norm_ip(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _proc_endpoint(field: str):
    """Decode a ``/proc/net/tcp*`` ``ADDR:PORT`` field (host-order words)."""
    hexaddr, hexport = field.split(":")
    raw = bytes.fromhex(hexaddr)
    # The kernel prints each 32-bit word in host byte order.
    words = [raw[i:i + 4][::-1] if sys.byteorder == "little" else raw[i:i + 4]
             for i in range(0, len(raw), 4)]
    ip = ipaddress.ip_address(b"".join(words))
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip, int(hexport, 16)


def _default_context() -> ssl.SSLContext:
    # Honors SSL_CERT_FILE / SSL_CERT_DIR, which __main__'s CA-bundle
    # step may have exported for the launch profile.
    return ssl.create_default_context()


class PinProxy:
    """Loopback CONNECT proxy that pin-checks pinned hosts before tunnelling."""

    def __init__(self,
                 store_getter: Callable[[], PinStore | None],
                 *,
                 resolver: Callable = socket.getaddrinfo,
                 context_factory: Callable[[], ssl.SSLContext] = _default_context,
                 uid: int | None = None,
                 probe_timeout: float = _CONNECT_TIMEOUT_S):
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
        sock.listen(64)
        self._sock = sock
        threading.Thread(target=self._accept_loop, name="qdbrowser-pin-proxy",
                         daemon=True).start()
        log.info("qdbrowser.cert pin proxy listening port=%d", self.port)
        return self.port

    def stop(self) -> None:
        self._stopped.set()
        if self._sock is not None:
            try:
                # close() alone does not wake a thread blocked in accept().
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
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
                time.sleep(0.05)  # e.g. EMFILE: don't spin
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
        except Exception as exc:  # noqa: BLE001 - never kill the accept loop
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
            upstream = self._plain_upstream(host, port)
        else:
            upstream = self._checked_upstream(store, host, port)
        if upstream is None:
            _reply(conn, 502, "upstream refused")
            return
        try:
            conn.settimeout(None)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _splice(conn, upstream)
        finally:
            upstream.close()

    # -- upstreams ------------------------------------------------------------

    def _addresses(self, host: str, port: int) -> list:
        return self._resolve(host, port, type=socket.SOCK_STREAM)

    def _open(self, family, socktype, proto, sockaddr) -> socket.socket:
        s = socket.socket(family, socktype, proto)
        try:
            s.settimeout(self._probe_timeout)
            s.connect(sockaddr)
            s.settimeout(None)
        except OSError:
            s.close()
            raise
        return s

    def _plain_upstream(self, host: str, port: int) -> socket.socket | None:
        """Unpinned host: first reachable address, no check."""
        try:
            addrs = self._addresses(host, port)
        except OSError:
            return None
        for family, socktype, proto, _canon, sockaddr in addrs:
            try:
                return self._open(family, socktype, proto, sockaddr)
            except OSError:
                continue
        return None

    def _checked_upstream(self, store: PinStore, host: str,
                          port: int) -> socket.socket | None:
        """Probe each resolved address; return a plain socket to the first
        address whose verified chain matches a pin, else None."""
        try:
            addrs = self._addresses(host, port)
        except OSError as exc:
            log.warning("qdbrowser.cert reject host=%s reason=resolve_failed "
                        "path=probe detail=%s", host, exc)
            return None
        last = ("no_address", "")
        for family, socktype, proto, _canon, sockaddr in addrs:
            verdict = self._probe(store, host, family, socktype, proto,
                                  sockaddr)
            if verdict is None:
                try:
                    up = self._open(family, socktype, proto, sockaddr)
                except OSError as exc:
                    last = ("connect_failed", str(exc))
                    continue
                log.info("qdbrowser.cert pin ok host=%s addr=%s path=probe",
                         host, sockaddr[0])
                return up
            if verdict[0] == "connect_failed":
                last = verdict
                continue
            # A reachable address presented a bad chain: refuse outright
            # rather than trying the next address.
            log.warning("qdbrowser.cert reject host=%s reason=%s path=probe "
                        "addr=%s detail=%r", host, verdict[0], sockaddr[0],
                        verdict[1])
            return None
        log.warning("qdbrowser.cert reject host=%s reason=%s path=probe "
                    "detail=%r", host, last[0], last[1])
        return None

    def _probe(self, store: PinStore, host: str, family, socktype, proto,
               sockaddr) -> tuple[str, str] | None:
        """None if the address's verified chain matches a pin, else
        ``(reason, detail)``; ``reason`` is a bare token for the journal."""
        try:
            raw = self._open(family, socktype, proto, sockaddr)
        except OSError as exc:
            return ("connect_failed", str(exc))
        raw.settimeout(self._probe_timeout)
        try:
            ctx = self._context_factory()
            try:
                tls = ctx.wrap_socket(raw, server_hostname=host)
            except ssl.SSLCertVerificationError as exc:
                return ("probe_unverified", str(exc.verify_message))
            except (ssl.SSLError, OSError) as exc:
                return ("probe_tls_error", str(exc))
            try:
                # Python >= 3.13: a list of DER bytes.
                chain = [bytes(c) for c in tls.get_verified_chain()]
            finally:
                tls.close()
        finally:
            raw.close()
        decision = store.evaluate(host, chain)
        return None if decision.allow else (decision.kind, decision.detail)


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
    head, rest = buf.split(b"\r\n\r\n", 1)
    if rest:
        return None  # a client must wait for "200" before sending TLS
    return head


def _parse_connect(head: bytes) -> tuple[str, int] | None:
    """``(canonical host, port)`` from a ``CONNECT`` request line."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = line.split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        return None
    hostport = parts[1]
    if hostport.startswith("["):
        host, sep, port_s = hostport[1:].partition("]:")
    else:
        host, sep, port_s = hostport.rpartition(":")
    if not sep or not port_s.isdigit():
        return None
    port = int(port_s)
    if not 0 < port < 65536:
        return None
    canon = canonical_host(host)
    if not canon:
        return None
    try:
        ipaddress.ip_address(canon)
    except ValueError:
        if not _HOST_RE.match(canon):
            return None
    return canon, port


def _reply(conn: socket.socket, code: int, reason: str) -> None:
    try:
        conn.sendall(f"HTTP/1.1 {code} {reason}\r\nContent-Length: 0\r\n"
                     "Connection: close\r\n\r\n".encode("ascii"))
    except OSError:
        pass


def _pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy ``src`` -> ``dst`` until EOF/error, then half-close ``dst``."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _splice(a: socket.socket, b: socket.socket) -> None:
    """Copy bytes both ways until both directions end. One thread per
    direction: a single loop doing blocking ``sendall`` can deadlock when
    both directions are backed up at once."""
    t = threading.Thread(target=_pump, args=(b, a), daemon=True)
    t.start()
    _pump(a, b)
    t.join()
