"""Unit tests for the permission-lineage resolver (qdistro_resolver),
Phase 2. The /proc readers are monkeypatched so the live-process facts
are deterministic.
"""
from __future__ import annotations

import pytest

import qdistro_proc_identity as pi
import qdistro_launch_record as lr
import qdistro_resolver as R


@pytest.fixture
def fake_proc(monkeypatch):
    """Install a controllable fake live process. Returns a dict the test
    mutates; the resolver reads through it."""
    state = {
        "exe": "/usr/bin/firefox",
        "starttime": 555,
        "uid": 2000,
        "label": "u:r:qdistro_tier1_t:s0",
        "cgroup": "/user.slice/app",
    }
    monkeypatch.setattr(
        pi, "read_exe_and_starttime",
        lambda pid: (state["exe"], state["starttime"]))
    monkeypatch.setattr(pi, "read_uid", lambda pid: state["uid"])
    monkeypatch.setattr(pi, "read_selinux_label", lambda pid: state["label"])
    monkeypatch.setattr(pi, "read_cgroup", lambda pid: state["cgroup"])
    return state


def _store_with_record(**overrides):
    s = lr.LaunchRecordStore()
    kw = dict(silo="work", uid=2000, pid=1234, starttime=555,
              exe="/usr/bin/firefox", selinux_label="u:r:qdistro_tier1_t:s0",
              cgroup="/user.slice/app", sandbox_engine="qdistro.tier1",
              app_id="qdistro.tier1.work", instance_id="i1")
    kw.update(overrides)
    s.register(**kw)
    return s


class TestUnknownPaths:
    def test_invalid_pid(self, fake_proc):
        subj = R.resolve_subject(0, lr.LaunchRecordStore())
        assert not subj.verified
        assert subj.silo == R.UNKNOWN_SILO
        assert subj.reason == "invalid-pid"

    def test_proc_gone(self, fake_proc):
        fake_proc["starttime"] = 0
        subj = R.resolve_subject(1234, lr.LaunchRecordStore())
        assert not subj.verified
        assert subj.reason == "proc-gone"

    def test_no_store(self, fake_proc):
        subj = R.resolve_subject(1234, None)
        assert not subj.verified
        assert subj.reason == "no-launch-record-store"
        # Still carries the live kernel facts for the tier-0 prompt path.
        assert subj.uid == 2000
        assert subj.exe == "/usr/bin/firefox"
        # ...but no silo / engine / app — forged claims can't satisfy.
        assert subj.sandbox_engine == ""
        assert subj.app_id == ""

    def test_no_record(self, fake_proc):
        subj = R.resolve_subject(1234, lr.LaunchRecordStore())
        assert not subj.verified
        assert subj.reason == "no-launch-record"

    def test_recycled_pid_starttime_differs(self, fake_proc):
        # Record stored at starttime 999; live process is starttime 555.
        store = _store_with_record(starttime=999)
        subj = R.resolve_subject(1234, store)
        assert not subj.verified
        assert subj.reason == "no-launch-record"


class TestVerified:
    def test_all_axes_match(self, fake_proc):
        store = _store_with_record()
        subj = R.resolve_subject(1234, store)
        assert subj.verified
        assert subj.silo == "work"
        assert subj.sandbox_engine == "qdistro.tier1"
        assert subj.app_id == "qdistro.tier1.work"
        assert subj.uid == 2000

    def test_selinux_off_both_sides_verified(self, fake_proc):
        # SELinux off → neither the record nor the live process has a
        # label → that axis is skipped on both sides, still verified.
        fake_proc["label"] = ""
        store = _store_with_record(selinux_label="")
        subj = R.resolve_subject(1234, store)
        assert subj.verified

    def test_missing_record_exe_skips_axis(self, fake_proc):
        # The record never captured an exe → skip the exe axis.
        store = _store_with_record(exe="?")
        subj = R.resolve_subject(1234, store)
        assert subj.verified

    def test_missing_record_cgroup_skips_axis(self, fake_proc):
        fake_proc["cgroup"] = ""
        store = _store_with_record(cgroup="")
        subj = R.resolve_subject(1234, store)
        assert subj.verified


class TestMismatchFailsClosed:
    def test_uid_mismatch(self, fake_proc):
        fake_proc["uid"] = 4242
        store = _store_with_record()
        subj = R.resolve_subject(1234, store)
        assert not subj.verified
        assert "record-mismatch" in subj.reason
        assert "uid" in subj.reason

    def test_unreadable_uid_fails_closed(self, fake_proc):
        # /proc/<pid>/status unreadable → read_uid None → fail closed
        # even though a record exists (no fail-open verified subject).
        fake_proc["uid"] = None
        store = _store_with_record()
        subj = R.resolve_subject(1234, store)
        assert not subj.verified
        assert "uid" in subj.reason

    def test_record_has_label_but_live_empty_fails_closed(self, fake_proc):
        # Record was minted with a label (SELinux-on host) but the live
        # read came back empty — suspicious, fail closed.
        fake_proc["label"] = ""
        store = _store_with_record()  # record carries a label
        subj = R.resolve_subject(1234, store)
        assert not subj.verified
        assert "label" in subj.reason

    def test_record_has_exe_but_live_unreadable_fails_closed(self, fake_proc):
        fake_proc["exe"] = "?"
        store = _store_with_record()  # record carries a real exe
        subj = R.resolve_subject(1234, store)
        assert not subj.verified
        assert "exe" in subj.reason

    def test_record_has_cgroup_but_live_empty_fails_closed(self, fake_proc):
        fake_proc["cgroup"] = ""
        store = _store_with_record()  # record carries a cgroup
        subj = R.resolve_subject(1234, store)
        assert not subj.verified
        assert "cgroup" in subj.reason

    def test_exe_mismatch(self, fake_proc):
        fake_proc["exe"] = "/usr/bin/evil"
        store = _store_with_record()
        subj = R.resolve_subject(1234, store)
        assert not subj.verified
        assert "exe" in subj.reason

    def test_label_mismatch(self, fake_proc):
        fake_proc["label"] = "u:r:unconfined_t:s0"
        store = _store_with_record()
        subj = R.resolve_subject(1234, store)
        assert not subj.verified
        assert "label" in subj.reason

    def test_cgroup_mismatch(self, fake_proc):
        fake_proc["cgroup"] = "/some/other/cg"
        store = _store_with_record()
        subj = R.resolve_subject(1234, store)
        assert not subj.verified
        assert "cgroup" in subj.reason
