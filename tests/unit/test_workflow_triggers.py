"""Unit tests for the production workflow trigger watchers.

Each real event source is faked at its boundary:
  - cron: a pure parser + a fake "now" fed to ``next_delay``.
  - process_spawn: a temp directory tree standing in for the cgroup v2
    hierarchy, driven through ``_scan_once`` deterministically.
  - dbus_signal: a fake bus capturing the match rule + a real
    session-bus round-trip (skipped when no session bus is present).
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "workflow"))

from workflow_schema import TriggerDef, TriggerType  # noqa: E402
from cron_parser import CronExpr, CronParseError, _parse_field  # noqa: E402
from trigger_registry import (  # noqa: E402
    CronTrigger,
    DBusSignalTrigger,
    ProcessSpawnTrigger,
    QbusEventTrigger,
)


# ======================================================================
# Cron parser
# ======================================================================


class TestCronParser:
    def test_step_minutes(self):
        c = CronExpr("*/15 * * * *")
        assert c.minutes == frozenset({0, 15, 30, 45})

    def test_range_and_list(self):
        c = CronExpr("0 9 * * 1-5")
        assert c.minutes == frozenset({0})
        assert c.hours == frozenset({9})
        assert c.dows == frozenset({1, 2, 3, 4, 5})

    def test_list_field(self):
        c = CronExpr("0,30 0,12 * * *")
        assert c.minutes == frozenset({0, 30})
        assert c.hours == frozenset({0, 12})

    def test_dow_seven_is_sunday(self):
        c = CronExpr("0 0 * * 7")
        assert 0 in c.dows

    def test_matches_weekday(self):
        c = CronExpr("30 14 * * *")
        from datetime import datetime
        assert c.matches(datetime(2026, 5, 28, 14, 30))
        assert not c.matches(datetime(2026, 5, 28, 14, 31))

    def test_dom_dow_or_semantics(self):
        # When both dom and dow are restricted, match on EITHER.
        from datetime import datetime
        c = CronExpr("0 0 13 * 5")  # 13th OR a Friday
        # 2026-05-13 is a Wednesday -> matches via day-of-month.
        assert c.matches(datetime(2026, 5, 13, 0, 0))
        # 2026-05-15 is a Friday -> matches via day-of-week.
        assert c.matches(datetime(2026, 5, 15, 0, 0))
        # 2026-05-14 is a Thursday, not the 13th -> no match.
        assert not c.matches(datetime(2026, 5, 14, 0, 0))

    def test_next_after(self):
        from datetime import datetime
        c = CronExpr("0 * * * *")  # top of every hour
        nxt = c.next_after(datetime(2026, 5, 28, 14, 30))
        assert nxt == datetime(2026, 5, 28, 15, 0)

    def test_next_after_skips_current_minute(self):
        from datetime import datetime
        c = CronExpr("30 14 * * *")
        nxt = c.next_after(datetime(2026, 5, 28, 14, 30))
        # Already 14:30 -> next is tomorrow 14:30.
        assert nxt == datetime(2026, 5, 29, 14, 30)

    @pytest.mark.parametrize("expr", [
        "* * * *",          # too few fields
        "* * * * * *",      # too many fields
        "60 * * * *",       # minute out of bounds
        "* 24 * * *",       # hour out of bounds
        "* * 0 * *",        # day-of-month below 1
        "* * 32 * *",       # day-of-month above 31
        "* * * 0 *",        # month below 1
        "* * * 13 *",       # month above 12
        "* * * * 8",        # day-of-week above 7
        "* * * * 5-8",      # dow range endpoint above 7
        "*/0 * * * *",      # zero step
        "*/-1 * * * *",     # negative step
        "5-1 * * * *",      # inverted range
        "abc * * * *",      # non-numeric
        "1,,3 * * * *",     # empty list term
        "-5 * * * *",       # bare leading dash
        "1- * * * *",       # range missing end
        "",                 # empty expression
        "   ",              # whitespace-only expression
    ])
    def test_invalid_expressions(self, expr):
        with pytest.raises(CronParseError):
            CronExpr(expr)

    # ---- day-of-week range/alias edge table ----------------------------------
    # Regression pin: 7 is a Sunday alias even as a RANGE ENDPOINT. Collapsing
    # 7->0 before expanding the range used to make "1-7" / "5-7" raise
    # (range start > end) and "0-7" yield only {0} — so a common schedule like
    # "Mon-Sun" silently failed to parse and the trigger fell back to interval.
    @pytest.mark.parametrize("field,expected_dows", [
        ("0",   {0}),
        ("7",   {0}),            # 7 alias for Sunday
        ("0-6", {0, 1, 2, 3, 4, 5, 6}),
        ("0-7", {0, 1, 2, 3, 4, 5, 6}),   # whole week (7 == 0, dedup)
        ("1-7", {0, 1, 2, 3, 4, 5, 6}),   # Mon..Sun -> whole week
        ("5-7", {0, 5, 6}),               # Fri, Sat, Sun
        ("6-7", {0, 6}),                  # Sat, Sun
        ("1-5", {1, 2, 3, 4, 5}),         # weekdays unaffected
        ("*",   {0, 1, 2, 3, 4, 5, 6}),
        ("*/2", {0, 2, 4, 6}),
        ("0,7", {0}),                     # both spellings of Sunday
    ])
    def test_dow_range_and_alias_table(self, field, expected_dows):
        c = CronExpr(f"0 0 * * {field}")
        assert set(c.dows) == expected_dows

    # ---- valid-expansion edge table ------------------------------------------
    @pytest.mark.parametrize("field,lo,hi,expected", [
        ("*/15", 0, 59, {0, 15, 30, 45}),
        ("1-30/2", 0, 59, set(range(1, 31, 2))),
        ("0-59/20", 0, 59, {0, 20, 40}),
        ("5/2", 0, 59, {5}),              # step on a single base is just the base
        ("59", 0, 59, {59}),              # upper boundary value
        ("0", 0, 59, {0}),                # lower boundary value
        ("1,2,3", 0, 59, {1, 2, 3}),
        ("1, 2 , 3", 0, 59, {1, 2, 3}),   # whitespace in list tolerated
    ])
    def test_field_expansion_table(self, field, lo, hi, expected):
        assert set(_parse_field(field, lo, hi)) == expected


# ======================================================================
# CronTrigger
# ======================================================================


class TestCronTrigger:
    def test_next_delay_uses_cron_schedule(self):
        trig = CronTrigger(
            "wf", TriggerDef(type=TriggerType.CRON,
                             config={"schedule": "0 * * * *"}),
            lambda n, c: None,
        )
        from datetime import datetime
        now = datetime(2026, 5, 28, 14, 30).timestamp()
        delay = trig.next_delay(now=now)
        # 30 minutes until the top of the next hour.
        assert abs(delay - 1800) < 1.0

    def test_next_delay_interval_fallback(self):
        trig = CronTrigger(
            "wf", TriggerDef(type=TriggerType.CRON,
                             config={"interval_seconds": 120}),
            lambda n, c: None,
        )
        assert trig.next_delay() == 120

    def test_invalid_schedule_falls_back_to_interval(self):
        trig = CronTrigger(
            "wf", TriggerDef(type=TriggerType.CRON,
                             config={"schedule": "not a cron",
                                     "interval_seconds": 77}),
            lambda n, c: None,
        )
        assert trig._cron is None
        assert trig.next_delay() == 77

    def test_interval_clamped(self):
        trig = CronTrigger(
            "wf", TriggerDef(type=TriggerType.CRON,
                             config={"interval_seconds": 0}),
            lambda n, c: None,
        )
        assert trig.next_delay() == CronTrigger._MIN_INTERVAL_S

    def test_unsatisfiable_schedule_falls_back(self):
        # "Feb 31" parses but never matches; must fall back to interval
        # at construction rather than searching 4 years every loop.
        trig = CronTrigger(
            "wf", TriggerDef(type=TriggerType.CRON,
                             config={"schedule": "0 0 31 2 *",
                                     "interval_seconds": 55}),
            lambda n, c: None,
        )
        assert trig._cron is None
        assert trig.next_delay() == 55

    def test_cron_delay_never_below_min(self):
        # A "every minute" schedule evaluated mid-minute could yield a
        # sub-second delay; it must still be clamped.
        trig = CronTrigger(
            "wf", TriggerDef(type=TriggerType.CRON,
                             config={"schedule": "* * * * *"}),
            lambda n, c: None,
        )
        from datetime import datetime
        # 0.5s before the next minute boundary.
        now = datetime(2026, 5, 28, 14, 30, 59, 500000).timestamp()
        assert trig.next_delay(now=now) >= CronTrigger._MIN_INTERVAL_S


# ======================================================================
# ProcessSpawnTrigger (temp cgroup tree)
# ======================================================================


def _make_cgroup(root: Path, name: str, pids: list[int]) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup.procs").write_text(
        "".join(f"{p}\n" for p in pids), encoding="ascii")
    return d


def _spawn_trigger(root, pattern="*", fire_on_existing=False, calls=None):
    if calls is None:
        calls = []
    trig = ProcessSpawnTrigger(
        "wf",
        TriggerDef(type=TriggerType.PROCESS_SPAWN, config={
            "cgroup_root": str(root),
            "cgroup_pattern": pattern,
            "fire_on_existing": fire_on_existing,
        }),
        lambda n, c: calls.append(c),
    )
    return trig, calls


class TestProcessSpawnTrigger:
    def test_baseline_does_not_fire_existing(self, tmp_path):
        _make_cgroup(tmp_path, "svc-a", [100, 200])
        trig, calls = _spawn_trigger(tmp_path, "svc-*")
        fired = trig._scan_once()
        assert fired == []
        assert calls == []

    def test_new_pid_fires(self, tmp_path):
        cg = _make_cgroup(tmp_path, "svc-a", [100])
        trig, calls = _spawn_trigger(tmp_path, "svc-*")
        trig._scan_once()  # baseline
        # A new process joins the cgroup.
        (cg / "cgroup.procs").write_text("100\n300\n", encoding="ascii")
        fired = trig._scan_once()
        assert len(fired) == 1
        assert fired[0]["pid"] == 300
        assert fired[0]["cgroup"] == "svc-a"
        assert calls and calls[-1]["pid"] == 300

    def test_fire_on_existing(self, tmp_path):
        _make_cgroup(tmp_path, "svc-a", [100, 200])
        trig, calls = _spawn_trigger(tmp_path, "svc-*", fire_on_existing=True)
        fired = trig._scan_once()
        pids = sorted(c["pid"] for c in fired)
        assert pids == [100, 200]

    def test_pattern_excludes_nonmatching(self, tmp_path):
        _make_cgroup(tmp_path, "svc-a", [100])
        _make_cgroup(tmp_path, "other-b", [999])
        trig, calls = _spawn_trigger(tmp_path, "svc-*", fire_on_existing=True)
        fired = trig._scan_once()
        assert [c["pid"] for c in fired] == [100]

    def test_no_refire_for_same_pid(self, tmp_path):
        cg = _make_cgroup(tmp_path, "svc-a", [100])
        trig, calls = _spawn_trigger(tmp_path, "svc-*")
        trig._scan_once()
        (cg / "cgroup.procs").write_text("100\n300\n", encoding="ascii")
        assert len(trig._scan_once()) == 1
        # Same membership on next scan -> no new fire.
        assert trig._scan_once() == []

    def test_disappearing_cgroup_pruned(self, tmp_path):
        cg = _make_cgroup(tmp_path, "svc-a", [100])
        trig, calls = _spawn_trigger(tmp_path, "svc-*")
        trig._scan_once()
        assert "svc-a" in trig._seen
        # cgroup removed.
        (cg / "cgroup.procs").unlink()
        cg.rmdir()
        trig._scan_once()
        assert "svc-a" not in trig._seen

    def test_unreadable_procs_is_isolated(self, tmp_path):
        cg = _make_cgroup(tmp_path, "svc-a", [100])
        trig, calls = _spawn_trigger(tmp_path, "svc-*", fire_on_existing=True)
        # Replace cgroup.procs with a directory so open() fails.
        (cg / "cgroup.procs").unlink()
        (cg / "cgroup.procs").mkdir()
        # Must not raise; just yields no pids for that cgroup.
        fired = trig._scan_once()
        assert fired == []

    def test_poll_interval_clamped(self, tmp_path):
        trig = ProcessSpawnTrigger(
            "wf",
            TriggerDef(type=TriggerType.PROCESS_SPAWN, config={
                "cgroup_root": str(tmp_path),
                "poll_interval": 0.0,
            }),
            lambda n, c: None,
        )
        assert trig._poll >= ProcessSpawnTrigger._MIN_POLL_S

    def test_thread_lifecycle(self, tmp_path):
        cg = _make_cgroup(tmp_path, "svc-a", [])
        trig = ProcessSpawnTrigger(
            "wf",
            TriggerDef(type=TriggerType.PROCESS_SPAWN, config={
                "cgroup_root": str(tmp_path),
                "cgroup_pattern": "svc-*",
                "poll_interval": 0.25,
            }),
            lambda n, c: None,
        )
        trig.start()
        assert trig.active
        trig.stop()
        assert not trig.active


# ======================================================================
# DBusSignalTrigger — boundary (fake bus)
# ======================================================================


class _FakeMatch:
    """Stand-in for dbus.connection.SignalMatch.

    Real dbus-python returns a SignalMatch whose ``.remove()`` replays
    the original filters back to the connection; the trigger relies on
    that method, so the fake exposes it.
    """

    def __init__(self, bus):
        self._bus = bus
        self.removed = False

    def remove(self):
        self.removed = True
        self._bus.removed.append(self)


class _FakeBus:
    def __init__(self, raise_on_add=False):
        self.added = []
        self.removed = []
        self._raise = raise_on_add

    def add_signal_receiver(self, handler, **kwargs):
        if self._raise:
            raise RuntimeError("boom")
        match = _FakeMatch(self)
        self.added.append((handler, kwargs, match))
        return match

    def remove_signal_receiver(self, match):
        self.removed.append(match)


class TestDBusSignalTriggerBoundary:
    def _trigger(self, config, bus, calls=None):
        if calls is None:
            calls = []
        trig = DBusSignalTrigger(
            "wf",
            TriggerDef(type=TriggerType.DBUS_SIGNAL, config=config),
            lambda n, c: calls.append(c),
            bus=bus, run_own_loop=False,
        )
        return trig, calls

    def test_subscribe_match_rule(self):
        bus = _FakeBus()
        trig, _ = self._trigger({
            "bus_name": "org.example.Svc",
            "interface": "org.example.Iface",
            "member": "Changed",
            "object_path": "/org/example",
        }, bus)
        trig.start()
        assert trig.active
        assert len(bus.added) == 1
        _handler, kwargs, _match = bus.added[0]
        assert kwargs["signal_name"] == "Changed"
        assert kwargs["dbus_interface"] == "org.example.Iface"
        assert kwargs["bus_name"] == "org.example.Svc"
        assert kwargs["path"] == "/org/example"

    def test_member_omitted_matches_all(self):
        bus = _FakeBus()
        trig, _ = self._trigger({
            "interface": "org.example.Iface",
        }, bus)
        trig.start()
        _handler, kwargs, _match = bus.added[0]
        assert "signal_name" not in kwargs
        assert kwargs["dbus_interface"] == "org.example.Iface"

    def test_signal_fires_callback(self):
        bus = _FakeBus()
        trig, calls = self._trigger({
            "interface": "org.example.Iface", "member": "Changed",
        }, bus)
        trig.start()
        handler = bus.added[0][0]
        handler("alpha", 42)
        assert len(calls) == 1
        assert calls[0]["args"] == ["alpha", 42]
        assert calls[0]["member"] == "Changed"

    def test_stop_removes_receiver(self):
        bus = _FakeBus()
        trig, _ = self._trigger({"interface": "org.example.Iface"}, bus)
        trig.start()
        match = bus.added[0][2]
        trig.stop()
        assert not trig.active
        assert bus.removed == [match]

    def test_subscribe_failure_is_isolated(self):
        bus = _FakeBus(raise_on_add=True)
        trig, _ = self._trigger({"interface": "org.example.Iface"}, bus)
        # Must not raise; just stays inactive.
        trig.start()
        assert not trig.active

    def test_late_signal_after_stop_is_dropped(self):
        bus = _FakeBus()
        trig, calls = self._trigger({"interface": "org.example.Iface"}, bus)
        trig.start()
        handler = bus.added[0][0]
        trig.stop()
        # A signal delivered after stop() (still queued on the host loop)
        # must not start a run.
        handler("late")
        assert calls == []


class TestQbusEventTrigger:
    def test_resolves_broker_defaults(self):
        bus = _FakeBus()
        trig = QbusEventTrigger(
            "wf",
            TriggerDef(type=TriggerType.QBUS_EVENT,
                       config={"event": "RulesReloaded"}),
            lambda n, c: None,
            bus=bus, run_own_loop=False,
        )
        trig.start()
        _h, kwargs, _m = bus.added[0]
        assert kwargs["signal_name"] == "RulesReloaded"
        assert kwargs["bus_name"] == "org.qdistro.AdminBroker1"
        assert kwargs["dbus_interface"] == "org.qdistro.AdminBroker1"

    def test_event_alias_member(self):
        bus = _FakeBus()
        trig = QbusEventTrigger(
            "wf",
            TriggerDef(type=TriggerType.QBUS_EVENT, config={}),
            lambda n, c: None,
            bus=bus, run_own_loop=False,
        )
        trig.start()
        # Defaults to RulesReloaded when no event configured.
        assert bus.added[0][1]["signal_name"] == "RulesReloaded"


# ======================================================================
# DBusSignalTrigger — real session-bus round-trip
# ======================================================================

try:
    import dbus  # noqa: F401
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib  # noqa: F401
    _HAVE_DBUS = True
except Exception:  # pragma: no cover
    _HAVE_DBUS = False


@pytest.mark.skipif(
    not _HAVE_DBUS or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
    reason="no session bus available",
)
def test_dbus_signal_real_session_bus():
    # Private connection with the GLib main loop attached: the shared
    # dbus.SessionBus() cache may hold a loop-less connection created by
    # an earlier test, on which exporting the emitter would raise.
    glib_loop = DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus(private=True, mainloop=glib_loop)
    name = f"org.qdistro.WorkflowTest{uuid.uuid4().hex[:8]}"
    bus_name = dbus.service.BusName(name, bus)
    iface = name

    class Emitter(dbus.service.Object):
        def __init__(self):
            super().__init__(bus_name, "/org/qdistro/WorkflowTest")

        @dbus.service.signal(iface, signature="s")
        def Pinged(self, msg):
            pass

    emitter = Emitter()
    fired = []
    # Drive a short-lived GLib main loop on THIS (main) thread rather than
    # letting the trigger spin its own loop thread. A live PyQt6
    # QApplication left by earlier tests installs Qt's glib event
    # dispatcher and acquires the default GLib context on the main thread,
    # which starves a background MainLoop.run(); running the loop here (on
    # the owning thread, recursively acquirable) dispatches reliably
    # regardless of test order. Production uses run_own_loop=False sharing
    # the broker's single loop.
    loop = GLib.MainLoop()

    def on_fire(n, c):
        fired.append(c)
        loop.quit()

    trig = DBusSignalTrigger(
        "wf",
        TriggerDef(type=TriggerType.DBUS_SIGNAL, config={
            "bus": "session", "bus_name": name,
            "interface": iface, "member": "Pinged",
        }),
        on_fire,
        run_own_loop=False,
    )
    trig.start()
    assert trig.active
    try:
        # Emit once the loop is running, and cap the wait with a safety
        # timeout that quits the loop if the signal never arrives.
        GLib.timeout_add(50, lambda: (emitter.Pinged("hello"), False)[1])
        GLib.timeout_add(5000, lambda: (loop.quit(), False)[1])
        loop.run()
    finally:
        trig.stop()
    assert fired, "signal was not delivered"
    assert fired[0]["args"] == ["hello"]
    assert fired[0]["member"] == "Pinged"
