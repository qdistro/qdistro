"""Unit tests for WaylandBridge -> lock-confirmed callback wiring.

The logind suspend delay-inhibitor releases its fd only when the
compositor confirms the locked state. That confirmation is surfaced by
WaylandBridge via the lock_confirmed callback. These tests pin:

  - the callback fires on a genuine compositor confirmation
    (locked_changed=1), the signal the inhibitor waits on;
  - it does NOT fire on locked_changed=0 (unlock);
  - a suspend lock_requested arriving while already locked still
    confirms (no fresh locked_changed=1 will arrive, so the inhibitor
    must not stall).
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtGui import QGuiApplication
from qdlocker.app import WaylandBridge


@pytest.fixture(scope="session")
def qapp():
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    yield app


def _make_bridge():
    controller = MagicMock()
    bridge = WaylandBridge(controller)
    return bridge, controller


def _make_bridge_with_pwd_notifier():
    controller = MagicMock()
    notifier = MagicMock()
    bridge = WaylandBridge(controller, pwd_lifecycle=notifier)
    return bridge, controller, notifier


def test_compositor_confirm_fires_callback(qapp):
    bridge, _ = _make_bridge()
    fired: list[int] = []
    bridge.set_lock_confirmed_cb(lambda: fired.append(1))

    # Compositor confirms locked state.
    bridge._on_locked_changed(True)
    assert fired == [1]


def test_unlock_does_not_fire_callback(qapp):
    bridge, _ = _make_bridge()
    fired: list[int] = []
    bridge.set_lock_confirmed_cb(lambda: fired.append(1))

    bridge._on_locked_changed(False)
    assert fired == []


def test_callback_cleared_with_none(qapp):
    bridge, _ = _make_bridge()
    fired: list[int] = []
    bridge.set_lock_confirmed_cb(lambda: fired.append(1))
    bridge.set_lock_confirmed_cb(None)

    bridge._on_locked_changed(True)
    assert fired == []


def test_already_compositor_locked_suspend_request_confirms(qapp):
    """If a suspend lock_requested arrives while the COMPOSITOR has already
    confirmed the lock, the bridge returns early (idempotency) but must
    still confirm — otherwise the inhibitor waits for a locked_changed=1
    that never comes (no fresh transition)."""
    bridge, controller = _make_bridge()
    fired: list[int] = []
    bridge.set_lock_confirmed_cb(lambda: fired.append(1))

    # Put the bridge into the COMPOSITOR-confirmed locked state.
    client = MagicMock()
    bridge.attach(client)
    bridge._on_lock_requested(2)   # request -> intent
    bridge._on_locked_changed(True)  # compositor confirms
    assert bridge.locked is True
    fired.clear()  # ignore the confirm from the genuine transition above

    # A second suspend request while compositor-confirmed -> early return,
    # but it must still confirm so the inhibitor releases.
    bridge._on_lock_requested(2)
    assert fired == [1]
    # And it must NOT re-issue set_locked/lock_acknowledged.
    assert client.set_locked.call_count == 1
    assert client.lock_acknowledged.call_count == 1


def test_intent_locked_but_not_compositor_confirmed_does_not_confirm(qapp):
    """REGRESSION GUARD (codex MAJOR): a suspend lock_requested arriving
    while an EARLIER lock is still only intent-mirrored (_locked=True) but
    NOT yet compositor-confirmed must NOT fire the confirm callback. Doing
    so would release the suspend inhibitor before the LOCK-layer frame is
    painted — the exact flash race this feature closes. The pending
    compositor locked_changed(1) is what should fire it."""
    bridge, _ = _make_bridge()
    client = MagicMock()
    bridge.attach(client)
    fired: list[int] = []
    bridge.set_lock_confirmed_cb(lambda: fired.append(1))

    # First request: intent only, compositor has NOT confirmed yet.
    bridge._on_lock_requested(0)  # idle lock
    assert bridge.locked is True            # intent mirror set
    assert bridge._compositor_locked is False
    assert fired == []                      # request path never confirms

    # Suspend arrives before compositor confirmation -> early return, but
    # must NOT confirm (would release inhibitor on an unpainted lock).
    bridge._on_lock_requested(2)
    assert fired == []

    # Only the real compositor transition releases the inhibitor.
    bridge._on_locked_changed(True)
    assert fired == [1]


def test_initially_locked_seeds_compositor_confirmed(qapp):
    """If the compositor reports already-locked at bind (_on_ready), that is
    a compositor-confirmed state: a suspend arriving before any fresh
    locked_changed(1) must confirm immediately (no needless inhibitor
    timeout)."""
    bridge, _ = _make_bridge()
    client = MagicMock()
    bridge.attach(client)
    fired: list[int] = []
    bridge.set_lock_confirmed_cb(lambda: fired.append(1))

    bridge._on_ready(True)  # compositor was already locked at bind
    assert bridge._compositor_locked is True

    # Suspend request while already (compositor-)locked -> immediate confirm.
    bridge._on_lock_requested(2)
    assert fired == [1]


def test_new_lock_request_notifies_pwd_lifecycle(qapp):
    bridge, _, notifier = _make_bridge_with_pwd_notifier()
    client = MagicMock()
    bridge.attach(client)

    bridge._on_lock_requested(3)

    notifier.notify_screen_lock.assert_called_once_with("manual")
    client.set_locked.assert_called_once_with(True)


def test_already_locked_request_does_not_notify_pwd_again(qapp):
    bridge, _, notifier = _make_bridge_with_pwd_notifier()
    bridge._on_lock_requested(0)
    notifier.notify_screen_lock.assert_called_once_with("idle")
    notifier.notify_screen_lock.reset_mock()

    bridge._on_lock_requested(2)

    notifier.notify_screen_lock.assert_not_called()


def test_initially_locked_ready_notifies_pwd_lifecycle(qapp):
    bridge, _, notifier = _make_bridge_with_pwd_notifier()

    bridge._on_ready(True)

    notifier.notify_screen_lock.assert_called_once_with("manual")


def test_lock_request_does_not_double_confirm_without_compositor(qapp):
    """A fresh lock_requested mirrors intent but the REAL confirmation is
    the compositor's locked_changed=1. The bridge must not pre-confirm on
    the request path (that would release the inhibitor before the surface
    is committed)."""
    bridge, _ = _make_bridge()
    client = MagicMock()
    bridge.attach(client)
    fired: list[int] = []
    bridge.set_lock_confirmed_cb(lambda: fired.append(1))

    bridge._on_lock_requested(2)  # request only; not yet confirmed
    assert fired == []

    # Now the compositor confirms.
    bridge._on_locked_changed(True)
    assert fired == [1]
