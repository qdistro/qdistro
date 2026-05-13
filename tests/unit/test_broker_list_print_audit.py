"""Tests for qdistro_admin_broker.Broker.ListPrintAudit.

Feeds the admin-approval-app's Printing tab. The broker reads the
print_audit.sqlite written by qdistro-print-proxy and surfaces rows
over D-Bus so the admin UI doesn't depend on the proxy being up.
"""
from __future__ import annotations

import os
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
from qdistro_print_audit import PrintAuditLog  # noqa: E402


ADMIN_UID = B.ADMIN_UID
QDSHELL_EXE = "/usr/bin/qdshell"


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
        self._peer_uid = ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = QDSHELL_EXE
        self._peer_start = 0

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)


@pytest.fixture
def broker(tmp_path: Path) -> _StubBroker:
    rd = tmp_path / "rules"; rd.mkdir()
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rd),
    )


@pytest.fixture
def print_db(tmp_path: Path, monkeypatch) -> str:
    p = tmp_path / "print_audit.sqlite"
    monkeypatch.setenv("QDISTRO_PRINT_AUDIT_DB", str(p))
    return str(p)


class TestEmpty:
    def test_returns_empty_when_db_absent(self, broker, tmp_path,
                                          monkeypatch):
        monkeypatch.setenv("QDISTRO_PRINT_AUDIT_DB",
                           str(tmp_path / "does-not-exist.sqlite"))
        assert broker.ListPrintAudit(50) == []

    def test_returns_empty_when_db_empty(self, broker, print_db):
        # Touch the DB so it exists but contains 0 rows.
        PrintAuditLog(print_db)
        assert broker.ListPrintAudit(50) == []


class TestRowsRoundTrip:
    def test_returns_recent_rows(self, broker, print_db):
        log = PrintAuditLog(print_db)
        log.record("connect", decision="allow", reason="gate-disabled",
                   caller_uid=1000, caller_pid=4242,
                   caller_exe="/usr/bin/lp", backend="vsock",
                   bytes_to_be=1024, bytes_from_be=512)
        log.record("close", decision="allow", reason="connection-end",
                   caller_uid=1000, caller_pid=4242,
                   caller_exe="/usr/bin/lp", backend="vsock")
        rows = broker.ListPrintAudit(50)
        assert len(rows) == 2
        # tail() is LIFO; we kept that ordering.
        ops = [str(r["op"]) for r in rows]
        assert ops == ["close", "connect"]
        # Decision, reason, exe roundtrip cleanly.
        connect = rows[1]
        assert str(connect["decision"]) == "allow"
        assert str(connect["reason"]) == "gate-disabled"
        assert str(connect["caller_exe"]) == "/usr/bin/lp"
        assert int(connect["caller_uid"]) == 1000
        assert int(connect["bytes_to_be"]) == 1024
        assert int(connect["bytes_from_be"]) == 512
        # `id` is non-zero (sqlite AUTOINCREMENT).
        assert int(connect["id"]) > 0

    def test_null_columns_surface_as_sentinel(self, broker, print_db):
        log = PrintAuditLog(print_db)
        log.record("close", decision="allow", reason="end")
        rows = broker.ListPrintAudit(50)
        r = rows[0]
        # nullable ints come back as -1 (caller_uid) / 0 (caller_pid)
        # / -1 (bytes) — D-Bus has no null primitive, so we use
        # documented sentinels per the docstring.
        assert int(r["caller_uid"]) == -1
        assert int(r["caller_pid"]) == 0
        assert str(r["caller_exe"]) == ""
        assert str(r["backend"]) == ""
        assert int(r["bytes_to_be"]) == -1
        assert int(r["bytes_from_be"]) == -1


class TestLimit:
    def test_limit_caps_rows(self, broker, print_db):
        log = PrintAuditLog(print_db)
        for i in range(5):
            log.record("connect", caller_uid=i, decision="allow", reason="x")
        rows = broker.ListPrintAudit(2)
        assert len(rows) == 2

    def test_zero_limit_uses_default(self, broker, print_db):
        log = PrintAuditLog(print_db)
        for i in range(3):
            log.record("connect", caller_uid=i, decision="allow", reason="x")
        rows = broker.ListPrintAudit(0)
        assert len(rows) == 3


class TestAccessControl:
    def test_non_admin_caller_rejected(self, broker, print_db):
        log = PrintAuditLog(print_db)
        log.record("connect", caller_uid=1000, decision="allow", reason="x")
        broker._peer_uid = 2000
        with pytest.raises(Exception) as ei:
            broker.ListPrintAudit(50)
        assert "AccessDenied" in str(ei.value)
