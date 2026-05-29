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


def _enqueue_direct(broker: _StubBroker, *, uid: int = NON_ADMIN_UID,
                    pid: int = 1234, exe: str = "/usr/bin/app",
                    action: str = "test.action",
                    argv: list[str] | None = None) -> int:
    details = {}
    for i, a in enumerate(argv or []):
        details[f"argv[{i:02d}]"] = a
    return broker._enqueue(uid, pid, exe, 0, action, details,
                           delegated=False)


class TestRequestPermissionAsIdentityVerification:
    def test_rejects_claimed_exe_that_changed_after_qsu_connect(self, broker,
                                                                 monkeypatch):
        broker.set_peer(uid=0)
        monkeypatch.setattr(B, "_read_proc_identity",
                            lambda pid: ("/usr/bin/changed", 777))
        monkeypatch.setattr(B, "_read_proc_uid", lambda pid: NON_ADMIN_UID)

        with pytest.raises(Exception) as ei:
            broker.RequestPermissionAs(
                NON_ADMIN_UID, 1234, "/usr/bin/original",
                "qsu.exec:root", {"target_user": "root"})

        assert "executable changed" in str(ei.value)

    def test_rejects_claimed_uid_that_no_longer_matches_pid(self, broker,
                                                            monkeypatch):
        broker.set_peer(uid=0)
        monkeypatch.setattr(B, "_read_proc_identity",
                            lambda pid: ("/usr/bin/qsu", 777))
        monkeypatch.setattr(B, "_read_proc_uid", lambda pid: NON_ADMIN_UID + 1)

        with pytest.raises(Exception) as ei:
            broker.RequestPermissionAs(
                NON_ADMIN_UID, 1234, "/usr/bin/qsu",
                "qsu.exec:root", {"target_user": "root"})

        assert "uid mismatch" in str(ei.value)

    def test_rejects_when_claimed_uid_cannot_be_verified(self, broker,
                                                         monkeypatch):
        broker.set_peer(uid=0)
        monkeypatch.setattr(B, "_read_proc_identity",
                            lambda pid: ("/usr/bin/qsu", 777))
        monkeypatch.setattr(B, "_read_proc_uid", lambda pid: None)

        with pytest.raises(Exception) as ei:
            broker.RequestPermissionAs(
                NON_ADMIN_UID, 1234, "/usr/bin/qsu",
                "qsu.exec:root", {"target_user": "root"})

        assert "uid could not be verified" in str(ei.value)

    def test_rejects_claimed_start_time_that_no_longer_matches_pid(
            self, broker, monkeypatch):
        broker.set_peer(uid=0)
        monkeypatch.setattr(B, "_read_proc_identity",
                            lambda pid: ("/usr/bin/qsu", 888))
        monkeypatch.setattr(B, "_read_proc_uid", lambda pid: NON_ADMIN_UID)

        with pytest.raises(Exception) as ei:
            broker.RequestPermissionAs(
                NON_ADMIN_UID, 1234, "/usr/bin/qsu",
                "qsu.exec:root",
                {"target_user": "root", "caller_start_time": 777})

        assert "start time mismatch" in str(ei.value)

    def test_matching_claim_uses_live_start_time_for_request(self, broker,
                                                             monkeypatch):
        broker.set_peer(uid=0)
        captured = {}
        monkeypatch.setattr(B, "_read_proc_identity",
                            lambda pid: ("/usr/bin/qsu", 777))
        monkeypatch.setattr(B, "_read_proc_uid", lambda pid: NON_ADMIN_UID)

        def fake_enqueue(uid, pid, exe, start_time, action, details, *,
                         delegated, one_shot=False):
            captured.update(
                uid=uid, pid=pid, exe=exe, start_time=start_time,
                action=action, delegated=delegated, one_shot=one_shot)
            return 99

        monkeypatch.setattr(broker, "_enqueue", fake_enqueue)

        rid = broker.RequestPermissionAs(
            NON_ADMIN_UID, 1234, "/usr/bin/qsu",
            "qsu.exec:root",
            {"target_user": "root", "caller_start_time": 777})

        assert rid == 99
        assert captured == {
            "uid": NON_ADMIN_UID,
            "pid": 1234,
            "exe": "/usr/bin/qsu",
            "start_time": 777,
            "action": "qsu.exec:root",
            "delegated": True,
            "one_shot": False,
        }

    def test_empty_claimed_exe_enqueues_verified_live_exe(self, broker,
                                                          monkeypatch):
        broker.set_peer(uid=0)
        captured = {}
        monkeypatch.setattr(B, "_read_proc_identity",
                            lambda pid: ("/usr/bin/qsu", 777))
        monkeypatch.setattr(B, "_read_proc_uid", lambda pid: NON_ADMIN_UID)

        def fake_enqueue(uid, pid, exe, start_time, action, details, *,
                         delegated, one_shot=False):
            captured["exe"] = exe
            return 99

        monkeypatch.setattr(broker, "_enqueue", fake_enqueue)

        broker.RequestPermissionAs(
            NON_ADMIN_UID, 1234, "",
            "qsu.exec:root", {"target_user": "root"})

        assert captured["exe"] == "/usr/bin/qsu"


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

    @pytest.mark.parametrize("scope", [
        "forever_argv", "forever_basename", "forever_prefix",
    ])
    def test_argv_aware_scope_rejected_without_argv(self, broker, scope):
        broker.set_peer(uid=ADMIN_UID)
        rid = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                  argv=None)
        with pytest.raises(Exception) as ei:
            broker.DecideRequest(rid, "allow", scope)
        assert "requires captured argv" in str(ei.value)
        assert broker.cache.list_all() == []

    @pytest.mark.parametrize("scope", [
        "forever_argv", "forever_basename", "forever_prefix",
    ])
    def test_argv_aware_scope_rejected_when_argv00_missing(self, broker,
                                                           scope):
        # Sparse argv that omits argv[00]: the program element the
        # argv-aware scopes pin on was never captured. Must fail closed
        # exactly like a wholly-absent argv (no downgrade to an
        # argv-blind cache row).
        broker.set_peer(uid=ADMIN_UID)
        details = {"target_user": "root",
                   "argv[01]": "/usr/bin/apt-get",
                   "argv[02]": "update"}
        rid = broker._enqueue(NON_ADMIN_UID, 1234, "/usr/bin/qsu", 0,
                              "qsu.exec:root", details, delegated=True)
        with pytest.raises(Exception) as ei:
            broker.DecideRequest(rid, "allow", scope)
        assert "requires captured argv" in str(ei.value)
        assert broker.cache.list_all() == []

    @pytest.mark.parametrize("scope", [
        "forever_argv", "forever_basename", "forever_prefix",
    ])
    def test_argv_required_scope_rejected_without_argv_direct(self, broker,
                                                              scope):
        broker.set_peer(uid=ADMIN_UID)
        rid = _enqueue_direct(broker)
        with pytest.raises(Exception) as ei:
            broker.DecideRequest(rid, "allow", scope)
        assert "requires captured argv" in str(ei.value)
        assert broker.cache.list_all() == []

    @pytest.mark.parametrize("scope", [
        "forever_argv", "forever_basename", "forever_prefix",
    ])
    def test_argv_required_scope_without_argv_still_allows_deny(self,
                                                                broker,
                                                                scope):
        broker.set_peer(uid=ADMIN_UID)
        rid = _enqueue_direct(broker)
        broker.DecideRequest(rid, "deny", scope)
        req = broker._pending[rid]
        assert req.decision is False
        assert broker.cache.list_all() == []

    @pytest.mark.parametrize("scope", ["1h", "24h"])
    def test_timed_scope_without_argv_direct_remains_exe_timed(self, broker,
                                                               scope):
        broker.set_peer(uid=ADMIN_UID)
        rid = _enqueue_direct(broker, exe="/usr/bin/app")
        broker.DecideRequest(rid, "allow", scope)
        row = broker.cache.lookup_detail(NON_ADMIN_UID, "test.action",
                                         "/usr/bin/app")
        assert row is not None
        assert row["match_kind"] == "exe_only"
        assert row["scope"] == scope

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


# --- decision-time guard: a delegated _enqueue is NOT auto-resolved by a
#     pre-existing argv-blind cache row, while a non-delegated one is ----

class TestDelegatedEnqueueNotAutoResolvedByBlindRow:
    """The store-path guard (TestArgvBlindScopesStillForbidden) stops a
    delegated decision from CREATING a blind row. This class covers the
    complementary decision-time guard added to _enqueue: even if a blind
    exe_only / always row already exists (e.g. seeded by a prior direct,
    authenticated request at the same uid), a delegated _enqueue must NOT
    inherit it — it stays pending for admin. A direct (non-delegated)
    _enqueue against the same row auto-resolves as before."""

    # The claimed exe/action used by _enqueue_delegated.
    CLAIM_EXE = "/usr/bin/qsu"
    ACTION = "qsu.exec:root"

    def _seed_blind(self, broker, scope, decision=True):
        """Seed a blind cache row for the delegated lookup key."""
        exe = "" if scope == "forever" else self.CLAIM_EXE
        broker.cache.store(NON_ADMIN_UID, self.ACTION, exe, scope,
                           decision, ADMIN_UID)

    @pytest.mark.parametrize("scope", ["forever_exe", "forever"])
    def test_delegated_enqueue_not_auto_resolved_by_blind_allow(self, broker,
                                                                scope):
        broker.set_peer(uid=ADMIN_UID)
        self._seed_blind(broker, scope, decision=True)
        rid = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                  argv=["/usr/bin/apt-get", "install", "x"])
        # No auto-resolution: request stays pending for admin.
        assert broker._pending[rid].decision is None

    @pytest.mark.parametrize("scope", ["forever_exe", "forever"])
    def test_delegated_enqueue_not_auto_denied_by_blind_deny(self, broker,
                                                             scope):
        """Symmetric: a blind DENY row must not auto-resolve a delegated
        request either — it re-prompts rather than inheriting a blanket
        deny the broker never authenticated for this peer."""
        broker.set_peer(uid=ADMIN_UID)
        self._seed_blind(broker, scope, decision=False)
        rid = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                  argv=["/usr/bin/apt-get", "install", "x"])
        assert broker._pending[rid].decision is None

    def test_direct_enqueue_is_auto_resolved_by_same_blind_row(self, broker):
        """Control: the very same seeded exe_only allow row auto-resolves
        a direct (non-delegated) request at the same uid/action/exe.
        Proves the pending-vs-resolved difference is the delegated flag,
        not an unrelated lookup miss."""
        broker.set_peer(uid=ADMIN_UID)
        # Seed against the direct request's exe (/usr/bin/app).
        broker.cache.store(NON_ADMIN_UID, "test.action", "/usr/bin/app",
                           "forever_exe", True, ADMIN_UID)
        rid = _enqueue_direct(broker, exe="/usr/bin/app",
                              argv=["/usr/bin/app", "go"])
        assert broker._pending[rid].decision is True

    def test_delegated_enqueue_still_resolved_by_argv_pinned_row(self, broker):
        """An argv-pinned (forever_argv → argv_exact) row IS argv-aware
        and must still auto-resolve a delegated request whose argv
        matches — the decision-time guard only strips blind kinds."""
        broker.set_peer(uid=ADMIN_UID)
        broker.cache.store(NON_ADMIN_UID, self.ACTION, self.CLAIM_EXE,
                           "forever_argv", True, ADMIN_UID,
                           argv=["/usr/bin/apt-get", "update"])
        rid = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                  argv=["/usr/bin/apt-get", "update"])
        assert broker._pending[rid].decision is True
        # A different argv under the same pinned row → no hit → pending.
        rid2 = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                   argv=["/usr/bin/apt-get", "install", "x"])
        assert broker._pending[rid2].decision is None

    def test_delegated_enqueue_blind_allow_skipped_argv_deny_applies(self,
                                                                     broker):
        """End-to-end priority interaction through _enqueue: with BOTH a
        blind 'always' allow (lower priority) and an argv-pinned deny
        (higher priority) present, a delegated request whose argv matches
        the pinned deny must resolve to DENY — the blind allow is stripped
        and must not mask the argv-aware deny. The cache-level twin is
        test_delegated_skips_blind_allow_falls_to_argv_deny; this proves
        the same outcome on the real broker decision path (no fail-open via
        the higher-priority blind row)."""
        broker.set_peer(uid=ADMIN_UID)
        # Blind always-allow (priority 4) for any exe under this action.
        broker.cache.store(NON_ADMIN_UID, self.ACTION, "", "forever",
                           True, ADMIN_UID)
        # Argv-pinned deny (argv_exact, priority 0) for the offending argv.
        broker.cache.store(NON_ADMIN_UID, self.ACTION, self.CLAIM_EXE,
                           "forever_argv", False, ADMIN_UID,
                           argv=["/usr/bin/apt-get", "install", "evil"])
        rid = _enqueue_delegated(broker, claim_uid=NON_ADMIN_UID,
                                  argv=["/usr/bin/apt-get", "install", "evil"])
        # Blind allow skipped; argv_exact deny still applies → deny.
        assert broker._pending[rid].decision is False
