"""Tests for qdistro_admin_audit.AuditLog."""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from qdistro_admin_audit import AuditLog


@pytest.fixture
def log(audit_db):
    return AuditLog(audit_db)


class TestAuditLog:
    def test_db_mode_0600(self, audit_db):
        AuditLog(audit_db)
        assert os.stat(audit_db).st_mode & 0o777 == 0o600

    def test_log_writes_one_row(self, log, audit_db):
        log.log(caller_uid=2000, caller_pid=1234, caller_exe="/usr/bin/python3",
                action="test.action", decision=True, scope="1h",
                source="prompt", approver_uid=1000)
        rows = sqlite3.connect(audit_db).execute("SELECT COUNT(*) FROM audit").fetchone()
        assert rows[0] == 1

    def test_log_columns_persist_correctly(self, log, audit_db):
        before = int(time.time())
        log.log(caller_uid=2000, caller_pid=1234, caller_exe="/usr/bin/p",
                action="x.y", decision=False, scope="24h",
                source="cache", approver_uid=None)
        after = int(time.time())
        row = sqlite3.connect(audit_db).execute(
            "SELECT ts, caller_uid, caller_pid, caller_exe, action, "
            "decision, scope, source, approver_uid FROM audit"
        ).fetchone()
        ts, uid, pid, exe, action, decision, scope, source, approver = row
        assert before <= ts <= after
        assert (uid, pid, exe, action) == (2000, 1234, "/usr/bin/p", "x.y")
        assert decision == 0
        assert (scope, source, approver) == ("24h", "cache", None)

    def test_log_accepts_null_scope_and_approver(self, log, audit_db):
        log.log(caller_uid=2000, caller_pid=1, caller_exe="/p",
                action="a", decision=True, scope=None,
                source="cache", approver_uid=None)
        row = sqlite3.connect(audit_db).execute(
            "SELECT scope, approver_uid FROM audit").fetchone()
        assert row == (None, None)

    def test_indexes_present(self, audit_db):
        AuditLog(audit_db)
        idx = {r[0] for r in sqlite3.connect(audit_db).execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        assert "audit_ts" in idx
        assert "audit_caller" in idx
        assert "audit_action" in idx

    def test_gc_deletes_old_rows(self, log, audit_db):
        now = int(time.time())
        old_ts = now - 100 * 86400
        recent_ts = now - 10 * 86400
        # Use the same connection the AuditLog holds so WAL sees the writes.
        with sqlite3.connect(audit_db) as db:
            db.execute("INSERT INTO audit (ts, caller_uid, caller_pid, caller_exe, "
                       "action, decision, scope, source, approver_uid) "
                       "VALUES (?, 2000, 1, '/p', 'a', 1, 'once', 'prompt', 1000)",
                       (old_ts,))
            db.execute("INSERT INTO audit (ts, caller_uid, caller_pid, caller_exe, "
                       "action, decision, scope, source, approver_uid) "
                       "VALUES (?, 2000, 1, '/p', 'a', 1, 'once', 'prompt', 1000)",
                       (recent_ts,))
            db.commit()
        # Keep rows newer than 30 days; the 100-day-old row goes.
        deleted = log.gc(30 * 86400)
        assert deleted == 1
        remaining = sqlite3.connect(audit_db).execute(
            "SELECT ts FROM audit").fetchall()
        assert remaining == [(recent_ts,)]

    def test_gc_empty_audit_returns_zero(self, log):
        assert log.gc(86400) == 0

    def test_gc_negative_raises(self, log):
        with pytest.raises(ValueError):
            log.gc(-1)


class TestAuditRecent:
    def test_recent_returns_newest_first(self, audit_db):
        log = AuditLog(audit_db)
        for i in range(3):
            log.log(caller_uid=2000 + i, caller_pid=100 + i,
                    caller_exe=f"/usr/bin/p{i}", action=f"a.{i}",
                    decision=bool(i % 2), scope="once", source="prompt",
                    approver_uid=1000, request_id=10 + i)
            time.sleep(0.01)  # so ts is monotonic even on fast clocks
        rows = log.recent(limit=10)
        assert len(rows) == 3
        actions = [r["action"] for r in rows]
        # Newest-first — "a.2" is the most recent insert.
        assert actions == ["a.2", "a.1", "a.0"]

    def test_recent_respects_limit(self, audit_db):
        log = AuditLog(audit_db)
        for i in range(5):
            log.log(caller_uid=2000, caller_pid=1, caller_exe="/p",
                    action=f"a.{i}", decision=True, scope="1h",
                    source="cache", approver_uid=None)
        assert len(log.recent(limit=3)) == 3
        assert len(log.recent(limit=100)) == 5

    def test_recent_normalises_limits(self, audit_db):
        log = AuditLog(audit_db)
        log.log(caller_uid=2000, caller_pid=1, caller_exe="/p",
                action="a", decision=True, scope="1h",
                source="cache", approver_uid=None)
        # 0 / negative → treated as 1 (no rows lost to a broken ceil).
        assert len(log.recent(limit=0)) == 1
        assert len(log.recent(limit=-5)) == 1
        # Huge limit capped at 10000, still returns what we have.
        assert len(log.recent(limit=9999999)) == 1

    def test_recent_preserves_nullable_columns(self, audit_db):
        log = AuditLog(audit_db)
        log.log(caller_uid=2000, caller_pid=1, caller_exe="/p",
                action="a", decision=True, scope=None,
                source="cache", approver_uid=None)
        row = log.recent()[0]
        assert row["scope"] is None
        assert row["approver_uid"] is None
        assert row["rule_path"] is None
        assert row["request_id"] is None

    def test_recent_shape(self, audit_db):
        log = AuditLog(audit_db)
        log.log(caller_uid=2000, caller_pid=1234, caller_exe="/usr/bin/x",
                action="q.r", decision=True, scope="forever",
                source="prompt", approver_uid=1000, request_id=42,
                rule_path="/etc/qdistro/rules.d/foo.yaml")
        row = log.recent()[0]
        expected = {"ts", "caller_uid", "caller_pid", "caller_exe",
                    "action", "decision", "scope", "source",
                    "approver_uid", "rule_path", "request_id",
                    "selinux_subj_type", "argv"}
        assert set(row.keys()) == expected
        assert row["decision"] is True
        assert row["action"] == "q.r"
        assert row["rule_path"] == "/etc/qdistro/rules.d/foo.yaml"
        assert row["request_id"] == 42
        # task(070): argv defaults to None when caller doesn't pass one.
        assert row["argv"] is None

    def test_argv_round_trip(self, audit_db):
        """task(070): qsu / RequestPermission rows stamp argv. recent()
        decodes the JSON column back to a list."""
        log = AuditLog(audit_db)
        log.log(caller_uid=2000, caller_pid=1234, caller_exe="/usr/bin/apt-get",
                action="qdistro.sudo.exec", decision=True, scope="forever_argv",
                source="prompt", approver_uid=1000,
                argv=["/usr/bin/apt-get", "update"])
        row = log.recent()[0]
        assert row["argv"] == ["/usr/bin/apt-get", "update"]

    def test_argv_none_persists_as_null(self, audit_db):
        """Clipboard / handoff rows pass argv=None; recent returns None."""
        log = AuditLog(audit_db)
        log.log(caller_uid=2000, caller_pid=1, caller_exe="/x",
                action="qdistro.clipboard.transfer:user1:admin",
                decision=False, scope=None, source="rule",
                approver_uid=None, argv=None)
        row = log.recent()[0]
        assert row["argv"] is None
