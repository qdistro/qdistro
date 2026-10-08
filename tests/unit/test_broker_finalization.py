"""Broker decision-finalization tests.

DecideRequest records ``req.decision`` under the broker lock, then commits
the audit row, the cache write and the polkit response WITHOUT the lock —
the span between decision-record and waiter-drain is the ``finalizing``
flag. A decision observed inside that window is provisional: when
AUDIT_REQUIRED is set (the default), an audit failure rolls it back to
deny, and a released waiter would tear down the polkit auth session
before the approval response reaches polkitd.

Before this fix, a WaitForDecision (or a transfer relay waiter)
registering inside the window was answered with the provisional value:
an admin's Approve could be observed as allow by a waiter even though
the audit write then failed and the request was durably denied. Astra
reproduced the race on the live broker (todo/test-audit-261002
approvals-astra-r4).

The fix makes readers and waiter registrations participate in the
finalization protocol: while ``req.finalizing`` is set they park on
``req.waiters``, and the finalizer drains that list again when it
commits the durable outcome — on success and on the audit-failure
rollback alike.

Pattern matches test_broker_pending_reap.py: subclass Broker to bypass
dbus.service.Object registration + GLib timers, call methods directly,
and drive the window from inside a patched ``audit.log``.
"""
from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

pytest.importorskip("dbus")

import dbus  # noqa: E402

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402

ADMIN_UID = B.ADMIN_UID
NON_ADMIN_UID = 2000
PEER_EXE = "/usr/bin/test-app"


class _StubBroker(Broker):
    """Minimal Broker bypassing real D-Bus registration + GLib timers."""

    def __init__(self, cache_db: str, audit_db: str, rules_dir: str):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        self._pending_retention_s = 300.0
        self._io_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="stub-broker-io")
        self._peer_uid = ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = PEER_EXE
        self._peer_start = 0
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        from qdistro_hook_client import HookClient
        self.hooks = HookClient(enabled=False)

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    def _peer_matches_admin_control(self, *, uid: int, pid: int,
                                    exe: str, method: str = ""
                                    ) -> tuple[bool, str]:
        if int(uid) in (0, ADMIN_UID):
            return True, "test-injected admin-control peer"
        return False, f"uid {uid} is not admin uid {ADMIN_UID}"

    def RequestPending(self, rid):  # type: ignore[override]
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        self.decided_signals.append((int(rid), str(decision)))

    def RulesReloaded(self, rule_count):  # type: ignore[override]
        pass

    def ApprovalRevoked(self, caller_uid, action, exe):  # type: ignore[override]
        pass


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


def _pending_rid(broker: _StubBroker, action: str = "test.finalize") -> int:
    """Enqueue an undecided request owned by the non-admin user."""
    rid = broker._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0, action, {},
                          delegated=False)
    assert broker._pending[rid].decision is None
    return rid


class TestWaitForDecisionDuringFinalization:
    """A waiter that registers inside the finalization window must see
    the durable outcome, never the provisional recorded decision."""

    def test_late_waiter_parked_then_gets_durable_allow(self, broker):
        rid = _pending_rid(broker)
        replies: list[bool] = []
        errors: list[Exception] = []
        replies_during_audit: list[int] = []
        waiters_during_audit: list[int] = []

        orig_log = broker.audit.log

        def audit_with_late_waiter(**kw):
            # Runs while req.decision is provisionally set and
            # req.finalizing is True — the window this test drives.
            broker.WaitForDecision(rid, replies.append, errors.append,
                                   sender=None, conn=None)
            replies_during_audit.append(len(replies))
            waiters_during_audit.append(
                len(broker._pending[rid].waiters))
            return orig_log(**kw)

        broker.audit.log = audit_with_late_waiter
        result = broker.DecideRequest(rid, "allow", "once",
                                      sender=None, conn=None)

        assert result == "applied"
        # The waiter was NOT answered inside the window — it parked on
        # req.waiters and was drained by the finalizer afterwards.
        assert replies_during_audit == [0]
        assert waiters_during_audit == [1]
        assert replies == [True]
        assert errors == []
        assert broker._pending[rid].waiters == []
        assert (rid, "allow") in broker.decided_signals

    def test_audit_failure_rolls_back_before_late_waiter(self, broker):
        """The race astra r4 reproduced: the waiter's allow must lose
        to the audit failure's durable deny."""
        rid = _pending_rid(broker)
        replies: list[bool] = []
        errors: list[Exception] = []

        def failing_audit(**kw):
            broker.WaitForDecision(rid, replies.append, errors.append,
                                   sender=None, conn=None)
            raise RuntimeError("simulated audit disk failure")

        broker.audit.log = failing_audit
        with pytest.raises(dbus.DBusException):
            broker.DecideRequest(rid, "allow", "once",
                                 sender=None, conn=None)

        # The waiter saw the rolled-back deny, not the provisional allow.
        assert replies == [False]
        assert errors == []
        assert broker._pending[rid].decision is False
        assert broker._pending[rid].finalizing is False
        assert broker._pending[rid].waiters == []
        assert (rid, "deny") in broker.decided_signals

        # A still-later waiter reads the durable state directly.
        replies2: list[bool] = []
        broker.WaitForDecision(rid, replies2.append, errors.append,
                               sender=None, conn=None)
        assert replies2 == [False]

    def test_second_decide_during_finalization_reports_deciding(
            self, broker):
        """A competing DecideRequest inside the window gets 'deciding'
        and does not disturb the in-flight decision."""
        rid = _pending_rid(broker)
        nested_results: list[str] = []

        orig_log = broker.audit.log

        def audit_with_nested_decide(**kw):
            nested_results.append(
                broker.DecideRequest(rid, "deny", "once",
                                     sender=None, conn=None))
            return orig_log(**kw)

        broker.audit.log = audit_with_nested_decide
        result = broker.DecideRequest(rid, "allow", "once",
                                      sender=None, conn=None)

        assert nested_results == ["deciding"]
        assert result == "applied"
        assert broker._pending[rid].decision is True

    def test_waiter_after_finalization_sees_final_decision(self, broker):
        """Once finalizing clears, WaitForDecision answers immediately
        with the durable value (no park)."""
        rid = _pending_rid(broker)
        result = broker.DecideRequest(rid, "deny", "once",
                                      sender=None, conn=None)
        assert result == "applied"

        replies: list[bool] = []
        broker.WaitForDecision(rid, replies.append, lambda e: None,
                               sender=None, conn=None)
        assert replies == [False]
        assert broker._pending[rid].waiters == []

    def test_waiter_during_cache_store_stays_parked_through_respond(
            self, broker):
        """The finalizing window covers cache.store AND the polkit
        response, not just the audit write: a waiter landing inside
        cache.store must not be answered before _respond_polkit — the
        agent's BeginAuthentication returns the instant its waiter
        releases, and polkitd then rejects a late response with
        "No session for cookie"."""
        rid = _pending_rid(broker)
        req = broker._pending[rid]
        req.polkit_cookie = "test-cookie"  # makes the respond path run
        replies: list[bool] = []
        errors: list[Exception] = []
        observed: list[tuple[str, int]] = []

        orig_store = broker.cache.store

        def store_with_waiter(*a, **kw):
            # Runs while req.decision is provisionally set, after the
            # audit row — inside the part of the window that must
            # still hold the waiter parked.
            broker.WaitForDecision(rid, replies.append, errors.append,
                                   sender=None, conn=None)
            observed.append(("cache.store", len(replies)))
            return orig_store(*a, **kw)

        def respond_spy(_req):
            # Reached before the durable drain; the parked waiter must
            # still be unanswered here.
            observed.append(("respond_polkit", len(replies)))

        broker.cache.store = store_with_waiter
        broker._respond_polkit = respond_spy

        result = broker.DecideRequest(rid, "allow", "1h",
                                      sender=None, conn=None)

        assert result == "applied"
        assert observed == [("cache.store", 0), ("respond_polkit", 0)]
        assert replies == [True]
        assert errors == []
        assert req.waiters == []
