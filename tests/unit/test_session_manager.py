"""Unit tests for the session-manager state machine and silo store.

The store is exercised against a `_FakeOps` adapter that records
useradd/userdel/cgroup calls and pretends success. The pure state
machine + persistence file are real; no dbus or root needed.

Mirrors the `_StubBroker` pattern in test_broker_sendto.py — the
production class is run as-is, only the side-effecting boundary is
swapped.
"""
from __future__ import annotations

import signal
import time
from pathlib import Path

import pytest

import qdistro_session_manager as sm
from qdistro_session_manager import (
    BadArgument, BadState, Silo, SiloBusy, SiloExists, State, UnknownSilo,
    _SiloStore, _STATE_TRANSITIONS, validate_name, validate_uid,
)


# ---------------------------------------------------------------------------
# Fake side-effect adapter
# ---------------------------------------------------------------------------

class _FakeOps:
    def __init__(self):
        self.users: dict[str, int] = {}        # name → uid
        self.state_dirs: dict[str, tuple[int, int, int]] = {}  # name → (uid, gid, mode)
        self.cgroups: set[str] = set()
        self.cgroup_pids_map: dict[str, list[int]] = {}
        self.cgroup_frozen: dict[str, bool] = {}
        self.systemctl_calls: list[tuple[str, str]] = []
        self.killed: list[tuple[int, int]] = []
        self.useradd_should_fail = False
        self.userdel_should_fail = False
        # When True, kill_pids drains cgroup_pids_map for that cgroup
        # (simulates the launcher exiting after SIGTERM). Tests that
        # exercise the SIGKILL fallback set this False.
        self.kill_drains = True

    # ---- queries ----------------------------------------------------------

    def user_exists(self, name: str) -> bool:
        return name in self.users

    def uid_exists(self, uid: int) -> bool:
        return int(uid) in self.users.values()

    # ---- mutations --------------------------------------------------------

    def useradd(self, name: str, uid: int) -> None:
        if self.useradd_should_fail:
            import subprocess
            raise subprocess.CalledProcessError(1, ["useradd", name])
        self.users[name] = int(uid)

    def userdel(self, name: str) -> None:
        if self.userdel_should_fail:
            import subprocess
            raise subprocess.CalledProcessError(1, ["userdel", name])
        self.users.pop(name, None)

    def make_state_dir(self, name: str, uid: int):
        self.state_dirs[name] = (int(uid), int(uid), 0o700)
        return Path("/var/lib/qdistro/silos") / name

    def remove_state_dir(self, name: str) -> None:
        self.state_dirs.pop(name, None)

    def cgroup_create(self, name: str):
        self.cgroups.add(name)
        self.cgroup_pids_map.setdefault(name, [])
        return Path("/sys/fs/cgroup/qdistro-silos") / name

    def cgroup_remove(self, name: str) -> None:
        self.cgroups.discard(name)
        self.cgroup_pids_map.pop(name, None)
        self.cgroup_frozen.pop(name, None)

    def cgroup_freeze(self, name: str, frozen: bool) -> None:
        self.cgroup_frozen[name] = bool(frozen)

    def cgroup_pids(self, name: str) -> list[int]:
        return list(self.cgroup_pids_map.get(name, []))

    def cgroup_is_populated(self, name: str) -> bool:
        return bool(self.cgroup_pids_map.get(name))

    def systemctl_start(self, unit: str) -> None:
        self.systemctl_calls.append(("start", unit))
        # Pretend the launcher spawned a PID inside the cgroup. The
        # test sets cgroup_pids_map[name] later if it cares.
        for name in list(self.cgroups):
            if unit.startswith(f"qdshell-session-{name}@"):
                self.cgroup_pids_map.setdefault(name, []).append(99000)

    def systemctl_stop(self, unit: str) -> None:
        self.systemctl_calls.append(("stop", unit))

    def kill_pids(self, pids, sig: int) -> None:
        for pid in pids:
            self.killed.append((int(pid), int(sig)))
        if self.kill_drains:
            for name in list(self.cgroup_pids_map.keys()):
                self.cgroup_pids_map[name] = []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ops() -> _FakeOps:
    return _FakeOps()


@pytest.fixture
def store(ops: _FakeOps, tmp_path: Path) -> _SiloStore:
    return _SiloStore(ops, config_path=tmp_path / "silos.yaml")


@pytest.fixture
def signals():
    return []


@pytest.fixture
def store_with_signals(ops: _FakeOps, tmp_path: Path, signals):
    return _SiloStore(
        ops, config_path=tmp_path / "silos.yaml",
        on_change=lambda n, s: signals.append((n, s)),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_name(self):
        assert validate_name("work") == "work"

    @pytest.mark.parametrize("bad", [
        "Work", "1work", "work!", "work space", "",
        "x" * 33, "admin", "root", "qdistro",
    ])
    def test_invalid_name(self, bad):
        with pytest.raises(BadArgument):
            validate_name(bad)

    def test_uid_in_range(self):
        assert validate_uid(2000) == 2000
        assert validate_uid(60000) == 60000

    @pytest.mark.parametrize("bad", [0, 100, 1999, 60001, -1, "x"])
    def test_uid_out_of_range(self, bad):
        with pytest.raises(BadArgument):
            validate_uid(bad)


# ---------------------------------------------------------------------------
# Create / delete
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_writes_state_dir_and_user(self, store, ops):
        silo = store.create("work", 2000)
        assert silo.name == "work"
        assert silo.uid == 2000
        assert silo.state == State.CREATED
        assert ops.users == {"work": 2000}
        assert ops.state_dirs["work"] == (2000, 2000, 0o700)

    def test_create_emits_signal(self, store_with_signals, signals):
        store_with_signals.create("work", 2000)
        assert ("work", State.CREATED) in signals

    def test_create_duplicate_name(self, store):
        store.create("work", 2000)
        with pytest.raises(SiloExists):
            store.create("work", 2001)

    def test_create_duplicate_uid(self, store):
        store.create("work", 2000)
        with pytest.raises(SiloExists):
            store.create("other", 2000)

    def test_create_system_user_exists(self, store, ops):
        ops.users["work"] = 999
        with pytest.raises(SiloExists):
            store.create("work", 2000)

    def test_create_persists_to_yaml(self, store, tmp_path):
        store.create("work", 2000)
        text = (tmp_path / "silos.yaml").read_text()
        assert "name: work" in text
        assert "uid: 2000" in text
        assert "state: Created" in text


class TestDelete:
    def test_delete_refuses_active(self, store):
        store.create("work", 2000)
        store.start("work")
        with pytest.raises(SiloBusy):
            store.delete("work")

    def test_delete_refuses_frozen(self, store):
        store.create("work", 2000)
        store.start("work")
        store.freeze("work")
        with pytest.raises(SiloBusy):
            store.delete("work")

    def test_delete_after_stop(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        store.stop("work")
        store.delete("work")
        assert "work" not in ops.users
        assert "work" not in ops.state_dirs
        with pytest.raises(UnknownSilo):
            store.get("work")

    def test_delete_from_created_state(self, store, ops):
        store.create("work", 2000)
        store.delete("work")
        assert "work" not in ops.users

    def test_delete_unknown(self, store):
        with pytest.raises(UnknownSilo):
            store.delete("nonexistent")


# ---------------------------------------------------------------------------
# Start / stop / freeze / resume
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_start_creates_cgroup_and_unit(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        assert "work" in ops.cgroups
        assert any(call == ("start", "qdshell-session-work@2000.service")
                   for call in ops.systemctl_calls)
        assert store.get("work").state == State.ACTIVE

    def test_start_is_idempotent(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        store.start("work")  # no-op, no second systemctl call
        starts = [c for c in ops.systemctl_calls if c[0] == "start"]
        assert len(starts) == 1

    def test_freeze_then_resume(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        store.freeze("work")
        assert store.get("work").state == State.FROZEN
        assert ops.cgroup_frozen["work"] is True
        store.resume("work")
        assert store.get("work").state == State.ACTIVE
        assert ops.cgroup_frozen["work"] is False

    def test_freeze_idempotent(self, store):
        store.create("work", 2000)
        store.start("work")
        store.freeze("work")
        store.freeze("work")  # no-op
        assert store.get("work").state == State.FROZEN

    def test_resume_from_active_is_idempotent(self, store):
        store.create("work", 2000)
        store.start("work")
        store.resume("work")
        assert store.get("work").state == State.ACTIVE

    def test_resume_from_stopped_rejected(self, store):
        store.create("work", 2000)
        store.start("work")
        store.stop("work")
        with pytest.raises(BadState):
            store.resume("work")

    def test_stop_sends_sigterm(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        # systemctl_start put PID 99000 in the cgroup.
        store.stop("work", grace_s=0)
        sigterms = [k for k in ops.killed if k[1] == signal.SIGTERM]
        assert sigterms, "expected at least one SIGTERM"
        assert store.get("work").state == State.STOPPED

    def test_stop_sigkill_after_grace(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        # Pretend the process refuses to exit on SIGTERM.
        ops.kill_drains = False
        store.stop("work", grace_s=0)
        sigkills = [k for k in ops.killed if k[1] == signal.SIGKILL]
        assert sigkills, "expected at least one SIGKILL after grace"

    def test_stop_unfreezes_first(self, store, ops):
        store.create("work", 2000)
        store.start("work")
        store.freeze("work")
        store.stop("work", grace_s=0)
        assert ops.cgroup_frozen.get("work", False) is False
        assert store.get("work").state == State.STOPPED


# ---------------------------------------------------------------------------
# State-machine table
# ---------------------------------------------------------------------------

class TestStateMachine:
    def test_illegal_freeze_from_created(self, store):
        store.create("work", 2000)
        with pytest.raises(BadState):
            store.freeze("work")

    def test_illegal_stop_from_created(self, store):
        store.create("work", 2000)
        with pytest.raises(BadState):
            store.stop("work")

    def test_transitions_table_pinned(self):
        # Pin the table so an accidental relaxation surfaces as a
        # test failure. If the design changes, update this assertion.
        assert _STATE_TRANSITIONS[State.CREATED] == frozenset(
            (State.ACTIVE, State.DELETING))
        assert _STATE_TRANSITIONS[State.ACTIVE] == frozenset(
            (State.FROZEN, State.STOPPING, State.STOPPED))
        assert _STATE_TRANSITIONS[State.FROZEN] == frozenset(
            (State.ACTIVE, State.STOPPING, State.STOPPED))
        assert _STATE_TRANSITIONS[State.STOPPING] == frozenset(
            (State.STOPPED,))
        assert _STATE_TRANSITIONS[State.STOPPED] == frozenset(
            (State.ACTIVE, State.DELETING))


# ---------------------------------------------------------------------------
# Persistence + autostart
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_yaml_round_trip(self, ops, tmp_path):
        s1 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        s1.create("work", 2000)
        s1.start("work")
        # New store on the same path picks up the saved file.
        s2 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        loaded = s2.get("work")
        assert loaded.uid == 2000
        assert loaded.state == State.ACTIVE

    def test_yaml_has_schema_comment(self, store, tmp_path):
        store.create("work", 2000)
        text = (tmp_path / "silos.yaml").read_text()
        assert "qdistro-session-manager persistence file" in text
        assert "Schema:" in text
        assert "autostart:" in text

    def test_autostart_pass_starts_marked_silos(self, ops, tmp_path):
        s1 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        s1.create("work", 2000, autostart=True)
        s1.create("scratch", 2001, autostart=False)
        # Simulate reboot: drop the in-memory state, reload from disk.
        ops.cgroups.clear()
        ops.cgroup_pids_map.clear()
        ops.systemctl_calls.clear()
        s2 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        started = s2.autostart_pass()
        assert "work" in started
        assert "scratch" not in started
        assert s2.get("work").state == State.ACTIVE
        # scratch was never started, autostart=False → stays Created.
        assert s2.get("scratch").state == State.CREATED

    def test_active_silo_restarts_across_reboot(self, ops, tmp_path):
        s1 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        s1.create("work", 2000)
        s1.start("work")  # Active gets persisted
        # Reboot: cgroups gone, but the state file still says Active.
        ops.cgroups.clear()
        ops.cgroup_pids_map.clear()
        ops.systemctl_calls.clear()
        s2 = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        started = s2.autostart_pass()
        assert "work" in started


# ---------------------------------------------------------------------------
# Review fixups: H1/H2/H3/SEC-H2 regression coverage
# ---------------------------------------------------------------------------

class TestReviewFixups:
    def test_start_rollback_emits_signal(self, ops, tmp_path, signals):
        """H2 — failed start() must emit SiloChanged(Stopped) after
        rolling back so the admin UI doesn't stick on Active."""
        store = _SiloStore(
            ops, config_path=tmp_path / "silos.yaml",
            on_change=lambda n, s: signals.append((n, s)),
        )
        store.create("work", 2000)
        # Force systemctl_start to fail.
        import subprocess

        def _boom(unit):
            raise subprocess.CalledProcessError(1, ["systemctl", "start", unit])

        ops.systemctl_start = _boom
        with pytest.raises(sm.SessionError):
            store.start("work")
        # Saw Active (the optimistic transition) then Stopped (rollback).
        states = [s for (n, s) in signals if n == "work"]
        assert State.ACTIVE in states
        assert states[-1] == State.STOPPED
        assert store.get("work").state == State.STOPPED

    def test_delete_rollback_emits_signal(self, ops, tmp_path, signals):
        """H2 — failed delete() (e.g. userdel error) rolls back to
        Stopped via _force_state, emitting SiloChanged."""
        store = _SiloStore(
            ops, config_path=tmp_path / "silos.yaml",
            on_change=lambda n, s: signals.append((n, s)),
        )
        store.create("work", 2000)
        store.start("work")
        store.stop("work", grace_s=0)
        ops.userdel_should_fail = True
        with pytest.raises(sm.SessionError):
            store.delete("work")
        # Saw Deleting then Stopped (rollback).
        states = [s for (n, s) in signals if n == "work"]
        assert State.DELETING in states
        assert states[-1] == State.STOPPED
        assert store.get("work").state == State.STOPPED

    def test_stop_recovers_from_mid_teardown_error(self, store, ops):
        """H3 — if cgroup_pids raises, stop() must not wedge the silo
        in STOPPING (which has no recovery path)."""
        store.create("work", 2000)
        store.start("work")

        # Make cgroup_pids blow up after the transition to STOPPING.
        def _boom(name):
            raise OSError("synthetic procfs corruption")

        ops.cgroup_pids = _boom
        # stop() should still drive the silo to STOPPED via _force_state.
        # It might raise; either way, the silo is no longer wedged.
        try:
            store.stop("work", grace_s=0)
        except sm.SessionError:
            pass
        assert store.get("work").state == State.STOPPED

    def test_concurrent_stop_both_observe_final_state(self, store, ops):
        """Round-2 review — a second stop() that races with an in-flight
        stop must wait for the in-flight teardown to finish and observe
        the final Stopped state, not return success prematurely."""
        import threading
        import time as _t

        store.create("work", 2000)
        store.start("work")
        assert store.get("work").state == State.ACTIVE

        # Make phase-2 teardown slow so the second caller is forced to
        # wait on the in-flight slot.
        orig_stop = ops.systemctl_stop

        def _slow_stop(unit):
            _t.sleep(0.3)
            orig_stop(unit)

        ops.systemctl_stop = _slow_stop

        results: list[tuple[str, str]] = []
        rlock = threading.Lock()

        def _do_stop():
            try:
                store.stop("work", grace_s=1)
                with rlock:
                    results.append(("ok", store.get("work").state))
            except Exception as e:  # noqa: BLE001
                with rlock:
                    results.append(("err", repr(e)))

        t1 = threading.Thread(target=_do_stop)
        t2 = threading.Thread(target=_do_stop)
        t1.start()
        _t.sleep(0.05)  # ensure t1 enters phase 2 first
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 2
        assert all(r[0] == "ok" for r in results), results
        # Both callers must observe the silo at its final Stopped state.
        assert all(r[1] == State.STOPPED for r in results), results
        assert store.get("work").state == State.STOPPED

    def test_stop_reentrant_from_on_change_does_not_deadlock(
            self, ops, tmp_path):
        """Round-3 review — the final Stopped on_change must fire AFTER the
        in-flight marker is cleared, so a callback that re-enters stop() on
        the same thread doesn't deadlock waiting on a marker only this
        thread can clear."""
        store_ref: dict = {}
        reentered: list[bool] = []

        def on_change(name, state):
            if name == "work" and state == State.STOPPED and not reentered:
                reentered.append(True)
                # Re-enter stop() on the same thread from the callback.
                store_ref["store"].stop("work", grace_s=0)

        store = _SiloStore(
            ops, config_path=tmp_path / "silos.yaml", on_change=on_change)
        store_ref["store"] = store
        store.create("work", 2000)
        store.start("work")
        # Must return without hanging.
        store.stop("work", grace_s=0)
        assert reentered == [True]
        assert store.get("work").state == State.STOPPED

    def test_load_drops_row_with_invalid_name(self, ops, tmp_path, caplog):
        """SEC-H2 — hand-edited silos.yaml with a path-traversal name
        must be rejected at load(), not silently accepted."""
        cfg = tmp_path / "silos.yaml"
        cfg.write_text(
            "silos:\n"
            '  - name: "../etc/passwd"\n'
            "    uid: 2000\n"
            "    state: Stopped\n"
            "    autostart: false\n"
            "    created_at: 0\n"
            "    last_change: 0\n"
        )
        import logging as _logging
        with caplog.at_level(_logging.ERROR):
            store = _SiloStore(ops, config_path=cfg)
        assert store.list_silos() == []
        assert any("invalid" in r.message.lower() or
                   "dropping" in r.message.lower()
                   for r in caplog.records)

    def test_load_drops_row_with_reserved_name(self, ops, tmp_path, caplog):
        """SEC-H2 — reserved names get the same treatment."""
        cfg = tmp_path / "silos.yaml"
        cfg.write_text(
            "silos:\n"
            "  - name: root\n"
            "    uid: 2000\n"
            "    state: Stopped\n"
            "    autostart: false\n"
            "    created_at: 0\n"
            "    last_change: 0\n"
        )
        import logging as _logging
        with caplog.at_level(_logging.ERROR):
            store = _SiloStore(ops, config_path=cfg)
        assert store.list_silos() == []

    def test_load_drops_row_with_bad_uid_range(self, ops, tmp_path):
        """SEC-H2 — out-of-range uid gets dropped, valid rows survive."""
        cfg = tmp_path / "silos.yaml"
        cfg.write_text(
            "silos:\n"
            "  - name: bogus\n"
            "    uid: 50\n"
            "    state: Stopped\n"
            "    autostart: false\n"
            "    created_at: 0\n"
            "    last_change: 0\n"
            "  - name: good\n"
            "    uid: 2000\n"
            "    state: Stopped\n"
            "    autostart: false\n"
            "    created_at: 0\n"
            "    last_change: 0\n"
        )
        store = _SiloStore(ops, config_path=cfg)
        names = [s.name for s in store.list_silos()]
        assert names == ["good"]
