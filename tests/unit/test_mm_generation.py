"""Tests for the dock-generation + display-lease reference state machine.

Reproduces and extends the D3 logic sim (the original asserts plus lease
expiry, fail-safe, input-never-stale, and the state x concern table). Pins the
contract the eventual daemon must satisfy. Pure-Python; a fake clock makes
lease-expiry deterministic.
"""
from __future__ import annotations

import pytest

from multimachine.generation import (
    DockSession, DockState, DisplayLease, concern_authority,
)


class FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def docked_session(ttl: float = 2.0) -> tuple[DockSession, FakeClock]:
    clk = FakeClock()
    s = DockSession(clock=clk, lease_ttl=ttl)
    s.begin_dock()
    s.complete_dock(output_id=1)
    return s, clk


# ---- D3 sim parity -------------------------------------------------------
class TestGenerationGuard:
    def test_current_generation_applied(self):
        s, _ = docked_session()
        assert s.recv(s.generation, "frame")
        assert s.recv(s.generation, "input_button")

    def test_stale_generation_rejected_after_redock(self):
        s, _ = docked_session()
        g1 = s.generation
        assert s.recv(g1, "frame")
        # undock + redock => new generation
        s.begin_undock(); s.complete_undock()
        s.begin_dock(); s.complete_dock(output_id=1)
        g2 = s.generation
        assert g2 != g1
        # delayed gen-1 replays must all be rejected
        assert not s.recv(g1, "input_button", b"up")
        assert not s.recv(g1, "frame", b"old")
        assert not s.recv(g1, "clipboard_offer", b"x")
        # fresh gen-2 still applied
        assert s.recv(g2, "input_motion")
        stale = [r for r in s.rejected if r[0] == g1 and r[2] == "stale-generation"]
        assert len(stale) == 3

    def test_suspend_resume_treated_as_new_negotiation(self):
        s, _ = docked_session()
        g1 = s.generation
        s.begin_undock(); s.complete_undock()
        s.begin_dock(); s.complete_dock(output_id=1)
        # a long-delayed gen-1 key after the gap is rejected
        assert not s.recv(g1, "input_key", b"a")


# ---- lease ---------------------------------------------------------------
class TestDisplayLease:
    def test_lease_valid_then_expires(self):
        s, clk = docked_session(ttl=2.0)
        assert s.lease.is_valid(clk())
        clk.advance(1.5)
        assert s.heartbeat()           # renew within ttl
        clk.advance(1.5)
        assert s.lease.is_valid(clk())  # still valid (renewed at 1.5)
        clk.advance(3.0)
        s.tick()                       # lease lapsed -> fail safe
        assert s.state is DockState.FAILED_SAFE
        assert s.lease is None

    def test_stale_generation_heartbeat_does_not_renew(self):
        s, clk = docked_session()
        lease = s.lease
        assert not lease.renew(clk(), generation=s.generation + 99)

    def test_expired_lease_cannot_be_resurrected_by_renew(self):
        # codex impl-3 finding 6: renew() itself must refuse an expired lease,
        # not rely on the DockSession caller's pre-check.
        s, clk = docked_session(ttl=1.0)
        lease = s.lease
        clk.advance(2.0)  # past ttl, no heartbeat in between
        assert lease.renew(clk(), generation=s.generation) is False
        assert not lease.is_valid(clk())

    def test_current_gen_frame_rejected_after_lease_lapse_without_tick(self):
        # codex impl-3 finding 7: a current-generation frame/control must be
        # rejected once the lease lapses, even if no tick()/heartbeat() ran.
        s, clk = docked_session(ttl=1.0)
        clk.advance(2.0)            # lease lapsed; no tick() called
        assert s.recv(s.generation, "frame") is False
        assert s.recv(s.generation, "clipboard_offer") is False
        assert any(r[2] == "no-valid-lease" for r in s.rejected)
        assert s.state is DockState.FAILED_SAFE

    def test_input_rejected_when_lease_invalid(self):
        s, clk = docked_session(ttl=1.0)
        clk.advance(2.0)               # lease lapsed but no tick yet
        # input must never be applied onto a panel we no longer hold
        assert not s.recv(s.generation, "input_button")
        assert any(r[2] == "no-valid-lease" for r in s.rejected)

    def test_pixels_allowed_input_blocked_distinction(self):
        # within a valid lease both apply; this guards the "input is stricter"
        # contract from the negative test above.
        s, clk = docked_session(ttl=5.0)
        assert s.recv(s.generation, "frame")
        assert s.recv(s.generation, "input_button")


# ---- state machine -------------------------------------------------------
class TestDockStateMachine:
    def test_no_traffic_accepted_outside_docked(self):
        clk = FakeClock()
        s = DockSession(clock=clk)
        assert not s.recv(0, "frame")           # standalone
        g = s.begin_dock()
        assert not s.recv(g, "frame")           # docking, not yet docked
        s.complete_dock(1)
        assert s.recv(g, "frame")               # docked
        s.begin_undock()
        assert not s.recv(g, "input_key")       # undocking

    def test_peer_lost_fails_safe(self):
        s, _ = docked_session()
        s.peer_lost()
        assert s.state is DockState.FAILED_SAFE
        assert s.lease is None
        assert not s.recv(s.generation, "frame")

    def test_can_redock_from_failed_safe(self):
        s, _ = docked_session()
        s.peer_lost()
        g = s.begin_dock()
        s.complete_dock(1)
        assert s.state is DockState.DOCKED
        assert s.recv(g, "frame")

    def test_illegal_transitions_raise(self):
        clk = FakeClock()
        s = DockSession(clock=clk)
        with pytest.raises(RuntimeError):
            s.complete_dock(1)         # not docking
        s.begin_dock()
        with pytest.raises(RuntimeError):
            s.begin_dock()             # already docking


# ---- concern table -------------------------------------------------------
class TestConcernTable:
    def test_docked_authority(self):
        assert concern_authority("composition", DockState.DOCKED) == "primary"
        assert concern_authority("lock", DockState.DOCKED) == "laptop"
        assert concern_authority("secondary_keyboard", DockState.DOCKED) == "dormant"

    def test_standalone_and_failsafe_are_local(self):
        for concern in ("composition", "seat", "clipboard"):
            assert concern_authority(concern, DockState.STANDALONE) == "local"
            assert concern_authority(concern, DockState.FAILED_SAFE) == "local"

    def test_clipboard_cleared_on_undock(self):
        assert concern_authority("clipboard", DockState.UNDOCKING) == "clear"

    def test_unknown_concern_raises(self):
        with pytest.raises(KeyError):
            concern_authority("nope", DockState.DOCKED)
