"""Tests for permission-lineage gate routing (findings P0-1, Phase 3).

The forgeable selectors are app_id / sandbox_engine. These tests prove:

- shadow mode (default): behaviour is unchanged — a claimed sandbox_engine
  still matches a sandbox_engine rule (the legacy, lineage-broken path),
  so nothing breaks before launchers register.
- enforce mode: a *forged* claim from a caller with no launch record can
  no longer satisfy a sandbox_engine/app_id rule (resolved value is empty);
  a *registered & verified* caller still matches via the launcher-attested
  value. uid/action/exe selectors are unaffected in both modes.

Both CheckPermission (any-uid fast path) and RequestPermission/_enqueue
(prompt path) are exercised.
"""
from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

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
CALLER_EXE = "/usr/bin/firefox"
CALLER_START = 4242
ACTION = "org.qdistro.device.camera.claim"


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


def _engine_rule(rules_dir: Path, *, engine="qdistro.tier1",
                 decision="allow"):
    (rules_dir / "engine.yaml").write_text(
        f"- name: tierrule\n"
        f"  decision: {decision}\n"
        f"  match:\n"
        f"    action: {ACTION!r}\n"
        f"    sandbox_engine: {engine!r}\n")


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


def _register_caller(broker, *, engine="qdistro.tier1",
                     app_id="qdistro.tier1.work"):
    return broker.launch_records.register(
        silo="work", uid=NON_ADMIN_UID, pid=CALLER_PID,
        starttime=CALLER_START, exe=CALLER_EXE, sandbox_engine=engine,
        app_id=app_id)


# --- shadow mode (default LINEAGE_ENFORCE False) ----------------------

class TestShadowMode:
    def test_forged_engine_still_matches_in_shadow(self, broker, rules_dir,
                                                   fake_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        _engine_rule(rules_dir)
        broker.rules.reload()
        # No launch record at all; caller forges the engine in details.
        v = broker.CheckPermission(
            ACTION, {"sandbox_engine": "qdistro.tier1"})
        assert v == "allow"  # legacy behaviour preserved in shadow

    def test_enqueue_forged_engine_matches_in_shadow(self, broker, rules_dir,
                                                     fake_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        _engine_rule(rules_dir)
        broker.rules.reload()
        rid = broker._enqueue(
            NON_ADMIN_UID, CALLER_PID, CALLER_EXE, CALLER_START, ACTION,
            {"sandbox_engine": "qdistro.tier1"}, delegated=False)
        assert broker._pending[rid].decision is True


# --- enforce mode ------------------------------------------------------

class TestEnforceMode:
    def test_forged_engine_no_record_denied(self, broker, rules_dir,
                                            fake_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _engine_rule(rules_dir)
        broker.rules.reload()
        # Forged engine, but no launch record → resolved engine "" →
        # the sandbox_engine rule cannot match → falls through to unknown.
        v = broker.CheckPermission(
            ACTION, {"sandbox_engine": "qdistro.tier1"})
        assert v == "unknown"

    def test_registered_caller_matches(self, broker, rules_dir,
                                       fake_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _engine_rule(rules_dir)
        broker.rules.reload()
        _register_caller(broker, engine="qdistro.tier1")
        # Caller need not even pass the engine — the broker supplies the
        # launcher-attested value.
        v = broker.CheckPermission(ACTION, {})
        assert v == "allow"

    def test_registered_caller_wrong_engine_rule_no_match(self, broker,
                                                          rules_dir, fake_live,
                                                          monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _engine_rule(rules_dir, engine="qdistro.tier1")
        broker.rules.reload()
        # The record attests tier2; the rule is for tier1 → no match.
        _register_caller(broker, engine="qdistro.tier2")
        v = broker.CheckPermission(ACTION, {"sandbox_engine": "qdistro.tier1"})
        assert v == "unknown"

    def test_enqueue_forged_engine_no_record_denied(self, broker, rules_dir,
                                                    fake_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _engine_rule(rules_dir)
        broker.rules.reload()
        rid = broker._enqueue(
            NON_ADMIN_UID, CALLER_PID, CALLER_EXE, CALLER_START, ACTION,
            {"sandbox_engine": "qdistro.tier1"}, delegated=False)
        # Rule no longer matches → request stays pending (default-prompt).
        assert broker._pending[rid].decision is None

    def test_enqueue_registered_caller_matches(self, broker, rules_dir,
                                               fake_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _engine_rule(rules_dir)
        broker.rules.reload()
        _register_caller(broker, engine="qdistro.tier1")
        rid = broker._enqueue(
            NON_ADMIN_UID, CALLER_PID, CALLER_EXE, CALLER_START, ACTION,
            {}, delegated=False)
        assert broker._pending[rid].decision is True

    def test_recycled_pid_denied(self, broker, rules_dir, fake_live,
                                 monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _engine_rule(rules_dir)
        broker.rules.reload()
        _register_caller(broker, engine="qdistro.tier1")
        # The live process now has a different starttime (pid recycled).
        fake_live["starttime"] = CALLER_START + 1
        v = broker.CheckPermission(ACTION, {"sandbox_engine": "qdistro.tier1"})
        assert v == "unknown"

    def test_uid_action_exe_rule_unaffected_by_enforce(self, broker,
                                                       rules_dir, fake_live,
                                                       monkeypatch):
        # A rule keyed only on the kernel-anchored selectors still works
        # in enforce mode without any launch record.
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        (rules_dir / "k.yaml").write_text(
            f"- name: k\n  decision: allow\n  match:\n"
            f"    action: {ACTION!r}\n    uid: {NON_ADMIN_UID}\n"
            f"    exe: {CALLER_EXE!r}\n")
        broker.rules.reload()
        v = broker.CheckPermission(ACTION, {})
        assert v == "allow"
