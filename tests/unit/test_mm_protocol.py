"""Protocol-layer fuzz tests (09 layer 2) for the dock generation + lease guard.

Fuzz/reorder/drop/replay messages by generation and assert the safety
invariants hold: stale-generation traffic is never applied, input is never
applied without a valid lease, and traffic outside the DOCKED state is rejected.
Uses hypothesis to explore interleavings the hand-written cases miss.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from multimachine.generation import (
    CONTROL_KINDS, INPUT_KINDS, PIXEL_KINDS, DockSession, DockState,
)
from multimachine.harness.netem import PROFILES, profile
from multimachine.harness.topology import PortLease, Screen, ScreenMap, Topology
from multimachine.harness.evidence import CaptureClass


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


ALL_KINDS = sorted(INPUT_KINDS | PIXEL_KINDS | CONTROL_KINDS)


# Events the fuzzer can emit against a session.
_event = st.one_of(
    st.tuples(st.just("recv"), st.integers(min_value=0, max_value=6),
              st.sampled_from(ALL_KINDS)),
    st.just(("redock",)),
    st.just(("undock",)),
    st.just(("peer_lost",)),
    st.just(("heartbeat",)),
    st.tuples(st.just("advance"), st.floats(min_value=0.0, max_value=5.0)),
)


class TestProtocolFuzz:
    @settings(max_examples=300, deadline=None)
    @given(events=st.lists(_event, max_size=40))
    def test_safety_invariants_hold_under_fuzz(self, events):
        clk = FakeClock()
        s = DockSession(clock=clk, lease_ttl=2.0)
        s.begin_dock()
        s.complete_dock(output_id=1)

        for ev in events:
            op = ev[0]
            if op == "recv":
                _, gen, kind = ev
                pre_state = s.state
                pre_gen = s.generation
                lease_ok = s.lease is not None and s.lease.is_valid(clk())
                accepted = s.recv(gen, kind)
                if accepted:
                    # SAFETY: only ever accept in DOCKED, current generation.
                    assert pre_state is DockState.DOCKED
                    assert gen == pre_gen
                    # input additionally requires a valid lease at apply time.
                    if kind in INPUT_KINDS:
                        assert lease_ok
            elif op == "redock":
                if s.state in (DockState.STANDALONE, DockState.FAILED_SAFE):
                    g = s.begin_dock()
                    s.complete_dock(output_id=1)
                    assert g == s.generation
            elif op == "undock":
                if s.state in (DockState.DOCKED, DockState.DOCKING):
                    s.begin_undock()
                    s.complete_undock()
            elif op == "peer_lost":
                s.peer_lost()
                assert s.state is DockState.FAILED_SAFE
            elif op == "heartbeat":
                s.heartbeat()
            elif op == "advance":
                clk.advance(ev[1])
                s.tick()

        # GLOBAL SAFETY: no applied message ever carried a generation that was
        # not the live one at apply time -> every applied gen is <= final gen,
        # and (since generation only increases and stale is rejected) no applied
        # entry has a generation greater than the session ever reached.
        assert all(g <= s.generation for g, _ in s.applied)

    @settings(max_examples=200, deadline=None)
    @given(stale_gen=st.integers(min_value=0, max_value=3),
           kind=st.sampled_from(ALL_KINDS),
           redocks=st.integers(min_value=1, max_value=5))
    def test_replayed_old_generation_always_rejected(self, stale_gen, kind, redocks):
        clk = FakeClock()
        s = DockSession(clock=clk, lease_ttl=10.0)
        first = s.begin_dock()
        s.complete_dock(output_id=1)
        for _ in range(redocks):
            s.begin_undock(); s.complete_undock()
            s.begin_dock(); s.complete_dock(output_id=1)
        # the first generation is now stale; any replay must be rejected.
        if first != s.generation:
            before = len(s.applied)
            assert s.recv(first, kind) is False
            assert len(s.applied) == before


class TestNetemProfiles:
    def test_all_named_profiles_present(self):
        assert set(PROFILES) == {"lan-clean", "lan-loaded", "wifi-good",
                                 "wifi-bad", "disconnect"}

    def test_tc_add_builds_expected_argv(self):
        p = profile("wifi-bad")
        cmd = p.tc_add("eth0")
        assert cmd[:6] == ["tc", "qdisc", "add", "dev", "eth0", "root"]
        assert "delay" in cmd and "40.0ms" in cmd
        assert "loss" in cmd and "2.0%" in cmd
        assert "rate" in cmd

    def test_disconnect_is_hard_drop(self):
        assert profile("disconnect").hard_drop is True

    def test_unknown_profile_raises(self):
        import pytest
        with pytest.raises(KeyError):
            profile("nope")


class TestPortLease:
    def test_acquire_skips_in_use_and_leased(self, tmp_path):
        in_use = {40000, 40001}
        lease = PortLease(tmp_path / "ports", lo=40000, hi=40010,
                          is_in_use=lambda p: p in in_use)
        a = lease.acquire()
        b = lease.acquire()
        assert a == 40002 and b == 40003   # 40000/40001 skipped, no dup
        lease.release(a)
        c = lease.acquire()
        assert c == 40000 if 40000 not in in_use else c == 40002  # reuse freed

    def test_no_duplicate_under_repeated_acquire(self, tmp_path):
        lease = PortLease(tmp_path / "ports", lo=41000, hi=41005,
                          is_in_use=lambda p: False)
        got = {lease.acquire() for _ in range(6)}
        assert len(got) == 6
        import pytest
        with pytest.raises(RuntimeError):
            lease.acquire()  # exhausted


class TestScreenMap:
    def test_default_topology_screen_mapping(self):
        topo = Topology.default()
        assert topo.screens.output_for("vm-a", 0) == 0
        assert topo.screens.output_for("vm-b", 0) == 1
        assert topo.screens.capture_class_for("vm-b", 0) == CaptureClass.VM_B_HOST

    def test_vm_b_is_decoded_remote_class(self):
        # the honesty rule: VM-B's monitor capture is the decoded remote half.
        topo = Topology.default()
        assert topo.screens.capture_class_for("vm-b", 0) == CaptureClass.VM_B_HOST
        assert (topo.screens.capture_class_for("vm-a", 1)
                == CaptureClass.VM_A_RDP_SOURCE)

    def test_unknown_screen_raises(self):
        import pytest
        with pytest.raises(KeyError):
            Topology.default().screens.output_for("vm-c", 9)
