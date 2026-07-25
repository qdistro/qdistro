"""Live D-Bus subscriber survives a broker restart.

Covers the still-open gap in todo/codex-testing/under-tested-areas.md
section 6 ("Broker/admin concurrency and persistence"):

    "Still uncovered: active-subscriber notification across broker
     restart (the restart tests reinitialize from persisted state but
     don't exercise a live D-Bus subscriber)."

The restart-persistence tests in test_broker_concurrency.py only
re-read persisted sqlite state from a fresh broker *object*; they never
register a real D-Bus signal subscriber and confirm it keeps receiving
RequestPending / RequestDecided signals after the broker that owns the
well-known name is torn down and re-created.

This test exercises exactly that, on a real session bus:

  1. A broker service object acquires the well-known name
     ``org.qdistro.AdminBroker1`` and emits the real broker's
     ``RequestPending`` / ``RequestDecided`` signals (the signal
     decorators are inherited verbatim from qdistro_admin_broker.Broker,
     so the interface/member/signature match production exactly).
  2. A subscriber registers with ``add_signal_receiver`` WITHOUT a
     ``bus_name=`` filter -- the pattern commit d72a430 introduced for
     the Qt admin app and the TUI (see tui/broker_client.py and
     tests/integration/permissions-gui/08-admin-app-survives-broker-restart.md).
  3. We confirm the subscriber receives a signal from broker instance #1.
  4. We RESTART the broker for real: release the well-known name, tear
     down the service object (drop it off its old unique connection),
     then create a brand-new service object on a brand-new connection
     and re-acquire the well-known name -- i.e. a fresh unique sender
     name, exactly as a service restart looks on the bus.
  5. We confirm the *same* subscriber, never re-registered, still
     receives RequestPending / RequestDecided from broker instance #2.

If a subscriber were registered with ``bus_name=`` (the pre-d72a430
behaviour) dbus-python would have pinned the match to instance #1's
unique name and step 5 would silently fail -- which is the regression
this guards against.

A real session bus is required. The module skips with a clear reason
if dbus-python / GLib / a usable session bus are unavailable, rather
than passing vacuously.
"""
from __future__ import annotations

import threading

import pytest

# --- Hard dependencies: skip the whole module with a clear reason. ---
dbus = pytest.importorskip("dbus", reason="python-dbus not installed")
pytest.importorskip("dbus.service", reason="dbus.service unavailable")
pytest.importorskip("dbus.mainloop.glib",
                    reason="dbus glib mainloop unavailable")
try:
    import dbus.service  # noqa: E402
    import dbus.mainloop.glib  # noqa: E402
    from gi.repository import GLib  # noqa: E402
except Exception as e:  # noqa: BLE001  -- pragma: no cover
    pytest.skip(f"GLib / dbus glib bindings unavailable: {e!r}",
                allow_module_level=True)

import qdistro_admin_broker as B  # noqa: E402

BUS_NAME = B.BUS_NAME      # "org.qdistro.AdminBroker1"
OBJ_PATH = B.OBJ_PATH      # "/org/qdistro/AdminBroker1"


# ---------------------------------------------------------------------------
# A minimal real D-Bus service object that emits the *actual* broker
# signals. We inherit the RequestPending / RequestDecided signal methods
# straight from the production Broker class (their @dbus.service.signal
# decorators carry the real interface name + signature), but we do NOT
# run Broker.__init__ -- that pulls in the SystemBus, GLib timers,
# inotify watches, sqlite stores, etc. which are hostile to a clean
# session-bus restart test. Registering as a dbus.service.Object on the
# session bus connection is all the signal-emit path needs.
# ---------------------------------------------------------------------------
class _BrokerSignalObject(dbus.service.Object):
    RequestPending = B.Broker.RequestPending
    RequestDecided = B.Broker.RequestDecided

    def __init__(self, conn, object_path):
        super().__init__(conn, object_path)


def _private_bus():
    """Open a private session bus that cannot take the pytest process down.

    MUST be used for every connection this module opens. libdbus defaults
    ``exit_on_disconnect`` to TRUE, and ``DBusGMainLoop(set_as_default=True)``
    attaches each connection's dispatch GSource to the DEFAULT GLib main
    context. ``bus.close()`` does not dispatch the resulting ``Disconnected``
    message — it only queues it; whoever iterates that context NEXT dispatches
    it, and libdbus's exit-on-disconnect handler then calls ``_dbus_exit(1)``,
    a C ``exit(1)`` that kills the interpreter with no exception, no traceback,
    no pytest summary and no flushed stdout.

    In a whole-directory ``python3 -m pytest tests/unit/`` run that "whoever"
    is pytest-qt: once any admin Qt test has created a QApplication, pytest-qt
    calls ``QApplication.processEvents()`` around every subsequent test
    boundary, and Qt on Linux runs on QEventDispatcherGlib — i.e. it iterates
    exactly this context. So the FIRST test after this module used to kill the
    run (rc=1, ~20% in, no output). CI did not see it because
    ci/lib/gates/host.sh runs the unit tests in sorted 30-file batches, which
    puts the admin Qt files (sorted positions 3-6) in batch 1 and this module
    (position 59) in batch 2 — their QApplication and these connections never
    coexist in one pytest process.

    Disabling the flag makes a disconnect an ordinary no-op for the process,
    which is what a test-owned connection must be.
    """
    bus = dbus.SessionBus(private=True)
    bus.set_exit_on_disconnect(False)
    return bus


def _session_bus_available() -> tuple[bool, str]:
    """Probe for a usable session bus. Returns (ok, reason)."""
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    try:
        bus = _private_bus()
        bus.close()
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, (f"no usable D-Bus session bus ({e!r}); run under "
                       "`dbus-run-session -- python3 -m pytest ...`")


_OK, _REASON = _session_bus_available()
if not _OK:
    pytest.skip(_REASON, allow_module_level=True)


class _Subscriber:
    """A live signal subscriber following the bus_name-free pattern
    (commit d72a430). Counts and records the signals it receives."""

    def __init__(self, bus):
        self.pending: list[int] = []
        self.decided: list[tuple[int, str]] = []
        self._ev = threading.Event()
        # NOTE: deliberately NO bus_name= argument -- this is the whole
        # point. Matching on interface + member only means a restarted
        # broker (new unique sender, same well-known name) still routes.
        self._m1 = bus.add_signal_receiver(
            self._on_pending,
            signal_name="RequestPending",
            dbus_interface=BUS_NAME,
            path=OBJ_PATH,
        )
        self._m2 = bus.add_signal_receiver(
            self._on_decided,
            signal_name="RequestDecided",
            dbus_interface=BUS_NAME,
            path=OBJ_PATH,
        )

    def _on_pending(self, rid):
        self.pending.append(int(rid))
        self._ev.set()

    def _on_decided(self, rid, decision):
        self.decided.append((int(rid), str(decision)))
        self._ev.set()

    def wait(self, timeout: float = 5.0) -> bool:
        got = self._ev.wait(timeout)
        self._ev.clear()
        return got


def _run_loop_until(predicate, timeout_s: float = 5.0) -> bool:
    """Run the GLib main loop ON THE CALLING (main) thread until
    ``predicate`` returns True or the timeout elapses. D-Bus signal
    delivery in dbus-python happens from inside the main loop, so the
    loop must be pumped here -- a poll-from-thread variant deadlocks.

    A periodic GLib timeout polls the predicate and quits the loop once
    it holds; a separate one-shot quits on the deadline so a missed
    signal fails fast instead of hanging."""
    loop = GLib.MainLoop()
    result = {"ok": False}

    def _poll():
        if predicate():
            result["ok"] = True
            loop.quit()
            return False  # stop polling
        return True  # keep polling

    def _deadline():
        loop.quit()
        return False

    # Only the callback that quit the loop removes itself: on success
    # _deadline survives for the rest of its timeout, and on timeout _poll
    # keeps returning True forever, its closure pinning the predicate (and
    # therefore the broker/subscriber objects) on the default main context.
    # Destroy whichever source is left, on every exit path.
    poll_id = GLib.timeout_add(10, _poll)
    deadline_id = GLib.timeout_add(int(timeout_s * 1000), _deadline)
    try:
        loop.run()
    finally:
        context = GLib.MainContext.default()
        for source_id in (poll_id, deadline_id):
            source = context.find_source_by_id(source_id)
            if source is not None:
                source.destroy()
    # Final check (predicate may have flipped between last poll and quit).
    if predicate():
        result["ok"] = True
    return result["ok"]


def _close_bus(bus) -> None:
    try:
        bus.close()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def session_bus():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = _private_bus()
    yield bus
    _close_bus(bus)


def _emit_and_wait(emit_fn, predicate, timeout_s: float = 5.0) -> bool:
    """Schedule emit_fn once on the main loop, then pump until predicate."""
    def _kick():
        emit_fn()
        return False  # one-shot

    GLib.idle_add(_kick)
    return _run_loop_until(predicate, timeout_s)


class TestLiveSubscriberAcrossRestart:

    def test_subscriber_receives_after_restart(self, session_bus, request):
        bus = session_bus

        # --- Subscribe FIRST (live, bus_name-free), before any broker
        #     instance exists, just like a client that outlives the
        #     broker. ---
        sub = _Subscriber(bus)

        # --- Broker instance #1: acquire the well-known name + object. ---
        name1 = dbus.service.BusName(BUS_NAME, bus, do_not_queue=True)
        unique1 = bus.get_unique_name()
        broker1 = _BrokerSignalObject(bus, OBJ_PATH)

        ok = _emit_and_wait(
            lambda: (broker1.RequestPending(101),
                     broker1.RequestDecided(101, "allow")),
            lambda: 101 in sub.pending and (101, "allow") in sub.decided,
        )
        assert ok, (
            f"subscriber did not receive signals from broker #1; "
            f"pending={sub.pending} decided={sub.decided}")
        assert sub.pending == [101]
        assert sub.decided == [(101, "allow")]

        # --- RESTART for real ---
        # Tear down broker #1's object and release the well-known name.
        broker1.remove_from_connection()
        del name1  # drop BusName ref -> releases org.qdistro.AdminBroker1
        # Give the bus a moment to process NameOwnerChanged (release).
        _run_loop_until(lambda: not bus.name_has_owner(BUS_NAME),
                        timeout_s=5.0)
        assert not bus.name_has_owner(BUS_NAME), (
            "well-known name was not released after broker #1 teardown")

        # Broker instance #2 on a FRESH connection -> a NEW unique sender
        # name. This is what a service *restart* looks like on the bus:
        # same well-known name, different unique name. A bus_name= filter
        # would have pinned the subscriber to unique1 and gone deaf here.
        bus2 = _private_bus()
        # Register the close BEFORE anything below can fail: an assertion
        # abort would otherwise skip the cleanup tail and leave bus2 (and the
        # well-known name it owns) live for the rest of the session.
        request.addfinalizer(lambda: _close_bus(bus2))
        name2 = dbus.service.BusName(BUS_NAME, bus2, do_not_queue=True)
        unique2 = bus2.get_unique_name()
        broker2 = _BrokerSignalObject(bus2, OBJ_PATH)

        assert unique2 != unique1, (
            "restart did not produce a new unique sender name; the test "
            "would not actually exercise sender-name change")
        assert bus.name_has_owner(BUS_NAME), (
            "broker #2 failed to re-acquire the well-known name")

        # Reset the subscriber's record so we measure post-restart
        # delivery only.
        sub.pending.clear()
        sub.decided.clear()

        ok2 = _emit_and_wait(
            lambda: (broker2.RequestPending(202),
                     broker2.RequestDecided(202, "deny")),
            lambda: 202 in sub.pending and (202, "deny") in sub.decided,
        )
        assert ok2, (
            "LIVE subscriber went deaf after broker restart: it did NOT "
            "receive RequestPending/RequestDecided from broker instance "
            f"#2 (new unique sender {unique2}). pending={sub.pending} "
            f"decided={sub.decided}. This is the d72a430 regression -- a "
            "bus_name-pinned subscription silently breaks across restart.")
        assert sub.pending == [202]
        assert sub.decided == [(202, "deny")]

        # cleanup (bus2 itself is closed by the finalizer registered above)
        broker2.remove_from_connection()
        del name2
