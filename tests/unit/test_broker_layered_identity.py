"""Tests for spec/25 §Phase-2 layered-identity capture.

The broker now snapshots three extra attributes per pending request:
  * exe_sha256        — SHA-256 of /proc/<pid>/exe (capped at 64 MiB).
  * selinux_label     — /proc/<pid>/attr/current (NUL-stripped).
  * cgroup            — unified-hierarchy cgroup path.

GetPending surfaces them so the admin-approval-app can render alongside
uid / pid / exe in the request detail pane. Process death between
RequestPermission and the read tolerated — empty string is acceptable
(spec/25: advisory data, not a gate).

Since the broker defers layered-identity IO to a thread pool (option e
from broker-serialization-concurrent-qsu.md), the tests use
_drain_layered_io() to wait for the pool and drain GLib idle callbacks
before asserting on the layered fields.

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

from gi.repository import GLib  # noqa: E402

import qdistro_admin_broker as B  # noqa: E402

from test_broker_check_permission import _StubBroker, NON_ADMIN_UID  # noqa: E402


def _drain_layered_io(broker: _StubBroker) -> None:
    """Wait for the broker's IO thread pool to finish all pending work
    and then drain GLib idle callbacks so _apply_layered_identity runs.
    """
    # Shut down the pool and wait for all futures to complete.
    broker._io_pool.shutdown(wait=True)
    # Re-create the pool for subsequent calls.
    import concurrent.futures
    broker._io_pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="stub-broker-io")
    # Drain GLib idle callbacks (the done_callback scheduled idle_add).
    ctx = GLib.MainContext.default()
    while ctx.iteration(False):
        pass


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
        # so /proc reads succeed during _enqueue. Use the resolved
        # /proc/<pid>/exe path (not sys.executable, which may be a
        # symlink like python3 -> python3.13) so the deferred
        # layered-identity checker recognizes the process as unchanged.
        proc_exe = os.readlink(f"/proc/{os.getpid()}/exe")
        broker.set_peer(uid=NON_ADMIN_UID, pid=os.getpid(),
                        exe=proc_exe, start=0)
        rid = broker.RequestPermission("test.layered", {"k": "v"})
        # Layered IO is deferred to the thread pool; drain it so the
        # idle callback populates the layered fields before we assert.
        _drain_layered_io(broker)
        # GetPending requires admin/root. Switch to the admin identity
        # after enqueueing the non-admin request.
        broker.set_peer(uid=B.ADMIN_UID)
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
        # layered_pending should be False after IO completes.
        assert not bool(row["layered_pending"])

    def test_get_pending_before_io_completes_shows_layered_pending(self, broker):
        """Before the IO pool delivers, GetPending shows layered_pending=True
        and empty layered fields. The admin app can render a spinner."""
        broker.set_peer(uid=NON_ADMIN_UID, pid=os.getpid(),
                        exe=sys.executable, start=0)
        rid = broker.RequestPermission("test.layered.pending", {"k": "v"})
        # Do NOT drain — check the immediate state.
        # The request should be in _pending with layered_pending=True.
        with broker._lock:
            req = broker._pending[rid]
            assert req.layered_pending is True
        broker.set_peer(uid=B.ADMIN_UID)
        out = broker.GetPending()
        assert len(out) == 1
        assert bool(out[0]["layered_pending"]) is True
        # Clean up the pool for subsequent tests.
        _drain_layered_io(broker)

    def test_get_pending_when_pid_invalid_yields_empty_strings(self, broker):
        # pid=0 -> /proc paths absent -> all three strings empty.
        # pid=0 is the no-IO fast path: layered_pending is False immediately.
        broker.set_peer(uid=NON_ADMIN_UID, pid=0, exe="?", start=0)
        rid = broker.RequestPermission("test.layered.missing", {})
        broker.set_peer(uid=B.ADMIN_UID)
        out = broker.GetPending()
        assert len(out) == 1
        row = out[0]
        assert int(row["id"]) == rid
        assert str(row["exe_sha256"]) == ""
        assert str(row["selinux_label"]) == ""
        assert str(row["cgroup"]) == ""
        assert not bool(row["layered_pending"])

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
        _drain_layered_io(broker)
        broker.set_peer(uid=B.ADMIN_UID)
        out = broker.GetPending()
        assert len(out) == 1
        # Either way: keys present, no exceptions raised.
        for key in ("exe_sha256", "selinux_label", "cgroup"):
            assert key in out[0]
        assert int(out[0]["id"]) == rid
