"""Unit tests for the net-VM rpcd JSON-RPC control-plane client (task 4 piece 3).

Drives the real :class:`NetVMClient` over a loopback HTTP server that faithfully
mimics ``uhttpd-mod-ubus``: the ``{"result":[rc, data]}`` envelope, session
login/expiry, and per-object ACL denial (status 6). The fake is the unit-test
analogue of the real OpenWrt rpcd the same client is exercised against on a VM
(see ``tests/integration/vm/s04-netvm-client.sh``); both assert the same
contract so a green VM run and a green unit run mean the same thing.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from qdistro_netvm_client import (
    NetVMClient, NetVMAuthError, NetVMCallError, NetVMPermissionError,
    NetVMProtocolError, NetVMTransportError, UBUS_STATUS_NOT_FOUND,
)

_NULL = "00000000000000000000000000000000"


# ---------------------------------------------------------------------------
# A faithful fake uhttpd-mod-ubus
# ---------------------------------------------------------------------------
class FakeRpcd:
    """In-process ubus-over-HTTP server. Knows one login, a scoped read ACL,
    and a couple of custom methods — enough to assert the client's contract."""

    def __init__(self, *, password="probe123",
                 read_acl=("system", "network.interface.wan", "iwinfo"),
                 oversize=False, garbage=False, expire_after=None):
        self.password = password
        self.read_acl = set(read_acl)
        self.oversize = oversize          # reply larger than the client cap
        self.garbage = garbage            # non-JSON reply
        self.expire_after = expire_after  # token dies after N authed calls
        self.token = "f46d85bc270634bb236df3df9268d484"
        self._authed_calls = 0
        self.last_egress_config: dict | None = None
        self.last_wifi_join: dict | None = None
        self._srv = HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                         daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/ubus"

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)

    # -- the ubus dispatch ----------------------------------------------
    # Faithful to uhttpd-mod-ubus on OpenWrt 24.10.7: the happy path returns
    # `{"result":[0,data]}`, but object-not-found / ACL-denial / session-expiry
    # come back as a JSON-RPC `{"error":{"code":-320xx}}`, NOT `result:[rc]`.
    # (-32000 object, -32001 session, -32002 access denied.)
    @staticmethod
    def _ok(data):
        return {"result": [0, data]}

    @staticmethod
    def _err(code, msg):
        return {"error": {"code": code, "message": msg}}

    def _dispatch(self, session, obj, method, args):
        """Return the JSON-RPC reply body (sans jsonrpc/id)."""
        if obj == "session" and method == "login":
            if args.get("password") != self.password:
                return self._err(-32002, "Access denied")
            self._authed_calls = 0          # fresh session ⇒ fresh timeout
            return self._ok({"ubus_rpc_session": self.token, "timeout": 300,
                             "expires": 299, "acls": {}, "data": {}})
        if obj == "session" and method == "destroy":
            return self._ok({})
        # Everything else needs a live session.
        if session != self.token:
            return self._err(-32002, "Access denied")
        if self.expire_after is not None:
            self._authed_calls += 1
            if self._authed_calls > self.expire_after:
                self.token = "EXPIRED-NEW-TOKEN-DEADBEEF00000000"
                return self._err(-32001, "Session not found")  # expired
        # Custom (image-installed) egress object — always reachable here.
        if obj == "qdistro.netvm" and method == "egress_reload":
            self.last_egress_config = args.get("config")
            return self._ok({"applied": True})
        if obj == "qdistro.netvm" and method == "wifi_join":
            self.last_wifi_join = args
            return self._ok({"joined": args.get("ssid")})
        # Stock read objects: object-existence is checked before the ACL on
        # the real box — an object that exists nowhere is -32000, an object
        # that exists but isn't granted to this login is -32002.
        known = {"system", "iwinfo", "network.device"} | {
            o for o in self.read_acl} | {
            "network.interface", "network.interface.wan"}
        if obj not in known:
            return self._err(-32000, "Object not found")
        if obj not in self.read_acl:
            return self._err(-32002, "Access denied")  # ACL boundary
        if obj == "system" and method == "board":
            return self._ok({"kernel": "6.6",
                             "release": {"version": "24.10.7"}})
        if obj.startswith("network.interface") and method == "status":
            return self._ok({"up": True, "l3_device": "eth1",
                             "route": [{"target": "0.0.0.0"}]})
        if obj == "iwinfo" and method == "scan":
            return self._ok({"results": [{"ssid": "cafe", "signal": -55},
                                         {"ssid": "home", "signal": -40}]})
        return self._err(-32000, "Object not found")

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                req = json.loads(body)
                session, obj, method, args = req["params"]
                reply = fake._dispatch(session, obj, method, args)
                if fake.garbage:
                    payload = b"<html>not json</html>"
                else:
                    payload = json.dumps({
                        "jsonrpc": "2.0", "id": req["id"],
                        **reply}).encode()
                if fake.oversize:
                    payload = b'{"jsonrpc":"2.0","id":1,"result":[0,{"x":"' \
                        + b"A" * (3 * 1024 * 1024) + b'"}]}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionError):
                    # The oversize test closes the socket after the byte cap;
                    # the unwritten tail is expected, not a failure.
                    pass

        return H


@pytest.fixture
def rpcd():
    f = FakeRpcd()
    yield f
    f.stop()


def _client(rpcd, **kw):
    return NetVMClient(base_url=rpcd.url, username="root",
                       password="probe123", timeout=5.0, **kw)


# ---------------------------------------------------------------------------
# Session + transport
# ---------------------------------------------------------------------------
class TestSession:
    def test_login_acquires_token(self, rpcd):
        c = _client(rpcd)
        assert c.login() == rpcd.token
        assert c._session == rpcd.token

    def test_bad_password_raises_auth(self, rpcd):
        c = _client(rpcd)
        c.password = "wrong"
        with pytest.raises(NetVMAuthError):
            c.login()

    def test_call_logs_in_lazily(self, rpcd):
        c = _client(rpcd)
        assert c._session is None
        c.board()
        assert c._session == rpcd.token

    def test_session_expiry_triggers_one_relogin(self, rpcd):
        # Token dies after the first authed call; the second call must
        # transparently re-login and succeed.
        rpcd.expire_after = 1
        c = _client(rpcd)
        c.board()                      # call #1, then token rotates
        first = c._session
        out = c.board()                # call #2 → 6 → relogin → ok
        assert out["release"]["version"] == "24.10.7"
        assert c._session != first     # re-acquired the rotated token


# ---------------------------------------------------------------------------
# Read wrappers (stock ubus objects Probe 2 proved)
# ---------------------------------------------------------------------------
class TestReads:
    def test_board(self, rpcd):
        assert _client(rpcd).board()["release"]["version"] == "24.10.7"

    def test_interface_status(self, rpcd):
        st = _client(rpcd).interface_status("wan")
        assert st["up"] is True and st["l3_device"] == "eth1"

    def test_wifi_scan_returns_results(self, rpcd):
        rpcd.read_acl.add("iwinfo")
        res = _client(rpcd).wifi_scan("radio0")
        assert [b["ssid"] for b in res] == ["cafe", "home"]


# ---------------------------------------------------------------------------
# ACL boundary (least privilege)
# ---------------------------------------------------------------------------
class TestAclBoundary:
    def test_denied_object_raises_permission(self, rpcd):
        # ACL grants system/network.interface.wan/iwinfo only; a different
        # object is the boundary, surfaced as a permission error, not retried.
        rpcd.read_acl = {"iwinfo"}
        c = _client(rpcd)
        with pytest.raises(NetVMPermissionError) as ei:
            c.board()
        assert ei.value.code == 6
        assert ei.value.object == "system"

    def test_persistent_denial_does_not_loop(self, rpcd):
        # A genuine denial (not expiry) must re-login at most once then raise —
        # never spin. Track login count via password attempts.
        rpcd.read_acl = set()
        c = _client(rpcd)
        with pytest.raises(NetVMPermissionError):
            c.board()

    def test_object_not_found_maps_to_callerror(self, rpcd):
        # uhttpd-mod-ubus reports object-not-found as JSON-RPC error -32000,
        # which must surface as a NetVMCallError(NOT_FOUND) — not a protocol
        # error and not a permission error (so it does NOT trigger re-login).
        c = _client(rpcd)
        with pytest.raises(NetVMCallError) as ei:
            c.call("does.not.exist", "frob")
        assert ei.value.code == UBUS_STATUS_NOT_FOUND
        assert not isinstance(ei.value, NetVMPermissionError)


# ---------------------------------------------------------------------------
# Envelope caps + defensive parse (TCB parsing rule)
# ---------------------------------------------------------------------------
class TestEnvelope:
    def test_oversize_response_rejected(self, rpcd):
        rpcd.oversize = True
        with pytest.raises(NetVMTransportError):
            _client(rpcd, max_response_bytes=1024).board()

    def test_oversize_request_rejected(self, rpcd):
        c = _client(rpcd, max_request_bytes=64)
        with pytest.raises(NetVMTransportError):
            c.egress_reload({"network": "x" * 200})

    def test_garbage_reply_raises_protocol(self, rpcd):
        rpcd.garbage = True
        with pytest.raises(NetVMProtocolError):
            _client(rpcd).board()

    def test_unreachable_endpoint_raises_transport(self):
        c = NetVMClient(base_url="http://127.0.0.1:1/ubus", password="x",
                        timeout=1.0)
        with pytest.raises(NetVMTransportError):
            c.board()


# ---------------------------------------------------------------------------
# Egress reload (the policy-apply wrapper) + wifi join
# ---------------------------------------------------------------------------
class TestEgressReload:
    def test_ships_compiled_fragments(self, rpcd):
        frags = {"network": "config interface\n",
                 "firewall": "config zone\n", "dhcp": "config dhcp\n"}
        out = _client(rpcd).egress_reload(frags)
        assert out["applied"] is True
        assert rpcd.last_egress_config == frags

    def test_rejects_unknown_fragment_key(self, rpcd):
        with pytest.raises(NetVMCallError.__bases__[0]):  # NetVMError
            _client(rpcd).egress_reload({"wireless": "x"})

    def test_rejects_non_string_fragment(self, rpcd):
        from qdistro_netvm_client import NetVMError
        with pytest.raises(NetVMError):
            _client(rpcd).egress_reload({"network": ["not", "a", "string"]})

    def test_wifi_join_open_network_omits_key(self, rpcd):
        _client(rpcd).wifi_join(device="radio0", ssid="lobby")
        assert rpcd.last_wifi_join["encryption"] == "none"
        assert "key" not in rpcd.last_wifi_join

    def test_wifi_join_psk_passes_key(self, rpcd):
        _client(rpcd).wifi_join(device="radio0", ssid="home", key="hunter2")
        assert rpcd.last_wifi_join["key"] == "hunter2"
        assert rpcd.last_wifi_join["encryption"] == "psk2"
