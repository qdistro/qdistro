"""Client helpers for calling the qdistro browser bridge.

Two entry points:

- :func:`call_bridge` — same-uid path. Reaches an
  ``org.qdistro.BrowserBridge.<ppid>`` on the current session bus
  and calls its ``RequestTabs(s op, s args_json) -> s reply_json``
  method. Used when the caller runs as the user who owns the Firefox
  process.

- :func:`call_via_relay` — cross-uid path. Calls
  ``org.qdistro.UserRelay.uid<NNNN>.ForwardBrowserBridgeOp`` on the
  system bus; the relay (running as the target user) turns around
  and calls the bridge on the user's session bus. See
  :mod:`qdistro_user_relay` and ``doc/firefox-containers.md`` for
  the routing model and the impostor-name gate.

Both return a Python dict (JSON-parsed reply). Failures inside the
client return ``{"ok": False, "error": "<code>", ...}`` so callers
handle them identically to bridge-side ``ok:false`` replies.

The actual D-Bus transport is swappable via the
:class:`_BaseDBusClient` indirection so tests don't need a real bus.
The default implementation uses jeepney (pure-Python, no C ext).
"""
from __future__ import annotations

import json
from typing import Any

# Wire constants. Kept in sync with browser_bridge/qdistro_browser_bridge.py
# and user_relay/qdistro_user_relay.py.
BRIDGE_NAME_PREFIX = "org.qdistro.BrowserBridge."
BRIDGE_OBJ_PATH = "/org/qdistro/BrowserBridge"
BRIDGE_IFACE = "org.qdistro.BrowserBridge"

RELAY_SYSTEM_NAME_FMT = "org.qdistro.UserRelay.uid{uid}"
RELAY_OBJ_PATH = "/org/qdistro/UserRelay"
RELAY_IFACE = "org.qdistro.UserRelay"


# ---------------------------------------------------------------------------
# Transport indirection
# ---------------------------------------------------------------------------

class _BaseDBusClient:
    """Two-method surface: list names on a bus, and call a method.

    Mirrors the bridge's ``_BaseDBusClient`` shape so the same testing
    pattern applies (drop in a fake to record calls and return canned
    replies).
    """

    def list_names(self, bus: str) -> list[str]:  # noqa: ARG002
        raise NotImplementedError

    def call(self, bus: str, service: str, object_path: str,  # noqa: ARG002
             interface: str, method: str, signature: str,
             body: tuple) -> Any:
        raise NotImplementedError


class _JeepneyDBusClient(_BaseDBusClient):
    """Default. Pure-Python via jeepney; no C extension."""

    def _conn(self, bus: str):
        from jeepney.io.blocking import open_dbus_connection
        return open_dbus_connection(bus=bus)

    def list_names(self, bus: str) -> list[str]:
        from jeepney.bus_messages import message_bus
        conn = self._conn(bus)
        try:
            reply = conn.send_and_get_reply(message_bus.ListNames(),
                                            timeout=5.0)
        finally:
            conn.close()
        # ListNames returns (as) — a single field that's a list of strings.
        if not reply.body:
            return []
        return [str(n) for n in reply.body[0]]

    def call(self, bus, service, object_path, interface, method,
             signature, body):
        from jeepney import DBusAddress, new_method_call
        addr = DBusAddress(object_path=object_path,
                           bus_name=service, interface=interface)
        msg = new_method_call(addr, method, signature, body)
        conn = self._conn(bus)
        try:
            reply = conn.send_and_get_reply(msg, timeout=15.0)
        finally:
            conn.close()
        if reply.header.message_type.name == "ERROR":
            raise _DBusCallError(
                dbus_name=str(reply.header.fields.get(4, "")),
                detail=str(reply.body)[:200])
        return reply.body[0] if reply.body else ""


class _DBusCallError(RuntimeError):
    def __init__(self, dbus_name: str, detail: str):
        super().__init__(detail)
        self.dbus_name = dbus_name
        self.detail = detail


_dbus_client: _BaseDBusClient | None = None


def _get_dbus_client() -> _BaseDBusClient:
    global _dbus_client
    if _dbus_client is None:
        _dbus_client = _JeepneyDBusClient()
    return _dbus_client


def set_dbus_client(client: _BaseDBusClient | None) -> None:
    """Test hook: replace the transport. Pass ``None`` to reset."""
    global _dbus_client
    _dbus_client = client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _parse_reply(reply_str: str) -> dict:
    """Decode the JSON reply from the bridge's RequestTabs. Returns
    a dict; non-dict / non-JSON replies are wrapped in
    ``{"ok": False, "error": "bad_reply", ...}`` so the caller never
    sees a non-dict shape."""
    if not isinstance(reply_str, str):
        return {"ok": False, "error": "bad_reply",
                "detail": f"expected str, got {type(reply_str).__name__}"}
    try:
        data = json.loads(reply_str)
    except (ValueError, json.JSONDecodeError) as e:
        return {"ok": False, "error": "bad_reply",
                "detail": str(e)[:200]}
    if not isinstance(data, dict):
        return {"ok": False, "error": "bad_reply",
                "detail": "reply was not a JSON object"}
    return data


def call_bridge(op: str, args: dict | None = None,
                ppid: int | None = None) -> dict:
    """Call a bridge op on the *current* session bus (same-uid path).

    ``ppid`` selects which bridge instance to talk to. If omitted, the
    first ``org.qdistro.BrowserBridge.<digits>`` name on the session
    bus is used — same numerically-sorted ordering as the relay.

    Returns a dict. Failure modes:

    - ``{"ok": False, "error": "no_bridge_found"}`` — no qualifying
      bridge name on the session bus.
    - ``{"ok": False, "error": "bridge_call_failed", "dbus_name": ...}``
      — the bridge raised a D-Bus error.
    - Otherwise the bridge's JSON reply, parsed.
    """
    client = _get_dbus_client()
    try:
        names = client.list_names("SESSION")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "list_names_failed",
                "detail": f"{type(e).__name__}: {e}"[:200]}
    bridges = _select_bridges_by_ppid(names)
    if not bridges:
        return {"ok": False, "error": "no_bridge_found"}
    if ppid is not None:
        match = [(p, n) for p, n in bridges if p == ppid]
        if not match:
            return {"ok": False, "error": "no_bridge_found",
                    "ppid": ppid}
        bridge_name = match[0][1]
    else:
        bridge_name = bridges[0][1]
    args_json = json.dumps(args or {})
    try:
        reply_str = client.call(
            "SESSION", bridge_name, BRIDGE_OBJ_PATH, BRIDGE_IFACE,
            "RequestTabs", "ss", (op, args_json))
    except _DBusCallError as e:
        return {"ok": False, "error": "bridge_call_failed",
                "bridge": bridge_name, "dbus_name": e.dbus_name,
                "detail": e.detail}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "bridge_call_failed",
                "bridge": bridge_name,
                "detail": f"{type(e).__name__}: {e}"[:200]}
    return _parse_reply(reply_str)


def call_via_relay(op: str, args: dict | None = None, *,
                   uid: int, ppid: int | None = None,
                   any_bridge: bool = False) -> dict:
    """Call a bridge op through the system-bus UserRelay (cross-uid).

    Exactly one of ``ppid`` or ``any_bridge=True`` must be provided —
    the relay refuses bare ``{}`` selectors so a typo doesn't route to
    a random browser.

    Returns the relay's JSON reply, which already wraps both relay-side
    failures (bad_selector, no_bridge_found, bridge_call_failed) and
    the bridge's own reply in a dict-shaped response.
    """
    if (ppid is None) == (not any_bridge):
        return {"ok": False, "error": "bad_call",
                "detail": "exactly one of ppid= or any_bridge=True required"}
    selector: dict[str, Any] = {"ppid": ppid} if ppid is not None else {"any": True}
    args_json = json.dumps(args or {})
    selector_json = json.dumps(selector)
    relay_name = RELAY_SYSTEM_NAME_FMT.format(uid=uid)
    client = _get_dbus_client()
    try:
        reply_str = client.call(
            "SYSTEM", relay_name, RELAY_OBJ_PATH, RELAY_IFACE,
            "ForwardBrowserBridgeOp", "sss",
            (op, args_json, selector_json))
    except _DBusCallError as e:
        return {"ok": False, "error": "relay_call_failed",
                "relay": relay_name, "dbus_name": e.dbus_name,
                "detail": e.detail}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "relay_call_failed",
                "relay": relay_name,
                "detail": f"{type(e).__name__}: {e}"[:200]}
    return _parse_reply(reply_str)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _select_bridges_by_ppid(names: list[str]) -> list[tuple[int, str]]:
    """Filter ``names`` to legitimate bridge names + sort by ppid.

    Same gate as the relay's ``_select_bridge``: the suffix after
    :data:`BRIDGE_NAME_PREFIX` must be all-digits, so a same-uid
    attacker that claims ``org.qdistro.BrowserBridge.evil`` is
    filtered out.
    """
    out: list[tuple[int, str]] = []
    for n in names:
        if not n.startswith(BRIDGE_NAME_PREFIX):
            continue
        if n.startswith(":"):
            continue
        suffix = n[len(BRIDGE_NAME_PREFIX):]
        if not suffix.isdigit():
            continue
        out.append((int(suffix), n))
    out.sort(key=lambda t: t[0])
    return out
