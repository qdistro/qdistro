"""Tests for qdistro_admin_broker.Broker.ListHistory.

ListHistory feeds the admin-approval-app's History tab. task(073) added
an `argv` field to the returned dict so the History tab can render the
qsu argv tuple for prompts that carried one.
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


# --- argv passthrough ----------------------------------------------------

class TestArgvField:
    def test_argv_present_propagates(self, broker):
        broker.audit.log(
            caller_uid=2000, caller_pid=1234,
            caller_exe="/usr/bin/apt-get",
            action="qdistro.sudo.exec",
            decision=True, scope="forever_argv",
            source="prompt", approver_uid=1000,
            argv=["/usr/bin/apt-get", "update"])
        rows = broker.ListHistory(50)
        assert len(rows) == 1
        # task(073): argv is a D-Bus 'as' element of the dict — list-
        # like iterable of strings. The exact type may be dbus.Array
        # but coerces to a Python list cleanly.
        argv = list(rows[0]["argv"])
        assert argv == ["/usr/bin/apt-get", "update"]

    def test_argv_absent_renders_empty(self, broker):
        broker.audit.log(
            caller_uid=2000, caller_pid=1, caller_exe="/x",
            action="qdistro.clipboard.transfer:user1:admin",
            decision=False, scope=None, source="rule",
            approver_uid=None, argv=None)
        rows = broker.ListHistory(50)
        assert len(rows) == 1
        # task(073): D-Bus a{sv} can't carry null; absent argv is a
        # zero-length 'as' so the History tab renders "-".
        assert list(rows[0]["argv"]) == []

    def test_argv_preserves_whitespace_elements(self, broker):
        # Round-trip an argv element with whitespace: shlex-join would
        # be lossy but the broker's wire format is one element per
        # 'as' slot, so the exact list is recovered.
        broker.audit.log(
            caller_uid=2000, caller_pid=1, caller_exe="/u/b/echo",
            action="qdistro.sudo.exec", decision=True,
            scope="forever_exe", source="prompt", approver_uid=1000,
            argv=["/u/b/echo", "hello world", "--flag=a b"])
        rows = broker.ListHistory(50)
        assert list(rows[0]["argv"]) == [
            "/u/b/echo", "hello world", "--flag=a b"]

    def test_non_admin_caller_rejected(self, broker):
        broker.audit.log(
            caller_uid=2000, caller_pid=1, caller_exe="/x",
            action="qdistro.foo", decision=True, scope="once",
            source="prompt", approver_uid=1000, argv=None)
        broker._peer_uid = 2000
        with pytest.raises(Exception) as ei:
            broker.ListHistory(50)
        assert "AccessDenied" in str(ei.value)


# --- existing fields still present -------------------------------------

class TestExistingFields:
    def test_listhistory_returns_all_columns(self, broker):
        broker.audit.log(
            caller_uid=42, caller_pid=99,
            caller_exe="/u/b/foo",
            action="qdistro.foo.bar",
            decision=True, scope="1h",
            source="cache", approver_uid=None,
            request_id=7, rule_path="/etc/qdistro/rules.d/foo.yaml",
            selinux_subj_type="qdistro_broker_t",
            argv=["/u/b/foo", "--bar"])
        rows = broker.ListHistory(50)
        r = rows[0]
        for k in ("ts", "caller_uid", "caller_pid", "caller_exe",
                  "action", "decision", "scope", "source",
                  "approver_uid", "rule_path", "request_id",
                  "selinux_subj_type", "argv"):
            assert k in r, f"missing key {k!r}"
        assert int(r["caller_uid"]) == 42
        assert str(r["caller_exe"]) == "/u/b/foo"
        assert str(r["scope"]) == "1h"
        assert str(r["rule_path"]) == "/etc/qdistro/rules.d/foo.yaml"
        assert list(r["argv"]) == ["/u/b/foo", "--bar"]
