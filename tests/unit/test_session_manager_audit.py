"""Durable-audit tests for the session-manager lifecycle.

Proves that every privileged lifecycle mutation (create/delete/start/
stop/freeze/resume) — and refusals — write a durable, queryable audit
row carrying the caller identity (uid/pid/exe) + outcome, and that the
rows survive a fresh open of the SQLite store (durability).

Reuses the same `_FakeOps` side-effect adapter pattern as
test_session_manager.py: the pure state machine + the real _AuditLog
SQLite store run as-is; only the useradd/cgroup/systemctl boundary is
faked.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import qdistro_session_manager as sm
from qdistro_session_manager import (
    BadState, SiloBusy, State, UnknownSilo, _AuditLog, _SiloStore,
)


# ---------------------------------------------------------------------------
# Fake side-effect adapter (same shape as test_session_manager._FakeOps)
# ---------------------------------------------------------------------------

class _FakeOps:
    def __init__(self):
        self.users: dict[str, int] = {}
        self.cgroups: set[str] = set()
        self.cgroup_pids_map: dict[str, list[int]] = {}
        self.cgroup_frozen: dict[str, bool] = {}
        self.useradd_should_fail = False
        self.kill_drains = True

    def user_exists(self, name: str) -> bool:
        return name in self.users

    def uid_exists(self, uid: int) -> bool:
        return int(uid) in self.users.values()

    def useradd(self, name: str, uid: int) -> None:
        if self.useradd_should_fail:
            raise subprocess.CalledProcessError(1, ["useradd", name])
        self.users[name] = int(uid)

    def userdel(self, name: str) -> None:
        self.users.pop(name, None)

    def make_state_dir(self, name: str, uid: int):
        return Path("/var/lib/qdistro/silos") / name

    def remove_state_dir(self, name: str) -> None:
        pass

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
        for name in list(self.cgroups):
            if unit.startswith(f"qdshell-session-{name}@"):
                self.cgroup_pids_map.setdefault(name, []).append(99000)

    def systemctl_stop(self, unit: str) -> None:
        pass

    def kill_pids(self, pids, sig: int) -> None:
        if self.kill_drains:
            for name in list(self.cgroup_pids_map.keys()):
                self.cgroup_pids_map[name] = []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CALLER = {"uid": 1000, "pid": 4242, "exe": "/usr/bin/qdistro-admin"}


@pytest.fixture
def ops() -> _FakeOps:
    return _FakeOps()


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "session_manager_audit.sqlite"


@pytest.fixture
def audit(audit_path: Path) -> _AuditLog:
    return _AuditLog(audit_path)


@pytest.fixture
def store(ops: _FakeOps, tmp_path: Path, audit: _AuditLog) -> _SiloStore:
    return _SiloStore(ops, config_path=tmp_path / "silos.yaml", audit=audit)


def _rows_for(audit: _AuditLog, action: str, silo: str) -> list[dict]:
    return [r for r in audit.tail(1000)
            if r["action"] == action and r["silo"] == silo]


# ---------------------------------------------------------------------------
# _AuditLog primitive
# ---------------------------------------------------------------------------

class TestAuditLogPrimitive:
    def test_record_and_tail_roundtrip(self, audit: _AuditLog):
        rid = audit.record("create", "work", decision="allow",
                           reason="uid=2000", caller=CALLER)
        assert rid >= 1
        rows = audit.tail()
        assert len(rows) == 1
        r = rows[0]
        assert r["action"] == "create"
        assert r["silo"] == "work"
        assert r["decision"] == "allow"
        assert r["reason"] == "uid=2000"
        assert r["caller_uid"] == 1000
        assert r["caller_pid"] == 4242
        assert r["caller_exe"] == "/usr/bin/qdistro-admin"
        assert isinstance(r["ts"], int) and r["ts"] > 0

    def test_db_file_is_0600(self, audit: _AuditLog):
        mode = audit.path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_synchronous_full_for_durability(self, audit: _AuditLog):
        # FULL == 2 in SQLite's PRAGMA synchronous encoding.
        val = audit._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert int(val) == 2
        jmode = audit._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(jmode).lower() == "wal"

    def test_tail_orders_most_recent_first(self, audit: _AuditLog):
        audit.record("create", "a", caller=CALLER)
        audit.record("start", "a", caller=CALLER)
        rows = audit.tail()
        assert [r["action"] for r in rows] == ["start", "create"]


# ---------------------------------------------------------------------------
# Each lifecycle mutation writes an allow row with caller identity
# ---------------------------------------------------------------------------

class TestLifecycleAuditRows:
    def test_create_writes_allow_row(self, store, audit):
        store.create("work", 2000, caller=CALLER)
        rows = _rows_for(audit, "create", "work")
        assert len(rows) == 1
        assert rows[0]["decision"] == "allow"
        assert rows[0]["caller_uid"] == 1000
        assert rows[0]["caller_pid"] == 4242
        assert rows[0]["caller_exe"] == "/usr/bin/qdistro-admin"

    def test_start_writes_allow_row(self, store, audit):
        store.create("work", 2000, caller=CALLER)
        store.start("work", caller=CALLER)
        rows = _rows_for(audit, "start", "work")
        assert len(rows) == 1
        assert rows[0]["decision"] == "allow"
        assert rows[0]["caller_uid"] == 1000

    def test_freeze_and_resume_write_allow_rows(self, store, audit):
        store.create("work", 2000, caller=CALLER)
        store.start("work", caller=CALLER)
        store.freeze("work", caller=CALLER)
        store.resume("work", caller=CALLER)
        assert len(_rows_for(audit, "freeze", "work")) == 1
        assert len(_rows_for(audit, "resume", "work")) == 1
        assert _rows_for(audit, "freeze", "work")[0]["decision"] == "allow"
        assert _rows_for(audit, "resume", "work")[0]["decision"] == "allow"

    def test_stop_writes_allow_row(self, store, audit):
        store.create("work", 2000, caller=CALLER)
        store.start("work", caller=CALLER)
        store.stop("work", grace_s=0, caller=CALLER)
        rows = _rows_for(audit, "stop", "work")
        assert len(rows) == 1
        assert rows[0]["decision"] == "allow"
        assert rows[0]["caller_pid"] == 4242

    def test_delete_writes_allow_row(self, store, audit):
        store.create("work", 2000, caller=CALLER)
        store.delete("work", caller=CALLER)
        rows = _rows_for(audit, "delete", "work")
        assert len(rows) == 1
        assert rows[0]["decision"] == "allow"

    def test_full_lifecycle_emits_every_action(self, store, audit):
        store.create("work", 2000, caller=CALLER)
        store.start("work", caller=CALLER)
        store.freeze("work", caller=CALLER)
        store.resume("work", caller=CALLER)
        store.stop("work", grace_s=0, caller=CALLER)
        store.delete("work", caller=CALLER)
        actions = {r["action"] for r in audit.tail(1000)
                   if r["decision"] == "allow"}
        assert actions == {"create", "start", "freeze", "resume",
                           "stop", "delete"}


# ---------------------------------------------------------------------------
# Refusals are audited too
# ---------------------------------------------------------------------------

class TestRefusalAuditRows:
    def test_start_unknown_silo_is_denied(self, store, audit):
        with pytest.raises(UnknownSilo):
            store.start("ghost", caller=CALLER)
        rows = _rows_for(audit, "start", "ghost")
        assert len(rows) == 1
        assert rows[0]["decision"] == "deny"
        assert rows[0]["caller_uid"] == 1000

    def test_stop_in_created_state_is_denied(self, store, audit):
        store.create("work", 2000, caller=CALLER)
        # Created cannot be stopped (only Active/Frozen/Stopped).
        with pytest.raises(BadState):
            store.stop("work", grace_s=0, caller=CALLER)
        rows = _rows_for(audit, "stop", "work")
        assert len(rows) == 1
        assert rows[0]["decision"] == "deny"

    def test_resume_non_frozen_is_denied(self, store, audit):
        # A Created (never-started) silo is neither Active nor Frozen, so
        # resume must refuse with BadState (resuming Active is the
        # idempotent no-op path, not a refusal).
        store.create("work", 2000, caller=CALLER)
        with pytest.raises(BadState):
            store.resume("work", caller=CALLER)
        rows = _rows_for(audit, "resume", "work")
        assert len(rows) == 1
        assert rows[0]["decision"] == "deny"

    def test_delete_active_silo_is_denied(self, store, audit):
        store.create("work", 2000, caller=CALLER)
        store.start("work", caller=CALLER)
        with pytest.raises(SiloBusy):
            store.delete("work", caller=CALLER)
        rows = _rows_for(audit, "delete", "work")
        assert len(rows) == 1
        assert rows[0]["decision"] == "deny"

    def test_create_side_effect_failure_is_error(self, ops, store, audit):
        ops.useradd_should_fail = True
        with pytest.raises(Exception):
            store.create("work", 2000, caller=CALLER)
        rows = _rows_for(audit, "create", "work")
        assert len(rows) == 1
        # useradd failure surfaces as a non-refusal error decision.
        assert rows[0]["decision"] in ("error", "deny")


# ---------------------------------------------------------------------------
# Durability: rows survive a fresh store/audit open
# ---------------------------------------------------------------------------

class TestDurability:
    def test_rows_survive_reload(self, ops, tmp_path, audit_path):
        audit1 = _AuditLog(audit_path)
        store1 = _SiloStore(ops, config_path=tmp_path / "silos.yaml",
                            audit=audit1)
        store1.create("work", 2000, caller=CALLER)
        store1.start("work", caller=CALLER)
        store1.stop("work", grace_s=0, caller=CALLER)
        audit1.close()

        # Re-open the audit DB from disk — a fresh connection, simulating
        # a daemon restart. The rows must still be there.
        audit2 = _AuditLog(audit_path)
        rows = audit2.tail(1000)
        actions = {(r["action"], r["decision"]) for r in rows}
        assert ("create", "allow") in actions
        assert ("start", "allow") in actions
        assert ("stop", "allow") in actions
        # Caller identity persisted across the reopen.
        create_row = [r for r in rows if r["action"] == "create"][0]
        assert create_row["caller_uid"] == 1000
        assert create_row["caller_exe"] == "/usr/bin/qdistro-admin"
        audit2.close()


# ---------------------------------------------------------------------------
# Auditing never breaks the lifecycle op + None-audit is safe
# ---------------------------------------------------------------------------

class TestAuditIsNonFatal:
    def test_store_without_audit_still_works(self, ops, tmp_path):
        store = _SiloStore(ops, config_path=tmp_path / "silos.yaml")
        # No audit sink: lifecycle must still succeed.
        store.create("work", 2000)
        store.start("work")
        store.stop("work", grace_s=0)
        store.delete("work")
        assert "work" not in ops.users

    def test_audit_write_failure_does_not_break_mutation(
            self, ops, tmp_path, audit):
        store = _SiloStore(ops, config_path=tmp_path / "silos.yaml",
                          audit=audit)
        # Break the audit sink mid-flight: record() now raises.
        def _boom(*a, **k):
            raise RuntimeError("audit db gone")
        audit.record = _boom  # type: ignore[assignment]
        # The mutation must still succeed despite the broken audit sink.
        silo = store.create("work", 2000, caller=CALLER)
        assert silo.state == State.CREATED
        assert ops.users == {"work": 2000}
