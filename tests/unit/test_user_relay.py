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
- `Forward` rejects non-`org.qdistro.*` targets and excluded
  names, and wraps peer failures in a relay-side error.
- `ForwardBrowserBridgeOp` selects the right bridge name, calls
  RequestTabs, and surfaces failures as JSON ok:false rather
  than D-Bus exceptions.
- `_friendly_name` corner cases not covered in test_sendto.py.

End-to-end forwarding (to a live QPlainTextEdit-backed receiver)
is covered by the in-VM GUI scenarios.
"""
from __future__ import annotations

import json
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


class _FakeBridge:
    """Stand-in for an org.qdistro.BrowserBridge.<ppid> object.

    Records every RequestTabs call and returns a canned JSON reply.
    Tests vary the reply per call by mutating `next_reply`.
    """

    def __init__(self, next_reply: dict | None = None,
                 raise_exc: BaseException | None = None):
        self.calls: list[tuple[str, str]] = []
        self.next_reply = next_reply or {"ok": True}
        self.raise_exc = raise_exc

    def RequestTabs(self, op, args_json, dbus_interface=None):  # noqa: ARG002
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append((str(op), str(args_json)))
        return json.dumps(self.next_reply)


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
            "org.qdistro.StubNotepad.uid3000",
            "org.qdistro.Foo.Bar",
        ])
        relay = _StubUserRelay(bus)
        rows = relay.ListLocalReceivers()
        as_tuples = [(str(a), str(b)) for a, b in rows]
        assert ("org.qdistro.StubNotepad.uid3000", "StubNotepad") in as_tuples
        assert ("org.qdistro.Foo.Bar", "Foo.Bar") in as_tuples

    def test_filters_unique_colon_names(self):
        bus = _FakeBus(names=[":1.1", ":1.2", ":1.99"])
        relay = _StubUserRelay(bus)
        assert list(relay.ListLocalReceivers()) == []

    def test_filters_non_qdistro_names(self):
        bus = _FakeBus(names=["org.freedesktop.systemd1", "com.other.Thing"])
        relay = _StubUserRelay(bus)
        assert list(relay.ListLocalReceivers()) == []

    def test_excludes_relay_own_name(self):
        bus = _FakeBus(names=[R.BUS_NAME, "org.qdistro.StubNotepad.uid2000"])
        relay = _StubUserRelay(bus)
        rows = [str(r[0]) for r in relay.ListLocalReceivers()]
        assert R.BUS_NAME not in rows
        assert "org.qdistro.StubNotepad.uid2000" in rows

    def test_excludes_stub_sender(self):
        bus = _FakeBus(names=[
            "org.qdistro.StubSender",
            "org.qdistro.StubNotepad.uid2000",
        ])
        relay = _StubUserRelay(bus)
        rows = [str(r[0]) for r in relay.ListLocalReceivers()]
        assert "org.qdistro.StubSender" not in rows
        assert "org.qdistro.StubNotepad.uid2000" in rows

    def test_empty_bus(self):
        relay = _StubUserRelay(_FakeBus(names=[]))
        assert list(relay.ListLocalReceivers()) == []


# --- Forward --------------------------------------------------------------

class TestForward:
    def test_happy_path_invokes_receiver(self):
        recv = _FakeReceiver()
        bus = _FakeBus()
        bus.objects[("org.qdistro.StubNotepad.uid3000",
                     R.APP1_OBJ_PATH)] = recv
        relay = _StubUserRelay(bus)
        relay.Forward("org.qdistro.StubNotepad.uid3000", "text/plain", "hi")
        assert recv.calls == [("text/plain", "hi")]

    def test_rejects_non_qdistro_target(self):
        relay = _StubUserRelay(_FakeBus())
        with pytest.raises(dbus.DBusException) as ei:
            relay.Forward("org.freedesktop.DBus", "k", "v")
        assert "non-qdistro" in str(ei.value).lower() or \
               "BadTarget" in ei.value.get_dbus_name()

    def test_rejects_excluded_names(self):
        relay = _StubUserRelay(_FakeBus())
        for bad in (R.BUS_NAME, "org.qdistro.AdminBroker1",
                    "org.qdistro.StubSender"):
            with pytest.raises(dbus.DBusException):
                relay.Forward(bad, "k", "v")

    def test_wraps_receiver_exception(self):
        recv = _FakeReceiver(raise_exc=RuntimeError("kaboom"))
        bus = _FakeBus()
        bus.objects[("org.qdistro.StubNotepad.uid2000",
                     R.APP1_OBJ_PATH)] = recv
        relay = _StubUserRelay(bus)
        with pytest.raises(dbus.DBusException) as ei:
            relay.Forward("org.qdistro.StubNotepad.uid2000", "k", "v")
        assert "ForwardFailed" in ei.value.get_dbus_name() \
            or "kaboom" in str(ei.value)

    def test_passes_through_dbus_exception_from_receiver(self):
        """If the target raised a DBusException itself (e.g. its
        Receive rejected), the relay should not double-wrap — the
        caller needs the real name to decide how to handle it."""
        inner = dbus.DBusException("no", name="com.example.Nope")
        recv = _FakeReceiver(raise_exc=inner)
        bus = _FakeBus()
        bus.objects[("org.qdistro.StubNotepad.uid2000",
                     R.APP1_OBJ_PATH)] = recv
        relay = _StubUserRelay(bus)
        with pytest.raises(dbus.DBusException) as ei:
            relay.Forward("org.qdistro.StubNotepad.uid2000", "k", "v")
        assert ei.value.get_dbus_name() == "com.example.Nope"


# --- ForwardBrowserBridgeOp -----------------------------------------------

class TestForwardBrowserBridgeOp:
    """Option-B cross-uid routing for browser-bridge ops. The relay
    runs as the target user; cross-uid callers reach it on the system
    bus and the relay turns around to call the bridge on the user's
    session bus. See qdistro/doc/firefox-containers.md."""

    def _relay(self, *, bridges: dict[int, _FakeBridge] | None = None):
        bridges = bridges or {}
        bus = _FakeBus(names=[
            f"org.qdistro.BrowserBridge.{ppid}" for ppid in bridges
        ])
        for ppid, bridge in bridges.items():
            bus.objects[
                (f"org.qdistro.BrowserBridge.{ppid}",
                 R.BRIDGE_OBJ_PATH)
            ] = bridge
        return _StubUserRelay(bus), bridges

    def test_any_selector_forwards_to_first_bridge(self):
        bridge = _FakeBridge(next_reply={
            "ok": True,
            "containers": [{"cookie_store_id": "firefox-container-1",
                            "name": "Personal"}],
        })
        relay, _ = self._relay(bridges={4321: bridge})
        reply_json = relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"any": true}')
        reply = json.loads(reply_json)
        assert reply["ok"] is True
        assert reply["containers"][0]["name"] == "Personal"
        assert bridge.calls == [("containers.list", "{}")]

    def test_ppid_selector_exact_match(self):
        b1 = _FakeBridge(next_reply={"ok": True, "via": "first"})
        b2 = _FakeBridge(next_reply={"ok": True, "via": "second"})
        relay, _ = self._relay(bridges={1111: b1, 2222: b2})
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "qdistro.ping", "{}", '{"ppid": 2222}'))
        assert reply["via"] == "second"
        assert b1.calls == []
        assert b2.calls == [("qdistro.ping", "{}")]

    def test_ppid_selector_no_match_returns_no_bridge_found(self):
        bridge = _FakeBridge()
        relay, _ = self._relay(bridges={1234: bridge})
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"ppid": 9999}'))
        assert reply["ok"] is False
        assert reply["error"] == "no_bridge_found"
        assert bridge.calls == []

    def test_any_selector_with_no_bridges(self):
        relay, _ = self._relay(bridges={})
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"any": true}'))
        assert reply["ok"] is False
        assert reply["error"] == "no_bridge_found"

    def test_any_selector_picks_numerically_lowest_ppid(self):
        """`{"any": true}` sorts numerically on the ppid so the choice
        matches operator intuition. Lexicographic sort on the full bus
        name would put 'BrowserBridge.10000' before 'BrowserBridge.9999';
        the relay sorts numerically to avoid that surprise."""
        b_999 = _FakeBridge(next_reply={"ok": True, "ppid": 999})
        b_10000 = _FakeBridge(next_reply={"ok": True, "ppid": 10000})
        relay, _ = self._relay(bridges={999: b_999, 10000: b_10000})
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"any": true}'))
        assert reply["ppid"] == 999
        assert b_999.calls == [("containers.list", "{}")]
        assert b_10000.calls == []

    def test_empty_op_rejected(self):
        relay, _ = self._relay()
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "", "{}", '{"any": true}'))
        assert reply["error"] == "missing_op"

    def test_bad_selector_json_returns_bad_selector(self):
        relay, _ = self._relay()
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", "not-json"))
        assert reply["error"] == "bad_selector"

    def test_selector_not_object_returns_bad_selector(self):
        relay, _ = self._relay()
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '["not", "an", "object"]'))
        assert reply["error"] == "bad_selector"

    def test_empty_selector_returns_no_bridge_found(self):
        """A bare ``{}`` chooses nothing — neither ppid nor any. This
        is intentional: callers must opt in to "pick any" so a typo
        doesn't accidentally route to a random browser."""
        relay, _ = self._relay(bridges={1111: _FakeBridge()})
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", "{}"))
        assert reply["error"] == "no_bridge_found"

    def test_bridge_raises_dbus_exception(self):
        bridge = _FakeBridge(
            raise_exc=dbus.DBusException(
                "service not running",
                name="org.freedesktop.DBus.Error.ServiceUnknown"))
        relay, _ = self._relay(bridges={5555: bridge})
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"ppid": 5555}'))
        assert reply["ok"] is False
        assert reply["error"] == "bridge_call_failed"
        assert reply["dbus_name"] == "org.freedesktop.DBus.Error.ServiceUnknown"

    def test_bridge_raises_generic_exception(self):
        bridge = _FakeBridge(raise_exc=RuntimeError("kaboom"))
        relay, _ = self._relay(bridges={7777: bridge})
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"ppid": 7777}'))
        assert reply["ok"] is False
        assert reply["error"] == "bridge_call_failed"
        assert "kaboom" in reply["detail"]

    def test_skips_non_digit_suffix_bridges(self):
        """Same-uid attacker can claim
        `org.qdistro.BrowserBridge.impostor` on the session bus. The
        relay's `_select_bridge` requires the suffix to be all-digits
        so `{"any": true}` cannot route admin calls to the impostor
        even if it's the only name on the bus."""
        real = _FakeBridge(next_reply={"ok": True, "via": "real"})
        impostor = _FakeBridge(next_reply={"ok": True, "via": "impostor"})
        bus = _FakeBus(names=[
            "org.qdistro.BrowserBridge.evil",
            "org.qdistro.BrowserBridge.1234",
        ])
        bus.objects[("org.qdistro.BrowserBridge.evil",
                     R.BRIDGE_OBJ_PATH)] = impostor
        bus.objects[("org.qdistro.BrowserBridge.1234",
                     R.BRIDGE_OBJ_PATH)] = real
        relay = _StubUserRelay(bus)
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"any": true}'))
        assert reply["via"] == "real"
        assert impostor.calls == []

    def test_impostor_only_returns_no_bridge_found(self):
        """If the *only* name on the bus is a non-digit-suffix
        impostor, the relay refuses entirely rather than routing
        through it."""
        impostor = _FakeBridge(next_reply={"ok": True, "via": "impostor"})
        bus = _FakeBus(names=["org.qdistro.BrowserBridge.evil"])
        bus.objects[("org.qdistro.BrowserBridge.evil",
                     R.BRIDGE_OBJ_PATH)] = impostor
        relay = _StubUserRelay(bus)
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"any": true}'))
        assert reply["error"] == "no_bridge_found"
        assert impostor.calls == []

    def test_selector_with_both_ppid_and_any_rejected(self):
        """Mixing `ppid` and `any` is rejected as bad_selector rather
        than silently letting one win — the caller's intent is
        ambiguous."""
        relay, _ = self._relay(bridges={1234: _FakeBridge()})
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"ppid": 1234, "any": true}'))
        assert reply["error"] == "bad_selector"
        assert "both" in reply["detail"]

    def test_ppid_must_be_int_not_string(self):
        """A quoted JSON ppid is rejected rather than coerced —
        callers should send a JSON int, and a quoted form usually
        indicates a serialization bug worth surfacing."""
        relay, _ = self._relay(bridges={1234: _FakeBridge()})
        reply = json.loads(relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"ppid": "1234"}'))
        assert reply["error"] == "bad_selector"

    def test_empty_args_json_becomes_braces(self):
        """args_json="" is normalised to "{}" so the bridge always
        receives JSON-parseable args. Same-uid callers that go direct
        get whatever they send; the relay enforces the JSON invariant
        on the cross-uid path."""
        bridge = _FakeBridge(next_reply={"ok": True})
        relay, _ = self._relay(bridges={4242: bridge})
        relay.ForwardBrowserBridgeOp(
            "qdistro.ping", "", '{"ppid": 4242}')
        assert bridge.calls == [("qdistro.ping", "{}")]

    def test_audit_line_on_happy_path(self, capsys):
        bridge = _FakeBridge(next_reply={"ok": True, "containers": []})
        relay, _ = self._relay(bridges={1234: bridge})
        relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"ppid": 1234}', sender=":1.42")
        line = capsys.readouterr().err
        assert "[qdistro-user-relay/audit]" in line
        assert "kind=forward_bridge_op" in line
        assert "sender=:1.42" in line
        assert "op=containers.list" in line
        assert "bridge=org.qdistro.BrowserBridge.1234" in line
        assert "ok=true" in line

    def test_audit_line_on_bad_selector(self, capsys):
        relay, _ = self._relay()
        relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", "not-json", sender=":1.7")
        line = capsys.readouterr().err
        assert "ok=false" in line
        assert "error=bad_selector" in line
        # No bridge was selected, so the bridge field is "-".
        assert "bridge=-" in line

    def test_audit_line_on_bridge_failure(self, capsys):
        bridge = _FakeBridge(
            raise_exc=dbus.DBusException(
                "boom", name="org.freedesktop.DBus.Error.ServiceUnknown"))
        relay, _ = self._relay(bridges={1234: bridge})
        relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"ppid": 1234}', sender=":1.99")
        line = capsys.readouterr().err
        assert "ok=false" in line
        assert "error=bridge_call_failed" in line
        assert "bridge=org.qdistro.BrowserBridge.1234" in line

    def test_audit_does_not_leak_container_metadata(self, capsys):
        """Audit lines record ok/error only. Container names, icons,
        and cookie_store_ids belong in the bridge's own audit (not yet
        implemented) — the relay is a routing layer, not a content
        log."""
        bridge = _FakeBridge(next_reply={
            "ok": True,
            "containers": [
                {"cookie_store_id": "firefox-container-1",
                 "name": "MY-SECRET-CONTAINER"},
            ],
        })
        relay, _ = self._relay(bridges={1234: bridge})
        relay.ForwardBrowserBridgeOp(
            "containers.list", "{}", '{"ppid": 1234}', sender=":1.42")
        line = capsys.readouterr().err
        assert "MY-SECRET-CONTAINER" not in line
        assert "firefox-container-1" not in line

    def test_args_json_passes_through_verbatim(self):
        """The relay does not parse or rewrite args_json — it's an
        opaque pass-through so future bridge ops with unknown fields
        keep working."""
        bridge = _FakeBridge(next_reply={"ok": True})
        relay, _ = self._relay(bridges={4242: bridge})
        relay.ForwardBrowserBridgeOp(
            "containers.create",
            '{"name": "Banking", "color": "red", "icon": "dollar"}',
            '{"ppid": 4242}')
        assert bridge.calls == [
            ("containers.create",
             '{"name": "Banking", "color": "red", "icon": "dollar"}'),
        ]


# --- _friendly_name edge cases --------------------------------------------

class TestFriendlyNameEdgeCases:
    def test_empty_string(self):
        assert R._friendly_name("") == ""

    def test_only_prefix(self):
        # org.qdistro. alone: prefix strip → empty; no dot to rsplit on
        # → returned as-is.
        assert R._friendly_name("org.qdistro.") == ""

    def test_uid_without_digits(self):
        assert R._friendly_name("org.qdistro.Foo.uid") == "Foo.uid"

    def test_uid_with_mixed(self):
        assert R._friendly_name("org.qdistro.Foo.uid3000abc") == "Foo.uid3000abc"

    def test_nested_components(self):
        assert R._friendly_name("org.qdistro.A.B.C") == "A.B.C"


# --- EXCLUDED_NAMES invariants --------------------------------------------

class TestRelayConstants:
    def test_excluded_contains_own(self):
        assert R.BUS_NAME in R.EXCLUDED_NAMES

    def test_excluded_contains_broker(self):
        # Broker is on the system bus, not this session bus, but we're
        # defensive about the name appearing anywhere.
        assert "org.qdistro.AdminBroker1" in R.EXCLUDED_NAMES

    def test_excluded_contains_sender(self):
        assert "org.qdistro.StubSender" in R.EXCLUDED_NAMES

    def test_prefix_is_com_qdistro_dot(self):
        assert R.RECEIVER_PREFIX == "org.qdistro."

    def test_system_bus_name_fmt_interpolates_uid(self):
        assert R.SYSTEM_BUS_NAME_FMT.format(uid=2000) == \
            "org.qdistro.UserRelay.uid2000"


# --- _is_receiver_name_change ---------------------------------------------

class TestIsReceiverNameChange:
    """Pure predicate behind the relay's LocalReceiversChanged emit:
    True iff a session-bus NameOwnerChanged represents a *receiver*
    appearing or disappearing — the only changes qdshell's launcher must
    re-fetch for. Mirrors the ListLocalReceivers filter (prefix,
    EXCLUDED_NAMES, `:`-unique) plus an acquire-xor-release owner check.
    """

    def test_receiver_acquire_is_true(self):
        # old empty, new non-empty == the receiver just appeared.
        assert R._is_receiver_name_change(
            "org.qdistro.StubNotepad.uid3000", "", ":1.42") is True

    def test_receiver_lose_is_true(self):
        # new empty, old non-empty == the receiver just went away.
        assert R._is_receiver_name_change(
            "org.qdistro.StubNotepad.uid3000", ":1.42", "") is True

    def test_non_prefix_name_is_false(self):
        assert R._is_receiver_name_change(
            "org.freedesktop.NetworkManager", "", ":1.9") is False

    def test_excluded_name_is_false(self):
        # Own name + the defensive broker entry + the stub sender are all
        # excluded even though they match the prefix.
        for name in (R.BUS_NAME, "org.qdistro.AdminBroker1",
                     "org.qdistro.StubSender"):
            assert R._is_receiver_name_change(name, "", ":1.42") is False

    def test_unique_colon_name_is_false(self):
        assert R._is_receiver_name_change(":1.4810", "", ":1.4810") is False

    def test_ownership_transfer_is_false(self):
        # Both owners present == a transfer; the SET of receivers is
        # unchanged, so the launcher need not re-fetch.
        assert R._is_receiver_name_change(
            "org.qdistro.StubNotepad.uid3000", ":1.10", ":1.99") is False

    def test_both_owners_empty_is_false(self):
        # Degenerate no-op; never a real receiver change.
        assert R._is_receiver_name_change(
            "org.qdistro.StubNotepad.uid3000", "", "") is False

    def test_lookalike_just_outside_prefix_is_false(self):
        # "org.qdistrox.*" shares a leading substring but is NOT under
        # the "org.qdistro." prefix (the dot is load-bearing), so a
        # foreign service can't masquerade as a receiver.
        assert R._is_receiver_name_change(
            "org.qdistrox.Evil", "", ":1.42") is False


# --- LocalReceiversChanged debounce/emit ----------------------------------

class TestLocalReceiversChangedEmit:
    """The session-bus NameOwnerChanged handler arms a single debounced
    LocalReceiversChanged for a burst. We stub GLib.timeout_add to run
    synchronously (capture the callback) and spy on the emit so the test
    needs no live bus or mainloop.
    """

    def _relay(self):
        relay = _StubUserRelay(_FakeBus())
        relay._uid = 3000
        relay._receivers_changed_timer = 0
        relay._emitted = []
        # Spy on the dbus signal method.
        relay.LocalReceiversChanged = (  # type: ignore[method-assign]
            lambda uid: relay._emitted.append(int(uid)))
        return relay

    def test_burst_coalesces_to_one_emit(self, monkeypatch):
        relay = self._relay()
        captured = {}

        def fake_timeout_add(_ms, cb):
            captured["cb"] = cb
            return 99  # fake non-zero timer id

        monkeypatch.setattr(R.GLib, "timeout_add", fake_timeout_add)
        # A burst of three receiver acquires.
        for i in range(3):
            relay._on_session_name_owner_changed(
                f"org.qdistro.Stub{i}.uid3000", "", f":1.{i}")
        # Only the first armed a timer; the rest coalesced onto it.
        assert relay._receivers_changed_timer == 99
        assert relay._emitted == []
        # Fire the debounce timer once.
        assert captured["cb"]() is False
        assert relay._emitted == [3000]
        assert relay._receivers_changed_timer == 0

    def test_non_receiver_change_does_not_arm(self, monkeypatch):
        relay = self._relay()
        monkeypatch.setattr(
            R.GLib, "timeout_add",
            lambda *_a: pytest.fail("should not arm a timer"))
        # Transfer (both owners) + non-prefix name: neither is a change.
        relay._on_session_name_owner_changed(
            "org.qdistro.Stub.uid3000", ":1.1", ":1.2")
        relay._on_session_name_owner_changed(
            "org.freedesktop.DBus", "", ":1.9")
        assert relay._receivers_changed_timer == 0
        assert relay._emitted == []

    def test_second_burst_after_emit_arms_again(self, monkeypatch):
        """Once the debounce timer has fired and cleared its id, a later
        receiver change must arm a fresh timer and emit again — the
        coalesce window is per-burst, not a permanently-latched one-shot
        that drops all future changes (the no-forward-progress bug)."""
        relay = self._relay()
        cbs = []
        monkeypatch.setattr(
            R.GLib, "timeout_add",
            lambda _ms, cb: (cbs.append(cb), 5)[1])

        relay._on_session_name_owner_changed(
            "org.qdistro.Stub0.uid3000", "", ":1.0")
        assert cbs[-1]() is False  # fire first burst
        assert relay._emitted == [3000]
        assert relay._receivers_changed_timer == 0

        relay._on_session_name_owner_changed(
            "org.qdistro.Stub1.uid3000", "", ":1.1")
        assert cbs[-1]() is False  # fire second burst
        assert relay._emitted == [3000, 3000]
        assert len(cbs) == 2  # two distinct timers were armed
