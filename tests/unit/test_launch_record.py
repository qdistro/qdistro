"""Unit tests for the broker launch-record store
(qdistro_launch_record), permission-lineage Phase 1.
"""
from __future__ import annotations

import qdistro_launch_record as lr


class _Clock:
    """Injectable monotonic-ish clock for deterministic expiry tests."""
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _store(**kw):
    return lr.LaunchRecordStore(time_fn=kw.pop("time_fn", _Clock()), **kw)


def _reg(store, *, pid=1234, starttime=555, silo="work",
         engine="qdistro.tier1", app_id="qdistro.tier1.work", uid=2000):
    return store.register(
        silo=silo, uid=uid, pid=pid, starttime=starttime,
        exe="/usr/bin/firefox", selinux_label="x:y:qdistro_tier1_t:s0",
        cgroup="/user.slice/app", sandbox_engine=engine, app_id=app_id,
        instance_id="inst-1")


class TestRegisterAndFind:
    def test_register_returns_record_with_id(self):
        s = _store()
        rec = _reg(s)
        assert rec.record_id
        assert rec.silo == "work"
        assert rec.sandbox_engine == "qdistro.tier1"
        assert len(s) == 1

    def test_find_by_proc_exact(self):
        s = _store()
        rec = _reg(s, pid=42, starttime=999)
        found = s.find_by_proc(42, 999)
        assert found is not None
        assert found.record_id == rec.record_id

    def test_find_by_proc_starttime_mismatch_is_none(self):
        # Anti-PID-reuse: same pid, different starttime → no record.
        s = _store()
        _reg(s, pid=42, starttime=999)
        assert s.find_by_proc(42, 1000) is None

    def test_find_unknown_pid_none(self):
        s = _store()
        _reg(s, pid=42, starttime=999)
        assert s.find_by_proc(7, 7) is None

    def test_get_by_id(self):
        s = _store()
        rec = _reg(s)
        assert s.get(rec.record_id).record_id == rec.record_id
        assert s.get("nope") is None


class TestReRegister:
    def test_reregister_same_proc_replaces(self):
        s = _store()
        r1 = _reg(s, pid=42, starttime=999, silo="work")
        r2 = _reg(s, pid=42, starttime=999, silo="dev")
        assert r1.record_id != r2.record_id
        # Only the latest record survives for the live process.
        assert len(s) == 1
        assert s.find_by_proc(42, 999).silo == "dev"
        assert s.get(r1.record_id) is None


class TestExpiry:
    def test_expired_record_not_found(self):
        clk = _Clock(1000.0)
        s = lr.LaunchRecordStore(ttl_s=60, time_fn=clk)
        _reg(s, pid=42, starttime=999)
        clk.t = 1000.0 + 61
        assert s.find_by_proc(42, 999) is None

    def test_get_expired_returns_none(self):
        clk = _Clock(1000.0)
        s = lr.LaunchRecordStore(ttl_s=60, time_fn=clk)
        rec = _reg(s, pid=42, starttime=999)
        clk.t = 1100.0
        assert s.get(rec.record_id) is None

    def test_reap_expired_counts(self):
        clk = _Clock(1000.0)
        s = lr.LaunchRecordStore(ttl_s=60, time_fn=clk)
        _reg(s, pid=1, starttime=1)
        _reg(s, pid=2, starttime=2)
        clk.t = 2000.0
        assert s.reap_expired() == 2
        assert len(s) == 0

    def test_ttl_zero_never_expires(self):
        clk = _Clock(1000.0)
        s = lr.LaunchRecordStore(ttl_s=0, time_fn=clk)
        _reg(s, pid=42, starttime=999)
        clk.t = 10 ** 9
        assert s.find_by_proc(42, 999) is not None


class TestRevoke:
    def test_revoke_proc(self):
        s = _store()
        _reg(s, pid=42, starttime=999)
        assert s.revoke_proc(42, 999) is True
        assert s.find_by_proc(42, 999) is None
        assert s.revoke_proc(42, 999) is False


class TestCapacity:
    def test_eviction_at_cap(self):
        clk = _Clock(1000.0)
        s = lr.LaunchRecordStore(ttl_s=3600, max_records=3, time_fn=clk)
        for i in range(3):
            clk.t = 1000.0 + i  # distinct issued_at
            s.register(silo=f"s{i}", uid=2000, pid=100 + i,
                       starttime=i, exe="/x")
        assert len(s) == 3
        # Adding a 4th evicts one (soonest-to-expire == oldest issued).
        clk.t = 1010.0
        s.register(silo="s3", uid=2000, pid=200, starttime=9, exe="/x")
        assert len(s) == 3
        # The first-issued record (pid 100) should be gone.
        assert s.find_by_proc(100, 0) is None
        assert s.find_by_proc(200, 9) is not None
