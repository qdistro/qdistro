"""CheckPermissionForClient / RequestPermissionForClient (portal Option A).

A trusted root portal *frontend* owns org.freedesktop.portal.Desktop on a
silo session bus, kernel-authenticates the originating app via
GetConnectionUnixProcessID on the app's own connection, and relays that
(pid, starttime) to the broker. The broker decides for the CLIENT, not the
frontend, resolving the client pid against the launch-record store.

These tests prove:
- root-only: a non-root caller is refused (AccessDenied).
- the decision is keyed on the resolved CLIENT (uid/exe/attested app_id),
  not the frontend's identity.
- a registered client under enforce matches via its launcher-attested
  sandbox_engine even with no claim in details.
- a forged claim from an unregistered client is dropped under enforce.
- a gone / recycled (starttime-drift) client fails closed (unknown / 0).
- the async twin enqueues a pending request for the client subject.
"""
from __future__ import annotations

import concurrent.futures
import threading

import pytest

pytest.importorskip("dbus")

import dbus  # noqa: E402
import qdistro_admin_broker as B  # noqa: E402
import qdistro_proc_identity as pi  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402
from qdistro_launch_record import LaunchRecordStore  # noqa: E402

# Frontend (the D-Bus caller) runs as root.
FRONTEND = (0, 900, "/usr/libexec/qdistro/qdistro-portal-frontend", 1)
# The originating app (the client the frontend names).
CLIENT_PID = 55501
CLIENT_EXE = "/usr/bin/firefox"
CLIENT_UID = 4001
CLIENT_START = 778899
ACTION = "org.freedesktop.portal.Screenshot"


class _StubBroker(Broker):
    def __init__(self, cache_db, audit_db, rules_dir, peer=FRONTEND):
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
        self._peer = peer
        self.pending_signals = []

    def _peer_info(self, sender, conn):
        return self._peer

    def RequestPending(self, rid):
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
def fake_client_live(monkeypatch):
    state = {"exe": CLIENT_EXE, "starttime": CLIENT_START,
             "uid": CLIENT_UID, "label": "", "cgroup": ""}

    def _exe_start(pid):
        if int(pid) == CLIENT_PID:
            return state["exe"], state["starttime"]
        return "?", 0

    monkeypatch.setattr(pi, "read_exe_and_starttime", _exe_start)
    monkeypatch.setattr(pi, "read_uid",
                        lambda pid: state["uid"] if int(pid) == CLIENT_PID
                        else None)
    monkeypatch.setattr(pi, "read_selinux_label", lambda pid: state["label"])
    monkeypatch.setattr(pi, "read_cgroup", lambda pid: state["cgroup"])
    return state


def _engine_rule(rules_dir, *, engine="qdistro.tier1", decision="allow"):
    (rules_dir / "e.yaml").write_text(
        f"- name: e\n  decision: {decision}\n  match:\n"
        f"    action: {ACTION!r}\n    sandbox_engine: {engine!r}\n")


def _register_client(broker, *, engine="qdistro.tier1",
                     app_id="qdistro.tier1.work", silo="work"):
    return broker.launch_records.register(
        silo=silo, uid=CLIENT_UID, pid=CLIENT_PID, starttime=CLIENT_START,
        exe=CLIENT_EXE, sandbox_engine=engine, app_id=app_id)


# --- authorization boundary -------------------------------------------

def test_non_root_caller_refused(tmp_path, rules_dir, fake_client_live,
                                 monkeypatch):
    b = _StubBroker(str(tmp_path / "c"), str(tmp_path / "a"), str(rules_dir),
                    peer=(1000, 901, "/usr/bin/evil", 1))
    with pytest.raises(dbus.DBusException):
        b.CheckPermissionForClient(ACTION, {}, CLIENT_PID, CLIENT_START)
    with pytest.raises(dbus.DBusException):
        b.RequestPermissionForClient(ACTION, {}, CLIENT_PID, CLIENT_START)


# --- enforce: decision is keyed on the resolved client ----------------

def test_registered_client_matches_attested_engine(broker, rules_dir,
                                                    fake_client_live,
                                                    monkeypatch):
    monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
    _engine_rule(rules_dir, engine="qdistro.tier1")
    broker.rules.reload()
    _register_client(broker, engine="qdistro.tier1")
    # Frontend passes NO sandbox_engine; the broker supplies the
    # launcher-attested value for the CLIENT pid.
    assert broker.CheckPermissionForClient(ACTION, {}, CLIENT_PID,
                                           CLIENT_START) == "allow"


def test_forged_engine_unregistered_client_denied(broker, rules_dir,
                                                  fake_client_live,
                                                  monkeypatch):
    monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
    _engine_rule(rules_dir, engine="qdistro.tier1")
    broker.rules.reload()
    # Client never registered; frontend forwards a forged engine claim.
    assert broker.CheckPermissionForClient(
        ACTION, {"sandbox_engine": "qdistro.tier1"},
        CLIENT_PID, CLIENT_START) == "unknown"


def test_client_uid_exe_rule_unaffected(broker, rules_dir, fake_client_live,
                                        monkeypatch):
    # A rule keyed on the CLIENT's kernel-anchored uid/exe matches without
    # any launch record — proving the decision uses the client identity,
    # not the root frontend's.
    monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
    (rules_dir / "k.yaml").write_text(
        f"- name: k\n  decision: allow\n  match:\n"
        f"    action: {ACTION!r}\n    uid: {CLIENT_UID}\n"
        f"    exe: {CLIENT_EXE!r}\n")
    broker.rules.reload()
    assert broker.CheckPermissionForClient(ACTION, {}, CLIENT_PID,
                                           CLIENT_START) == "allow"


def test_frontend_identity_not_used_for_decision(broker, rules_dir,
                                                 fake_client_live,
                                                 monkeypatch):
    # A rule for the FRONTEND's uid/exe must NOT match — the subject is the
    # client, never the frontend.
    monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
    (rules_dir / "f.yaml").write_text(
        f"- name: f\n  decision: allow\n  match:\n"
        f"    action: {ACTION!r}\n    uid: 0\n"
        f"    exe: {FRONTEND[2]!r}\n")
    broker.rules.reload()
    assert broker.CheckPermissionForClient(ACTION, {}, CLIENT_PID,
                                           CLIENT_START) == "unknown"


# --- fail-closed on an unauthenticatable client -----------------------

def test_gone_client_unknown(broker, rules_dir, monkeypatch):
    monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
    monkeypatch.setattr(pi, "read_exe_and_starttime", lambda pid: ("?", 0))
    monkeypatch.setattr(pi, "read_uid", lambda pid: None)
    _engine_rule(rules_dir)
    broker.rules.reload()
    assert broker.CheckPermissionForClient(ACTION, {}, CLIENT_PID,
                                           CLIENT_START) == "unknown"
    assert broker.RequestPermissionForClient(ACTION, {}, CLIENT_PID,
                                             CLIENT_START) == 0


def test_recycled_client_starttime_drift_unknown(broker, rules_dir,
                                                 fake_client_live,
                                                 monkeypatch):
    monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
    _engine_rule(rules_dir)
    broker.rules.reload()
    _register_client(broker)
    # Frontend relays a starttime that no longer matches the live process.
    assert broker.CheckPermissionForClient(
        ACTION, {"sandbox_engine": "qdistro.tier1"},
        CLIENT_PID, CLIENT_START + 1) == "unknown"


# --- async twin --------------------------------------------------------

def test_request_for_client_enqueues_attested(broker, rules_dir,
                                              fake_client_live, monkeypatch):
    monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
    _engine_rule(rules_dir, engine="qdistro.tier1")
    broker.rules.reload()
    _register_client(broker, engine="qdistro.tier1")
    rid = broker.RequestPermissionForClient(ACTION, {}, CLIENT_PID,
                                            CLIENT_START)
    assert rid > 0
    # The pending request auto-resolved against the attested engine rule.
    assert broker._pending[rid].decision is True


def test_shadow_preserves_claim_for_client(broker, rules_dir,
                                           fake_client_live, monkeypatch):
    monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
    _engine_rule(rules_dir, engine="qdistro.tier1")
    broker.rules.reload()
    # No record; shadow keeps the legacy claim → still matches.
    assert broker.CheckPermissionForClient(
        ACTION, {"sandbox_engine": "qdistro.tier1"},
        CLIENT_PID, CLIENT_START) == "allow"
