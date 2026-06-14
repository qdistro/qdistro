"""Unit tests for qdistro-snap-swap (fableplan2 task 05).

The crash-consistent restore primitive: a writable clone of a snapshot is
materialized in a sibling temp, then swapped into state_path so a crash at
any step leaves state_path either the old or the restored contents — never
missing (task 01 makes an absent state_path a hard launch error).

These run on the host with mechanism=copy (no btrfs needed). The subvolume
mechanism and RENAME_EXCHANGE are exercised in the VM; here we force the
two-rename journal path to test crash recovery deterministically.
"""
from __future__ import annotations

import os

import qdistro_snap_swap as ss

import pytest


def _mkstate(root, name, payload):
    """Create a state dir with a sentinel file holding `payload`."""
    path = os.path.join(root, name)
    os.makedirs(path, mode=0o700)
    with open(os.path.join(path, "sentinel"), "w", encoding="utf-8") as fh:
        fh.write(payload)
    return path


def _read_sentinel(path):
    with open(os.path.join(path, "sentinel"), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# happy path — both swap methods
# --------------------------------------------------------------------------

@pytest.mark.parametrize("allow_exchange", [True, False])
def test_restore_swaps_contents_and_keeps_displaced_state(tmp_path, allow_exchange):
    root = str(tmp_path)
    state = _mkstate(root, "state", "LIVE-B")
    snap = _mkstate(root, "snap-A", "SNAP-A")

    result = ss.restore(snap, state, mechanism="copy", now=1000.0,
                        allow_exchange=allow_exchange)

    # state_path now holds the snapshot's contents.
    assert _read_sentinel(state) == "SNAP-A"
    # The displaced live state is kept aside, never deleted.
    rejected = result["rejected"]
    assert os.path.isdir(rejected)
    assert _read_sentinel(rejected) == "LIVE-B"
    assert "state-rejected-" in rejected
    # No journal left behind.
    assert not os.path.exists(ss._marker_path(state))
    # The snapshot source is untouched (we cloned it, not moved it).
    assert _read_sentinel(snap) == "SNAP-A"


def test_restore_refuses_missing_snapshot(tmp_path):
    state = _mkstate(str(tmp_path), "state", "LIVE")
    with pytest.raises(ss.SnapSwapError):
        ss.restore(os.path.join(str(tmp_path), "nope"), state, mechanism="copy")
    # state untouched.
    assert _read_sentinel(state) == "LIVE"


def test_restore_refuses_when_state_absent_and_no_journal(tmp_path):
    snap = _mkstate(str(tmp_path), "snap", "A")
    missing = os.path.join(str(tmp_path), "state")
    with pytest.raises(ss.SnapSwapError):
        ss.restore(snap, missing, mechanism="copy")


# --------------------------------------------------------------------------
# crash consistency — kill between materialize and swap → recover
# --------------------------------------------------------------------------

def test_crash_between_two_renames_recovers_to_restored_state(tmp_path, monkeypatch):
    """Simulate a kill AFTER state->rejected but BEFORE temp->state in the
    two-rename path. state_path is momentarily absent; recover() must finish
    the swap so state_path holds the restored snapshot."""
    root = str(tmp_path)
    state = _mkstate(root, "state", "LIVE-B")
    snap = _mkstate(root, "snap-A", "SNAP-A")

    real_rename = os.rename

    def crashing_rename(src, dst):
        # Crash exactly on the temp->state rename (the second move).
        if os.path.abspath(dst) == os.path.abspath(state) and ".snap-swap-" in src:
            raise KeyboardInterrupt("simulated crash mid-swap")
        return real_rename(src, dst)

    monkeypatch.setattr(ss.os, "rename", crashing_rename)
    with pytest.raises(KeyboardInterrupt):
        ss.restore(snap, state, mechanism="copy", now=2000.0, allow_exchange=False)

    # The crash left state_path missing — exactly the dangerous window.
    assert not os.path.lexists(state)
    monkeypatch.undo()

    # Recovery completes the swap: state_path holds the restored snapshot.
    outcome = ss.recover(state)
    assert outcome == "completed"
    assert os.path.isdir(state)
    assert _read_sentinel(state) == "SNAP-A"
    assert not os.path.exists(ss._marker_path(state))


def test_recover_aborts_a_materialized_but_unswapped_clone(tmp_path):
    """phase=materialized: the clone exists but was never swapped in. Old
    state is intact; recover() drops the orphan clone and clears the journal."""
    root = str(tmp_path)
    state = _mkstate(root, "state", "LIVE")
    temp = os.path.join(root, ".state.snap-swap-3000")
    os.makedirs(temp)
    with open(os.path.join(temp, "sentinel"), "w") as fh:
        fh.write("CLONE")
    rejected = state + "-rejected-3000"
    ss._write_marker(state, {"phase": ss._PHASE_MATERIALIZED, "temp": temp,
                             "rejected": rejected, "snapshot": "x"})

    outcome = ss.recover(state)
    assert outcome == "aborted"
    assert _read_sentinel(state) == "LIVE"      # old state retained
    assert not os.path.exists(temp)             # orphan clone dropped
    assert not os.path.exists(ss._marker_path(state))


def test_recover_finishes_post_exchange_cleanup(tmp_path):
    """phase=exchanged: RENAME_EXCHANGE already put the clone at state_path;
    temp holds the old state awaiting its rename to state-rejected. recover()
    finishes that rename."""
    root = str(tmp_path)
    state = _mkstate(root, "state", "RESTORED")   # clone already live
    temp = os.path.join(root, ".state.snap-swap-4000")
    os.makedirs(temp)
    with open(os.path.join(temp, "sentinel"), "w") as fh:
        fh.write("OLD")
    rejected = state + "-rejected-4000"
    ss._write_marker(state, {"phase": ss._PHASE_EXCHANGED, "temp": temp,
                             "rejected": rejected, "snapshot": "x"})

    outcome = ss.recover(state)
    assert outcome == "cleaned"
    assert _read_sentinel(state) == "RESTORED"
    assert _read_sentinel(rejected) == "OLD"     # old state preserved aside
    assert not os.path.exists(temp)
    assert not os.path.exists(ss._marker_path(state))


def test_recover_is_noop_without_journal(tmp_path):
    state = _mkstate(str(tmp_path), "state", "LIVE")
    assert ss.recover(state) == "none"
    assert _read_sentinel(state) == "LIVE"


def test_recover_is_idempotent(tmp_path):
    """Running recover twice is safe — the second call is a no-op."""
    root = str(tmp_path)
    state = _mkstate(root, "state", "LIVE")
    snap = _mkstate(root, "snap", "A")
    real_rename = os.rename

    # Force a crash window via a one-shot failing rename, then recover twice.
    import unittest.mock as mock
    def crashing_rename(src, dst):
        if os.path.abspath(dst) == os.path.abspath(state) and ".snap-swap-" in src:
            raise KeyboardInterrupt("crash")
        return real_rename(src, dst)
    with mock.patch.object(ss.os, "rename", crashing_rename):
        with pytest.raises(KeyboardInterrupt):
            ss.restore(snap, state, mechanism="copy", now=5000.0,
                       allow_exchange=False)

    assert ss.recover(state) == "completed"
    assert ss.recover(state) == "none"
    assert _read_sentinel(state) == "A"


def test_restore_recovers_a_stale_journal_before_starting(tmp_path):
    """A second restore call first heals any journal a prior crash left, then
    proceeds — state_path is never left in a half-swapped state across two
    restore attempts."""
    root = str(tmp_path)
    state = _mkstate(root, "state", "LIVE")
    snapA = _mkstate(root, "snapA", "A")
    snapB = _mkstate(root, "snapB", "B")

    # Leave a materialized-but-unswapped journal from a "previous" attempt.
    stale_temp = os.path.join(root, ".state.snap-swap-1")
    os.makedirs(stale_temp)
    with open(os.path.join(stale_temp, "sentinel"), "w") as fh:
        fh.write("STALE")
    ss._write_marker(state, {"phase": ss._PHASE_MATERIALIZED, "temp": stale_temp,
                             "rejected": state + "-rejected-1", "snapshot": "x"})

    # A fresh restore heals the stale journal (aborts it) then restores B.
    ss.restore(snapB, state, mechanism="copy", now=6000.0, allow_exchange=False)
    assert _read_sentinel(state) == "B"
    assert not os.path.exists(stale_temp)


# --------------------------------------------------------------------------
# the exchange path is selected when supported (real fs, no monkeypatching)
# --------------------------------------------------------------------------

def test_exchange_keeps_state_when_rejected_path_preexists(tmp_path):
    """Regression (codex r1 BLOCKER): a pre-existing state-rejected-<ts> must
    not make the swap displace or delete the displaced live state. The
    collision-proof rejected name keeps BOTH old states."""
    root = str(tmp_path)
    state = _mkstate(root, "state", "LIVE-B")
    snap = _mkstate(root, "snap-A", "SNAP-A")
    # A rejected dir from a prior swap already occupies the preferred name.
    prior = os.path.join(root, "state-rejected-9000")
    os.makedirs(prior)
    with open(os.path.join(prior, "sentinel"), "w") as fh:
        fh.write("PRIOR")

    result = ss.restore(snap, state, mechanism="copy", now=9000.0)
    assert _read_sentinel(state) == "SNAP-A"
    # The prior rejected dir is untouched, and the freshly displaced state is
    # kept under a distinct name — no state lost.
    assert _read_sentinel(prior) == "PRIOR"
    assert result["rejected"] != prior
    assert _read_sentinel(result["rejected"]) == "LIVE-B"


def test_exchange_real_failure_aborts_without_losing_state(tmp_path, monkeypatch):
    """Regression (codex r1 SHOULD): a non-ENOSYS errno from the exchange must
    NOT downgrade to two-rename; it raises and state_path stays intact, then
    recover() sets the clone aside without touching state."""
    root = str(tmp_path)
    state = _mkstate(root, "state", "LIVE")
    snap = _mkstate(root, "snap", "A")

    def boom(old, new):
        raise OSError(13, "EACCES")   # a real failure, not "unsupported"

    monkeypatch.setattr(ss, "exchange_supported", lambda: True)
    monkeypatch.setattr(ss, "_renameat2_exchange", boom)
    with pytest.raises(ss.SnapSwapError):
        ss.restore(snap, state, mechanism="copy", now=8000.0, allow_exchange=True)
    # state_path intact (the exchange never took effect).
    assert _read_sentinel(state) == "LIVE"
    monkeypatch.undo()
    # recover() heals the journal without touching state.
    ss.recover(state)
    assert _read_sentinel(state) == "LIVE"


def test_exchange_method_used_when_supported(tmp_path):
    if not ss.exchange_supported():
        pytest.skip("renameat2 not exposed by libc on this host")
    root = str(tmp_path)
    state = _mkstate(root, "state", "LIVE")
    snap = _mkstate(root, "snap", "A")
    result = ss.restore(snap, state, mechanism="copy", now=7000.0,
                        allow_exchange=True)
    # tmpfs/ext4/btrfs (pytest's tmp_path) all support RENAME_EXCHANGE, so the
    # atomic exchange path MUST be the one taken — asserting only "one of the
    # two" would be vacuous (restore can return nothing else). The skip above
    # already excludes a host whose libc lacks renameat2.
    assert result["method"] == "exchange"
    assert _read_sentinel(state) == "A"


def test_which_btrfs_sbin_fallback(monkeypatch):
    """snap_swap._which must find btrfs in /usr/sbin even when it is off PATH
    (the per-user rollback runs as the silo user / admin, whose PATH lacks
    /usr/sbin)."""
    import qdistro_snap_swap as qsw
    monkeypatch.setattr("shutil.which", lambda n: None)
    monkeypatch.setattr(qsw.os.path, "isfile", lambda p: p == "/usr/sbin/btrfs")
    monkeypatch.setattr(qsw.os, "access", lambda p, m: p == "/usr/sbin/btrfs")
    assert qsw._which("btrfs") == "/usr/sbin/btrfs"
    # a non-btrfs tool gets no sbin fallback (which() None -> None).
    assert qsw._which("definitely-not-a-real-tool-xyz") is None
