"""Tests for P04 Phase-B: bridge_adapter ↔ broker / pwd wiring.

The qdbrowser bridge_adapter publishes ``org.qdistro.QdBrowser1`` on
the session bus and exposes tabs / page / downloads / media to admin
daemons. P04 extends the contract so:

  * the adapter probes for ``org.qdistro.Pwd1`` (a P03 commit shipped
    the probe; P04 adds the autofill-path gate on the same name);
  * the same-uid + cross-uid bridge clients round-trip pwd.fill
    through ``RequestTabs(op, args_json)`` cleanly.

Both surfaces are pure-python; we mock the D-Bus transport via the
:class:`_BaseDBusClient` indirection that already lives in
qdistro_browser_bridge_client.py and use :func:`set_dbus_client` to
swap the global transport.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# Load the bridge client module by path so tests don't require a
# qdistro install on $PYTHONPATH.
_CLIENT_MOD = (Path(__file__).resolve().parent.parent.parent
               / "browser_bridge"
               / "qdistro_browser_bridge_client.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge_client", _CLIENT_MOD)
client = importlib.util.module_from_spec(spec)
sys.modules["qdistro_browser_bridge_client"] = client
spec.loader.exec_module(client)


# ---------------------------------------------------------------------------
# Fake transport — records calls + answers from a programmable table.
# ---------------------------------------------------------------------------

class _FakeBus(client._BaseDBusClient):
    def __init__(self, names_by_bus=None, replies=None,
                 list_names_raises=False):
        self.names_by_bus = names_by_bus or {}
        self.replies = replies or {}
        self.calls = []
        self.list_names_raises = list_names_raises

    def list_names(self, bus):
        if self.list_names_raises:
            raise RuntimeError("simulated list_names failure")
        return list(self.names_by_bus.get(bus, []))

    def call(self, bus, service, object_path, interface,
             method, signature, body):
        key = (bus, service, interface, method)
        self.calls.append({
            "bus": bus, "service": service, "interface": interface,
            "method": method, "signature": signature, "body": body,
        })
        r = self.replies.get(key)
        if r is None:
            return json.dumps({"ok": False, "error": "no_reply"})
        if callable(r):
            out = r(body)
        else:
            out = dict(r)
        # call() returns the raw string body the bridge would put on
        # the wire; the public client.call_bridge wraps it in _parse_reply.
        return json.dumps(out)


@pytest.fixture(autouse=True)
def _reset_dbus_client():
    """Ensure no test leaks a fake transport across the boundary."""
    yield
    client.set_dbus_client(None)


# ---------------------------------------------------------------------------
# call_bridge (same-uid)
# ---------------------------------------------------------------------------

class TestCallBridgeSameUid:
    def test_finds_bridge_by_ppid(self):
        bus = _FakeBus(
            names_by_bus={"SESSION": ["org.qdistro.BrowserBridge.4242"]},
            replies={("SESSION", "org.qdistro.BrowserBridge.4242",
                     client.BRIDGE_IFACE, "RequestTabs"):
                     lambda body: {"ok": True, "echo": body[0]}})
        client.set_dbus_client(bus)
        out = client.call_bridge("tabs.list", {}, ppid=4242)
        assert out["ok"] is True
        assert out["echo"] == "tabs.list"

    def test_returns_error_when_no_bridge_name_present(self):
        bus = _FakeBus(names_by_bus={"SESSION": []})
        client.set_dbus_client(bus)
        out = client.call_bridge("tabs.list", {})
        assert out["ok"] is False
        assert out["error"] == "no_bridge_found"

    def test_pwd_fill_round_trip(self):
        captured_args = {}

        def reply(body):
            op, args_json = body
            captured_args["op"] = op
            captured_args["args"] = json.loads(args_json)
            return {"ok": True, "credentials": [
                {"username": "alice", "password": "s3cret"}]}

        bus = _FakeBus(
            names_by_bus={"SESSION": ["org.qdistro.BrowserBridge.99"]},
            replies={("SESSION", "org.qdistro.BrowserBridge.99",
                     client.BRIDGE_IFACE, "RequestTabs"): reply})
        client.set_dbus_client(bus)
        out = client.call_bridge(
            "pwd.fill",
            {"url": "https://example.com/",
             "intent_token": {"request_id": "r1", "ts": 1.0,
                              "op": "pwd.fill", "hmac": "0" * 64}},
            ppid=99)
        assert out["ok"] is True
        assert captured_args["op"] == "pwd.fill"
        assert captured_args["args"]["url"] == "https://example.com/"
        # Intent token is forwarded verbatim, not stripped.
        tok = captured_args["args"]["intent_token"]
        assert tok["op"] == "pwd.fill"

    def test_bridge_error_propagates_in_reply_body(self):
        bus = _FakeBus(
            names_by_bus={"SESSION": ["org.qdistro.BrowserBridge.42"]},
            replies={("SESSION", "org.qdistro.BrowserBridge.42",
                     client.BRIDGE_IFACE, "RequestTabs"):
                     {"ok": False, "error": "parent_not_allowed"}})
        client.set_dbus_client(bus)
        out = client.call_bridge("tabs.list", {}, ppid=42)
        assert out["ok"] is False
        assert out["error"] == "parent_not_allowed"

    def test_no_ppid_picks_lowest(self):
        bus = _FakeBus(
            names_by_bus={"SESSION": ["org.foo.Bar",
                                       "org.qdistro.BrowserBridge.7",
                                       "org.qdistro.BrowserBridge.99",
                                       "org.baz.Quux"]},
            replies={("SESSION", "org.qdistro.BrowserBridge.7",
                     client.BRIDGE_IFACE, "RequestTabs"):
                     {"ok": True, "picked": 7},
                     ("SESSION", "org.qdistro.BrowserBridge.99",
                     client.BRIDGE_IFACE, "RequestTabs"):
                     {"ok": True, "picked": 99}})
        client.set_dbus_client(bus)
        out = client.call_bridge("tabs.list", {})
        assert out["ok"] is True
        assert out["picked"] == 7

    def test_explicit_ppid_filters_correctly(self):
        bus = _FakeBus(
            names_by_bus={"SESSION": ["org.qdistro.BrowserBridge.7",
                                       "org.qdistro.BrowserBridge.99"]},
            replies={("SESSION", "org.qdistro.BrowserBridge.99",
                     client.BRIDGE_IFACE, "RequestTabs"):
                     {"ok": True, "picked": 99}})
        client.set_dbus_client(bus)
        out = client.call_bridge("tabs.list", {}, ppid=99)
        assert out["ok"] is True
        assert out["picked"] == 99

    def test_list_names_failure_surfaces_error(self):
        bus = _FakeBus(list_names_raises=True)
        client.set_dbus_client(bus)
        out = client.call_bridge("tabs.list", {})
        assert out["ok"] is False
        assert out["error"] == "list_names_failed"


# ---------------------------------------------------------------------------
# call_via_relay (cross-uid)
# ---------------------------------------------------------------------------

class TestCallViaRelay:
    def test_routes_to_system_bus_relay(self):
        body_seen = {}

        def reply(body):
            op, args_json, selector_json = body
            body_seen["op"] = op
            body_seen["args"] = json.loads(args_json)
            body_seen["selector"] = json.loads(selector_json)
            return {"ok": True}

        bus = _FakeBus(replies={
            ("SYSTEM", "com.qdistro.UserRelay.uid2000",
             client.RELAY_IFACE, "ForwardBrowserBridgeOp"): reply})
        client.set_dbus_client(bus)
        out = client.call_via_relay(
            "tabs.open", {"url": "https://example.com/"},
            uid=2000, ppid=1234)
        assert out["ok"] is True
        assert body_seen["op"] == "tabs.open"
        assert body_seen["args"]["url"] == "https://example.com/"
        assert body_seen["selector"] == {"ppid": 1234}

    def test_any_bridge_selector(self):
        body_seen = {}

        def reply(body):
            body_seen["selector"] = json.loads(body[2])
            return {"ok": True}

        bus = _FakeBus(replies={
            ("SYSTEM", "com.qdistro.UserRelay.uid3000",
             client.RELAY_IFACE, "ForwardBrowserBridgeOp"): reply})
        client.set_dbus_client(bus)
        out = client.call_via_relay(
            "tabs.list", uid=3000, any_bridge=True)
        assert out["ok"] is True
        assert body_seen["selector"] == {"any": True}

    def test_both_ppid_and_any_rejected(self):
        bus = _FakeBus()
        client.set_dbus_client(bus)
        out = client.call_via_relay("tabs.list", uid=2000,
                                      ppid=42, any_bridge=True)
        assert out["ok"] is False
        assert out["error"] == "bad_call"

    def test_neither_ppid_nor_any_rejected(self):
        bus = _FakeBus()
        client.set_dbus_client(bus)
        out = client.call_via_relay("tabs.list", uid=2000)
        assert out["ok"] is False
        assert out["error"] == "bad_call"


# ---------------------------------------------------------------------------
# Phase-B health-check: the qdbrowser bridge adapter probes Pwd1.
# ---------------------------------------------------------------------------

class TestDaemonNamesAdapter:
    """Spec assertion: bridge_adapter._DAEMON_NAMES contains the pwd
    daemon's well-known name so qdbrowser flips active when pwd is
    on the bus."""

    def test_pwd1_name_present_in_adapter_probe(self):
        adapter_path = (Path(__file__).resolve().parent.parent.parent
                        .parent / "qdbrowser" / "qdbrowser"
                        / "plugins" / "bridge_adapter.py")
        if not adapter_path.is_file():
            pytest.skip("qdbrowser/qdbrowser/plugins/bridge_adapter.py "
                        "not present in this checkout")
        text = adapter_path.read_text()
        assert "org.qdistro.Pwd1" in text
