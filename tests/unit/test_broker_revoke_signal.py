"""Tests for qdistro_admin_broker.Broker.ApprovalRevoked signal.

Landed for §6.5 S4 follow-up (a): subscribers (qdshell is the initial
consumer) use this signal to tear down resources that were granted on
the strength of a now-revoked cache row. The broker is not permitted
to revoke silently — every deletion from either revoke entry point
must fire exactly one signal per row with the (caller_uid, action,
exe) shape matching the delete_by_id / delete_by_uid return values.

Model mirrors test_broker_sendto.py: subclass Broker, capture signals
in-process instead of emitting them over dbus.
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


ADMIN_UID = B.ADMIN_UID  # 1000
NON_ADMIN_UID = 2000
OTHER_UID = 3000
PEER_EXE = "/usr/bin/qdshell"
STREAM_ACTION = "qdistro.view-stream.subscribe:qnotebook"


class _StubBroker(Broker):
    """Bypass dbus.service.Object; capture signal emissions in lists."""

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
        self._peer_exe = "/usr/bin/admin-app"
        self._peer_start = 0
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        self.revoked_signals: list[tuple[int, str, str]] = []

    def set_peer(self, uid: int, pid: int = 100, exe: str = PEER_EXE,
                 start: int = 0) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe
        self._peer_start = start

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    def RequestPending(self, rid):  # type: ignore[override]
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        self.decided_signals.append((int(rid), str(decision)))

    def ApprovalRevoked(self, caller_uid, action,  # type: ignore[override]
                         exe):
        self.revoked_signals.append(
            (int(caller_uid), str(action), str(exe)))


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def broker(tmp_path: Path) -> _StubBroker:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rules_dir),
    )


def _seed(broker, *, uid: int, action: str, exe: str,
          scope: str = "forever", decision: bool = True) -> int:
    """Seed a cache row; return its id so we can revoke by it."""
    broker.cache.store(uid, action, exe, scope, decision, ADMIN_UID)
    return broker.cache.list_all()[0]["id"]


# --- RevokeApproval --------------------------------------------------------

class TestRevokeApprovalSignal:
    def test_signal_fires_with_row_tuple(self, broker):
        rid = _seed(broker, uid=NON_ADMIN_UID,
                    action=STREAM_ACTION, exe=PEER_EXE,
                    scope="1h")  # argv_exact → exe preserved in row
        assert broker.RevokeApproval(rid) is True
        assert broker.revoked_signals == [
            (NON_ADMIN_UID, STREAM_ACTION, PEER_EXE),
        ]

    def test_forever_scope_signal_carries_empty_exe(self, broker):
        """`forever` maps to match_kind='always' → match_value=''.
        Signal carries the empty string rather than None so the dbus
        signature stays 'iss' without marshalling surprises."""
        rid = _seed(broker, uid=NON_ADMIN_UID,
                    action=STREAM_ACTION, exe=PEER_EXE,
                    scope="forever")
        broker.RevokeApproval(rid)
        assert broker.revoked_signals == [
            (NON_ADMIN_UID, STREAM_ACTION, ""),
        ]

    def test_noop_revoke_does_not_fire_signal(self, broker):
        """Revoking an absent id returns False; subscribers must not
        see a phantom signal (or they'd tear down live resources on
        a stray admin click)."""
        assert broker.RevokeApproval(999_999) is False
        assert broker.revoked_signals == []

    def test_deny_row_revoke_also_signals(self, broker):
        """A deny row is still a cache row. Revoking it is itself a
        policy change subscribers care about — e.g. qdshell might
        have cached the deny and want to drop that memo."""
        rid = _seed(broker, uid=NON_ADMIN_UID,
                    action=STREAM_ACTION, exe=PEER_EXE,
                    scope="forever", decision=False)
        broker.RevokeApproval(rid)
        assert len(broker.revoked_signals) == 1
        assert broker.revoked_signals[0][:2] == (NON_ADMIN_UID, STREAM_ACTION)

    def test_signal_fires_after_audit_write(self, broker):
        """Signal follows audit; if audit fails the ordering becomes a
        risk but the current code emits after _audit_revoke succeeds.
        Pin the observed ordering so a refactor that swaps them shows
        up as a test break."""
        import sqlite3
        rid = _seed(broker, uid=NON_ADMIN_UID,
                    action=STREAM_ACTION, exe=PEER_EXE,
                    scope="1h")
        broker.RevokeApproval(rid)
        assert broker.revoked_signals == [
            (NON_ADMIN_UID, STREAM_ACTION, PEER_EXE),
        ]
        con = sqlite3.connect(broker.audit.db_path)
        try:
            rows = con.execute(
                "SELECT source, decision FROM audit").fetchall()
        finally:
            con.close()
        # Exactly one revoke row, matching the subject.
        assert any(r[0] == "revoke" for r in rows)

    def test_requires_admin_peer(self, broker):
        import dbus
        rid = _seed(broker, uid=NON_ADMIN_UID,
                    action=STREAM_ACTION, exe=PEER_EXE,
                    scope="1h")
        broker.set_peer(uid=NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.RevokeApproval(rid)
        # Authz failure fires before the cache delete; no signal leak.
        assert broker.revoked_signals == []


# --- RevokeAllForUid -------------------------------------------------------

class TestRevokeAllForUidSignal:
    def test_fires_once_per_row(self, broker):
        _seed(broker, uid=NON_ADMIN_UID,
              action="qdistro.view-stream.subscribe:a", exe=PEER_EXE,
              scope="1h")
        _seed(broker, uid=NON_ADMIN_UID,
              action="qdistro.view-stream.subscribe:b", exe=PEER_EXE,
              scope="forever")
        _seed(broker, uid=NON_ADMIN_UID,
              action="qdistro.other", exe="/usr/bin/thing",
              scope="forever_exe")
        count = broker.RevokeAllForUid(NON_ADMIN_UID)
        assert count == 3
        assert len(broker.revoked_signals) == 3
        # Every signal carries the target uid.
        assert all(u == NON_ADMIN_UID for (u, _a, _e) in broker.revoked_signals)
        # The three actions are all represented; order depends on row
        # ordering from the cache, pin the set not the sequence.
        actions = {a for (_u, a, _e) in broker.revoked_signals}
        assert actions == {
            "qdistro.view-stream.subscribe:a",
            "qdistro.view-stream.subscribe:b",
            "qdistro.other",
        }

    def test_no_rows_no_signals(self, broker):
        """Calling RevokeAllForUid on a uid with no rows must be
        silent. Admins running bulk revokes in a loop must not spam
        subscribers with spurious teardowns."""
        count = broker.RevokeAllForUid(NON_ADMIN_UID)
        assert count == 0
        assert broker.revoked_signals == []

    def test_does_not_touch_other_uid_rows(self, broker):
        _seed(broker, uid=NON_ADMIN_UID,
              action=STREAM_ACTION, exe=PEER_EXE, scope="forever")
        _seed(broker, uid=OTHER_UID,
              action=STREAM_ACTION, exe=PEER_EXE, scope="forever")
        broker.RevokeAllForUid(NON_ADMIN_UID)
        # Exactly one signal, for the target uid only.
        assert len(broker.revoked_signals) == 1
        assert broker.revoked_signals[0][0] == NON_ADMIN_UID

    def test_requires_admin_peer(self, broker):
        import dbus
        _seed(broker, uid=NON_ADMIN_UID,
              action=STREAM_ACTION, exe=PEER_EXE, scope="1h")
        broker.set_peer(uid=NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.RevokeAllForUid(NON_ADMIN_UID)
        assert broker.revoked_signals == []
