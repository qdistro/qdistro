"""Unit tests for AuthBackend fprintd hardening.

Item 1: every D-Bus await on the fingerprint path (connect, both
introspects, GetDefaultDevice, Claim, VerifyStart, and the verify
result) is timeout-bounded and fails CLOSED — a hang anywhere routes to
the PAM/password fallback instead of wedging the locker.

dbus-next is a lazy import inside `_fprint_async`, so we inject a fake
module via `sys.modules` and drive `_fprint_async` directly on an event
loop with a tiny timeout.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("USER", "tester")

import pytest
from qdlocker.auth import AuthBackend, AuthOutcome


async def _noop_async():
    """Stand-in for AuthBackend._fprint_async that does nothing — lets a test
    drive the real _fprint_worker() purely for its busy-clear / re-arm
    `finally` logic without touching D-Bus."""
    return None


# ---- fake dbus-next plumbing ------------------------------------------------


class _FakeBusType:
    SYSTEM = "system"


def _install_fake_dbus(monkeypatch, *, bus):
    """Install a fake `dbus_next` + `dbus_next.aio` exposing MessageBus
    whose `.connect()` resolves to `bus`."""
    aio = types.ModuleType("dbus_next.aio")

    class _MessageBus:
        def __init__(self, bus_type=None):
            self._bus_type = bus_type

        async def connect(self):
            return await bus.connect_impl()

    aio.MessageBus = _MessageBus

    root = types.ModuleType("dbus_next")
    root.BusType = _FakeBusType
    root.aio = aio

    monkeypatch.setitem(sys.modules, "dbus_next", root)
    monkeypatch.setitem(sys.modules, "dbus_next.aio", aio)


class _FakeBus:
    """Configurable fake system bus. Each phase can be told to hang
    (await forever) so the corresponding wait_for fires its timeout."""

    def __init__(self, *, hang=None):
        self.hang = hang or set()
        self.disconnected = False

    async def _maybe_hang(self, phase):
        if phase in self.hang:
            await asyncio.Event().wait()  # never completes

    async def connect_impl(self):
        await self._maybe_hang("connect")
        return self

    async def introspect(self, service, path):
        if "Manager" in path:
            await self._maybe_hang("mgr_introspect")
        else:
            await self._maybe_hang("dev_introspect")
        return MagicMock()

    def get_proxy_object(self, service, path, intro):
        return _FakeProxy(self)

    async def disconnect(self):
        self.disconnected = True


class _FakeProxy:
    def __init__(self, bus):
        self._bus = bus

    def get_interface(self, name):
        if name.endswith("Manager"):
            return _FakeManager(self._bus)
        return _FakeDevice(self._bus)


class _FakeManager:
    def __init__(self, bus):
        self._bus = bus

    async def call_get_default_device(self):
        await self._bus._maybe_hang("get_device")
        return "/net/reactivated/Fprint/Device/0"


class _FakeDevice:
    def __init__(self, bus):
        self._bus = bus
        self._status_cb = None

    def on_verify_status(self, cb):
        self._status_cb = cb

    def off_verify_status(self, cb):
        self._status_cb = None

    async def call_claim(self, user):
        await self._bus._maybe_hang("claim")

    async def call_verify_start(self, finger):
        await self._bus._maybe_hang("verify_start")

    async def call_verify_stop(self):
        await self._bus._maybe_hang("verify_stop")

    async def call_release(self):
        pass


def _make_backend():
    # Tiny timeout so the hang tests resolve fast. PAM disabled at the
    # point we care about — we only assert _record_fprintd_failure fired.
    return AuthBackend(max_fprintd_failures=1, fprintd_timeout_s=0.05)


# ---- item 1: each phase hangs → times out → fails closed -------------------


@pytest.mark.cheat_aware(
    protects="every D-Bus await on the fingerprint unlock path is "
    "timeout-bounded and fails CLOSED — a hang anywhere records a "
    "failure and never emits SUCCESS (the locker stays locked)",
    severity="critical",
    cheats=[
        "drop the 'phase != connect' branches from the parametrize list",
        "assert fallback.called without also asserting no SUCCESS emitted",
        "widen the timeout/raise fprintd_timeout_s so the hang resolves",
        "treat a timed-out verify as a match to 'fix' a flaky test",
    ],
    consequence="a wedged or hostile fprintd hangs the locker or, worse, "
    "a timeout is read as a successful fingerprint and the screen unlocks "
    "without a real auth result",
)
@pytest.mark.parametrize(
    "phase",
    ["connect", "mgr_introspect", "get_device", "dev_introspect",
     "claim", "verify_start"],
)
def test_hang_at_each_phase_times_out_and_falls_back(monkeypatch, phase):
    bus = _FakeBus(hang={phase})
    _install_fake_dbus(monkeypatch, bus=bus)

    backend = _make_backend()
    fallback = MagicMock()
    monkeypatch.setattr(backend, "_record_fprintd_failure", fallback)
    # start_pam shouldn't be needed but stub it to be safe
    monkeypatch.setattr(backend, "start_pam", MagicMock())

    emitted = []
    backend.outcome.connect(lambda p: emitted.append(p))

    # Run the async path; must return quickly (timeout), not hang.
    asyncio.run(asyncio.wait_for(backend._fprint_async(), timeout=5.0))

    # Failed closed: recorded a failure, never emitted SUCCESS.
    assert fallback.called, f"phase {phase} did not record a failure"
    assert all(
        not (isinstance(p, tuple) and p[0] is AuthOutcome.SUCCESS)
        for p in emitted
    ), f"phase {phase} spuriously emitted SUCCESS"

    # Bus is cleaned up on every path that got far enough to connect.
    if phase != "connect":
        assert bus.disconnected, f"phase {phase} leaked the bus"


@pytest.mark.parametrize("phase", ["connect", "claim", "verify_start"])
def test_environmental_timeout_starts_pam_immediately(monkeypatch, phase):
    # Default 3-strike threshold; a single environmental timeout must
    # still fail CLOSED to PAM right away rather than waiting out the
    # strikes. A wedged claim/verify-start is environmental too (codex
    # findings 1 & H1).
    bus = _FakeBus(hang={phase})
    _install_fake_dbus(monkeypatch, bus=bus)

    backend = AuthBackend(max_fprintd_failures=3, fprintd_timeout_s=0.05)
    started = MagicMock()
    monkeypatch.setattr(backend, "start_pam", started)

    asyncio.run(asyncio.wait_for(backend._fprint_async(), timeout=5.0))

    started.assert_called_once()
    assert backend._fprintd_unavailable is True
    # Exactly one strike recorded (no double-count).
    assert backend._fprintd_failures == 1


def test_verify_result_timeout_is_non_environmental_single_strike(monkeypatch):
    # No finger presented in time: the verify-RESULT wait times out. This
    # is a real (non-environmental) miss — one strike, fprintd stays
    # available, and with threshold>1 PAM is NOT started yet. Also proves
    # the old double-count (except + else) is gone.
    # verify_start completes, then nothing fires the status cb → the
    # result_future wait times out.
    bus = _FakeBus()
    _install_fake_dbus(monkeypatch, bus=bus)
    backend = AuthBackend(max_fprintd_failures=3, fprintd_timeout_s=0.05)
    started = MagicMock()
    monkeypatch.setattr(backend, "start_pam", started)

    asyncio.run(asyncio.wait_for(backend._fprint_async(), timeout=5.0))

    assert backend._fprintd_failures == 1  # single strike, no double-count
    assert backend._fprintd_unavailable is False
    started.assert_not_called()
    assert bus.disconnected


def test_no_match_is_single_strike(monkeypatch):
    # Wrong finger (verify-no-match, done=True): one strike, not env.
    bus = _FakeBus()
    _install_fake_dbus(monkeypatch, bus=bus)
    backend = AuthBackend(max_fprintd_failures=3, fprintd_timeout_s=0.05)
    started = MagicMock()
    monkeypatch.setattr(backend, "start_pam", started)

    orig_vs = _FakeDevice.call_verify_start

    async def fire_no_match(self, finger):
        await orig_vs(self, finger)
        self._status_cb("verify-no-match", True)

    monkeypatch.setattr(_FakeDevice, "call_verify_start", fire_no_match)
    asyncio.run(asyncio.wait_for(backend._fprint_async(), timeout=5.0))

    assert backend._fprintd_failures == 1
    assert backend._fprintd_unavailable is False
    started.assert_not_called()


def test_stale_generation_worker_side_effects_dropped(monkeypatch):
    # A slow fprintd worker captured generation N, but a fresh lock
    # advanced to N+1 before it recorded its no-match. Its failure must
    # not poison the fresh session's counter nor start PAM (finding 3).
    bus = _FakeBus()
    _install_fake_dbus(monkeypatch, bus=bus)

    backend = AuthBackend(max_fprintd_failures=1, fprintd_timeout_s=0.05)
    started = MagicMock()
    monkeypatch.setattr(backend, "start_pam", started)

    # Worker captures gen 0; deliver a no-match (done=True), but advance
    # the session before the result is recorded.
    orig_vs = _FakeDevice.call_verify_start

    async def fire_no_match_after_relock(self, finger):
        await orig_vs(self, finger)
        backend.reset_session()  # advance to generation 1
        assert self._status_cb is not None
        self._status_cb("verify-no-match", True)

    monkeypatch.setattr(
        _FakeDevice, "call_verify_start", fire_no_match_after_relock
    )

    asyncio.run(asyncio.wait_for(backend._fprint_async(), timeout=5.0))

    started.assert_not_called()
    assert backend._fprintd_failures == 0  # fresh session untouched


@pytest.mark.cheat_aware(
    protects="a deviceless host (fprintd answers GetDefaultDevice with a "
    "DBusError 'No devices available') fails CLOSED — records an "
    "environmental failure and starts PAM, instead of letting the error "
    "escape the worker uncaught and leaving the user with no prompt",
    severity="critical",
    cheats=[
        "only catch asyncio.TimeoutError on GetDefaultDevice (the original bug)",
        "swallow the error in _fprint_worker without recording a failure",
        "assert recorded but not that PAM fallback (start_pam) was reached",
    ],
    consequence="on a laptop with no enrolled/working reader the locker arms "
    "fprintd on every lock, the DBusError escapes uncaught, no failure is "
    "recorded, and the password prompt never appears until a keystroke — a "
    "fail-OPEN-to-stuck regression once touch-to-unlock is armed on lock",
)
def test_get_default_device_dbus_error_fails_closed(monkeypatch):
    bus = _FakeBus()
    _install_fake_dbus(monkeypatch, bus=bus)

    backend = AuthBackend(max_fprintd_failures=3, fprintd_timeout_s=0.05)
    started = MagicMock()
    monkeypatch.setattr(backend, "start_pam", started)

    async def raise_no_device(self):
        raise RuntimeError("No devices available")  # stand-in for DBusError

    monkeypatch.setattr(_FakeManager, "call_get_default_device", raise_no_device)

    emitted = []
    backend.outcome.connect(lambda p: emitted.append(p))

    # Must NOT raise out of the worker; the async path returns cleanly.
    asyncio.run(asyncio.wait_for(backend._fprint_async(), timeout=5.0))

    # Environmental → unavailable latched, PAM started, never SUCCESS.
    assert backend._fprintd_unavailable is True
    started.assert_called_once()
    assert all(
        not (isinstance(p, tuple) and p[0] is AuthOutcome.SUCCESS)
        for p in emitted
    )
    assert bus.disconnected


def test_device_introspect_error_fails_closed(monkeypatch):
    # The device introspect (after GetDefaultDevice) raising a non-timeout
    # error must also fail CLOSED rather than escape the worker.
    bus = _FakeBus()
    _install_fake_dbus(monkeypatch, bus=bus)

    backend = AuthBackend(max_fprintd_failures=3, fprintd_timeout_s=0.05)
    started = MagicMock()
    monkeypatch.setattr(backend, "start_pam", started)

    orig_introspect = _FakeBus.introspect

    async def introspect_raise_on_device(self, service, path):
        if "Manager" in path:
            return await orig_introspect(self, service, path)
        raise RuntimeError("device introspect failed")

    monkeypatch.setattr(_FakeBus, "introspect", introspect_raise_on_device)

    asyncio.run(asyncio.wait_for(backend._fprint_async(), timeout=5.0))

    assert backend._fprintd_unavailable is True
    started.assert_called_once()
    assert bus.disconnected


@pytest.mark.cheat_aware(
    protects="arming the sensor while a PRIOR-generation worker is still busy "
    "schedules a re-arm; once that worker clears _fprintd_busy a FRESH worker "
    "is started for the current lock — so a relock racing an in-flight worker "
    "still gets a fingerprint verify (and PAM fallback) without a keystroke",
    severity="high",
    cheats=[
        "make occupy_fingerprint_sensor a plain no-op when busy (the bug)",
        "re-arm regardless of generation, including for a superseded lock",
        "spawn the re-arm worker without re-setting _fprintd_busy (two at once)",
    ],
    consequence="a screen that relocks while a previous fprintd worker is in "
    "its D-Bus cleanup is left with no fprintd verify and no PAM prompt until "
    "the user types — reintroducing the dormant-sensor bug this change fixes",
)
def test_rearm_when_busy_starts_fresh_worker_on_clear(monkeypatch):
    backend = AuthBackend(max_fprintd_failures=1, fprintd_timeout_s=0.05)
    spawned = []
    monkeypatch.setattr(
        backend, "_spawn_fprint_worker",
        lambda: spawned.append(backend._current_generation()),
    )

    # Simulate a prior-generation (gen 1) worker already in flight.
    backend.reset_session()  # generation -> 1
    with backend._state_lock:
        backend._fprintd_busy = True
        backend._fprintd_busy_generation = backend._session_generation

    # A relock advances the generation, then arms while still busy.
    backend.reset_session()  # generation -> 2
    backend.occupy_fingerprint_sensor(True)
    # Suppressed (busy), but a re-arm is now pending for gen 2.
    assert spawned == []
    assert backend._fprintd_rearm_generation == 2

    # The old worker finishes: run the real worker with a no-op async body so
    # its `finally` clears busy and honors the pending re-arm.
    monkeypatch.setattr(backend, "_fprint_async", _noop_async)
    backend._fprint_worker()
    assert spawned == [2], "re-arm did not start a fresh worker for the current lock"
    assert backend._fprintd_rearm_generation is None


def test_same_generation_arm_while_busy_is_noop(monkeypatch):
    """A duplicate arm for the CURRENT lock (e.g. a keystroke while the
    lock-start worker is still running) must NOT schedule a redundant
    re-arm — the sensor is already armed for this session."""
    backend = AuthBackend(max_fprintd_failures=1, fprintd_timeout_s=0.05)
    spawned = []
    monkeypatch.setattr(
        backend, "_spawn_fprint_worker", lambda: spawned.append(True)
    )
    # Worker for the current generation is already in flight.
    with backend._state_lock:
        backend._fprintd_busy = True
        backend._fprintd_busy_generation = backend._session_generation

    backend.occupy_fingerprint_sensor(True)  # duplicate arm, same generation
    assert backend._fprintd_rearm_generation is None, "redundant re-arm scheduled"

    # Worker finishes: no extra worker is owed.
    monkeypatch.setattr(backend, "_fprint_async", _noop_async)
    backend._fprint_worker()
    assert spawned == [], "same-generation duplicate arm spawned an extra worker"
    assert backend._fprintd_busy is False


def test_rearm_for_superseded_generation_is_dropped(monkeypatch):
    backend = AuthBackend(max_fprintd_failures=1, fprintd_timeout_s=0.05)
    spawned = []
    monkeypatch.setattr(
        backend, "_spawn_fprint_worker", lambda: spawned.append(True)
    )
    with backend._state_lock:
        backend._fprintd_busy = True
        # A re-arm was requested for generation 1...
        backend._session_generation = 1
        backend._fprintd_rearm_generation = 1
        # ...but a newer lock already advanced to generation 2.
        backend._session_generation = 2

    monkeypatch.setattr(backend, "_fprint_async", _noop_async)
    backend._fprint_worker()
    assert spawned == [], "re-armed a worker for a superseded lock session"
    assert backend._fprintd_busy is False


def test_rearm_skipped_when_env_unavailable(monkeypatch):
    backend = AuthBackend(max_fprintd_failures=1, fprintd_timeout_s=0.05)
    spawned = []
    monkeypatch.setattr(
        backend, "_spawn_fprint_worker", lambda: spawned.append(True)
    )
    with backend._state_lock:
        backend._fprintd_busy = True
        backend._fprintd_rearm_generation = backend._session_generation
        backend._fprintd_unavailable = True  # fprintd died this session

    monkeypatch.setattr(backend, "_fprint_async", _noop_async)
    backend._fprint_worker()
    assert spawned == [], "re-armed fprintd after it was declared unavailable"
    assert backend._fprintd_busy is False


def test_reset_session_clears_transient_unavailable_latch():
    # A transient wedge latched _fprintd_unavailable; the next lock must
    # clear it so fprintd is retried (codex round-3 note).
    backend = AuthBackend(max_fprintd_failures=1, fprintd_timeout_s=0.05)
    backend._fprintd_unavailable = True
    backend.reset_session()
    assert backend._fprintd_unavailable is False


def test_reset_session_keeps_disabled_fprintd_unavailable():
    # When fprintd is disabled at construction it must stay unavailable.
    backend = AuthBackend(fprintd_enabled=False, fprintd_timeout_s=0.05)
    assert backend._fprintd_unavailable is True
    backend.reset_session()
    assert backend._fprintd_unavailable is True


def test_successful_verify_emits_tagged_success(monkeypatch):
    bus = _FakeBus()
    _install_fake_dbus(monkeypatch, bus=bus)

    backend = _make_backend()
    # Bump generation once so we assert the real (non-zero) value flows.
    backend.reset_session()
    gen = backend._current_generation()

    emitted = []
    backend.outcome.connect(lambda p: emitted.append(p))

    # Drive the verify-match signal as soon as verify_start is awaited.
    orig_vs = _FakeDevice.call_verify_start

    async def fire_match(self, finger):
        await orig_vs(self, finger)
        # deliver a match on the registered listener
        assert self._status_cb is not None
        self._status_cb("verify-match", False)

    monkeypatch.setattr(_FakeDevice, "call_verify_start", fire_match)

    asyncio.run(asyncio.wait_for(backend._fprint_async(), timeout=5.0))

    assert (AuthOutcome.SUCCESS, gen) in emitted
    assert bus.disconnected
