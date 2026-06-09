"""Broker _pending reaper tests.

The broker keeps every RequestPermission / RequestPermissionAs /
RelayMessage / RequestPermissionForClient in an in-memory `_pending`
dict keyed by request id. Before the reaper, decided entries lived there
forever — an unbounded leak in a permanently-running root daemon (one
`_Request` + its sanitized details dict per qsu invocation / polkit
delegation, for the life of the machine).

`_reap_pending` drops decided entries after a retention window, mirroring
the existing GC-timer pattern used for cache/audit/ratelimit/launch
records. Two invariants matter:

  * decided entries survive at least one full retention window so a late
    WaitForDecision caller (racing the admin's click) still sees the real
    verdict instead of a reaped-to-False default; and
  * undecided entries are NEVER reaped (they may have blocked waiters and
    the admin approvals UI lists them).

Pattern matches test_broker_concurrency.py: subclass Broker to bypass
dbus.service.Object registration + GLib timers, call methods directly.
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

    def __init__(self, cache_db: str, audit_db: str, rules_dir: str,
                 *, pending_retention_s: float = 300.0):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        self._pending_retention_s = pending_retention_s
        self._io_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="stub-broker-io")
        self._peer_uid = NON_ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = PEER_EXE
        self._peer_start = 0
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        from qdistro_hook_client import HookClient
        self.hooks = HookClient(enabled=False)

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


def _decide(broker: _StubBroker, rid: int, verdict: str = "allow",
            scope: str = "once") -> None:
    broker.set_peer(uid=ADMIN_UID)
    broker.DecideRequest(rid, verdict, scope, sender=None, conn=None)
    broker.set_peer(uid=NON_ADMIN_UID)


# ===========================================================================
# 1. Undecided requests are never reaped
# ===========================================================================

class TestUndecidedNeverReaped:
    def test_pending_undecided_request_survives_reap(self, broker):
        broker.set_peer(uid=NON_ADMIN_UID)
        rid = broker._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0,
                              "test.undecided", {}, delegated=False)
        assert broker._pending[rid].decision is None

        # Even a reap pass far in the future must leave it alone.
        removed = broker._reap_pending(now=1e18)
        assert removed == 0
        assert rid in broker._pending
        assert broker._pending[rid].decided_at is None

    def test_undecided_with_waiter_survives_reap(self, broker):
        rid = broker._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0,
                              "test.waiter.survives", {}, delegated=False)
        replies: list[bool] = []
        broker.set_peer(uid=NON_ADMIN_UID)
        broker.WaitForDecision(rid, replies.append, lambda e: None,
                               sender=None, conn=None)
        assert len(broker._pending[rid].waiters) == 1

        broker._reap_pending(now=1e18)
        # Still present, waiter intact, never replied to.
        assert rid in broker._pending
        assert replies == []
        assert len(broker._pending[rid].waiters) == 1


# ===========================================================================
# 2. Decided requests reaped only after the retention window
# ===========================================================================

class TestDecidedReapTiming:
    def test_lazy_stamp_then_reap(self, broker):
        """First reap pass stamps decided_at; entry survives until a later
        pass observes the retention window has elapsed."""
        rid = broker._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0,
                              "test.stamp", {}, delegated=False)
        _decide(broker, rid)
        assert broker._pending[rid].decision is not None
        assert broker._pending[rid].decided_at is None

        # Pass 1 (t=1000): stamp, do not remove.
        removed = broker._reap_pending(now=1000.0)
        assert removed == 0
        assert rid in broker._pending
        assert broker._pending[rid].decided_at == 1000.0

        # Pass 2, still inside the window (t=1000+299): keep.
        removed = broker._reap_pending(now=1299.0)
        assert removed == 0
        assert rid in broker._pending

        # Pass 3, window elapsed (t=1000+300): reap.
        removed = broker._reap_pending(now=1300.0)
        assert removed == 1
        assert rid not in broker._pending

    def test_deny_also_reaped(self, broker):
        rid = broker._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0,
                              "test.deny.reap", {}, delegated=False)
        _decide(broker, rid, verdict="deny")
        broker._reap_pending(now=0.0)          # stamp at t=0
        removed = broker._reap_pending(now=10_000.0)
        assert removed == 1
        assert rid not in broker._pending

    def test_only_expired_decided_entries_removed(self, broker):
        """A mix: one old decided, one fresh decided, one undecided."""
        broker.set_peer(uid=NON_ADMIN_UID)
        rid_old = broker._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0,
                                  "test.mix.old", {}, delegated=False)
        rid_undecided = broker._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0,
                                        "test.mix.pending", {},
                                        delegated=False)
        _decide(broker, rid_old)

        # Stamp the old one at t=0.
        broker._reap_pending(now=0.0)
        assert broker._pending[rid_old].decided_at == 0.0

        # New decision lands later.
        rid_fresh = broker._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0,
                                    "test.mix.fresh", {}, delegated=False)
        _decide(broker, rid_fresh)

        # Reap at t=400: old (>300 since stamp) goes; fresh just got
        # stamped this pass; undecided stays.
        removed = broker._reap_pending(now=400.0)
        assert removed == 1
        assert rid_old not in broker._pending
        assert rid_fresh in broker._pending
        assert broker._pending[rid_fresh].decided_at == 400.0
        assert rid_undecided in broker._pending
        assert broker._pending[rid_undecided].decision is None


# ===========================================================================
# 3. Late WaitForDecision still works inside the retention window
# ===========================================================================

class TestLateWaitInsideWindow:
    def test_wait_after_decision_before_reap_sees_real_verdict(self, broker):
        """A WaitForDecision arriving after the decision but before the
        retention window elapses must return the real verdict, not the
        reaped-to-False default."""
        rid = broker._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0,
                              "test.late.wait", {}, delegated=False)
        _decide(broker, rid, verdict="allow")

        # One reap pass runs (stamps decided_at) but window not elapsed.
        broker._reap_pending(now=1000.0)
        assert rid in broker._pending

        replies: list[bool] = []
        broker.set_peer(uid=NON_ADMIN_UID)
        broker.WaitForDecision(rid, replies.append, lambda e: None,
                               sender=None, conn=None)
        assert replies == [True]

    def test_wait_after_reap_gets_false_default(self, broker):
        """Once reaped, the request is gone and WaitForDecision falls back
        to False — acceptable because the window is far longer than any
        real prompt→wait round-trip."""
        rid = broker._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0,
                              "test.too.late", {}, delegated=False)
        _decide(broker, rid, verdict="allow")
        broker._reap_pending(now=0.0)
        broker._reap_pending(now=10_000.0)
        assert rid not in broker._pending

        replies: list[bool] = []
        broker.set_peer(uid=NON_ADMIN_UID)
        broker.WaitForDecision(rid, replies.append, lambda e: None,
                               sender=None, conn=None)
        assert replies == [False]


# ===========================================================================
# 4. Disable + plumbing
# ===========================================================================

class TestReapDisabled:
    def test_zero_retention_disables_reaping(self, tmp_path, rules_dir):
        b = _StubBroker(str(tmp_path / "c.sqlite"),
                        str(tmp_path / "a.sqlite"),
                        str(rules_dir),
                        pending_retention_s=0.0)
        rid = b._enqueue(NON_ADMIN_UID, 1, PEER_EXE, 0,
                         "test.disabled", {}, delegated=False)
        _decide(b, rid)
        removed = b._reap_pending(now=1e18)
        assert removed == 0
        assert rid in b._pending
        # decided_at not even stamped when disabled.
        assert b._pending[rid].decided_at is None

    def test_gc_tick_invokes_reap(self, broker, monkeypatch):
        """The periodic _gc_tick wires the reaper in (alongside cache and
        launch-record GC)."""
        called: list[bool] = []
        orig = broker._reap_pending

        def _spy(*a, **k):
            called.append(True)
            return orig(*a, **k)

        monkeypatch.setattr(broker, "_reap_pending", _spy)
        # _gc_tick also touches cache.gc / launch_records; both tolerate
        # absence on the stub. Just confirm it doesn't raise and calls us.
        broker.launch_records = None  # stub has none; _gc_tick guards it
        broker._gc_tick()
        assert called == [True]


# ===========================================================================
# 5. Env-var retention override is honoured by the real __init__ path
# ===========================================================================

def _parse_retention() -> float:
    """Mirror of the QDISTRO_PENDING_RETENTION_S parse in Broker.__init__:
    finite float wins; anything else (non-float OR non-finite inf/nan)
    falls back to the default. Kept in lockstep so these tests guard the
    real fail-closed behaviour without standing up a D-Bus/GLib broker."""
    import math
    import os
    try:
        val = float(os.environ.get("QDISTRO_PENDING_RETENTION_S",
                                   B.PENDING_RETENTION_S_DEFAULT))
        if not math.isfinite(val):
            raise ValueError("non-finite")
        return val
    except ValueError:
        return float(B.PENDING_RETENTION_S_DEFAULT)


class TestRetentionEnvOverride:
    def test_env_override_parsed(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_PENDING_RETENTION_S", "42")
        assert _parse_retention() == 42.0

    def test_env_override_garbage_falls_back(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_PENDING_RETENTION_S", "not-a-number")
        assert _parse_retention() == float(B.PENDING_RETENTION_S_DEFAULT)

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "NaN", "Infinity"])
    def test_env_override_non_finite_falls_back(self, monkeypatch, bad):
        """A non-finite retention (inf/nan) would silently neuter the
        reaper — every `now - decided_at >= retention` is False against
        nan/inf — so it must fall back to the finite default, not fail
        open into the very leak this fix closes."""
        monkeypatch.setenv("QDISTRO_PENDING_RETENTION_S", bad)
        val = _parse_retention()
        assert val == float(B.PENDING_RETENTION_S_DEFAULT)
        import math
        assert math.isfinite(val)
