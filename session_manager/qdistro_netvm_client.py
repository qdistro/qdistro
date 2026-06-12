"""Host-side rpcd JSON-RPC control-plane client for the net VM (task 4 piece 3).

Probe 2 (``todo/fable-networking/02-RESULTS.md`` step 3) chose the transport:
**rpcd JSON-RPC over HTTP on a host-only management vif, behind a scoped rpcd
ACL.** This is the admin/session-manager side of that link. It speaks the
ubus-over-HTTP envelope that ``uhttpd-mod-ubus`` serves at ``/ubus``:

    POST /ubus
    {"jsonrpc":"2.0","id":N,"method":"call",
     "params":[<session>, <object>, <method>, <args>]}

and decodes the ``{"result":[rc, data]}`` reply, where ``rc`` is a ``ubus``
status code (0 == OK, 6 == permission denied — the ACL boundary).

Design constraints, straight from the docs this implements:

  * **Size-capped, structured envelope (TCB parsing rule,
    ``threat-model.md``).** The net VM terminates hostile input (802.11, DHCP,
    DNS); its control plane is therefore treated as an untrusted parser
    boundary even though the mgmt vif is host-only. Every request body is
    capped before send and every response is read under a hard byte cap and
    JSON-parsed defensively — an oversized or malformed reply raises rather
    than allocating unboundedly.
  * **Least privilege via the rpcd ACL, not this client.** The client never
    assumes it can call an object; the *scoped rpcd ACL on the VM* (Probe 2's
    ``network.interface``/``iwinfo`` read grant) is the boundary. A denied call
    surfaces as ``NetVMPermissionError`` — the client does not silently retry
    with broader scope.
  * **No third-party deps.** stdlib ``urllib`` only — qdistro modules ship as
    flat ``.py`` files on target, like the interim backend's siblings.

The control-plane operations the design names (status / scan / join / egress
reload) are typed wrappers over the generic :meth:`NetVMClient.call`. The
**egress reload** wrapper ships the broker-approved per-silo policy — compiled
to UCI by the pure :mod:`qdistro_netvm_uci` (piece 4) — to a scoped custom ubus
object the image (piece 1) installs; it is the net-VM analogue of the interim
backend's ``SetSiloEgress`` apply. Read wrappers (board/interface/wifi) ride
the stock ubus objects Probe 2 proved.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# The all-zero session id is ubus' "no session" sentinel used for the login
# call itself (RPC_DEFAULT_SESSION).
_NULL_SESSION = "00000000000000000000000000000000"

# ubus status codes we care about (libubus ``enum ubus_msg_status``).
UBUS_STATUS_OK = 0
UBUS_STATUS_INVALID_COMMAND = 1
UBUS_STATUS_INVALID_ARGUMENT = 2
UBUS_STATUS_METHOD_NOT_FOUND = 3
UBUS_STATUS_NOT_FOUND = 4
UBUS_STATUS_PERMISSION_DENIED = 6
UBUS_STATUS_TIMEOUT = 7

# Envelope caps (TCB parsing rule). Generous enough for an `iwinfo scan` of a
# crowded band (dozens of BSSes) yet bounded — a compromised net VM cannot make
# the host allocate without limit.
DEFAULT_MAX_REQUEST_BYTES = 256 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# uhttpd-mod-ubus does NOT return ubus application failures as `result:[rc]`;
# it maps them onto JSON-RPC `error.code` in a private -320xx range (proven on
# OpenWrt 24.10.7 — object-not-found and ACL/expiry denials come back as
# `error`, only the happy path is `result:[0,data]`). Decode that range back to
# ubus status so the ACL boundary and session expiry are classified correctly
# (and re-login fires) instead of looking like a malformed reply.
_JSONRPC_OBJECT_NOT_FOUND = -32000   # ubus NOT_FOUND
_JSONRPC_SESSION = -32001            # session gone ⇒ treat as expiry/denied
_JSONRPC_ACCESS_DENIED = -32002      # ubus PERMISSION_DENIED (ACL or expiry)
_JSONRPC_UBUS_TIMEOUT = -32003       # ubus TIMEOUT
# JSON-RPC error codes that mean WE sent a malformed envelope (client bug),
# not a VM-side policy outcome.
_JSONRPC_CLIENT_FAULT = frozenset({-32700, -32600, -32601, -32602, -32603})

# The custom ubus object + method the image installs for egress policy reload.
# Kept configurable so the in-VM plugin name can move without a client change.
DEFAULT_EGRESS_OBJECT = "qdistro.netvm"
DEFAULT_EGRESS_METHOD = "egress_reload"


class NetVMError(Exception):
    """Base class for net-VM control-plane failures."""


class NetVMTransportError(NetVMError):
    """The HTTP/TCP layer failed (connect/timeout/oversize/garbage)."""


class NetVMProtocolError(NetVMError):
    """A well-formed HTTP reply that is not a valid ubus JSON-RPC result."""


class NetVMCallError(NetVMError):
    """A ubus call returned a non-zero status code.

    ``code`` is the ubus status (see ``UBUS_STATUS_*``)."""

    def __init__(self, obj: str, method: str, code: int):
        self.object = obj
        self.method = method
        self.code = code
        super().__init__(
            f"ubus call {obj}.{method} failed: status {code} "
            f"({_STATUS_NAMES.get(code, 'unknown')})")


class NetVMPermissionError(NetVMCallError):
    """A ubus call was refused by the rpcd ACL (status 6).

    This is the least-privilege boundary asserting itself, *not* a bug to retry
    around: the VM's scoped ACL does not grant this object/method to the
    client's login."""


class NetVMAuthError(NetVMError):
    """Login failed, or a session could not be (re)established."""


_STATUS_NAMES = {
    UBUS_STATUS_OK: "ok",
    UBUS_STATUS_INVALID_COMMAND: "invalid command",
    UBUS_STATUS_INVALID_ARGUMENT: "invalid argument",
    UBUS_STATUS_METHOD_NOT_FOUND: "method not found",
    UBUS_STATUS_NOT_FOUND: "not found",
    UBUS_STATUS_PERMISSION_DENIED: "permission denied",
    UBUS_STATUS_TIMEOUT: "timeout",
}


@dataclass
class NetVMClient:
    """A session-managed ubus-over-HTTP client for one net VM.

    ``base_url`` is the ``/ubus`` endpoint on the host-only management vif,
    e.g. ``http://127.0.0.1:8080/ubus``. ``username``/``password`` are the
    rpcd login the scoped ACL is attached to. The session token is acquired
    lazily on first call and transparently re-acquired once if the VM expires
    it mid-flight."""

    base_url: str
    username: str = "root"
    password: str = ""
    timeout: float = 10.0
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    egress_object: str = DEFAULT_EGRESS_OBJECT
    egress_method: str = DEFAULT_EGRESS_METHOD
    # Internal session state.
    _session: str | None = field(default=None, repr=False)
    _rpc_id: int = field(default=0, repr=False)

    # ---- low-level transport ---------------------------------------------
    def _post(self, payload: dict) -> dict:
        """POST one JSON-RPC envelope, return the decoded reply dict.

        Enforces both envelope caps and parses the reply defensively."""
        body = json.dumps(payload, separators=(",", ":")).encode()
        if len(body) > self.max_request_bytes:
            raise NetVMTransportError(
                f"request envelope {len(body)}B exceeds cap "
                f"{self.max_request_bytes}B")
        req = urllib.request.Request(
            self.base_url, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                # Read one byte past the cap so an exactly-cap-sized body is
                # accepted but anything larger is detected, even when the VM
                # omits/forges Content-Length.
                raw = resp.read(self.max_response_bytes + 1)
        except urllib.error.URLError as e:
            raise NetVMTransportError(f"POST {self.base_url}: {e}") from e
        except (TimeoutError, socket.timeout) as e:
            raise NetVMTransportError(f"POST {self.base_url} timed out") from e
        if len(raw) > self.max_response_bytes:
            raise NetVMTransportError(
                f"response exceeds cap {self.max_response_bytes}B "
                "(truncated, refusing)")
        try:
            reply = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as e:
            raise NetVMProtocolError(f"non-JSON reply: {e}") from e
        if not isinstance(reply, dict):
            raise NetVMProtocolError(
                f"reply is {type(reply).__name__}, expected object")
        return reply

    def _rpc(self, session: str, obj: str, method: str,
             args: dict) -> dict:
        """One ``call`` RPC; returns the JSON-RPC reply dict unchanged."""
        self._rpc_id += 1
        return self._post({
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "call",
            "params": [session, obj, method, args],
        })

    @staticmethod
    def _result(reply: dict, obj: str, method: str) -> dict:
        """Extract ``data`` from a ``{"result":[rc, data]}`` reply, raising on
        a JSON-RPC error, a non-zero ubus status, or a malformed shape."""
        if "error" in reply:
            err = reply["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message", err) if isinstance(err, dict) else err
            # Map uhttpd-mod-ubus' private error range back to ubus status so
            # the ACL boundary / session expiry are first-class, not "protocol
            # broke". Access-denied AND session-gone both raise the permission
            # error (which `call` uses to re-login once).
            if code in (_JSONRPC_ACCESS_DENIED, _JSONRPC_SESSION):
                raise NetVMPermissionError(obj, method,
                                           UBUS_STATUS_PERMISSION_DENIED)
            if code == _JSONRPC_OBJECT_NOT_FOUND:
                raise NetVMCallError(obj, method, UBUS_STATUS_NOT_FOUND)
            if code == _JSONRPC_UBUS_TIMEOUT:
                raise NetVMCallError(obj, method, UBUS_STATUS_TIMEOUT)
            # -327xx / -326xx: our request was malformed — a client fault.
            raise NetVMProtocolError(f"JSON-RPC error {code}: {msg}")
        if "result" not in reply:
            raise NetVMProtocolError("reply has neither result nor error")
        res = reply["result"]
        # ubus-over-HTTP shapes the result as [status] or [status, data].
        if not isinstance(res, list) or not res:
            raise NetVMProtocolError(
                f"result is {res!r}, expected [status, data?]")
        code = res[0]
        if not isinstance(code, int):
            raise NetVMProtocolError(f"status {code!r} is not an int")
        if code == UBUS_STATUS_PERMISSION_DENIED:
            raise NetVMPermissionError(obj, method, code)
        if code != UBUS_STATUS_OK:
            raise NetVMCallError(obj, method, code)
        data = res[1] if len(res) > 1 else {}
        if not isinstance(data, dict):
            raise NetVMProtocolError(
                f"call data is {type(data).__name__}, expected object")
        return data

    # ---- session ---------------------------------------------------------
    def login(self) -> str:
        """Acquire (or refresh) a session token. Returns the token."""
        reply = self._rpc(_NULL_SESSION, "session", "login",
                          {"username": self.username,
                           "password": self.password})
        try:
            data = self._result(reply, "session", "login")
        except NetVMCallError as e:
            raise NetVMAuthError(
                f"login as {self.username!r} refused (status {e.code})") from e
        token = data.get("ubus_rpc_session")
        if not isinstance(token, str) or not token:
            raise NetVMAuthError("login reply carried no session token")
        self._session = token
        return token

    def logout(self) -> None:
        """Best-effort destroy of the current session on the VM."""
        if self._session is None:
            return
        try:
            self._rpc(self._session, "session", "destroy", {})
        except NetVMError:
            pass
        finally:
            self._session = None

    # ---- generic call (auto re-login once on expiry) ---------------------
    def call(self, obj: str, method: str, args: dict | None = None) -> dict:
        """Call ``obj.method(args)`` on the net VM, returning its data dict.

        Logs in lazily; if a previously-valid session has expired (the VM
        answers a normally-permitted call with permission-denied), re-logs in
        once and retries. A genuine ACL denial after a fresh login propagates
        as :class:`NetVMPermissionError` — it is the boundary, not a retry
        loop."""
        args = args or {}
        relogged = False
        if self._session is None:
            self.login()
            relogged = True
        while True:
            reply = self._rpc(self._session or _NULL_SESSION,
                              obj, method, args)
            try:
                return self._result(reply, obj, method)
            except NetVMPermissionError:
                # Expired session presents as permission-denied. Re-login once;
                # a second denial is a real ACL boundary, not expiry.
                if relogged:
                    raise
                self.login()
                relogged = True

    # ---- typed control-plane operations ----------------------------------
    def board(self) -> dict:
        """``system.board`` — image/version/board identity (status read)."""
        return self.call("system", "board")

    def interface_status(self, name: str) -> dict:
        """``network.interface.<name> status`` — uplink up/route/addresses.

        Probe 2 proved ``network.interface.wan status`` over this transport;
        the admin app polls it to render uplink health."""
        return self.call(f"network.interface.{name}", "status")

    def interface_dump(self) -> dict:
        """``network.interface dump`` — all interfaces at once."""
        return self.call("network.interface", "dump")

    def wifi_scan(self, device: str) -> list[dict]:
        """``iwinfo scan`` on a radio — the visible BSS list (scan op).

        Returns the ``results`` list (possibly empty); a missing/!radio device
        raises ``NetVMCallError``. iwinfo replies can be large on a crowded
        band — the response cap (not this method) bounds it."""
        data = self.call("iwinfo", "scan", {"device": device})
        results = data.get("results", [])
        if not isinstance(results, list):
            raise NetVMProtocolError("iwinfo scan results not a list")
        return results

    def wifi_join(self, *, device: str, ssid: str, key: str | None = None,
                  encryption: str = "psk2") -> dict:
        """Associate a radio with an SSID (join op).

        Delegates to the image's egress object (``<egress_object>.wifi_join``)
        rather than rewriting ``/etc/config/wireless`` field-by-field over
        ``uci`` ACLs: a single scoped method keeps the rpcd ACL surface to one
        verb and lets the VM validate the SSID/key (untrusted strings) before
        they touch ``wpa_supplicant``. ``key`` of ``None`` means an open
        network (``encryption=none``)."""
        args: dict = {"device": device, "ssid": ssid,
                      "encryption": "none" if key is None else encryption}
        if key is not None:
            args["key"] = key
        return self.call(self.egress_object, "wifi_join", args)

    def egress_reload(self, fragments: dict[str, str]) -> dict:
        """Push compiled UCI egress fragments to the VM and reload (reload op).

        ``fragments`` is exactly :func:`qdistro_netvm_uci.compile_all`'s output:
        ``{"network": ..., "firewall": ..., "dhcp": ...}``. The VM-side method
        writes them as the declarative overlay, then reloads network/firewall/
        dnsmasq atomically — the net-VM counterpart of the interim backend's
        per-silo apply. The kill-switch is in the *compiled* fw4 (default-deny
        forward), so a reload can only narrow or re-pin egress, never silently
        open WAN.

        The fragment set is validated here (host side) before it is shipped:
        only the three known config files, each a string, total under the
        request cap. The VM revalidates — defence in depth — but a malformed
        compile never reaches the wire."""
        if set(fragments) - {"network", "firewall", "dhcp"}:
            raise NetVMError(
                f"unknown UCI fragment keys: "
                f"{sorted(set(fragments) - {'network', 'firewall', 'dhcp'})}")
        for fname, text in fragments.items():
            if not isinstance(text, str):
                raise NetVMError(f"fragment {fname!r} is not a string")
        return self.call(self.egress_object, self.egress_method,
                         {"config": fragments})

    # ---- context manager -------------------------------------------------
    def __enter__(self) -> NetVMClient:
        return self

    def __exit__(self, *exc) -> None:
        self.logout()
