"""task(078) — argv-aware scopes (forever_argv / forever_basename /
forever_prefix) are now PERMITTED for delegated requests
(RequestPermissionAs, used by qsu) because they pin argv, closing
the leak that motivated tasks(069)/(072). argv-blind scopes
(`forever`, `forever_exe`, `1h`, `24h`) remain forbidden — those
would let one approval extend across argv tuples for an
unauthenticated future caller at the same uid.

These tests run against the same in-memory _StubBroker harness as
test_broker_check_permission.py: real cache, real rules engine,
real audit, real DecideRequest path — only the dbus peer lookup is
shimmed so the test can fix uid/pid/exe.
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
NON_ADMIN_UID = 2000
PEER_EXE = "/usr/local/lib/qdistro/qdistro-root-exec"


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
        self._peer_uid = NON_ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = PEER_EXE
        self._peer_start = 0

    def set_peer(self, uid: int, pid: int = 1, exe: str = PEER_EXE,
                 start: int = 0) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe
        self._peer_start = start

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    def RequestPending(self, rid):  # type: ignore[override]
        pass

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        pass


@pytest.fixture
def broker(tmp_path: Path) -> _StubBroker:
    rd = tmp_path / "rules"; rd.mkdir()
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rd),
    )


def _enqueue_delegated(broker: _StubBroker, *, claim_uid: int,
                       claim_pid: int = 1234,
                       claim_exe: str = "/usr/bin/qsu",
                       action: str = "qsu.exec:root",
                       argv: list[str] | None = None) -> int:
    """Mimic RequestPermissionAs: route an _enqueue with delegated=True
    and the claimed peer identity from a root delegator.

    Returns the request id."""
    details = {"target_user": "root"}
    for i, a in enumerate(argv or []):
        details[f"argv[{i:02d}]"] = a
    # Delegator must be root for RequestPermissionAs; bypass the
    # network round-trip and call _enqueue directly with the claimed
    # peer details.
    return broker._enqueue(
        claim_uid, claim_pid, claim_exe, 0,
        action, details, delegated=True)


# --- argv-blind scopes still rejected for delegated --------------------

class TestArgvBlindScopesStillForbidden:
    @pytest.mark.parametrize("scope", ["1h", "24h", "forever", "forever_exe"])
    def test_argv_blind_scope_rejected(self, broker, scope):
        broker.set_peer(uid=ADMIN_UID)
        rid = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                  argv=["/usr/bin/apt-get", "update"])
        with pytest.raises(Exception) as ei:
            broker.DecideRequest(rid, "allow", scope)
        assert "not permitted for delegated" in str(ei.value)


# --- argv-aware scopes now permitted for delegated --------------------

class TestArgvAwareScopesPermitted:
    @pytest.mark.parametrize("scope", [
        "forever_argv", "forever_basename", "forever_prefix",
    ])
    def test_argv_aware_scope_accepted(self, broker, scope):
        broker.set_peer(uid=ADMIN_UID)
        rid = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                  argv=["/usr/bin/apt-get", "update"])
        # Should not raise; cache row should land for the claimed uid.
        broker.DecideRequest(rid, "allow", scope)
        rows = broker.cache.lookup_detail(
            NON_ADMIN_UID, "qsu.exec:root", "/usr/bin/qsu",
            ["/usr/bin/apt-get", "update"])
        assert rows is not None, \
            f"cache row missing after delegated DecideRequest scope={scope!r}"

    def test_forever_argv_pins_argv(self, broker):
        """The whole point of un-forbidding forever_argv: a delegated
        approval of `[apt-get, update]` does NOT match a future
        delegated call with `[apt-get, install, foo]`."""
        broker.set_peer(uid=ADMIN_UID)
        rid = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                  argv=["/usr/bin/apt-get", "update"])
        broker.DecideRequest(rid, "allow", "forever_argv")
        # Match: same argv → cache hit.
        same = broker.cache.lookup_detail(
            NON_ADMIN_UID, "qsu.exec:root", "/usr/bin/qsu",
            ["/usr/bin/apt-get", "update"])
        assert same is not None
        assert bool(same["decision"]) is True
        # Mismatch: different argv → no cache row hit.
        diff = broker.cache.lookup_detail(
            NON_ADMIN_UID, "qsu.exec:root", "/usr/bin/qsu",
            ["/usr/bin/apt-get", "install", "foo"])
        assert diff is None, \
            "forever_argv leaked to a different argv tuple — qsu argv-leak regression"

    def test_forever_basename_matches_alt_path(self, broker):
        """forever_basename: same basename(argv[0]) any path."""
        broker.set_peer(uid=ADMIN_UID)
        rid = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                  argv=["/usr/bin/apt-get", "update"])
        broker.DecideRequest(rid, "allow", "forever_basename")
        # Same basename, different path → match.
        match = broker.cache.lookup_detail(
            NON_ADMIN_UID, "qsu.exec:root", "/usr/bin/qsu",
            ["/usr/local/bin/apt-get", "install", "x"])
        assert match is not None and bool(match["decision"]) is True
        # Different basename → no match.
        nomatch = broker.cache.lookup_detail(
            NON_ADMIN_UID, "qsu.exec:root", "/usr/bin/qsu",
            ["/usr/bin/dpkg", "-l"])
        assert nomatch is None

    def test_forever_prefix_matches_trailing_args(self, broker):
        broker.set_peer(uid=ADMIN_UID)
        rid = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                  argv=["/usr/bin/systemctl"])
        broker.DecideRequest(rid, "allow", "forever_prefix")
        match = broker.cache.lookup_detail(
            NON_ADMIN_UID, "qsu.exec:root", "/usr/bin/qsu",
            ["/usr/bin/systemctl", "restart", "foo"])
        assert match is not None and bool(match["decision"]) is True
        nomatch = broker.cache.lookup_detail(
            NON_ADMIN_UID, "qsu.exec:root", "/usr/bin/qsu",
            ["/usr/bin/journalctl", "-u", "foo"])
        assert nomatch is None
