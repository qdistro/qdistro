"""DecideRequest broker-instance binding tests.

Broker request ids are per-INSTANCE and restart at 1 on every broker
start. A pending row is therefore only decidable against the unique bus
name (``:1.NN``) that reported it: after a restart, the same rid can
name an unrelated pending request on the new broker, and a fire-
and-forget retry would decide THAT request and report "applied".

The Qt admin app and the TUI bind decisions per-snapshot:

* ``get_pending`` resolves the owner BEFORE the call and fetches rows
  on a proxy bound to that unique name, then tags every row
  (``row["_owner"]`` / ``req.owner``) — the tag can never attribute a
  row to a broker that did not answer.
* ``decide`` requires the row's owner, refuses it as
  ``"instance-changed"`` when the owner is empty or no longer owns the
  well-known name, and sends DecideRequest on a proxy bound to the
  recorded unique name — never through ``_call``'s reconnect-retry.

These tests drive the real methods with fake buses; no D-Bus daemon or
Qt loop is needed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("dbus")
pytest.importorskip("PyQt6.QtCore")

# Promote admin_app/ and tui/ onto sys.path so test imports work.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "admin_app"))
sys.path.insert(0, str(_ROOT / "tui"))

import dbus  # noqa: E402
from qdistro_admin_app import BrokerBridge  # noqa: E402
from broker_client import DBusBrokerClient  # noqa: E402

ROW_A = {"id": 7, "uid": 2000, "pid": 1, "exe": "/old",
         "action": "old-action", "details": {}}
ROW_B = {"id": 7, "uid": 2000, "pid": 1, "exe": "/unrelated",
         "action": "new-action", "details": {}}


def _fake_bus(owner: str = ":1.42"):
    """A fake system bus: get_name_owner reports `owner` (mutable) and
    get_object returns a fresh mock proxy per unique name, recorded so
    tests can assert WHICH instance a call was bound to."""
    bus = MagicMock()
    bus.get_name_owner.return_value = owner
    bus.proxies: dict[str, MagicMock] = {}
    bus.get_object.side_effect = (
        lambda name, path: bus.proxies.setdefault(name, MagicMock(
            name=f"proxy({name})")))
    bus.proxy = lambda name: bus.proxies.setdefault(  # noqa: E731
        name, MagicMock(name=f"proxy({name})"))
    return bus


def _bridge(owner: str = ":1.42") -> BrokerBridge:
    """BrokerBridge minus its __init__ (no SystemBus, no Qt loop)."""
    bridge = BrokerBridge.__new__(BrokerBridge)
    bridge.bus = _fake_bus(owner)
    bridge._proxy = MagicMock()
    return bridge


def _client(owner: str = ":1.42") -> DBusBrokerClient:
    c = DBusBrokerClient(app=None)
    c._bus = _fake_bus(owner)
    c._proxy = MagicMock()
    return c


class TestAdminAppInstanceBinding:
    def test_rows_tagged_with_answering_owner(self):
        bridge = _bridge()
        bridge.bus.proxy(":1.42").GetPending.return_value = [ROW_A]
        rows = bridge.get_pending()
        # The fetch went to a proxy bound to the unique name — not to
        # the shared well-known-name proxy.
        bridge.bus.get_object.assert_called_once()
        assert bridge.bus.get_object.call_args[0][0] == ":1.42"
        assert rows[0]["_owner"] == ":1.42"
        bridge._proxy.GetPending.assert_not_called()

    def test_decide_lands_on_snapshot_owner(self):
        bridge = _bridge()
        bridge.bus.proxy(":1.42").GetPending.return_value = [ROW_A]
        rows = bridge.get_pending()
        bridge.bus.proxy(":1.42").DecideRequest.return_value = "applied"
        assert bridge.decide(rows[0]["id"], "allow", "once",
                             rows[0]["_owner"]) == "applied"
        bridge.bus.proxy(":1.42").DecideRequest.assert_called_once()
        # Never via the shared well-known-name proxy.
        bridge._proxy.DecideRequest.assert_not_called()

    def test_stale_row_refused_after_refresh_rebind(self):
        """Astra r2 repro: bulk snapshot taken under broker A survives a
        refresh that rebinds the client to restarted broker B — deciding
        the OLD row must be refused, not land on B's unrelated rid."""
        bridge = _bridge()
        bridge.bus.proxy(":1.42").GetPending.return_value = [ROW_A]
        snapshot_a = bridge.get_pending()

        # Broker restarts; a background refresh rebinds to ":1.99".
        bridge.bus.get_name_owner.return_value = ":1.99"
        bridge.bus.proxy(":1.99").GetPending.return_value = [ROW_B]
        bridge.get_pending()

        # The old row still carries A's unique name — the decision is
        # refused and nothing is sent anywhere.
        result = bridge.decide(snapshot_a[0]["id"], "allow", "once",
                               snapshot_a[0]["_owner"])
        assert result == "instance-changed"
        for proxy in bridge.bus.proxies.values():
            proxy.DecideRequest.assert_not_called()

    def test_decide_refused_with_missing_owner(self):
        bridge = _bridge()
        # A row without provenance (unreachable via get_pending, but
        # fail closed anyway).
        assert bridge.decide(7, "allow", "once", "") == "instance-changed"
        bridge.bus.get_object.assert_not_called()

    def test_decide_refused_when_name_unowned(self):
        bridge = _bridge()
        bridge.bus.get_name_owner.side_effect = dbus.DBusException(
            "name has no owner")
        assert bridge.decide(7, "allow", "once",
                             ":1.42") == "instance-changed"

    def test_get_pending_fails_closed_when_unowned(self):
        bridge = _bridge()
        bridge.bus.get_name_owner.return_value = ""
        with pytest.raises(dbus.DBusException):
            bridge.get_pending()


class TestTuiInstanceBinding:
    def test_rows_tagged_with_answering_owner(self):
        c = _client()
        c._bus.proxy(":1.42").GetPending.return_value = [ROW_A]
        rows = c.get_pending()
        assert c._bus.get_object.call_args[0][0] == ":1.42"
        assert rows[0].owner == ":1.42"
        c._proxy.GetPending.assert_not_called()

    def test_decide_lands_on_snapshot_owner(self):
        c = _client()
        c._bus.proxy(":1.42").GetPending.return_value = [ROW_A]
        rows = c.get_pending()
        c._bus.proxy(":1.42").DecideRequest.return_value = "applied"
        assert c.decide_request(rows[0].id, "allow", "once",
                                rows[0].owner) == "applied"
        c._bus.proxy(":1.42").DecideRequest.assert_called_once()
        c._proxy.DecideRequest.assert_not_called()

    def test_stale_row_refused_after_refresh_rebind(self):
        c = _client()
        c._bus.proxy(":1.42").GetPending.return_value = [ROW_A]
        snapshot_a = c.get_pending()

        c._bus.get_name_owner.return_value = ":1.99"
        c._bus.proxy(":1.99").GetPending.return_value = [ROW_B]
        c.get_pending()

        result = c.decide_request(snapshot_a[0].id, "allow", "once",
                                  snapshot_a[0].owner)
        assert result == "instance-changed"
        for proxy in c._bus.proxies.values():
            proxy.DecideRequest.assert_not_called()

    def test_decide_refused_with_missing_owner(self):
        c = _client()
        assert c.decide_request(7, "allow", "once",
                                "") == "instance-changed"
        c._bus.get_object.assert_not_called()

    def test_get_pending_fails_closed_when_unowned(self):
        """Unowned name must raise DBusException (the documented
        refresh-failure path), not a bare NameError — astra r3."""
        c = _client()
        c._bus.get_name_owner.return_value = ""
        with pytest.raises(dbus.DBusException):
            c.get_pending()
