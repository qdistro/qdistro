"""Success-path cert pinning: the loopback pin proxy (iso2 `13` E2).

The pure tests drive ``PinProxy`` over real loopback sockets against a
real TLS server with a generated two-CA PKI; the client trusts both CAs,
so the "rogue" chain is CA-valid exactly like a mis-issued certificate or
a TLS-inspecting middlebox. ``test_chromium_*`` runs QtWebEngine in a
subprocess (``pinproxy_chromium_harness.py``) with the production proxy
flags to show Chromium's own traffic is routed through and refused by
the proxy — including loopback names and a dead proxy (no DIRECT
fallback).
"""

import logging
import os
import socket
import ssl
import subprocess
import sys

import pytest

pytest.importorskip("cryptography")

from qdbrowser.cert_policy import PinStore, canonical_host
from qdbrowser.pin_proxy import (
    PinProxy,
    _parse_connect,
    _proc_endpoint,
    chromium_flags,
    peer_uid,
)

from .pinproxy_pki import (
    MARKER,
    Pki,
    TlsServer,
    loopback_resolver,
    spki_pin,
)

HOST = "pinned.test"


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    return Pki(str(tmp_path_factory.mktemp("pki")), HOST)


@pytest.fixture
def serve(pki):
    servers = []

    def _serve(which, extra=()):
        s = TlsServer(*pki.server_files(which, extra))
        servers.append(s)
        return s
    yield _serve
    for s in servers:
        s.close()


def _proxy(pki, server, pins, *, overrides=(), uid=None, context=None):
    store = PinStore(pins=pins, overrides=overrides)
    p = PinProxy(lambda: store,
                 resolver=loopback_resolver({HOST: server.port}),
                 context_factory=context or pki.client_context,
                 uid=uid, probe_timeout=5)
    p.start()
    return p


def _connect(port, line=f"CONNECT {HOST}:443 HTTP/1.1"):
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(line.encode() + b"\r\nHost: x\r\n\r\n")
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(1)
        if not chunk:
            break
        head += chunk
    return s, head.decode()


# --- the pin check on a successful (CA-valid) handshake ---------------------

def test_matching_pin_tunnels_to_server(pki, serve):
    srv = serve("good")
    proxy = _proxy(pki, srv, {HOST: [spki_pin(pki.good_leaf)]})
    try:
        s, head = _connect(proxy.port)
        assert head.startswith("HTTP/1.1 200"), head
        tls = pki.client_context().wrap_socket(s, server_hostname=HOST)
        tls.sendall(b"GET / HTTP/1.1\r\nHost: pinned.test\r\n\r\n")
        assert MARKER in tls.recv(65536)
        tls.close()
    finally:
        proxy.stop()


def test_intermediate_pin_matches_verified_chain(pki, serve):
    srv = serve("good")
    proxy = _proxy(pki, srv, {HOST: [spki_pin(pki.good_ca)]})
    try:
        s, head = _connect(proxy.port)
        assert head.startswith("HTTP/1.1 200"), head
        s.close()
    finally:
        proxy.stop()


def test_ca_valid_wrong_key_is_refused(pki, serve, caplog):
    """The gap the error-path hook cannot see: a chain the trust store
    accepts, with an SPKI that matches no pin."""
    srv = serve("rogue")
    proxy = _proxy(pki, srv, {HOST: [spki_pin(pki.good_leaf),
                                     spki_pin(pki.good_ca)]})
    try:
        with caplog.at_level(logging.INFO, logger="qdbrowser.cert"):
            s, head = _connect(proxy.port)
        assert head.startswith("HTTP/1.1 502"), head
        s.close()
        assert srv.served == 0, "no request may reach the server"
        assert any("reject host=pinned.test reason=pin_mismatch path=probe"
                   in r.getMessage() for r in caplog.records)
    finally:
        proxy.stop()


def test_appended_pinned_cert_does_not_satisfy_pin(pki, serve):
    """Pinned certs are public. A forged chain that merely *carries* the
    pinned CA certificate must not match: only the verified chain counts."""
    srv = serve("rogue", extra=[pki.good_ca])
    proxy = _proxy(pki, srv, {HOST: [spki_pin(pki.good_ca)]})
    try:
        s, head = _connect(proxy.port)
        assert head.startswith("HTTP/1.1 502"), head
        s.close()
    finally:
        proxy.stop()


def test_unverifiable_chain_is_refused(pki, serve, caplog):
    srv = serve("good")
    proxy = _proxy(pki, srv, {HOST: [spki_pin(pki.good_leaf)]},
                   context=ssl.create_default_context)  # trusts neither CA
    try:
        with caplog.at_level(logging.INFO, logger="qdbrowser.cert"):
            s, head = _connect(proxy.port)
        assert head.startswith("HTTP/1.1 502"), head
        s.close()
        assert any("reason=probe_unverified" in r.getMessage()
                   for r in caplog.records)
    finally:
        proxy.stop()


# --- unpinned / overridden hosts: plain tunnel ------------------------------

def _tls_through(pki, port):
    s, head = _connect(port)
    assert head.startswith("HTTP/1.1 200"), head
    tls = pki.client_context().wrap_socket(s, server_hostname=HOST)
    tls.sendall(b"GET / HTTP/1.1\r\nHost: pinned.test\r\n\r\n")
    got = tls.recv(65536)
    tls.close()
    return got


def test_unpinned_host_is_tunnelled_unchecked(pki, serve):
    srv = serve("rogue")
    proxy = _proxy(pki, srv, {"other.test": [spki_pin(pki.good_leaf)]})
    try:
        assert MARKER in _tls_through(pki, proxy.port)
    finally:
        proxy.stop()


def test_overridden_host_is_tunnelled_unchecked(pki, serve):
    srv = serve("rogue")
    proxy = _proxy(pki, srv, {HOST: [spki_pin(pki.good_leaf)]},
                   overrides=[HOST])
    try:
        assert MARKER in _tls_through(pki, proxy.port)
    finally:
        proxy.stop()


def test_pin_key_spelling_does_not_escape_the_check(pki, serve):
    """Trailing dot / case in the pin file still pins the CONNECT host."""
    srv = serve("rogue")
    proxy = _proxy(pki, srv, {"PINNED.test.": [spki_pin(pki.good_leaf)]})
    try:
        s, head = _connect(proxy.port, "CONNECT pinned.test.:443 HTTP/1.1")
        assert head.startswith("HTTP/1.1 502"), head
        s.close()
    finally:
        proxy.stop()


def test_non_connect_is_refused(pki, serve):
    srv = serve("good")
    proxy = _proxy(pki, srv, {HOST: [spki_pin(pki.good_leaf)]})
    try:
        s, head = _connect(proxy.port, "GET http://pinned.test/ HTTP/1.1")
        assert head.startswith("HTTP/1.1 405"), head
        s.close()
    finally:
        proxy.stop()


def test_other_uid_gets_nothing(pki, serve):
    srv = serve("good")
    proxy = _proxy(pki, srv, {HOST: [spki_pin(pki.good_leaf)]},
                   uid=os.getuid() + 1)
    try:
        try:
            s, head = _connect(proxy.port)
        except ConnectionResetError:
            s, head = None, ""
        assert head == "", head
        assert srv.served == 0
        if s is not None:
            s.close()
    finally:
        proxy.stop()


def test_peer_uid_of_own_connection():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.create_connection(srv.getsockname())
    conn, _ = srv.accept()
    try:
        assert peer_uid(conn) == os.getuid()
    finally:
        for s in (c, conn, srv):
            s.close()


def test_peer_uid_matches_addresses_not_just_ports():
    """A client bound to 127.0.0.2 must be found by its full tuple."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.socket()
    c.bind(("127.0.0.2", 0))
    c.connect(srv.getsockname())
    conn, _ = srv.accept()
    try:
        assert conn.getpeername()[0] == "127.0.0.2"
        assert peer_uid(conn) == os.getuid()
    finally:
        for s in (c, conn, srv):
            s.close()


@pytest.mark.parametrize("field,want", [
    ("0100007F:01BB", ("127.0.0.1", 443)),
    ("0200007F:0050", ("127.0.0.2", 80)),
    ("0000000000000000FFFF00000100007F:01BB", ("127.0.0.1", 443)),
    ("00000000000000000000000001000000:0016", ("::1", 22)),
])
def test_proc_endpoint_decoding(field, want):
    if sys.byteorder != "little":
        pytest.skip("fixtures are little-endian /proc rows")
    ip, port = _proc_endpoint(field)
    assert (str(ip), port) == want


# --- parsing / host names ----------------------------------------------------

@pytest.mark.parametrize("line,want", [
    (b"CONNECT pinned.test:443 HTTP/1.1", ("pinned.test", 443)),
    (b"CONNECT Pinned.Test.:8443 HTTP/1.0", ("pinned.test", 8443)),
    (b"CONNECT pinned.test HTTP/1.1", None),
    (b"CONNECT pinned.test:0 HTTP/1.1", None),
    (b"CONNECT [::1]:443 HTTP/1.1", ("::1", 443)),
    (b"CONNECT 127.0.0.1:443 HTTP/1.1", ("127.0.0.1", 443)),
    (b"CONNECT xn--bcher-kva.test:443 HTTP/1.1", ("xn--bcher-kva.test", 443)),
    (b"CONNECT a b:443 HTTP/1.1", None),
    (b"GET / HTTP/1.1", None),
    (b"CONNECT pin\"ned.test:443 HTTP/1.1", None),
])
def test_parse_connect(line, want):
    assert _parse_connect(line + b"\r\nHost: x") == want


@pytest.mark.parametrize("raw,want", [
    ("Pinned.Test.", "pinned.test"),
    ("[::1]", "::1"),
    ("0:0::1", "::1"),
    ("bücher.test", "xn--bcher-kva.test"),
    ("", ""),
    (None, ""),
])
def test_canonical_host(raw, want):
    assert canonical_host(raw) == want


def test_store_matches_idn_and_ip_spellings():
    store = PinStore(pins={"bücher.test": ["sha256/x"], "[::1]": ["sha256/y"]})
    assert store.is_pinned("xn--bcher-kva.test")
    assert store.is_pinned("BÜCHER.test.")
    assert store.is_pinned("::1")


def test_chromium_flags_have_no_spaces_and_no_fallback():
    flags = chromium_flags(4242)
    assert flags == ["--proxy-server=https=127.0.0.1:4242",
                     "--proxy-bypass-list=<-loopback>"]
    assert not any(" " in f for f in flags)


# --- Chromium end to end -----------------------------------------------------

def _harness(case, tmp_path):
    here = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(here) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, os.path.join(here, "pinproxy_chromium_harness.py"),
         case, str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=120)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("HARNESS ")]
    assert lines, f"harness printed nothing (rc={proc.returncode})\n{proc.stderr[-3000:]}"
    return dict(kv.split("=", 1) for kv in lines[-1].split()[1:])


def test_chromium_loads_pinned_host_with_matching_key(tmp_path):
    r = _harness("match", tmp_path)
    assert r["marker"] == "1", r
    assert int(r["connects"]) >= 1, r


def test_chromium_refuses_ca_valid_wrong_key(tmp_path):
    r = _harness("mismatch", tmp_path)
    assert int(r["connects"]) >= 1, f"Chromium did not use the pin proxy: {r}"
    assert r["marker"] == "0", r
    assert r["served"] == "0", r
    assert r["cert_errors"] == "0", r


def test_chromium_proxies_loopback_names_too(tmp_path):
    """Chromium bypasses proxies for localhost/loopback by default; the
    ``<-loopback>`` bypass rule must undo that for a pinned *.localhost."""
    r = _harness("localhost", tmp_path)
    assert int(r["connects"]) >= 1, f"loopback name bypassed the proxy: {r}"
    assert r["marker"] == "0", r
    assert r["served"] == "0", r


def test_chromium_tunnels_unpinned_hosts(tmp_path):
    r = _harness("unpinned", tmp_path)
    assert int(r["connects"]) >= 1, r
    assert r["marker"] == "1", r


def test_chromium_dead_proxy_fails_closed(tmp_path):
    """A fixed proxy that is down fails the load; it never goes DIRECT
    (a PAC script that fails would)."""
    r = _harness("deadproxy", tmp_path)
    assert r["marker"] == "0", r
    assert r["served"] == "0", r


def test_chromium_control_wrong_key_loads_without_proxy(tmp_path):
    """Premise check: without the proxy flags Chromium accepts the rogue
    chain, so the refusals above are the proxy's doing."""
    r = _harness("unproxied", tmp_path)
    assert r["connects"] == "0", r
    assert r["marker"] == "1", r


# --- startup wiring (__main__._start_pin_proxy) -----------------------------

class _FakeConfig:
    security: dict = {}

    def get(self, *keys, default=None):
        return self.security if keys == ("security",) else default


@pytest.fixture
def start_pin_proxy(monkeypatch, tmp_path):
    import qdbrowser.__main__ as qmain
    import qdbrowser.cert_policy as cp
    import qdbrowser.config as qconfig

    pins = tmp_path / "pins.json"
    pins.write_text('{"Pinned.Test": ["sha256/AAAA"], "other.test": ["sha256/BBBB"]}')
    _FakeConfig.security = {"cert_pins_path": str(pins)}
    monkeypatch.setattr(qconfig, "Config", _FakeConfig)
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    prev_store = cp.active_pin_store()
    started = []

    def run():
        qmain._start_pin_proxy()
        if qmain._PIN_PROXY is not None:
            started.append(qmain._PIN_PROXY)
        return os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    yield run
    for p in started:
        p.stop()
    monkeypatch.setattr(qmain, "_PIN_PROXY", None)
    cp.set_active_pin_store(prev_store)


def test_startup_routes_pinned_hosts_through_a_live_proxy(start_pin_proxy):
    import qdbrowser.__main__ as qmain
    flags = start_pin_proxy()
    proxy = qmain._PIN_PROXY
    assert proxy is not None
    assert flags == ("--no-sandbox --proxy-server=https=127.0.0.1:"
                     f"{proxy.port} --proxy-bypass-list=<-loopback>")
    # The proxy is listening.
    socket.create_connection(("127.0.0.1", proxy.port), timeout=5).close()


def test_startup_respects_config_off(start_pin_proxy):
    _FakeConfig.security["success_path_pinning"] = False
    assert start_pin_proxy() == "--no-sandbox"


def test_startup_no_pins_no_proxy(start_pin_proxy, tmp_path):
    _FakeConfig.security["cert_pins_path"] = str(tmp_path / "missing.json")
    assert start_pin_proxy() == "--no-sandbox"


def test_startup_user_proxy_flag_wins_and_is_logged(start_pin_proxy, monkeypatch,
                                                    caplog):
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--proxy-server=10.0.0.1:3128")
    with caplog.at_level(logging.ERROR, logger="qdbrowser"):
        assert start_pin_proxy() == "--proxy-server=10.0.0.1:3128"
    assert any("NOT checked on successful handshakes" in r.getMessage()
               for r in caplog.records)
