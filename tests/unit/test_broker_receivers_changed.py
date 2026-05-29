"""Tests for qdistro_admin_broker.Broker.ReceiversChanged debounce.

Part of the "App1 receivers changed" signal chain (relay →
LocalReceiversChanged → broker → ReceiversChanged → qdshell). The broker
observes every per-uid UserRelay's LocalReceiversChanged on the system
bus via one sender-agnostic match, coalesces a burst across relays into a
single payload-free ReceiversChanged, and qdshell's launcher re-runs
ListReceivers on it.

Host-only per tests/AGENTS.md: no live bus. We bypass
dbus.service.Object.__init__, drive `_on_relay_receivers_changed`
directly, stub GLib.timeout_add so the debounce runs synchronously, and
spy on the ReceiversChanged emit.
"""
from __future__ import annotations

import pytest

pytest.importorskip("dbus")

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402


class _ReceiversStubBroker(Broker):
    """Bypass dbus.service.Object.__init__; capture ReceiversChanged
    emits in a list and only wire the receivers-changed debounce state.
    """

    def __init__(self):
        self._receivers_changed_timer = 0
        self.receivers_changed_emits = 0

    def ReceiversChanged(self):  # type: ignore[override]
        self.receivers_changed_emits += 1


@pytest.fixture
def broker() -> _ReceiversStubBroker:
    return _ReceiversStubBroker()


def test_burst_emits_once(broker, monkeypatch):
    """A burst of LocalReceiversChanged from several relays (or one relay
    firing rapidly) collapses to exactly one ReceiversChanged after the
    debounce fires."""
    captured = {}

    def fake_timeout_add(_ms, cb):
        captured["cb"] = cb
        return 7  # fake non-zero timer id

    monkeypatch.setattr(B.GLib, "timeout_add", fake_timeout_add)

    # Five relays report a change in quick succession.
    for uid in (1000, 2000, 3000, 2000, 1000):
        broker._on_relay_receivers_changed(uid)

    # Only the first armed a timer; the rest coalesced onto it. Nothing
    # emitted yet.
    assert broker._receivers_changed_timer == 7
    assert broker.receivers_changed_emits == 0

    # Fire the debounce timer once.
    assert captured["cb"]() is False
    assert broker.receivers_changed_emits == 1
    assert broker._receivers_changed_timer == 0


def test_second_burst_after_emit_arms_again(broker, monkeypatch):
    """Once the timer has fired, a later change arms a fresh timer and
    emits again — the coalesce window is per-burst, not a one-shot
    latch."""
    cbs = []
    monkeypatch.setattr(
        B.GLib, "timeout_add",
        lambda _ms, cb: (cbs.append(cb), 11)[1])

    broker._on_relay_receivers_changed(1000)
    cbs[-1]()  # fire first burst
    assert broker.receivers_changed_emits == 1

    broker._on_relay_receivers_changed(2000)
    cbs[-1]()  # fire second burst
    assert broker.receivers_changed_emits == 2
    assert len(cbs) == 2  # two distinct timers were armed


def test_zero_debounce_class_attr_is_honored(broker, monkeypatch):
    """The debounce interval is a class attribute so it can be tuned;
    the handler reads it for the timeout. Pin that it's passed through
    to timeout_add (a test could zero it to make a real GLib run fire
    immediately)."""
    seen_ms = {}
    monkeypatch.setattr(
        B.GLib, "timeout_add",
        lambda ms, cb: (seen_ms.setdefault("ms", ms), 3)[1])
    broker._on_relay_receivers_changed(1000)
    assert seen_ms["ms"] == Broker.RECEIVERS_CHANGED_DEBOUNCE_MS
