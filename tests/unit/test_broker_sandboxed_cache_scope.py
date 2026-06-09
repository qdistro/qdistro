"""Broker-level tests for the tier-2 approval-cache escalation guard
(issue broker-forever-cache-scope).

An argv-blind ``forever`` (always) / ``forever_exe`` (exe_only) approval
cache row matches by ``(uid, action[, exe])`` alone — any argv, any tier.
On the non-delegated RequestPermission / CheckPermission path, such a
grant minted for one process could once auto-allow a later request from a
*different exe/argv/isolation tier* at the same uid. The broker is the
sole gateway between a compromised tier-2 (sandboxed) workload and
host-side sensitive ops, so it now derives a ``sandboxed`` flag from the
*verified* launch record and skips the argv-blind kinds for an
authenticated sandboxed caller.

These tests prove:
- a verified sandboxed caller is NOT auto-decided by a pre-existing
  forever / forever_exe row (it stays pending / returns "unknown");
- it IS still satisfied by an argv-pinned (forever_argv) row;
- a verified NON-sandboxed caller is unchanged (forever still applies);
- the guard is anchored on the verified record, not the claimed engine,
  so it holds in lineage shadow mode and cannot be loosened by a forged
  claim.

Both the async prompt path (_enqueue) and the synchronous fast path
(CheckPermission / _decide_check) are exercised.
"""
from __future__ import annotations

import concurrent.futures
import threading

import pytest

pytest.importorskip("dbus")

import qdistro_admin_broker as B  # noqa: E402
import qdistro_proc_identity as pi  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402
from qdistro_launch_record import LaunchRecordStore  # noqa: E402

NON_ADMIN_UID = 2000
CALLER_PID = 31337
CALLER_EXE = "/usr/bin/apt-get"
CALLER_START = 4242
ACTION = "qdistro.sudo.exec"


class _StubBroker(Broker):
    def __init__(self, cache_db, audit_db, rules_dir):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        self._io_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.launch_records = LaunchRecordStore()
        self.hooks = type("_NoHooks", (), {"query": lambda self, *a: None})()
        self._peer = (NON_ADMIN_UID, CALLER_PID, CALLER_EXE, CALLER_START)
        self.pending_signals = []

    def _peer_info(self, sender, conn):
        return self._peer

    def RequestPending(self, rid):  # noqa: D401
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):
        pass


@pytest.fixture
def rules_dir(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    return d


@pytest.fixture
def broker(tmp_path, rules_dir):
    return _StubBroker(str(tmp_path / "c.sqlite"),
                       str(tmp_path / "a.sqlite"), str(rules_dir))


@pytest.fixture
def fake_live(monkeypatch):
    """Make the caller pid resolve to a known live process."""
    state = {"exe": CALLER_EXE, "starttime": CALLER_START,
             "uid": NON_ADMIN_UID, "label": "", "cgroup": ""}
    monkeypatch.setattr(pi, "read_exe_and_starttime",
                        lambda pid: (state["exe"], state["starttime"]))
    monkeypatch.setattr(pi, "read_uid", lambda pid: state["uid"])
    monkeypatch.setattr(pi, "read_selinux_label", lambda pid: state["label"])
    monkeypatch.setattr(pi, "read_cgroup", lambda pid: state["cgroup"])
    return state


def _register_caller(broker, *, engine, app_id="app.work"):
    return broker.launch_records.register(
        silo="work", uid=NON_ADMIN_UID, pid=CALLER_PID,
        starttime=CALLER_START, exe=CALLER_EXE, sandbox_engine=engine,
        app_id=app_id)


def _argv_details(argv):
    """Encode argv into qsu per-element argv[NN] detail keys."""
    return {f"argv[{i:02d}]": a for i, a in enumerate(argv)}


# --- _cache_sandboxed signal -------------------------------------------

class TestCacheSandboxedSignal:
    def test_verified_sandboxed_record_is_true(self, broker, fake_live):
        _register_caller(broker, engine="qdistro.tier2")
        assert broker._cache_sandboxed(CALLER_PID) is True

    def test_verified_non_sandboxed_record_is_false(self, broker, fake_live):
        # Empty sandbox_engine = a plain host app (non-sandboxed).
        _register_caller(broker, engine="")
        assert broker._cache_sandboxed(CALLER_PID) is False

    def test_unverified_caller_is_false(self, broker, fake_live):
        # No launch record at all → unverified → not classified sandboxed,
        # even if the caller would later forge a non-empty engine claim.
        assert broker._cache_sandboxed(CALLER_PID) is False

    def test_recycled_pid_is_false(self, broker, fake_live):
        _register_caller(broker, engine="qdistro.tier2")
        # Live process now has a different starttime → record no longer
        # verifies → not classified sandboxed (fail-closed, can't loosen).
        fake_live["starttime"] = CALLER_START + 1
        assert broker._cache_sandboxed(CALLER_PID) is False


# --- _enqueue (async prompt path) --------------------------------------

class TestEnqueueGuard:
    def _seed_forever(self, broker, decision=True):
        broker.cache.store(NON_ADMIN_UID, ACTION, "", "forever",
                           decision, 1000)

    def test_sandboxed_not_auto_allowed_by_forever(self, broker, fake_live,
                                                   monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)  # shadow default
        self._seed_forever(broker, decision=True)
        _register_caller(broker, engine="qdistro.tier2")
        rid = broker._enqueue(
            NON_ADMIN_UID, CALLER_PID, CALLER_EXE, CALLER_START, ACTION,
            _argv_details([CALLER_EXE, "install", "evil"]), delegated=False)
        # Blind 'always' row skipped → request stays pending (default-deny).
        assert broker._pending[rid].decision is None

    def test_sandboxed_not_auto_allowed_by_forever_exe(self, broker,
                                                       fake_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        broker.cache.store(NON_ADMIN_UID, ACTION, CALLER_EXE, "forever_exe",
                           True, 1000)
        _register_caller(broker, engine="qdistro.tier2")
        rid = broker._enqueue(
            NON_ADMIN_UID, CALLER_PID, CALLER_EXE, CALLER_START, ACTION,
            _argv_details([CALLER_EXE, "install", "evil"]), delegated=False)
        assert broker._pending[rid].decision is None

    def test_non_sandboxed_still_auto_allowed_by_forever(self, broker,
                                                         fake_live,
                                                         monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        self._seed_forever(broker, decision=True)
        _register_caller(broker, engine="")  # plain host app
        rid = broker._enqueue(
            NON_ADMIN_UID, CALLER_PID, CALLER_EXE, CALLER_START, ACTION,
            _argv_details([CALLER_EXE, "install", "evil"]), delegated=False)
        assert broker._pending[rid].decision is True

    def test_unregistered_caller_unchanged_by_forever(self, broker, fake_live,
                                                      monkeypatch):
        # No launch record → not classified sandboxed → legacy behaviour:
        # the blind 'always' row still applies (this is today's behaviour
        # for ordinary host callers and is intentionally preserved).
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        self._seed_forever(broker, decision=True)
        rid = broker._enqueue(
            NON_ADMIN_UID, CALLER_PID, CALLER_EXE, CALLER_START, ACTION,
            _argv_details([CALLER_EXE, "install", "evil"]), delegated=False)
        assert broker._pending[rid].decision is True

    def test_sandboxed_still_satisfied_by_argv_pinned_row(self, broker,
                                                          fake_live,
                                                          monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        argv = [CALLER_EXE, "update"]
        broker.cache.store(NON_ADMIN_UID, ACTION, CALLER_EXE, "forever_argv",
                           True, 1000, argv=argv)
        _register_caller(broker, engine="qdistro.tier2")
        rid = broker._enqueue(
            NON_ADMIN_UID, CALLER_PID, CALLER_EXE, CALLER_START, ACTION,
            _argv_details(argv), delegated=False)
        # argv-pinned row is NOT skipped for sandboxed callers → auto-allow.
        assert broker._pending[rid].decision is True


# --- CheckPermission (synchronous fast path) ---------------------------

class TestCheckPermissionGuard:
    def test_sandboxed_forever_returns_unknown(self, broker, fake_live,
                                               monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        broker.cache.store(NON_ADMIN_UID, ACTION, "", "forever", True, 1000)
        _register_caller(broker, engine="qdistro.tier2")
        v = broker.CheckPermission(
            ACTION, _argv_details([CALLER_EXE, "install", "evil"]))
        assert v == "unknown"

    def test_non_sandboxed_forever_returns_allow(self, broker, fake_live,
                                                 monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        broker.cache.store(NON_ADMIN_UID, ACTION, "", "forever", True, 1000)
        _register_caller(broker, engine="")
        v = broker.CheckPermission(
            ACTION, _argv_details([CALLER_EXE, "install", "evil"]))
        assert v == "allow"

    def test_sandboxed_argv_pinned_returns_allow(self, broker, fake_live,
                                                 monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        argv = [CALLER_EXE, "update"]
        broker.cache.store(NON_ADMIN_UID, ACTION, CALLER_EXE, "forever_argv",
                           True, 1000, argv=argv)
        _register_caller(broker, engine="qdistro.tier2")
        v = broker.CheckPermission(ACTION, _argv_details(argv))
        assert v == "allow"

    def test_forged_engine_claim_cannot_block_unregistered(self, broker,
                                                           fake_live,
                                                           monkeypatch):
        """A forged non-empty engine CLAIM from an unregistered caller does
        NOT flip the guard (the signal is verified-record-anchored). The
        blind row therefore still applies — a forgery cannot loosen OR
        spuriously tighten via this path, matching today's behaviour for
        ordinary callers."""
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        broker.cache.store(NON_ADMIN_UID, ACTION, "", "forever", True, 1000)
        det = _argv_details([CALLER_EXE, "x"])
        det["sandbox_engine"] = "qdistro.tier2"  # forged claim, no record
        v = broker.CheckPermission(ACTION, det)
        assert v == "allow"

    def test_verified_sandboxed_blocked_even_with_empty_claim(self, broker,
                                                              fake_live,
                                                              monkeypatch):
        """The key shadow-mode fix: a verified-sandboxed caller is blocked
        even when it CLAIMS an empty engine (the value _lineage_selectors
        would surface in shadow mode). The guard reads the verified record,
        not the claim."""
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        broker.cache.store(NON_ADMIN_UID, ACTION, "", "forever", True, 1000)
        _register_caller(broker, engine="qdistro.tier2")
        det = _argv_details([CALLER_EXE, "x"])
        det["sandbox_engine"] = ""  # claim empty to try to keep the blind row
        v = broker.CheckPermission(ACTION, det)
        assert v == "unknown"
