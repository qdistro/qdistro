"""bridge_adapter plugin: skeleton coverage.

Phase 10 will fill in the inbound/outbound D-Bus protocol. For now we
verify the plugin loads, stays inactive when no qdistro daemons exist,
and is idempotent across deactivate/activate cycles.
"""

import sys
import types


def test_plugin_loads():
    from qdbrowser.plugins.bridge_adapter import BridgeAdapterPlugin
    plug = BridgeAdapterPlugin()
    assert plug.name == "bridge_adapter"
    assert "bridge_adapter" in plug.capabilities
    assert plug.active is False


def test_stays_inactive_when_no_daemons(monkeypatch):
    """In a normal test environment the qdistro daemons are not running,
    so activate() must skip without raising."""
    import qdbrowser.plugins.bridge_adapter as ba
    monkeypatch.setattr(ba, "_daemons_available", lambda: False)
    plug = ba.BridgeAdapterPlugin()
    plug.activate(app_controller=object())
    assert plug.active is False
    # Deactivate is a no-op when never activated.
    plug.deactivate()
    assert plug.active is False


def test_activates_when_daemons_present(monkeypatch):
    import qdbrowser.plugins.bridge_adapter as ba
    monkeypatch.setattr(ba, "_daemons_available", lambda: True)
    monkeypatch.setattr(
        ba.BridgeAdapterPlugin, "_claim_bus_name", lambda self: True)
    plug = ba.BridgeAdapterPlugin()
    plug.activate(app_controller=object())
    assert plug.active is True
    plug.deactivate()
    assert plug.active is False


def test_explicit_enabled_true_bypasses_daemon_probe(
        fresh_config, monkeypatch):
    import qdbrowser.plugins.bridge_adapter as ba
    from qdbrowser.config import Config
    Config().set("plugins", "bridge_adapter", "enabled", True)
    monkeypatch.setattr(ba, "_daemons_available", lambda: False)
    monkeypatch.setattr(
        ba.BridgeAdapterPlugin, "_claim_bus_name", lambda self: True)
    plug = ba.BridgeAdapterPlugin()
    plug.activate(app_controller=object())
    assert plug.active is True
    plug.deactivate()


def test_explicit_enabled_false_blocks_direct_activation(
        fresh_config, monkeypatch):
    import qdbrowser.plugins.bridge_adapter as ba
    from qdbrowser.config import Config
    Config().set("plugins", "bridge_adapter", "enabled", False)
    monkeypatch.setattr(ba, "_daemons_available", lambda: True)
    monkeypatch.setattr(
        ba.BridgeAdapterPlugin, "_claim_bus_name", lambda self: True)
    plug = ba.BridgeAdapterPlugin()
    plug.activate(app_controller=object())
    assert plug.active is False


def test_daemons_available_returns_bool(monkeypatch):
    """The probe must never raise — any failure returns False so qdbrowser
    can run standalone."""
    from qdbrowser.plugins.bridge_adapter import _daemons_available
    # Real probe: in CI there's no session bus / no daemons. Result is
    # almost certainly False, but the contract is "returns a bool without
    # raising."
    result = _daemons_available()
    assert isinstance(result, bool)


def test_daemons_available_detects_system_bus_only(monkeypatch):
    from qdbrowser.plugins.bridge_adapter import _daemons_available

    class _FakeConn:
        def __init__(self, names):
            self._names = names

        def send_and_get_reply(self, *_args, **_kwargs):
            return types.SimpleNamespace(body=[self._names])

        def close(self):
            pass

    def _open_dbus_connection(bus):
        if bus == "SESSION":
            return _FakeConn([])
        if bus == "SYSTEM":
            return _FakeConn(["org.qdistro.AdminBroker1"])
        raise AssertionError(bus)

    fake_jeepney = types.ModuleType("jeepney")
    fake_jeepney.DBusAddress = lambda *_args, **_kwargs: object()
    fake_jeepney.new_method_call = lambda *_args, **_kwargs: object()
    fake_io = types.ModuleType("jeepney.io")
    fake_blocking = types.ModuleType("jeepney.io.blocking")
    fake_blocking.open_dbus_connection = _open_dbus_connection

    monkeypatch.setitem(sys.modules, "jeepney", fake_jeepney)
    monkeypatch.setitem(sys.modules, "jeepney.io", fake_io)
    monkeypatch.setitem(sys.modules, "jeepney.io.blocking", fake_blocking)

    assert _daemons_available() is True
