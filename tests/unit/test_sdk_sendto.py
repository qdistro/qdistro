"""Tests for the SDK surface added in Phase 3.

`qdistro_app.list_receivers` and `qdistro_app.send_to` are thin
wrappers around broker calls. These tests assert the wire mapping:
right destination, right interface, right typed args, right return
coercion. A live broker is exercised in-VM; here we fake the bus
to keep the tests in-process.

`qdistro_app.AppReceiver` is also covered — it claims a bus name,
registers itself at a fixed path, and dispatches Receive to a
user callback.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


pytest.importorskip("dbus")
import dbus  # noqa: E402

# Make the SDK importable.
_SDK = Path(__file__).resolve().parents[1] / "sdk"
sys.path.insert(0, str(_SDK))

import qdistro_app  # noqa: E402


# --- fake bus+broker -------------------------------------------------------

class _FakeBrokerProxy:
    """Capture every method call as (method, args, kwargs)."""

    def __init__(self, responses: dict | None = None):
        self.calls: list[tuple] = []
        self._responses = responses or {}

    def _handle(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return self._responses.get(name)

    def __getattr__(self, name):
        # Only methods the SDK uses.
        if name in ("RequestPermission", "WaitForDecision",
                    "ListReceivers", "RelayMessage"):
            return lambda *a, **kw: self._handle(name, *a, **kw)
        raise AttributeError(name)


class _FakeSystemBus:
    def __init__(self, proxy):
        self._proxy = proxy

    def get_object(self, name, path):  # noqa: ARG002
        return self._proxy


@pytest.fixture
def fake_broker(monkeypatch):
    proxy = _FakeBrokerProxy()
    monkeypatch.setattr(qdistro_app.dbus, "SystemBus",
                        lambda: _FakeSystemBus(proxy))
    return proxy


# --- list_receivers --------------------------------------------------------

class TestListReceivers:
    def test_calls_broker_on_system_bus(self, fake_broker):
        fake_broker._responses["ListReceivers"] = []
        qdistro_app.list_receivers()
        assert fake_broker.calls[0][0] == "ListReceivers"
        assert fake_broker.calls[0][2]["dbus_interface"] == \
            "com.qdistro.AdminBroker1"

    def test_coerces_row_types(self, fake_broker):
        # dbus typically hands us `dbus.Int32 / dbus.String` wrappers;
        # the SDK must return plain ints and strs.
        fake_broker._responses["ListReceivers"] = [
            (dbus.Int32(2000), dbus.String("com.qdistro.X"),
             dbus.String("X")),
            (3000, "com.qdistro.Y", "Y"),  # raw types also fine
        ]
        rows = qdistro_app.list_receivers()
        assert rows == [
            (2000, "com.qdistro.X", "X"),
            (3000, "com.qdistro.Y", "Y"),
        ]
        for uid, svc, friendly in rows:
            assert isinstance(uid, int)
            assert isinstance(svc, str)
            assert isinstance(friendly, str)

    def test_empty_list(self, fake_broker):
        fake_broker._responses["ListReceivers"] = []
        assert qdistro_app.list_receivers() == []


# --- send_to ---------------------------------------------------------------

class TestSendTo:
    def test_passes_typed_args(self, fake_broker):
        fake_broker._responses["RelayMessage"] = None
        ok = qdistro_app.send_to(3000, "com.qdistro.StubNotepad.uid3000",
                                  "text/plain", "hi")
        assert ok is True
        assert len(fake_broker.calls) == 1
        name, args, kwargs = fake_broker.calls[0]
        assert name == "RelayMessage"
        # First four are positional: target_uid, service, kind, payload.
        assert int(args[0]) == 3000
        assert str(args[1]) == "com.qdistro.StubNotepad.uid3000"
        assert str(args[2]) == "text/plain"
        assert str(args[3]) == "hi"

    def test_uses_admin_broker_interface(self, fake_broker):
        fake_broker._responses["RelayMessage"] = None
        qdistro_app.send_to(3000, "com.qdistro.X", "k", "v")
        _name, _args, kwargs = fake_broker.calls[0]
        assert kwargs["dbus_interface"] == "com.qdistro.AdminBroker1"

    def test_custom_timeout_propagates(self, fake_broker):
        fake_broker._responses["RelayMessage"] = None
        qdistro_app.send_to(3000, "com.qdistro.X", "k", "v", timeout=42.0)
        _name, _args, kwargs = fake_broker.calls[0]
        assert kwargs["timeout"] == pytest.approx(42.0)

    def test_dbus_exception_bubbles(self, fake_broker):
        class _Proxy(_FakeBrokerProxy):
            def RelayMessage(self, *_a, **_kw):
                raise dbus.DBusException(
                    "admin denied relay",
                    name="com.qdistro.AdminBroker1.Denied")
        # Replace the monkey-patched proxy with a raising one.
        import qdistro_app as qa
        bus = _FakeSystemBus(_Proxy())
        qa.dbus.SystemBus = lambda: bus  # type: ignore[assignment]
        with pytest.raises(dbus.DBusException):
            qa.send_to(3000, "com.qdistro.X", "k", "v")


# --- request (regression, should still work after Phase 3 SDK changes) ---

class TestRequestBackCompat:
    def test_request_translates_to_two_calls(self, fake_broker):
        # RequestPermission returns an rid, then WaitForDecision
        # returns the bool.
        fake_broker._responses["RequestPermission"] = 42
        fake_broker._responses["WaitForDecision"] = True
        assert qdistro_app.request("act", {"k": "v"}) is True
        names = [c[0] for c in fake_broker.calls]
        assert names == ["RequestPermission", "WaitForDecision"]
        # rid passed into WaitForDecision matches.
        wait_args = fake_broker.calls[1][1]
        assert int(wait_args[0]) == 42

    def test_request_deny_returns_false(self, fake_broker):
        fake_broker._responses["RequestPermission"] = 7
        fake_broker._responses["WaitForDecision"] = False
        assert qdistro_app.request("act") is False


# --- AppReceiver ----------------------------------------------------------

class _StubBus:
    """Minimal SessionBus-shaped stub for AppReceiver tests. Collects
    BusName claims and exposes object-registration hooks."""

    def __init__(self):
        self.claimed_names: list[str] = []

    # dbus.service.BusName is called as BusName(name, bus, do_not_queue=True).
    # We can't easily intercept that constructor here, so the test
    # monkeypatches dbus.service.BusName and dbus.service.Object's init.


class TestAppReceiver:
    def test_claims_service_name_and_dispatches(self, monkeypatch):
        claimed: list[str] = []

        class _FakeBusName:
            def __init__(self, name, bus, do_not_queue=False):  # noqa: ARG002
                claimed.append(str(name))

        monkeypatch.setattr(qdistro_app.dbus.service, "BusName",
                            _FakeBusName)

        # dbus.service.Object.__init__ attaches to a connection — skip
        # it; AppReceiver only needs self._bus_name + self._on_receive +
        # self._service_name to be set, which our __init__ does.
        monkeypatch.setattr(qdistro_app.dbus.service.Object, "__init__",
                            lambda self, bus, path: None)

        got: list[tuple[str, str]] = []
        rx = qdistro_app.AppReceiver(
            "com.qdistro.StubNotepad.uid2000",
            on_receive=lambda k, p: got.append((k, p)),
            bus=object())  # real object not needed with monkeypatched init

        assert claimed == ["com.qdistro.StubNotepad.uid2000"]
        assert rx.service_name == "com.qdistro.StubNotepad.uid2000"
        rx.Receive("text/plain", "hello")
        assert got == [("text/plain", "hello")]

    def test_receive_coerces_to_str(self, monkeypatch):
        class _FakeBusName:
            def __init__(self, *a, **kw): pass
        monkeypatch.setattr(qdistro_app.dbus.service, "BusName", _FakeBusName)
        monkeypatch.setattr(qdistro_app.dbus.service.Object, "__init__",
                            lambda self, bus, path: None)

        captured: list[tuple] = []
        rx = qdistro_app.AppReceiver(
            "com.qdistro.Foo", on_receive=lambda k, p: captured.append(
                (type(k).__name__, type(p).__name__)),
            bus=object())
        # Even if dbus hands us dbus.String, callback sees `str`.
        rx.Receive(dbus.String("k"), dbus.String("v"))
        assert captured == [("str", "str")]

    def test_get_last_received_empty_by_default(self, monkeypatch):
        class _FakeBusName:
            def __init__(self, *a, **kw): pass
        monkeypatch.setattr(qdistro_app.dbus.service, "BusName", _FakeBusName)
        monkeypatch.setattr(qdistro_app.dbus.service.Object, "__init__",
                            lambda self, bus, path: None)
        rx = qdistro_app.AppReceiver(
            "com.qdistro.Foo", on_receive=lambda k, p: None,
            bus=object())
        assert rx.GetLastReceived() == ""
        assert rx.last_received is None

    def test_get_last_received_formats_latest(self, monkeypatch):
        class _FakeBusName:
            def __init__(self, *a, **kw): pass
        monkeypatch.setattr(qdistro_app.dbus.service, "BusName", _FakeBusName)
        monkeypatch.setattr(qdistro_app.dbus.service.Object, "__init__",
                            lambda self, bus, path: None)
        rx = qdistro_app.AppReceiver(
            "com.qdistro.Foo", on_receive=lambda k, p: None,
            bus=object())
        rx.Receive(dbus.String("text/plain"), dbus.String("first"))
        assert rx.GetLastReceived() == "[text/plain] first"
        # Last-wins — plugins tracking "last received" (GetDocument
        # behaviour is on the app if it wants accumulation).
        rx.Receive(dbus.String("text/markdown"), dbus.String("second"))
        assert rx.GetLastReceived() == "[text/markdown] second"
        assert rx.last_received == ("text/markdown", "second")

    def test_get_last_received_updates_even_when_callback_raises(
            self, monkeypatch):
        class _FakeBusName:
            def __init__(self, *a, **kw): pass
        monkeypatch.setattr(qdistro_app.dbus.service, "BusName", _FakeBusName)
        monkeypatch.setattr(qdistro_app.dbus.service.Object, "__init__",
                            lambda self, bus, path: None)

        def _boom(k, p):
            raise RuntimeError("callback broken")

        rx = qdistro_app.AppReceiver(
            "com.qdistro.Foo", on_receive=_boom, bus=object())
        with pytest.raises(RuntimeError):
            rx.Receive("text/plain", "payload")
        # Probe still sees what landed even though the user callback
        # blew up — matters for test-debug when something is wrong
        # with the app-side delivery code.
        assert rx.GetLastReceived() == "[text/plain] payload"


# --- constants pinned ----------------------------------------------------

def test_app1_iface_constant():
    assert qdistro_app.APP1_IFACE == "com.qdistro.App1"


def test_default_timeout_is_long_enough_for_admin():
    # 10 minutes is the documented choice — see the docstring.
    assert qdistro_app.DEFAULT_TIMEOUT_S >= 300
