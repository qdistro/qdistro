"""Unit tests for qdlocker.wayland.LockerClient.

The lock surface / protocol client is the thin layer between the compositor
and the session; a routing bug here could let keystrokes escape the locker
(e.g. password chars leaking to the shell connection) or let the compositor
signal a state change that the locker silently ignores.

Strategy
--------
pywayland IS importable on the host but requires a live compositor socket
for `Display.connect()`.  We bypass that entirely by injecting fake
_display and _locker objects directly onto a freshly constructed
LockerClient — the same sys.modules / attribute-injection pattern used by
test_auth_pam.py and test_app_config.py.

The dispatcher callbacks (_on_ready, _on_locked_changed, _on_lock_requested,
_on_overlay_key, _on_global) are ordinary Python methods with no Wayland
machinery in them; they can be called directly.  The poll loop
(_poll_loop) uses select.select and a handful of display methods, all of
which are replaced with in-test fakes.

No real WAYLAND_DISPLAY socket is needed.
"""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock

import pytest
from qdlocker.wayland import LockerClient, LockerEvents

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_events(**overrides):
    """Return a LockerEvents with MagicMock callbacks (override as needed)."""
    defaults = dict(
        on_ready=MagicMock(),
        on_locked_changed=MagicMock(),
        on_lock_requested=MagicMock(),
        on_overlay_key=MagicMock(),
    )
    defaults.update(overrides)
    return LockerEvents(**defaults)


def _make_client(events=None):
    """Return a LockerClient with fake _display and _locker injected
    (bypasses Connect entirely — no real Wayland socket needed)."""
    if events is None:
        events = _make_events()
    client = LockerClient(events)
    # Inject fakes so the request-sending helpers work without a real
    # compositor. _bound is set to simulate a successful connect().
    client._display = MagicMock()
    client._locker = MagicMock()
    client._bound.set()
    return client


# ---------------------------------------------------------------------------
# _on_global — global registry routing
# ---------------------------------------------------------------------------


def test_on_global_records_name_and_version():
    client = _make_client()
    client._on_global(None, name=3, interface="qdwin_locker_v1", version=1)
    assert client._globals["qdwin_locker_v1"] == (3, 1)


def test_on_global_records_multiple_interfaces():
    client = _make_client()
    client._on_global(None, name=1, interface="wl_compositor", version=4)
    client._on_global(None, name=2, interface="qdwin_locker_v1", version=1)
    assert "wl_compositor" in client._globals
    assert "qdwin_locker_v1" in client._globals


def test_on_global_later_announcement_overwrites():
    """A second global announcement for the same interface replaces the
    earlier one — the client always uses the most-recently-advertised name."""
    client = _make_client()
    client._on_global(None, name=1, interface="qdwin_locker_v1", version=1)
    client._on_global(None, name=99, interface="qdwin_locker_v1", version=2)
    assert client._globals["qdwin_locker_v1"] == (99, 2)


# ---------------------------------------------------------------------------
# _on_ready — sets _bound and calls on_ready
# ---------------------------------------------------------------------------


@pytest.mark.cheat_aware(
    protects="on_ready(True) is fired when initially_locked=1 — the locker "
    "must raise its lock window if the compositor was already locked when "
    "the locker bound; missing this leaves the screen unguarded",
    severity="critical",
    cheats=[
        "coerce initially_locked unconditionally to False",
        "fire on_ready with a hardcoded False so the locker never raises its "
        "lock UI on reattach",
        "ignore the initially_locked argument entirely",
    ],
    consequence="after a locker restart while locked the compositor knows it "
    "is locked but qdlocker never shows the lock UI — session exposed",
)
def test_on_ready_initially_locked_true():
    events = _make_events()
    client = _make_client(events)
    client._bound.clear()

    client._on_ready(None, initially_locked=1)

    assert client._bound.is_set()
    events.on_ready.assert_called_once_with(True)


def test_on_ready_initially_locked_false():
    events = _make_events()
    client = _make_client(events)
    client._bound.clear()

    client._on_ready(None, initially_locked=0)

    assert client._bound.is_set()
    events.on_ready.assert_called_once_with(False)


def test_on_ready_sets_bound_event():
    """_bound gates the connect() return; if it never becomes set the
    locker fails to start cleanly."""
    client = _make_client()
    client._bound.clear()
    assert not client._bound.is_set()
    client._on_ready(None, initially_locked=0)
    assert client._bound.is_set()


# ---------------------------------------------------------------------------
# _on_locked_changed — compositor notifies us of a state transition
# ---------------------------------------------------------------------------


@pytest.mark.cheat_aware(
    protects="on_locked_changed(True) fires when locked=1 — the locker "
    "must bring its UI to the front; silently dropping this event leaves "
    "the screen unlocked while the compositor believes it is locked",
    severity="critical",
    cheats=[
        "coerce the argument to always False",
        "only pass locked=True when the locker itself requested it",
        "drop locked_changed events that arrive out of the expected order",
    ],
    consequence="compositor and locker have different views of lock state; "
    "the screen is left unguarded",
)
def test_on_locked_changed_fires_locked_true():
    events = _make_events()
    client = _make_client(events)
    client._on_locked_changed(None, locked=1)
    events.on_locked_changed.assert_called_once_with(True)


def test_on_locked_changed_fires_locked_false():
    events = _make_events()
    client = _make_client(events)
    client._on_locked_changed(None, locked=0)
    events.on_locked_changed.assert_called_once_with(False)


def test_on_locked_changed_bool_coercion():
    """The wire value is uint (0 or 1); the callback must receive a real
    Python bool, not 0/1 int."""
    events = _make_events()
    client = _make_client(events)
    client._on_locked_changed(None, locked=1)
    arg = events.on_locked_changed.call_args[0][0]
    assert arg is True
    client._on_locked_changed(None, locked=0)
    arg = events.on_locked_changed.call_args[0][0]
    assert arg is False


# ---------------------------------------------------------------------------
# _on_lock_requested — compositor relays a lock trigger
# ---------------------------------------------------------------------------


def test_on_lock_requested_routes_reason():
    events = _make_events()
    client = _make_client(events)
    client._on_lock_requested(None, reason=3)  # reason 3 = manual hotkey
    events.on_lock_requested.assert_called_once_with(3)


@pytest.mark.parametrize("reason", [0, 1, 2, 3])
def test_on_lock_requested_all_defined_reasons(reason):
    """All four defined lock reasons (idle/lid/suspend/manual) must route
    through to on_lock_requested without modification."""
    events = _make_events()
    client = _make_client(events)
    client._on_lock_requested(None, reason=reason)
    events.on_lock_requested.assert_called_once_with(reason)


# ---------------------------------------------------------------------------
# _on_overlay_key — keyboard input routing while locked
# ---------------------------------------------------------------------------


@pytest.mark.cheat_aware(
    protects="the client-side overlay_key dispatcher forwards the EXACT "
    "sym+utf8 the compositor sent to on_overlay_key, and to no other "
    "callback. (Connection isolation — that the key only ever arrives on the "
    "locker's private connection — is a qdwin-side property, not verified "
    "here; this pins only the client dispatch.)",
    severity="high",
    cheats=[
        "mangle or reorder the sym/utf8 args before dispatch",
        "fan the event out to additional handlers beyond on_overlay_key",
        "silently drop overlay_key events when the locker is busy",
    ],
    consequence="the locker mis-reads or duplicates the keystrokes the "
    "compositor delivered for the unlock prompt",
)
def test_on_overlay_key_routes_sym_and_utf8():
    events = _make_events()
    client = _make_client(events)
    client._on_overlay_key(None, sym=0x61, utf8="a")
    events.on_overlay_key.assert_called_once_with(0x61, "a")


def test_on_overlay_key_empty_utf8_for_control_key():
    """Non-printable keys (Escape, Return, BackSpace) arrive with utf8=''."""
    events = _make_events()
    client = _make_client(events)
    client._on_overlay_key(None, sym=0xFF1B, utf8="")  # XKB_KEY_Escape
    events.on_overlay_key.assert_called_once_with(0xFF1B, "")


def test_on_overlay_key_only_calls_overlay_handler():
    """An overlay_key event must not accidentally trigger lock/ready
    callbacks — no cross-event routing."""
    events = _make_events()
    client = _make_client(events)
    client._on_overlay_key(None, sym=0x41, utf8="A")
    events.on_ready.assert_not_called()
    events.on_locked_changed.assert_not_called()
    events.on_lock_requested.assert_not_called()


# ---------------------------------------------------------------------------
# set_locked — outbound request to compositor
# ---------------------------------------------------------------------------


@pytest.mark.cheat_aware(
    protects="set_locked(True) sends locked=1 to the compositor, "
    "transitioning it to the locked state; passing the wrong value "
    "(e.g. always 0) leaves the compositor unlocked",
    severity="critical",
    cheats=[
        "call _locker.set_locked(0) regardless of the argument",
        "skip calling set_locked on _locker entirely",
        "convert bool directly (True → 1) but then negate it",
    ],
    consequence="the compositor never enters the locked state even though "
    "qdlocker believes it sent set_locked(1) — session left unlocked",
)
def test_set_locked_true_sends_1():
    client = _make_client()
    client.set_locked(True)
    client._locker.set_locked.assert_called_once_with(1)
    client._display.flush.assert_called()


def test_set_locked_false_sends_0():
    client = _make_client()
    client.set_locked(False)
    client._locker.set_locked.assert_called_once_with(0)
    client._display.flush.assert_called()


def test_set_locked_before_connect_logs_error(caplog):
    """set_locked before connect() must log an error and not crash."""
    import logging

    events = _make_events()
    client = LockerClient(events)
    # _locker is None — simulates calling set_locked before connect()
    with caplog.at_level(logging.ERROR, logger="qdlocker.wayland"):
        client.set_locked(True)
    assert any("set_locked" in r.message for r in caplog.records)


def test_set_locked_display_flushed():
    """Every set_locked call must flush the display so the request reaches
    the compositor; an unflushed request is silently buffered — the compositor
    never sees the lock transition."""
    client = _make_client()
    client.set_locked(True)
    assert client._display.flush.call_count >= 1


def test_set_locked_noop_when_display_gone():
    """If _display is cleared (e.g. disconnect racing with set_locked),
    set_locked must not crash."""
    client = _make_client()
    client._display = None  # simulate disconnect race
    # Should not raise
    client.set_locked(True)


# ---------------------------------------------------------------------------
# lock_acknowledged — outbound ack
# ---------------------------------------------------------------------------


def test_lock_acknowledged_sends_reason():
    client = _make_client()
    client.lock_acknowledged(3)
    client._locker.lock_acknowledged.assert_called_once_with(3)
    client._display.flush.assert_called()


def test_lock_acknowledged_noop_without_locker():
    events = _make_events()
    client = LockerClient(events)
    # _locker is None — should not raise
    client.lock_acknowledged(1)


# ---------------------------------------------------------------------------
# _poll_loop — the worker thread event dispatch loop
# ---------------------------------------------------------------------------


def _make_poll_client():
    """Return a client ready for poll-loop tests with a controllable pipe fd
    and instrumented display methods."""
    client = _make_client()
    # Use a real pipe so select.select works against a real fd.
    r_fd, w_fd = os.pipe()
    client._display.get_fd.return_value = r_fd
    client._poll_r_fd = r_fd
    client._poll_w_fd = w_fd
    return client


def _cleanup_poll_client(client):
    try:
        os.close(client._poll_r_fd)
    except OSError:
        pass
    try:
        os.close(client._poll_w_fd)
    except OSError:
        pass


def test_poll_loop_exits_on_stop_event():
    """When _stop is set, _poll_loop returns cleanly without blocking."""
    client = _make_poll_client()
    try:
        client._stop.set()
        t = threading.Thread(target=client._poll_loop)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive(), "poll loop did not exit after _stop was set"
    finally:
        _cleanup_poll_client(client)


def test_poll_loop_calls_flush_each_iteration():
    """The loop must flush the display on every iteration so queued
    outbound requests reach the compositor promptly."""
    client = _make_poll_client()
    flush_count = []

    def counting_flush():
        flush_count.append(1)
        if len(flush_count) >= 2:
            client._stop.set()

    client._display.flush = counting_flush
    try:
        t = threading.Thread(target=client._poll_loop)
        t.start()
        t.join(timeout=3.0)
        assert not t.is_alive()
        assert len(flush_count) >= 2
    finally:
        _cleanup_poll_client(client)


def test_poll_loop_dispatches_when_fd_readable():
    """When the Wayland fd becomes readable, the loop must call
    display.read() + display.dispatch(block=False) to drain the event queue."""
    client = _make_poll_client()

    dispatched = threading.Event()
    read_called = threading.Event()

    def fake_read():
        read_called.set()

    def fake_dispatch(**kwargs):
        dispatched.set()
        client._stop.set()

    client._display.read = fake_read
    client._display.dispatch = fake_dispatch

    try:
        # Write a byte to make the pipe readable, triggering the select branch.
        os.write(client._poll_w_fd, b"\x00")
        t = threading.Thread(target=client._poll_loop)
        t.start()
        t.join(timeout=3.0)
        assert not t.is_alive()
        assert read_called.is_set(), "display.read() was not called when fd was readable"
        assert dispatched.is_set(), "display.dispatch() was not called when fd was readable"
    finally:
        _cleanup_poll_client(client)


def test_poll_loop_dispatch_block_false():
    """dispatch must be called with block=False (non-blocking), not
    block=True, to avoid starving main-thread set_locked requests."""
    client = _make_poll_client()
    dispatch_kwargs = []

    def fake_dispatch(**kwargs):
        dispatch_kwargs.append(kwargs)
        client._stop.set()

    client._display.dispatch = fake_dispatch

    try:
        os.write(client._poll_w_fd, b"\x00")
        t = threading.Thread(target=client._poll_loop)
        t.start()
        t.join(timeout=3.0)
        assert not t.is_alive()
        if dispatch_kwargs:  # only assert if dispatch was actually reached
            assert dispatch_kwargs[0].get("block") is False, (
                "dispatch() was called with block=True — this can starve "
                "main-thread set_locked requests"
            )
    finally:
        _cleanup_poll_client(client)


@pytest.mark.cheat_aware(
    protects="when the poll loop hits an exception it BREAKS OUT within "
    "bounded time rather than spinning in a tight error loop (the thread "
    "actually terminates, verified by join). NOTE: whether a dead locker "
    "leaves the session locked is a qdwin-side property (display EOF "
    "handling) not verified in this repo — this pins only that the client "
    "loop does not busy-spin.",
    severity="high",
    cheats=[
        "catch the exception and continue the loop so the test times out "
        "instead of breaking",
        "swallow all exceptions without the break so the loop appears to run",
    ],
    consequence="an exception in the poll loop causes a CPU-spinning error "
    "loop; the locker may stop processing auth events",
)
def test_poll_loop_breaks_on_exception():
    """If display.flush() raises, the poll loop must break out (not spin).
    The compositor will then treat the dead connection as still-locked."""
    client = _make_poll_client()
    call_count = [0]

    def exploding_flush():
        call_count[0] += 1
        if call_count[0] >= 1:
            raise RuntimeError("simulated Wayland error")

    client._display.flush = exploding_flush
    try:
        t = threading.Thread(target=client._poll_loop)
        t.start()
        t.join(timeout=3.0)
        assert not t.is_alive(), (
            "poll loop did not exit after flush() raised — possible spin loop"
        )
    finally:
        _cleanup_poll_client(client)


def test_poll_loop_exits_cleanly_when_display_cleared():
    """If _display is set to None while the loop is running (disconnect
    race), the loop must exit cleanly without raising AttributeError."""
    client = _make_poll_client()

    def nullifying_flush():
        client._display = None

    client._display.flush = nullifying_flush

    try:
        t = threading.Thread(target=client._poll_loop)
        t.start()
        t.join(timeout=3.0)
        assert not t.is_alive()
    finally:
        _cleanup_poll_client(client)


# ---------------------------------------------------------------------------
# disconnect — stop + join + close
# ---------------------------------------------------------------------------


def test_disconnect_sets_stop_event():
    client = _make_client()
    assert not client._stop.is_set()
    client.disconnect()
    assert client._stop.is_set()


def test_disconnect_calls_display_disconnect():
    client = _make_client()
    # Capture the mock before disconnect() sets _display to None.
    display_mock = client._display
    client.disconnect()
    display_mock.disconnect.assert_called_once()


def test_disconnect_clears_display_ref():
    """After disconnect(), _display must be None so a stray callback
    or second disconnect() doesn't double-close the fd."""
    client = _make_client()
    client.disconnect()
    assert client._display is None


def test_disconnect_tolerates_display_exception(caplog):
    """If display.disconnect() raises (e.g. broken pipe), disconnect()
    must still clear _display and not propagate the exception."""

    client = _make_client()
    client._display.disconnect.side_effect = OSError("broken pipe")
    # Should not raise
    client.disconnect()
    assert client._display is None
