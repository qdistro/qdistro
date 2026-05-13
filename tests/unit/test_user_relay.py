"""Tests for the UserRelay daemon class (session-side logic).

The relay's methods are called from two bus connections in
production (system-bus incoming from the broker, session-bus
outgoing to receivers). Here we instantiate `UserRelay` with a
fake bus so we can exercise the logic without standing up a real
session bus.

Scope:
- `ListLocalReceivers` filters by prefix, excludes its own name,
  ignores unique-connection names (`:1.N`), and surfaces the
  friendly-name derivation.
- `Forward` rejects non-`com.qdistro.*` targets and excluded
  names, and wraps peer failures in a relay-side error.
- `_friendly_name` corner cases not covered in test_sendto.py.

End-to-end forwarding (to a live QPlainTextEdit-backed receiver)
is covered by the in-VM GUI scenarios.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


# The relay imports dbus at top-level; skip the whole module on
# hosts without dbus-python.
pytest.importorskip("dbus")

# Make qdistro_user_relay importable (pyproject adds user_relay/
# to pythonpath; belt-and-braces here too).
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "user_relay"))

import dbus  # noqa: E402
import qdistro_user_relay as R  # noqa: E402


# --- fake dbus bus --------------------------------------------------------

class _FakeDBusIface:
    """Stand-in for `org.freedesktop.DBus` proxy used by the relay's
    ListNames call. Returns whatever the test set on the owning bus.
    """

    def __init__(self, bus: "_FakeBus"):
        self._bus = bus

    def ListNames(self):
        return list(self._bus.names)


class _FakeReceiver:
    """Minimal stand-in for a receiver object — records Receive calls."""

    def __init__(self, raise_exc: BaseException | None = None):
        self.calls: list[tuple[str, str]] = []
        self.raise_exc = raise_exc

    def Receive(self, kind, payload, dbus_interface=None):  # noqa: ARG002
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append((str(kind), str(payload)))


class _FakeBus:
    """Fake session bus: owns a `.names` list (for ListNames) and a
    `.objects` dict (for get_object(name, path))."""

    def __init__(self, names: list[str] | None = None):
        self.names = list(names or [])
        self.objects: dict[tuple[str, str], object] = {}

    def get_object(self, name: str, path: str):
        if name == "org.freedesktop.DBus":
            return _FakeDBusIface(self)
        if (name, path) in self.objects:
            return self.objects[(name, path)]
        raise dbus.DBusException(
            f"no such name {name!r}",
            name="org.freedesktop.DBus.Error.ServiceUnknown")


# patch dbus.Interface so the relay's `ListNames` path resolves through
# our fake cleanly — the real dbus.Interface wraps the proxy in a
# method-dispatch layer; our fake proxy already has ListNames defined.
@pytest.fixture(autouse=True)
def _patch_dbus_interface(monkeypatch):
    monkeypatch.setattr(dbus, "Interface", lambda proxy, _iface: proxy)


# Bypass dbus.service.Object.__init__ — constructing it for real
# would try to attach to an actual connection. UserRelay's methods
# only touch self._bus which we inject.
class _StubUserRelay(R.UserRelay):
    def __init__(self, bus):
        self._bus = bus


# --- ListLocalReceivers ---------------------------------------------------

class TestListLocalReceivers:
    def test_returns_service_and_friendly_pairs(self):
        bus = _FakeBus(names=[
            "org.freedesktop.DBus",
            ":1.4",
            "com.qdistro.StubNotepad.uid3000",
            "com.qdistro.Foo.Bar",
        ])
        relay = _StubUserRelay(bus)
        rows = relay.ListLocalReceivers()
        as_tuples = [(str(a), str(b)) for a, b in rows]
        assert ("com.qdistro.StubNotepad.uid3000", "StubNotepad") in as_tuples
        assert ("com.qdistro.Foo.Bar", "Foo.Bar") in as_tuples

    def test_filters_unique_colon_names(self):
        bus = _FakeBus(names=[":1.1", ":1.2", ":1.99"])
        relay = _StubUserRelay(bus)
        assert list(relay.ListLocalReceivers()) == []

    def test_filters_non_qdistro_names(self):
        bus = _FakeBus(names=["org.freedesktop.systemd1", "com.other.Thing"])
        relay = _StubUserRelay(bus)
        assert list(relay.ListLocalReceivers()) == []

    def test_excludes_relay_own_name(self):
        bus = _FakeBus(names=[R.BUS_NAME, "com.qdistro.StubNotepad.uid2000"])
        relay = _StubUserRelay(bus)
        rows = [str(r[0]) for r in relay.ListLocalReceivers()]
        assert R.BUS_NAME not in rows
        assert "com.qdistro.StubNotepad.uid2000" in rows

    def test_excludes_stub_sender(self):
        bus = _FakeBus(names=[
            "com.qdistro.StubSender",
            "com.qdistro.StubNotepad.uid2000",
        ])
        relay = _StubUserRelay(bus)
        rows = [str(r[0]) for r in relay.ListLocalReceivers()]
        assert "com.qdistro.StubSender" not in rows
        assert "com.qdistro.StubNotepad.uid2000" in rows

    def test_empty_bus(self):
        relay = _StubUserRelay(_FakeBus(names=[]))
        assert list(relay.ListLocalReceivers()) == []


# --- Forward --------------------------------------------------------------

class TestForward:
    def test_happy_path_invokes_receiver(self):
        recv = _FakeReceiver()
        bus = _FakeBus()
        bus.objects[("com.qdistro.StubNotepad.uid3000",
                     R.APP1_OBJ_PATH)] = recv
        relay = _StubUserRelay(bus)
        relay.Forward("com.qdistro.StubNotepad.uid3000", "text/plain", "hi")
        assert recv.calls == [("text/plain", "hi")]

    def test_rejects_non_qdistro_target(self):
        relay = _StubUserRelay(_FakeBus())
        with pytest.raises(dbus.DBusException) as ei:
            relay.Forward("org.freedesktop.DBus", "k", "v")
        assert "non-qdistro" in str(ei.value).lower() or \
               "BadTarget" in ei.value.get_dbus_name()

    def test_rejects_excluded_names(self):
        relay = _StubUserRelay(_FakeBus())
        for bad in (R.BUS_NAME, "com.qdistro.AdminBroker1",
                    "com.qdistro.StubSender"):
            with pytest.raises(dbus.DBusException):
                relay.Forward(bad, "k", "v")

    def test_wraps_receiver_exception(self):
        recv = _FakeReceiver(raise_exc=RuntimeError("kaboom"))
        bus = _FakeBus()
        bus.objects[("com.qdistro.StubNotepad.uid2000",
                     R.APP1_OBJ_PATH)] = recv
        relay = _StubUserRelay(bus)
        with pytest.raises(dbus.DBusException) as ei:
            relay.Forward("com.qdistro.StubNotepad.uid2000", "k", "v")
        assert "ForwardFailed" in ei.value.get_dbus_name() \
            or "kaboom" in str(ei.value)

    def test_passes_through_dbus_exception_from_receiver(self):
        """If the target raised a DBusException itself (e.g. its
        Receive rejected), the relay should not double-wrap — the
        caller needs the real name to decide how to handle it."""
        inner = dbus.DBusException("no", name="com.example.Nope")
        recv = _FakeReceiver(raise_exc=inner)
        bus = _FakeBus()
        bus.objects[("com.qdistro.StubNotepad.uid2000",
                     R.APP1_OBJ_PATH)] = recv
        relay = _StubUserRelay(bus)
        with pytest.raises(dbus.DBusException) as ei:
            relay.Forward("com.qdistro.StubNotepad.uid2000", "k", "v")
        assert ei.value.get_dbus_name() == "com.example.Nope"


# --- _friendly_name edge cases --------------------------------------------

class TestFriendlyNameEdgeCases:
    def test_empty_string(self):
        assert R._friendly_name("") == ""

    def test_only_prefix(self):
        # com.qdistro. alone: prefix strip → empty; no dot to rsplit on
        # → returned as-is.
        assert R._friendly_name("com.qdistro.") == ""

    def test_uid_without_digits(self):
        assert R._friendly_name("com.qdistro.Foo.uid") == "Foo.uid"

    def test_uid_with_mixed(self):
        assert R._friendly_name("com.qdistro.Foo.uid3000abc") == "Foo.uid3000abc"

    def test_nested_components(self):
        assert R._friendly_name("com.qdistro.A.B.C") == "A.B.C"


# --- EXCLUDED_NAMES invariants --------------------------------------------

class TestRelayConstants:
    def test_excluded_contains_own(self):
        assert R.BUS_NAME in R.EXCLUDED_NAMES

    def test_excluded_contains_broker(self):
        # Broker is on the system bus, not this session bus, but we're
        # defensive about the name appearing anywhere.
        assert "com.qdistro.AdminBroker1" in R.EXCLUDED_NAMES

    def test_excluded_contains_sender(self):
        assert "com.qdistro.StubSender" in R.EXCLUDED_NAMES

    def test_prefix_is_com_qdistro_dot(self):
        assert R.RECEIVER_PREFIX == "com.qdistro."

    def test_system_bus_name_fmt_interpolates_uid(self):
        assert R.SYSTEM_BUS_NAME_FMT.format(uid=2000) == \
            "com.qdistro.UserRelay.uid2000"
