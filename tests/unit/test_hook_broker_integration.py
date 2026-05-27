"""Tests for broker <-> hook-executor integration.

Covers:
- Broker consults hooks after rules are inconclusive
- Hook allow -> request allowed without admin prompt
- Hook deny -> request denied without admin prompt
- Hook null -> falls through to admin prompt
- Hook transform -> treated as allow
- Executor unreachable -> falls through to admin prompt
- Audit trail records hook verdicts
- CheckPermission fast-path also consults hooks
- HookClient unit behavior (disabled, unreachable, real executor)

Strategy: most tests use a _MockHookClient that returns canned
verdicts (fast, no IPC).  The end-to-end tests at the bottom start
a real executor process for full-stack verification.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("dbus")  # harness needs real dbus-python

import sys
_BROKER = Path(__file__).resolve().parents[1] / "broker"
if str(_BROKER) not in sys.path:
    sys.path.insert(0, str(_BROKER))

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402
from qdistro_hook_client import HookClient  # noqa: E402
from qdistro_hook_executor import (  # noqa: E402
    HookLoader,
    serve,
    _send_frame,
    _recv_frame,
)


ADMIN_UID = B.ADMIN_UID
USER_UID = 1001
PEER_EXE = "/usr/bin/testapp"


# ---------------------------------------------------------------------------
# Mock hook client for fast, deterministic broker tests
# ---------------------------------------------------------------------------

class _MockHookClient:
    """HookClient stand-in that returns a canned verdict.

    Set ``self.verdict`` to the dict to return, or None for fallthrough.
    """

    def __init__(self, verdict: dict | None = None, enabled: bool = True):
        self.verdict = verdict
        self._enabled = enabled
        self.queries: list[tuple[str, dict]] = []

    @property
    def enabled(self):
        return self._enabled

    @property
    def _socket_path(self):
        return "(mock)"

    def query(self, action: str, event: dict[str, Any]) -> dict | None:
        self.queries.append((action, event))
        if not self._enabled:
            return None
        return self.verdict


# ---------------------------------------------------------------------------
# Stub broker (same pattern as other test_broker_*.py files)
# ---------------------------------------------------------------------------

class _StubBroker(Broker):
    """Minimal Broker subclass that skips real D-Bus registration."""

    def __init__(self, cache_db: str, audit_db: str, rules_dir: str,
                 hook_client=None):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        self._peer_uid = USER_UID
        self._peer_pid = 100
        self._peer_exe = PEER_EXE
        self._peer_start = 0
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        self.rules_reloaded_signals: list[int] = []
        self.hooks = hook_client or _MockHookClient(enabled=False)

    def set_peer(self, uid: int, pid: int = 100,
                 exe: str = PEER_EXE) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    def RequestPending(self, rid):
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):
        self.decided_signals.append((int(rid), str(decision)))

    def RulesReloaded(self, rule_count):
        self.rules_reloaded_signals.append(int(rule_count))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    d = tmp_path / "rules"
    d.mkdir()
    return d


@pytest.fixture
def hook_dir(tmp_path: Path) -> Path:
    d = tmp_path / "hooks"
    d.mkdir()
    return d


def _make_broker(tmp_path, rules_dir, hook_client=None):
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rules_dir),
        hook_client=hook_client,
    )


# ---------------------------------------------------------------------------
# HookClient unit tests
# ---------------------------------------------------------------------------

class TestHookClient:
    def test_disabled_client_returns_none(self):
        c = HookClient(enabled=False)
        assert c.query("test", {}) is None

    def test_unreachable_socket_returns_none(self, tmp_path):
        c = HookClient(socket_path=str(tmp_path / "nonexistent.sock"),
                       enabled=True, timeout_s=1)
        assert c.query("test", {}) is None


# ---------------------------------------------------------------------------
# Broker _enqueue: hook allow skips admin prompt
# ---------------------------------------------------------------------------

class TestBrokerEnqueueHooks:
    def test_hook_allow_skips_admin_prompt(self, tmp_path, rules_dir):
        """When hook returns allow, the request is decided immediately
        without emitting RequestPending (no admin prompt)."""
        mock = _MockHookClient(verdict={"verdict": "allow"})
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission("org.qdistro.test.hook", {})

        assert rid not in broker.pending_signals
        req = broker._pending[rid]
        assert req.decision is True

    def test_hook_deny_skips_admin_prompt(self, tmp_path, rules_dir):
        mock = _MockHookClient(
            verdict={"verdict": "deny", "reason": "hook says no"})
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission("org.qdistro.test.hook", {})

        assert rid not in broker.pending_signals
        req = broker._pending[rid]
        assert req.decision is False

    def test_hook_null_falls_through_to_prompt(self, tmp_path, rules_dir):
        """When hook returns None (fall through), the request lands in
        the admin prompt queue."""
        mock = _MockHookClient(verdict=None)
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission("org.qdistro.test.hook", {})

        assert rid in broker.pending_signals
        req = broker._pending[rid]
        assert req.decision is None

    def test_hook_transform_treated_as_allow(self, tmp_path, rules_dir):
        mock = _MockHookClient(
            verdict={"verdict": "transform",
                     "new_payload_size": 100,
                     "transform_desc": "truncated"})
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission("org.qdistro.test.hook", {})

        assert rid not in broker.pending_signals
        req = broker._pending[rid]
        assert req.decision is True  # transform -> allow

    def test_no_hooks_falls_through(self, tmp_path, rules_dir):
        """Disabled hooks -> null verdict -> admin prompt."""
        mock = _MockHookClient(enabled=False)
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission("org.qdistro.test.nohooks", {})

        assert rid in broker.pending_signals
        req = broker._pending[rid]
        assert req.decision is None

    def test_hook_receives_event_details(self, tmp_path, rules_dir):
        """The hook query includes caller identity and sanitized
        details from the request."""
        mock = _MockHookClient(verdict=None)
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID, pid=42, exe="/usr/bin/myapp")
        broker.RequestPermission(
            "org.qdistro.test.details",
            {"key1": "val1", "key2": "val2"})

        assert len(mock.queries) == 1
        action, event = mock.queries[0]
        assert action == "org.qdistro.test.details"
        assert event["caller_uid"] == USER_UID
        assert event["caller_pid"] == 42
        assert event["caller_exe"] == "/usr/bin/myapp"
        assert event["action_full"] == "org.qdistro.test.details"

    def test_one_shot_skips_hooks(self, tmp_path, rules_dir):
        """One-shot requests (e.g. RelayMessage) skip hooks entirely:
        every call reaches admin."""
        mock = _MockHookClient(verdict={"verdict": "allow"})
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        # _enqueue with one_shot=True directly.
        rid = broker._enqueue(USER_UID, 100, PEER_EXE, 0,
                              "app.send-to:1002:org.qdistro.Test",
                              {}, delegated=False, one_shot=True)
        # Should be pending (admin prompt).
        assert rid in broker.pending_signals
        # Hook was never called.
        assert len(mock.queries) == 0


# ---------------------------------------------------------------------------
# Rules take priority over hooks
# ---------------------------------------------------------------------------

class TestRulesPriorityOverHooks:
    def test_rule_allow_overrides_hook_deny(self, tmp_path, rules_dir):
        """Declarative rules are consulted before hooks. A rule-allow
        means hooks are never reached."""
        mock = _MockHookClient(
            verdict={"verdict": "deny", "reason": "hook denies"})
        (rules_dir / "allow.yaml").write_text(textwrap.dedent("""\
            - name: allow_test
              decision: allow
              match:
                action: 'org.qdistro.test.priority'
                uid: 1001
        """))
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.rules.reload()
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission(
            "org.qdistro.test.priority", {})

        req = broker._pending[rid]
        assert req.decision is True
        # Hook was NOT consulted because the rule fired.
        assert len(mock.queries) == 0

    def test_rule_deny_overrides_hook_allow(self, tmp_path, rules_dir):
        mock = _MockHookClient(verdict={"verdict": "allow"})
        (rules_dir / "deny.yaml").write_text(textwrap.dedent("""\
            - name: deny_test
              decision: deny
              match:
                action: 'org.qdistro.test.priority2'
                uid: 1001
        """))
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.rules.reload()
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission(
            "org.qdistro.test.priority2", {})

        req = broker._pending[rid]
        assert req.decision is False
        assert len(mock.queries) == 0

    def test_cache_hit_skips_hooks(self, tmp_path, rules_dir):
        mock = _MockHookClient(verdict={"verdict": "deny"})
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.cache.store(USER_UID, "org.qdistro.test.cached",
                           PEER_EXE, "forever", True, ADMIN_UID)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission(
            "org.qdistro.test.cached", {})

        req = broker._pending[rid]
        assert req.decision is True  # from cache
        # Hook not consulted.
        assert len(mock.queries) == 0


# ---------------------------------------------------------------------------
# Executor unreachable
# ---------------------------------------------------------------------------

class TestExecutorUnreachable:
    def test_unreachable_executor_falls_through(self, tmp_path,
                                                 rules_dir):
        dead_sock = str(tmp_path / "dead.sock")
        client = HookClient(socket_path=dead_sock, timeout_s=1,
                             enabled=True)
        broker = _make_broker(tmp_path, rules_dir, hook_client=client)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission(
            "org.qdistro.test.unreachable", {})

        assert rid in broker.pending_signals
        req = broker._pending[rid]
        assert req.decision is None


# ---------------------------------------------------------------------------
# Hooks disabled
# ---------------------------------------------------------------------------

class TestHooksDisabled:
    def test_disabled_hooks_skip_executor(self, tmp_path, rules_dir):
        mock = _MockHookClient(enabled=False)
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission(
            "org.qdistro.test.disabled", {})

        assert rid in broker.pending_signals
        req = broker._pending[rid]
        assert req.decision is None


# ---------------------------------------------------------------------------
# CheckPermission also consults hooks
# ---------------------------------------------------------------------------

class TestCheckPermissionHooks:
    def test_check_permission_hook_allow(self, tmp_path, rules_dir):
        mock = _MockHookClient(verdict={"verdict": "allow"})
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        result = broker.CheckPermission("org.qdistro.test.check", {})
        assert result == "allow"

    def test_check_permission_hook_deny(self, tmp_path, rules_dir):
        mock = _MockHookClient(
            verdict={"verdict": "deny", "reason": "no"})
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        result = broker.CheckPermission("org.qdistro.test.check", {})
        assert result == "deny"

    def test_check_permission_hook_transform(self, tmp_path, rules_dir):
        mock = _MockHookClient(
            verdict={"verdict": "transform"})
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        result = broker.CheckPermission("org.qdistro.test.check", {})
        assert result == "allow"

    def test_check_permission_hook_null(self, tmp_path, rules_dir):
        mock = _MockHookClient(verdict=None)
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        result = broker.CheckPermission("org.qdistro.test.check", {})
        assert result == "unknown"

    def test_check_permission_rule_beats_hook(self, tmp_path, rules_dir):
        mock = _MockHookClient(verdict={"verdict": "deny"})
        (rules_dir / "r.yaml").write_text(textwrap.dedent("""\
            - name: r
              decision: allow
              match:
                action: 'org.qdistro.test.check2'
                uid: 1001
        """))
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.rules.reload()
        broker.set_peer(uid=USER_UID)
        result = broker.CheckPermission("org.qdistro.test.check2", {})
        assert result == "allow"
        assert len(mock.queries) == 0

    def test_check_permission_hook_unreachable(self, tmp_path,
                                                rules_dir):
        dead_sock = str(tmp_path / "dead.sock")
        client = HookClient(socket_path=dead_sock, timeout_s=1,
                             enabled=True)
        broker = _make_broker(tmp_path, rules_dir, hook_client=client)
        broker.set_peer(uid=USER_UID)
        result = broker.CheckPermission(
            "org.qdistro.test.unreachable", {})
        assert result == "unknown"


# ---------------------------------------------------------------------------
# Audit trail for hook decisions
# ---------------------------------------------------------------------------

class TestHookAuditTrail:
    def test_hook_allow_writes_audit_row(self, tmp_path, rules_dir):
        mock = _MockHookClient(verdict={"verdict": "allow"})
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        broker.RequestPermission("org.qdistro.test.audit", {})

        rows = broker.audit.recent(10)
        assert len(rows) >= 1
        last = rows[0]
        assert last["action"] == "org.qdistro.test.audit"
        assert last["decision"] is True
        assert "hook" in last["source"]

    def test_hook_deny_writes_audit_row(self, tmp_path, rules_dir):
        mock = _MockHookClient(
            verdict={"verdict": "deny", "reason": "policy"})
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        broker.RequestPermission(
            "org.qdistro.test.audit_deny", {})

        rows = broker.audit.recent(10)
        assert len(rows) >= 1
        last = rows[0]
        assert last["action"] == "org.qdistro.test.audit_deny"
        assert last["decision"] is False
        assert "hook" in last["source"]
        assert "policy" in last["source"]

    def test_hook_null_no_audit_row(self, tmp_path, rules_dir):
        """When hooks fall through to admin prompt, no audit row is
        written at _enqueue time (the prompt path writes its own)."""
        mock = _MockHookClient(verdict=None)
        broker = _make_broker(tmp_path, rules_dir, hook_client=mock)
        broker.set_peer(uid=USER_UID)
        broker.RequestPermission("org.qdistro.test.noaudit", {})

        rows = broker.audit.recent(10)
        assert len(rows) == 0


# ---------------------------------------------------------------------------
# End-to-end: real executor + broker
# ---------------------------------------------------------------------------

class TestEndToEndWithRealExecutor:
    """Start a real hook executor and broker, verifying the full
    stack from hook file on disk through to broker decision."""

    def test_e2e_hook_allow(self, tmp_path, rules_dir, hook_dir):
        # Write hook before starting executor.
        (hook_dir / "allow_hook.py").write_text(textwrap.dedent("""\
            def on_org_qdistro_test_e2e(event):
                return {"action": "allow"}
        """))
        sock_path = str(tmp_path / "e2e.sock")
        stop = threading.Event()
        t = threading.Thread(
            target=serve,
            kwargs={
                "hook_dir": str(hook_dir),
                "socket_path": sock_path,
                "broker_uid": -1,
                "stop_event": stop,
            },
            daemon=True,
        )
        t.start()
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.05)

        client = HookClient(socket_path=sock_path, timeout_s=5,
                             enabled=True)
        broker = _make_broker(tmp_path, rules_dir, hook_client=client)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission("org.qdistro.test.e2e", {})

        req = broker._pending[rid]
        assert req.decision is True
        assert rid not in broker.pending_signals

        stop.set()
        t.join(timeout=5)

    def test_e2e_hook_deny(self, tmp_path, rules_dir, hook_dir):
        (hook_dir / "deny_hook.py").write_text(textwrap.dedent("""\
            def on_org_qdistro_test_e2e_deny(event):
                return {"action": "deny", "reason": "e2e deny"}
        """))
        sock_path = str(tmp_path / "e2e_deny.sock")
        stop = threading.Event()
        t = threading.Thread(
            target=serve,
            kwargs={
                "hook_dir": str(hook_dir),
                "socket_path": sock_path,
                "broker_uid": -1,
                "stop_event": stop,
            },
            daemon=True,
        )
        t.start()
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.05)

        client = HookClient(socket_path=sock_path, timeout_s=5,
                             enabled=True)
        broker = _make_broker(tmp_path, rules_dir, hook_client=client)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission(
            "org.qdistro.test.e2e.deny", {})

        req = broker._pending[rid]
        assert req.decision is False

        stop.set()
        t.join(timeout=5)

    def test_e2e_hook_fallthrough(self, tmp_path, rules_dir, hook_dir):
        (hook_dir / "pass_hook.py").write_text(textwrap.dedent("""\
            def on_org_qdistro_test_e2e_pass(event):
                return None
        """))
        sock_path = str(tmp_path / "e2e_pass.sock")
        stop = threading.Event()
        t = threading.Thread(
            target=serve,
            kwargs={
                "hook_dir": str(hook_dir),
                "socket_path": sock_path,
                "broker_uid": -1,
                "stop_event": stop,
            },
            daemon=True,
        )
        t.start()
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.05)

        client = HookClient(socket_path=sock_path, timeout_s=5,
                             enabled=True)
        broker = _make_broker(tmp_path, rules_dir, hook_client=client)
        broker.set_peer(uid=USER_UID)
        rid = broker.RequestPermission(
            "org.qdistro.test.e2e.pass", {})

        req = broker._pending[rid]
        assert req.decision is None
        assert rid in broker.pending_signals

        stop.set()
        t.join(timeout=5)

    def test_e2e_check_permission_hook(self, tmp_path, rules_dir,
                                        hook_dir):
        (hook_dir / "cp_hook.py").write_text(textwrap.dedent("""\
            def on_org_qdistro_test_e2e_cp(event):
                return {"action": "allow"}
        """))
        sock_path = str(tmp_path / "e2e_cp.sock")
        stop = threading.Event()
        t = threading.Thread(
            target=serve,
            kwargs={
                "hook_dir": str(hook_dir),
                "socket_path": sock_path,
                "broker_uid": -1,
                "stop_event": stop,
            },
            daemon=True,
        )
        t.start()
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.05)

        client = HookClient(socket_path=sock_path, timeout_s=5,
                             enabled=True)
        broker = _make_broker(tmp_path, rules_dir, hook_client=client)
        broker.set_peer(uid=USER_UID)
        result = broker.CheckPermission(
            "org.qdistro.test.e2e.cp", {})
        assert result == "allow"

        stop.set()
        t.join(timeout=5)
