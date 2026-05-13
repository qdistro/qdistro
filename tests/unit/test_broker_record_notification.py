"""Tests for qdistro_admin_broker.Broker.RecordNotification.

Phase-5 passive notification audit. The qdshell-side
Services/Qdshell/Notifications.qml fires this method (fire-and-forget)
once per FreeDesktop notification its NotificationServer accepts; the
broker writes a row to the audit log so admin's History tab can review
which apps emitted what under each silo's uid.

Bus signature: RecordNotification(s app_name, s summary, s body,
                                  i urgency) -> ""

Behavior pinned here:
  - row appears in audit.recent() with action="notification.posted"
    and decision=True
  - caller_uid carries the silo's uid (broker auto-detects via _peer_info,
    no client-supplied silo string)
  - source field carries app/urgency/summary/body in a stable shape
  - urgency outside {0, 1, 2} is normalized to 1 (normal)
  - over-long fields are truncated (app:128, summary:256, body:512)
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("dbus")

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402


SILO_UID_USER1 = 1001
SILO_UID_USER2 = 1002
PEER_EXE = "/usr/bin/qs"


class _StubBroker(Broker):
    def __init__(self, cache_db: str, audit_db: str, rules_dir: str):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        self._peer_uid = SILO_UID_USER1
        self._peer_pid = 100
        self._peer_exe = PEER_EXE
        self._peer_start = 0

    def set_peer(self, uid: int, pid: int = 100, exe: str = PEER_EXE) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)


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


class TestRecordNotificationBasics:
    def test_writes_audit_row(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("Slack", "New message", "from alice", 1)
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "notification.posted"
        assert bool(row["decision"]) is True
        assert int(row["caller_uid"]) == SILO_UID_USER1
        assert "app=Slack" in row["source"]
        assert "urgency=normal" in row["source"]
        assert "New message" in row["source"]
        assert "from alice" in row["source"]

    def test_per_uid_isolation(self, broker):
        """Each silo's uid is auto-detected; no cross-silo bleed."""
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("AppA", "S1", "B1", 0)
        broker.set_peer(uid=SILO_UID_USER2)
        broker.RecordNotification("AppB", "S2", "B2", 2)

        rows = broker.audit.recent(10)
        # recent() is newest-first
        uids = [int(r["caller_uid"]) for r in rows]
        assert uids[0] == SILO_UID_USER2
        assert uids[1] == SILO_UID_USER1
        assert "urgency=critical" in rows[0]["source"]
        assert "urgency=low" in rows[1]["source"]

    def test_returns_none(self, broker):
        # No D-Bus return payload: docs and signature both say "".
        broker.set_peer(uid=SILO_UID_USER1)
        out = broker.RecordNotification("a", "b", "c", 1)
        assert out is None


class TestRecordNotificationNormalization:
    def test_urgency_out_of_range_normalized_to_normal(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("App", "s", "b", 999)
        row = broker.audit.recent(1)[0]
        assert "urgency=normal" in row["source"]

    def test_urgency_negative_normalized_to_normal(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("App", "s", "b", -1)
        row = broker.audit.recent(1)[0]
        assert "urgency=normal" in row["source"]

    def test_summary_truncated_to_256(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        long_summary = "x" * 1000
        broker.RecordNotification("App", long_summary, "b", 1)
        row = broker.audit.recent(1)[0]
        # Source includes summary={!r} → repr adds two quotes; total
        # summary token length should not exceed 256 + 2 quotes.
        # Just confirm a 1000-char string didn't land verbatim.
        assert "x" * 1000 not in row["source"]
        assert "x" * 256 in row["source"]

    def test_body_truncated_to_512(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        long_body = "y" * 2000
        broker.RecordNotification("App", "s", long_body, 1)
        row = broker.audit.recent(1)[0]
        assert "y" * 2000 not in row["source"]
        assert "y" * 512 in row["source"]

    def test_app_truncated_to_128(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        long_app = "a" * 500
        broker.RecordNotification(long_app, "s", "b", 1)
        row = broker.audit.recent(1)[0]
        assert "a" * 500 not in row["source"]
        assert "a" * 128 in row["source"]

    def test_empty_app_falls_back_to_unknown(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("", "s", "b", 1)
        row = broker.audit.recent(1)[0]
        assert "app=(unknown)" in row["source"]


class TestRecordNotificationFailureSafety:
    def test_audit_failure_does_not_raise(self, broker, monkeypatch):
        """Broker must never raise out to the QML caller — qdshell
        treats this as fire-and-forget."""
        def boom(**kwargs):
            raise RuntimeError("audit storage offline")
        monkeypatch.setattr(broker.audit, "log", boom)
        broker.set_peer(uid=SILO_UID_USER1)
        # Should not raise.
        broker.RecordNotification("App", "s", "b", 1)


class TestRecordNotificationFieldEdges:
    def test_unicode_in_summary_preserved(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("App", "café ☕ 中文", "body", 1)
        row = broker.audit.recent(1)[0]
        assert "café ☕ 中文" in row["source"]

    def test_newlines_in_body_preserved_as_repr(self, broker):
        """body uses repr({!r}) so newlines surface as \\n; admin's
        History tab can render single-line entries without panel
        layout breakage."""
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("App", "s", "line1\nline2", 1)
        row = broker.audit.recent(1)[0]
        assert "line1\\nline2" in row["source"]

    def test_quote_chars_escaped_in_repr(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("App", 'said "hi"', "body", 1)
        row = broker.audit.recent(1)[0]
        # repr handles quotes safely.
        assert "said" in row["source"]

    def test_empty_summary_and_body(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("App", "", "", 1)
        row = broker.audit.recent(1)[0]
        assert "app=App" in row["source"]
        assert "summary=''" in row["source"]
        assert "body=''" in row["source"]


class TestRecordNotificationOrderAndSequencing:
    def test_recent_returns_newest_first(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        for i in range(5):
            broker.RecordNotification(f"App{i}", f"s{i}", "b", 1)
        rows = broker.audit.recent(5)
        # AuditLog.recent is newest-first by ts.
        assert "App4" in rows[0]["source"]
        assert "App0" in rows[4]["source"]

    def test_high_volume_does_not_drop_rows(self, broker):
        """Pin a sanity floor — 100 successive notifications all land."""
        broker.set_peer(uid=SILO_UID_USER1)
        for i in range(100):
            broker.RecordNotification("App", f"s{i}", "b", 1)
        rows = broker.audit.recent(200)
        assert len(rows) == 100

    def test_decision_always_true(self, broker):
        """Notification audit is informational; decision must be True
        regardless of urgency. Pinned so a future 'silence noisy
        apps' policy doesn't accidentally mutate this surface."""
        broker.set_peer(uid=SILO_UID_USER1)
        for u in (0, 1, 2):
            broker.RecordNotification("App", "s", "b", u)
        for r in broker.audit.recent(3):
            assert bool(r["decision"]) is True


class TestRecordNotificationActionNamespace:
    def test_action_is_stable(self, broker):
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("App", "s", "b", 1)
        rows = broker.audit.recent(1)
        assert rows[0]["action"] == "notification.posted"

    def test_action_distinguishes_from_other_audits(self, broker):
        """Hook-allowed audit and notification audit live under
        different action namespaces; spot-check the partition."""
        broker.set_peer(uid=SILO_UID_USER1)
        broker.RecordNotification("App", "s", "b", 1)
        # Synthesize a different-action audit so we can filter.
        broker.audit.log(
            caller_uid=SILO_UID_USER1, caller_pid=100,
            caller_exe="/usr/bin/qs",
            action="hook.allowed:screenLock",
            decision=True, scope=None, source="test",
            approver_uid=None,
        )
        rows = broker.audit.recent(10)
        notif_actions = [r["action"] for r in rows
                         if r["action"] == "notification.posted"]
        hook_actions = [r["action"] for r in rows
                        if r["action"].startswith("hook.allowed:")]
        assert len(notif_actions) == 1
        assert len(hook_actions) == 1


class TestRecordNotificationPidExePassthrough:
    def test_caller_pid_recorded(self, broker):
        broker.set_peer(uid=SILO_UID_USER1, pid=4242)
        broker.RecordNotification("App", "s", "b", 1)
        row = broker.audit.recent(1)[0]
        assert int(row["caller_pid"]) == 4242

    def test_caller_exe_recorded(self, broker):
        broker.set_peer(uid=SILO_UID_USER1, exe="/usr/bin/qs-custom")
        broker.RecordNotification("App", "s", "b", 1)
        row = broker.audit.recent(1)[0]
        assert row["caller_exe"] == "/usr/bin/qs-custom"
