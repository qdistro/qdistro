"""P03 broker tests: same-silo RelayMessage fast path + cross-silo
admin approval path.

The existing ``test_broker_sendto.py`` covers the cross-silo / admin
approved flow in depth; this file pins the P03-introduced bypass:

- same-silo (caller_uid == target_uid) skips the admin prompt and
  delivers immediately. An audit row is still written with
  ``source="same_silo …"`` and ``approver_uid=None``.
- cross-silo (caller_uid != target_uid) keeps the old behaviour —
  RelayMessage enqueues a one-shot request and admin must approve.

Re-uses the ``_StubBroker`` pattern from ``test_broker_sendto.py``.
"""
from __future__ import annotations

import sqlite3
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


ADMIN_UID = B.ADMIN_UID  # 1000


class _StubBroker(Broker):
    """Same-silo / cross-silo aware _StubBroker. Mirrors the one in
    test_broker_sendto.py — kept local so the two test files don't
    couple."""

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
        self._peer_exe = "/usr/bin/test-harness"
        self._peer_start = 0
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        self.forwards: list[tuple[int, str, str, str]] = []
        self.forward_exc: BaseException | None = None
        # Cross-uid silo gate: override the real session-manager lookup
        # so the test never tries to bind to the system bus. Set to a
        # callable returning the string state ("Active", "Frozen", …).
        # None means "manager has no row" (legacy trust path).
        self._silo_state_override = lambda _uid: "Active"

    def set_peer(self, uid: int, pid: int = 100,
                 exe: str = "/usr/bin/peer", start: int = 0) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe
        self._peer_start = start

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe, self._peer_start)

    def _silo_state(self, target_uid):  # type: ignore[override]
        return self._silo_state_override(target_uid)

    def _relay_forward(self, target_uid, target_service, kind, payload):
        if self.forward_exc is not None:
            raise self.forward_exc
        self.forwards.append(
            (int(target_uid), str(target_service), str(kind), str(payload)))

    def RequestPending(self, rid):  # type: ignore[override]
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        self.decided_signals.append((int(rid), str(decision)))


class _ReplyCollector:
    def __init__(self):
        self.replies: list[tuple] = []
        self.errors: list[BaseException] = []

    def reply(self, *args):
        self.replies.append(tuple(args))

    def error(self, exc):
        self.errors.append(exc)


@pytest.fixture
def broker(tmp_path: Path) -> _StubBroker:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rules_dir),
    )


@pytest.fixture
def cb() -> _ReplyCollector:
    return _ReplyCollector()


def _audit_rows(broker: _StubBroker) -> list[dict]:
    con = sqlite3.connect(broker.audit.db_path)
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT caller_uid, action, decision, scope, source, "
            "       approver_uid "
            "FROM audit ORDER BY id")]
    finally:
        con.close()


# --- same-silo fast path --------------------------------------------------

class TestSameSiloBypass:
    def test_same_uid_delivers_without_pending_request(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            2000, "com.qdistro.QNotebook.uid2000",
            "text/plain", "hi self", cb.reply, cb.error)
        # No pending request (admin not involved).
        assert broker._pending == {}
        assert broker.pending_signals == []
        # Delivered exactly once.
        assert broker.forwards == [
            (2000, "com.qdistro.QNotebook.uid2000", "text/plain", "hi self"),
        ]
        # Reply (no error) on the async callback.
        assert cb.replies == [()]
        assert cb.errors == []

    def test_same_uid_writes_audit_row(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            2000, "com.qdistro.QNotebook.uid2000",
            "text/plain", "auditable", cb.reply, cb.error)
        rows = _audit_rows(broker)
        assert len(rows) == 1
        r = rows[0]
        assert r["caller_uid"] == 2000
        assert r["action"] == "app.send-to:2000:com.qdistro.QNotebook.uid2000"
        assert r["decision"] == 1
        assert r["scope"] == "once"
        assert r["source"].startswith("same_silo ")
        assert "kind=text/plain" in r["source"]
        assert "target=com.qdistro.QNotebook.uid2000" in r["source"]
        assert r["approver_uid"] is None

    def test_same_uid_skips_silo_state_probe(self, broker, cb):
        # If the silo gate ran we'd get "SiloNotActive" here; we make
        # the gate return Frozen and prove the same-silo path didn't
        # consult it.
        broker._silo_state_override = lambda _u: "Frozen"
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            2000, "com.qdistro.QNotebook.uid2000",
            "text/plain", "x", cb.reply, cb.error)
        assert cb.errors == []
        assert broker.forwards == [
            (2000, "com.qdistro.QNotebook.uid2000", "text/plain", "x"),
        ]

    def test_same_uid_relay_failure_surfaces_error(self, broker, cb):
        import dbus
        broker.forward_exc = dbus.DBusException(
            "boom", name="com.qdistro.AdminBroker1.RelayFailed")
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            2000, "com.qdistro.QNotebook.uid2000",
            "k", "v", cb.reply, cb.error)
        assert cb.replies == []
        assert len(cb.errors) == 1
        # No audit row when the forward itself fails — caller sees the
        # error and can retry; we don't want a false "delivered" log.
        assert _audit_rows(broker) == []


# --- cross-silo path keeps the admin gate --------------------------------

class TestCrossSiloStillPrompts:
    def test_cross_uid_enqueues_request(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            3000, "com.qdistro.QNotebook.uid3000",
            "text/plain", "cross", cb.reply, cb.error)
        # No immediate delivery — pending until admin decides.
        assert broker.forwards == []
        assert len(broker._pending) == 1
        req = next(iter(broker._pending.values()))
        assert req.action == "app.send-to:3000:com.qdistro.QNotebook.uid3000"
        assert req.one_shot is True
        assert broker.pending_signals == [next(iter(broker._pending.keys()))]
        # No reply / no error yet — admin must approve.
        assert cb.replies == []
        assert cb.errors == []

    def test_cross_uid_silo_inactive_rejects(self, broker, cb):
        broker._silo_state_override = lambda _u: "Frozen"
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            3000, "com.qdistro.X.uid3000",
            "k", "v", cb.reply, cb.error)
        assert cb.replies == []
        assert len(cb.errors) == 1
        assert "not Active" in str(cb.errors[0])

    def test_cross_uid_admin_approve_delivers(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            3000, "com.qdistro.QNotebook.uid3000",
            "text/plain", "approve me", cb.reply, cb.error)
        rid = next(iter(broker._pending.keys()))
        broker.set_peer(uid=ADMIN_UID)
        broker.DecideRequest(rid, "allow", "once")
        assert broker.forwards == [
            (3000, "com.qdistro.QNotebook.uid3000", "text/plain", "approve me"),
        ]
        assert cb.replies == [()]
        assert cb.errors == []

    def test_cross_uid_admin_deny_errors(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            3000, "com.qdistro.QNotebook.uid3000",
            "k", "v", cb.reply, cb.error)
        rid = next(iter(broker._pending.keys()))
        broker.set_peer(uid=ADMIN_UID)
        broker.DecideRequest(rid, "deny", "once")
        assert broker.forwards == []
        assert cb.replies == []
        assert len(cb.errors) == 1
        assert "denied" in str(cb.errors[0]).lower()


# --- audit log distinguishes the two paths -------------------------------

class TestAuditTrailDistinguishesPaths:
    def test_same_silo_source_marker(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            2000, "com.qdistro.QFileMan.uid2000",
            "text/plain", "x", cb.reply, cb.error)
        rows = _audit_rows(broker)
        assert any(r["source"].startswith("same_silo ")
                   for r in rows)

    def test_cross_silo_source_marker(self, broker, cb):
        broker.set_peer(uid=2000)
        broker.RelayMessage(
            3000, "com.qdistro.QFileMan.uid3000",
            "k", "v", cb.reply, cb.error)
        rid = next(iter(broker._pending.keys()))
        broker.set_peer(uid=ADMIN_UID)
        broker.DecideRequest(rid, "allow", "once")
        rows = _audit_rows(broker)
        # Cross-silo writes the prompt-source row.
        assert any(r["source"] == "prompt" for r in rows)
        # And the approver is admin.
        prompt = [r for r in rows if r["source"] == "prompt"][0]
        assert prompt["approver_uid"] == ADMIN_UID
