"""Tests for spec/25 §Phase-2 layered-identity capture.

The broker now snapshots three extra attributes per pending request:
  * exe_sha256        — SHA-256 of /proc/<pid>/exe (capped at 64 MiB).
  * selinux_label     — /proc/<pid>/attr/current (NUL-stripped).
  * cgroup            — unified-hierarchy cgroup path.

GetPending surfaces them so the admin-approval-app can render alongside
uid / pid / exe in the request detail pane. Process death between
RequestPermission and the read tolerated — empty string is acceptable
(spec/25: advisory data, not a gate).

Mirrors test_broker_check_permission's _StubBroker harness shape so we
don't need a live system bus.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("dbus")

import qdistro_admin_broker as B  # noqa: E402

from test_broker_check_permission import _StubBroker, NON_ADMIN_UID  # noqa: E402


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    d = tmp_path / "rules"
    d.mkdir()
    return d


@pytest.fixture
def broker(tmp_path: Path, rules_dir: Path) -> _StubBroker:
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rules_dir),
    )


class TestReadProcLayered:
    def test_self_pid_returns_all_three_keys(self):
        out = B._read_proc_layered(os.getpid())
        assert set(out) == {"exe_sha256", "selinux_label", "cgroup"}

    def test_self_pid_hash_matches_python_binary(self):
        out = B._read_proc_layered(os.getpid())
        # Re-hash sys.executable directly and compare. /proc/<pid>/exe
        # may resolve to a versioned name (python3.13) while
        # sys.executable is "python3" — readlink to dereference.
        exe = os.readlink(f"/proc/{os.getpid()}/exe")
        h = hashlib.sha256()
        with open(exe, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        # Big binaries: respect the cap. sys.executable is well under
        # 64 MiB on every platform we care about.
        assert os.path.getsize(exe) < B._EXE_HASH_BYTES_MAX
        assert out["exe_sha256"] == h.hexdigest()

    def test_dead_pid_returns_empty_strings(self):
        # PID 0 is the kernel idle pseudo-process; /proc/0 is absent
        # on Linux, so every read inside _read_proc_layered fails open
        # to "" exactly the way a vanished caller would.
        out = B._read_proc_layered(0)
        assert out == {"exe_sha256": "", "selinux_label": "", "cgroup": ""}

    def test_cgroup_unified_path_when_present(self):
        out = B._read_proc_layered(os.getpid())
        # Either unified ("/...") or the first cgroup line if a hybrid
        # hierarchy is in play. Both shapes should contain a slash.
        if out["cgroup"]:
            assert "/" in out["cgroup"]


class TestGetPendingSurfacesLayeredFields:
    def test_get_pending_returns_layered_keys(self, broker):
        # Enqueue a request as a non-admin caller using *our own pid*
        # so /proc reads succeed during _enqueue. PEER_EXE points at
        # qdshell which doesn't exist on the test host; what matters
        # for this test is that GetPending surfaces the new keys with
        # the right types — empty strings are acceptable when the
        # path can't be hashed.
        broker.set_peer(uid=NON_ADMIN_UID, pid=os.getpid(),
                        exe=sys.executable, start=0)
        rid = broker.RequestPermission("test.layered", {"k": "v"})
        # _StubBroker overrides set_peer to also drive _peer_info.
        # GetPending requires admin/root in production; the stub
        # bypasses authz so we can read the queue directly.
        out = broker.GetPending()
        assert len(out) == 1
        row = out[0]
        assert int(row["id"]) == rid
        for key in ("exe_sha256", "selinux_label", "cgroup"):
            assert key in row, f"GetPending missing {key}"
        # exe_sha256 should be a 64-char hex string for a process we
        # can read (the test runner itself).
        sha = str(row["exe_sha256"])
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)

    def test_get_pending_when_pid_invalid_yields_empty_strings(self, broker):
        # pid=0 -> /proc paths absent -> all three strings empty.
        broker.set_peer(uid=NON_ADMIN_UID, pid=0, exe="?", start=0)
        rid = broker.RequestPermission("test.layered.missing", {})
        out = broker.GetPending()
        assert len(out) == 1
        row = out[0]
        assert int(row["id"]) == rid
        assert str(row["exe_sha256"]) == ""
        assert str(row["selinux_label"]) == ""
        assert str(row["cgroup"]) == ""

    def test_layered_snapshot_survives_process_exit(self, broker, tmp_path):
        """The whole point of snapshotting at request time: even if the
        peer disappears before admin clicks, the captured identity
        still describes what was in /proc when the request landed.
        """
        # Spawn a short-lived child, capture its pid, let it exit,
        # then RequestPermission spoofing that pid. _read_proc_layered
        # should fail for the dead pid → empty strings; this verifies
        # the shape stays stable rather than crashing.
        import subprocess
        helper = tmp_path / "helper.sh"
        helper.write_text("#!/bin/sh\nexit 0\n")
        helper.chmod(0o755)
        p = subprocess.Popen([str(helper)])
        dead_pid = p.pid
        p.wait()

        broker.set_peer(uid=NON_ADMIN_UID, pid=dead_pid,
                        exe=str(helper), start=0)
        rid = broker.RequestPermission("test.layered.dead", {})
        out = broker.GetPending()
        assert len(out) == 1
        # Either way: keys present, no exceptions raised.
        for key in ("exe_sha256", "selinux_label", "cgroup"):
            assert key in out[0]
        assert int(out[0]["id"]) == rid
