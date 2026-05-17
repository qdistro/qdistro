"""Tests for Broker.RecordSelinuxAvc (spec/30 step 7).

The broker exposes RecordSelinuxAvc as the audispd plugin's sole
entrypoint into the audit log. These tests pin the access boundary
(root only), the subject-type filter (qdistro_*_t only), and the
shape of the audit row that lands in the sqlite table.

Pattern matches test_broker_check_permission.py: subclass Broker to
skip dbus.service.Object setup and the GLib timers.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

pytest.importorskip("dbus")  # harness needs real dbus-python
import dbus  # noqa: E402

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402


class _StubBroker(Broker):
    def __init__(self, cache_db: str, audit_db: str, rules_dir: str,
                 *, peer_uid: int = 0):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        self._peer_uid = peer_uid
        self._peer_pid = 1
        self._peer_exe = "/usr/local/sbin/qdistro-audisp-plugin"
        self._peer_start = 0
        self.pending_signals = []
        self.decided_signals = []
        self.rules_reloaded_signals = []

    def set_peer_uid(self, uid: int) -> None:
        self._peer_uid = uid

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    def RequestPending(self, rid):  # type: ignore[override]
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        self.decided_signals.append((int(rid), str(decision)))

    def RulesReloaded(self, rule_count):  # type: ignore[override]
        self.rules_reloaded_signals.append(int(rule_count))


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
        peer_uid=0,
    )


# ---------------------------------------------------------------------
# Access boundary
# ---------------------------------------------------------------------

class TestAccessControl:
    def test_root_caller_accepted(self, broker):
        ok = broker.RecordSelinuxAvc(
            "staff_u:staff_r:qdistro_tier1_t:s0",
            "system_u:object_r:krb5_conf_t:s0",
            "file", "read", "denied",
            "firefox", 0, 1234,
            "/usr/bin/firefox", "/etc/krb5.conf",
            1614729383.123,
        )
        assert ok is True

    def test_admin_uid_rejected(self, broker):
        broker.set_peer_uid(B.ADMIN_UID)
        with pytest.raises(dbus.DBusException) as exc:
            broker.RecordSelinuxAvc(
                "staff_u:staff_r:qdistro_tier1_t:s0",
                "tcontext", "file", "read", "denied",
                "x", 0, 1, "", "", 0.0,
            )
        assert "RecordSelinuxAvc restricted to root" in str(exc.value)

    def test_arbitrary_uid_rejected(self, broker):
        broker.set_peer_uid(2000)
        with pytest.raises(dbus.DBusException):
            broker.RecordSelinuxAvc(
                "staff_u:staff_r:qdistro_tier1_t:s0",
                "tcontext", "file", "read", "denied",
                "x", 0, 1, "", "", 0.0,
            )


# ---------------------------------------------------------------------
# Subject-type filter
# ---------------------------------------------------------------------

class TestSubjectTypeFilter:
    def test_qdistro_tier1_accepted(self, broker):
        ok = broker.RecordSelinuxAvc(
            "staff_u:staff_r:qdistro_tier1_t:s0",
            "system_u:object_r:foo_t:s0",
            "file", "read", "denied",
            "x", 0, 1, "", "", 0.0,
        )
        assert ok is True

    def test_unrelated_subj_type_dropped(self, broker, tmp_path):
        ok = broker.RecordSelinuxAvc(
            "system_u:system_r:init_t:s0",
            "system_u:object_r:foo_t:s0",
            "file", "read", "denied",
            "systemd", 0, 1, "", "", 0.0,
        )
        assert ok is False
        # Audit table should still be empty.
        rows = sqlite3.connect(
            str(tmp_path / "audit.sqlite")
        ).execute("SELECT COUNT(*) FROM audit").fetchone()
        assert rows[0] == 0

    def test_malformed_scontext_dropped(self, broker):
        ok = broker.RecordSelinuxAvc(
            "garbage", "tcontext", "file", "read", "denied",
            "x", 0, 1, "", "", 0.0,
        )
        assert ok is False


# ---------------------------------------------------------------------
# Audit row shape
# ---------------------------------------------------------------------

class TestAuditRowShape:
    def test_row_carries_subj_type_and_action(self, broker, tmp_path):
        broker.RecordSelinuxAvc(
            "staff_u:staff_r:qdistro_tier1_t:s0",
            "system_u:object_r:krb5_conf_t:s0",
            "file", "read open", "denied",
            "firefox", 0, 1234,
            "/usr/bin/firefox", "/etc/krb5.conf",
            1614729383.123,
        )
        row = sqlite3.connect(
            str(tmp_path / "audit.sqlite")
        ).execute(
            "SELECT action, decision, source, selinux_subj_type, "
            "caller_pid, caller_exe FROM audit"
        ).fetchone()
        action, decision, source, subj_type, pid, exe = row
        assert action == "selinux.avc:file:read open"
        assert decision == 0  # denied
        assert subj_type == "qdistro_tier1_t"
        assert pid == 1234
        assert exe == "/usr/bin/firefox"
        assert "verdict=denied" in source
        assert "permissive=0" in source
        assert "path=/etc/krb5.conf" in source
        assert "comm=firefox" in source

    def test_granted_verdict_decision_true(self, broker, tmp_path):
        broker.RecordSelinuxAvc(
            "staff_u:staff_r:qdistro_tier1_t:s0",
            "tcontext", "dir", "search", "granted",
            "x", 0, 5, "", "", 0.0,
        )
        row = sqlite3.connect(
            str(tmp_path / "audit.sqlite")
        ).execute("SELECT decision FROM audit").fetchone()
        assert row[0] == 1

    def test_permissive_one_recorded_in_source(self, broker, tmp_path):
        broker.RecordSelinuxAvc(
            "staff_u:staff_r:qdistro_tier1_t:s0",
            "tcontext", "dir", "write", "denied",
            "sleep", 1, 99, "", "", 0.0,
        )
        row = sqlite3.connect(
            str(tmp_path / "audit.sqlite")
        ).execute("SELECT source FROM audit").fetchone()
        assert "permissive=1" in row[0]

    def test_long_strings_truncated(self, broker):
        # Ensure overly-long path / context don't blow past column
        # budgets. Should still write the row.
        ok = broker.RecordSelinuxAvc(
            "staff_u:staff_r:qdistro_tier1_t:s0",
            "x" * 4096,
            "file", "read", "denied",
            "x" * 4096, 0, 1,
            "y" * 4096, "z" * 4096,
            0.0,
        )
        assert ok is True


# ---------------------------------------------------------------------
# Migration is additive (existing audit rows survive without subj_type)
# ---------------------------------------------------------------------

class TestMigration:
    def test_selinux_subj_type_column_present(self, audit_db):
        AuditLog(audit_db)
        cols = {r[1] for r in sqlite3.connect(audit_db).execute(
            "PRAGMA table_info(audit)")}
        assert "selinux_subj_type" in cols

    def test_existing_rows_get_null_after_migration(self, audit_db):
        # Simulate a pre-migration DB by creating it without the new
        # column, inserting a row, then re-opening through AuditLog.
        with sqlite3.connect(audit_db) as db:
            db.execute("""
                CREATE TABLE audit (
                    id INTEGER PRIMARY KEY,
                    ts INTEGER NOT NULL,
                    caller_uid INTEGER NOT NULL,
                    caller_pid INTEGER NOT NULL,
                    caller_exe TEXT NOT NULL,
                    action TEXT NOT NULL,
                    decision INTEGER NOT NULL,
                    scope TEXT,
                    source TEXT NOT NULL,
                    approver_uid INTEGER
                )""")
            db.execute(
                "INSERT INTO audit (ts, caller_uid, caller_pid, "
                "caller_exe, action, decision, scope, source, "
                "approver_uid) "
                "VALUES (1, 2000, 1, '/p', 'a', 1, 'once', 'prompt', 1000)")
        log = AuditLog(audit_db)
        rows = log.recent(10)
        assert len(rows) == 1
        assert rows[0]["selinux_subj_type"] is None
