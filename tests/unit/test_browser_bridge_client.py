"""Unit tests for qdistro_browser_bridge_client.

The client wraps two D-Bus call paths: own-uid (session bus to a
local bridge) and cross-uid (system bus to a UserRelay). Both go
through a swappable :class:`_BaseDBusClient` so tests don't need
a real bus.
"""
from __future__ import annotations

import json

import pytest

import qdistro_browser_bridge_client as C


# ---- fake transport ------------------------------------------------------

class _FakeDBus(C._BaseDBusClient):
    """Records every call; replies to ListNames with a configurable
    list, and routes RequestTabs / ForwardBrowserBridgeOp to canned
    replies keyed by (bus, service, method)."""

    def __init__(self, *, names: dict[str, list[str]] | None = None,
                 by_call: dict[tuple, str] | None = None,
                 raise_call: BaseException | None = None,
                 raise_list: BaseException | None = None):
        self.calls: list[dict] = []
        self.list_calls: list[str] = []
        self._names = names or {}
        self._by_call = by_call or {}
        self._raise_call = raise_call
        self._raise_list = raise_list

    def list_names(self, bus):
        self.list_calls.append(bus)
        if self._raise_list is not None:
            raise self._raise_list
        return list(self._names.get(bus, []))

    def call(self, bus, service, object_path, interface, method,
             signature, body):
        self.calls.append({
            "bus": bus, "service": service, "object_path": object_path,
            "interface": interface, "method": method,
            "signature": signature, "body": body,
        })
        if self._raise_call is not None:
            raise self._raise_call
        key = (bus, service, method)
        if key in self._by_call:
            return self._by_call[key]
        return json.dumps({"ok": True})


@pytest.fixture(autouse=True)
def _reset_client():
    C.set_dbus_client(None)
    yield
    C.set_dbus_client(None)


# ---- call_bridge (same-uid) ----------------------------------------------

class TestCallBridgeSameUid:
    def test_no_bridges_returns_no_bridge_found(self):
        C.set_dbus_client(_FakeDBus(names={"SESSION": []}))
        reply = C.call_bridge("containers.list")
        assert reply["ok"] is False
        assert reply["error"] == "no_bridge_found"

    def test_picks_first_bridge_when_ppid_omitted(self):
        fake = _FakeDBus(
            names={"SESSION": [
                "org.qdistro.BrowserBridge.10000",
                "org.qdistro.BrowserBridge.999",
                ":1.4",  # unique name; ignored
                "org.freedesktop.systemd1",  # wrong prefix; ignored
            ]},
            by_call={
                ("SESSION", "org.qdistro.BrowserBridge.999",
                 "RequestTabs"): json.dumps({
                    "ok": True, "containers": [], "via": "999"}),
            },
        )
        C.set_dbus_client(fake)
        reply = C.call_bridge("containers.list")
        assert reply["via"] == "999"  # numeric sort: 999 < 10000
        assert fake.calls[0]["body"][0] == "containers.list"
        assert fake.calls[0]["body"][1] == "{}"

    def test_exact_ppid_match(self):
        fake = _FakeDBus(
            names={"SESSION": [
                "org.qdistro.BrowserBridge.1111",
                "org.qdistro.BrowserBridge.2222",
            ]},
            by_call={
                ("SESSION", "org.qdistro.BrowserBridge.2222",
                 "RequestTabs"): json.dumps({"ok": True, "via": "2222"}),
            },
        )
        C.set_dbus_client(fake)
        reply = C.call_bridge("qdistro.ping", ppid=2222)
        assert reply["via"] == "2222"

    def test_ppid_no_match_returns_no_bridge_found(self):
        fake = _FakeDBus(names={"SESSION": [
            "org.qdistro.BrowserBridge.1111"]})
        C.set_dbus_client(fake)
        reply = C.call_bridge("qdistro.ping", ppid=9999)
        assert reply["error"] == "no_bridge_found"
        assert reply["ppid"] == 9999
        assert fake.calls == []

    def test_skips_non_digit_suffix_bridge(self):
        """Mirror of the relay's impostor-name gate. A name like
        org.qdistro.BrowserBridge.evil is filtered out of the
        enumeration; only digit-suffix names are valid bridges."""
        fake = _FakeDBus(
            names={"SESSION": [
                "org.qdistro.BrowserBridge.evil",
                "org.qdistro.BrowserBridge.1234",
            ]},
            by_call={
                ("SESSION", "org.qdistro.BrowserBridge.1234",
                 "RequestTabs"): json.dumps({"ok": True, "via": "1234"}),
            },
        )
        C.set_dbus_client(fake)
        reply = C.call_bridge("containers.list")
        assert reply["via"] == "1234"

    def test_args_serialized_as_json(self):
        fake = _FakeDBus(
            names={"SESSION": ["org.qdistro.BrowserBridge.1"]},
            by_call={("SESSION", "org.qdistro.BrowserBridge.1",
                      "RequestTabs"): json.dumps({"ok": True})},
        )
        C.set_dbus_client(fake)
        C.call_bridge("containers.create",
                      {"name": "Banking", "color": "red"})
        sent_args = fake.calls[0]["body"][1]
        assert json.loads(sent_args) == {"name": "Banking", "color": "red"}

    def test_list_names_failure_returns_error(self):
        fake = _FakeDBus(raise_list=RuntimeError("bus not up"))
        C.set_dbus_client(fake)
        reply = C.call_bridge("containers.list")
        assert reply["error"] == "list_names_failed"
        assert "bus not up" in reply["detail"]

    def test_call_dbus_error_wrapped(self):
        fake = _FakeDBus(
            names={"SESSION": ["org.qdistro.BrowserBridge.1"]},
            raise_call=C._DBusCallError(
                dbus_name="org.freedesktop.DBus.Error.NoReply",
                detail="bridge went away"),
        )
        C.set_dbus_client(fake)
        reply = C.call_bridge("containers.list")
        assert reply["error"] == "bridge_call_failed"
        assert reply["dbus_name"] == "org.freedesktop.DBus.Error.NoReply"

    def test_call_generic_exception_wrapped(self):
        fake = _FakeDBus(
            names={"SESSION": ["org.qdistro.BrowserBridge.1"]},
            raise_call=ConnectionRefusedError("nope"),
        )
        C.set_dbus_client(fake)
        reply = C.call_bridge("containers.list")
        assert reply["error"] == "bridge_call_failed"
        assert "nope" in reply["detail"]

    def test_non_json_reply_becomes_bad_reply(self):
        fake = _FakeDBus(
            names={"SESSION": ["org.qdistro.BrowserBridge.1"]},
            by_call={("SESSION", "org.qdistro.BrowserBridge.1",
                      "RequestTabs"): "not-json"},
        )
        C.set_dbus_client(fake)
        reply = C.call_bridge("containers.list")
        assert reply["error"] == "bad_reply"


# ---- call_via_relay (cross-uid) ------------------------------------------

class TestCallViaRelay:
    def test_with_ppid(self):
        fake = _FakeDBus(by_call={
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "containers": [{"name": "Personal"}]}),
        })
        C.set_dbus_client(fake)
        reply = C.call_via_relay(
            "containers.list", uid=2000, ppid=1234)
        assert reply["ok"] is True
        assert reply["containers"][0]["name"] == "Personal"
        # Verify the relay was actually called with the right selector.
        call = fake.calls[0]
        assert call["bus"] == "SYSTEM"
        assert call["service"] == "org.qdistro.UserRelay.uid2000"
        assert call["method"] == "ForwardBrowserBridgeOp"
        op, args_json, sel_json = call["body"]
        assert op == "containers.list"
        assert json.loads(args_json) == {}
        assert json.loads(sel_json) == {"ppid": 1234}

    def test_with_any_bridge_sends_any_selector(self):
        fake = _FakeDBus(by_call={
            ("SYSTEM", "org.qdistro.UserRelay.uid3000",
             "ForwardBrowserBridgeOp"): json.dumps({"ok": True}),
        })
        C.set_dbus_client(fake)
        C.call_via_relay("qdistro.ping", uid=3000, any_bridge=True)
        sel = json.loads(fake.calls[0]["body"][2])
        assert sel == {"any": True}

    def test_neither_ppid_nor_any_rejected_locally(self):
        """The relay would also reject this, but we catch it before
        even doing the round-trip — clearer error and saves a call."""
        fake = _FakeDBus()
        C.set_dbus_client(fake)
        reply = C.call_via_relay("containers.list", uid=2000)
        assert reply["error"] == "bad_call"
        assert fake.calls == []

    def test_both_ppid_and_any_rejected_locally(self):
        fake = _FakeDBus()
        C.set_dbus_client(fake)
        reply = C.call_via_relay(
            "containers.list", uid=2000, ppid=1, any_bridge=True)
        assert reply["error"] == "bad_call"
        assert fake.calls == []

    def test_relay_call_failure_wrapped(self):
        fake = _FakeDBus(raise_call=C._DBusCallError(
            dbus_name="org.freedesktop.DBus.Error.ServiceUnknown",
            detail="no relay for that uid"))
        C.set_dbus_client(fake)
        reply = C.call_via_relay(
            "containers.list", uid=9999, any_bridge=True)
        assert reply["error"] == "relay_call_failed"
        assert reply["relay"] == "org.qdistro.UserRelay.uid9999"
        assert reply["dbus_name"] == "org.freedesktop.DBus.Error.ServiceUnknown"

    def test_relay_reply_bad_json_becomes_bad_reply(self):
        fake = _FakeDBus(by_call={
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): "not-json-at-all",
        })
        C.set_dbus_client(fake)
        reply = C.call_via_relay(
            "containers.list", uid=2000, any_bridge=True)
        assert reply["error"] == "bad_reply"

    def test_args_default_to_empty_object(self):
        fake = _FakeDBus(by_call={
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({"ok": True}),
        })
        C.set_dbus_client(fake)
        C.call_via_relay("containers.list", uid=2000, any_bridge=True)
        args_json = fake.calls[0]["body"][1]
        assert json.loads(args_json) == {}

    def test_args_passed_through(self):
        fake = _FakeDBus(by_call={
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({"ok": True}),
        })
        C.set_dbus_client(fake)
        C.call_via_relay(
            "containers.create",
            {"name": "Banking", "color": "red", "icon": "dollar"},
            uid=2000, any_bridge=True)
        sent = json.loads(fake.calls[0]["body"][1])
        assert sent == {"name": "Banking", "color": "red", "icon": "dollar"}


# ---- _select_bridges_by_ppid ---------------------------------------------

class TestSelectBridgesByPpid:
    def test_filters_non_qdistro_names(self):
        out = C._select_bridges_by_ppid([
            "org.freedesktop.systemd1",
            "com.canonical.Foo",
            "org.qdistro.BrowserBridge.42",
        ])
        assert out == [(42, "org.qdistro.BrowserBridge.42")]

    def test_filters_non_digit_suffix(self):
        out = C._select_bridges_by_ppid([
            "org.qdistro.BrowserBridge.evil",
            "org.qdistro.BrowserBridge.42",
            "org.qdistro.BrowserBridge.uid2000",
        ])
        assert out == [(42, "org.qdistro.BrowserBridge.42")]

    def test_filters_unique_colon_names(self):
        out = C._select_bridges_by_ppid([":1.42", ":1.99"])
        assert out == []

    def test_sorts_numerically(self):
        out = C._select_bridges_by_ppid([
            "org.qdistro.BrowserBridge.10000",
            "org.qdistro.BrowserBridge.999",
            "org.qdistro.BrowserBridge.1",
        ])
        assert [p for p, _ in out] == [1, 999, 10000]

    def test_empty(self):
        assert C._select_bridges_by_ppid([]) == []
