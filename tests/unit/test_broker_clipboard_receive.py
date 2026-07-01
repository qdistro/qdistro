"""Tests for Broker.CheckClipboardReceive (spec/10 v15).

Mirror of test_broker_clipboard_transfer.py, exercising the per-MIME
receive gate that v15 adds. The key shape difference:

  - Action: ``qdistro.clipboard.receive:<source>:<dest>`` (not
    ``transfer``); admin can author distinct policies for set-time vs
    receive-time.
  - mime_type is a single string per call (one per receive), surfaced
    in the audit row's source-tag.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

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
from qdistro_lineage_store import LineageStore  # noqa: E402


ADMIN_UID = B.ADMIN_UID  # 1000
QDSHELL_EXE = "/usr/bin/qdshell"
SOURCE_PID = 66501
SOURCE_UID = 4001
SOURCE_EXE = "/usr/bin/firefox"
SOURCE_START = 123456


class _StubBroker(Broker):
    def __init__(self, cache_db: str, audit_db: str, rules_dir: str,
                 *, ratelimit_limit: int = 10_000,
                 ratelimit_window_s: float = 1.0):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=ratelimit_limit,
                                     window_s=ratelimit_window_s)
        self._audit_retention_days = 0
        self._peer_uid = ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = QDSHELL_EXE
        self._peer_start = 0
        self.launch_records = LaunchRecordStore()
        self._lineage_store = LineageStore(str(Path(audit_db).with_name("lineage.sqlite")))
        self.pending_signals = []
        self.decided_signals = []
        self.rules_reloaded_signals = []

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    def RequestPending(self, rid):  # type: ignore[override]
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        self.decided_signals.append((int(rid), str(decision)))

    def RulesReloaded(self, rule_count):  # type: ignore[override]
        self.rules_reloaded_signals.append(int(rule_count))


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


def _write_rule(rules_dir: Path, *, decision: str, action: str,
                name: str = "r") -> None:
    (rules_dir / f"{name}.yaml").write_text(
        f"- name: {name}\n"
        f"  decision: {decision}\n"
        f"  match:\n"
        f"    action: {action!r}\n"
    )


@pytest.fixture
def fake_source_live(monkeypatch):
    state = {"exe": SOURCE_EXE, "starttime": SOURCE_START,
             "uid": SOURCE_UID, "label": "", "cgroup": ""}

    def _exe_start(pid):
        if int(pid) == SOURCE_PID:
            return state["exe"], state["starttime"]
        return "?", 0

    monkeypatch.setattr(pi, "read_exe_and_starttime", _exe_start)
    monkeypatch.setattr(pi, "read_uid",
                        lambda pid: state["uid"] if int(pid) == SOURCE_PID
                        else None)
    monkeypatch.setattr(pi, "read_selinux_label",
                        lambda pid: state["label"])
    monkeypatch.setattr(pi, "read_cgroup", lambda pid: state["cgroup"])
    return state


@pytest.fixture
def silo_security_registry(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "silo-security.toml"
    p.write_text(
        "[silo.work]\n"
        "guards = ['local-only', 'no-cross-contaminate']\n"
        "compartments = ['work']\n"
        "conflict_classes = ['work-home']\n"
        "\n"
        "[silo.home]\n"
        "guards = ['no-cross-contaminate']\n"
        "compartments = ['home']\n"
        "conflict_classes = ['work-home']\n"
        "\n"
        "[silo.clean]\n"
        "guards = []\n"
        "compartments = ['work']\n"
        "conflict_classes = ['work-home']\n"
    )
    p.chmod(0o644)
    monkeypatch.setenv("QDISTRO_SILO_SECURITY", str(p))
    return p


def _register_source(broker: _StubBroker, *, silo: str = "work") -> None:
    broker.launch_records.register(
        silo=silo, uid=SOURCE_UID, pid=SOURCE_PID,
        starttime=SOURCE_START, exe=SOURCE_EXE,
        sandbox_engine="qdistro.tier3", app_id=f"qdistro.tier3.{silo}",
    )


# ---------------------------------------------------------------------
# Same-silo short-circuit
# ---------------------------------------------------------------------

class TestSameSilo:
    # Option-B contract: same-silo allow requires identity_verified=True.
    # See todo/decisions/secctx-identity-contract.md.

    def test_same_silo_allow_when_verified(self, broker, tmp_path):
        verdict = broker.CheckClipboardReceive(
            "user1", "user1", "text/plain",
            "", "", "", True)
        assert verdict == "allow"
        # Audit row should land regardless.
        row = sqlite3.connect(str(tmp_path / "audit.sqlite")).execute(
            "SELECT action, source, decision FROM audit"
        ).fetchone()
        action, source, decision = row
        assert action == "qdistro.clipboard.receive:user1:user1"
        assert decision == 1
        assert "clipboard_receive_same_silo" in source
        assert "mime=text/plain" in source

    def test_same_silo_admin_admin_allow_when_verified(self, broker):
        verdict = broker.CheckClipboardReceive(
            "admin", "admin", "image/png",
            "", "", "", True)
        assert verdict == "allow"

    def test_same_silo_unverified_falls_through_to_deny(self, broker):
        # Default-deny once identity_verified is missing.
        assert broker.CheckClipboardReceive(
            "user1", "user1", "text/plain") == "deny"


# ---------------------------------------------------------------------
# Cross-silo default deny + rule allow
# ---------------------------------------------------------------------

class TestCrossSilo:
    def test_cross_silo_default_deny(self, broker, tmp_path):
        verdict = broker.CheckClipboardReceive(
            "user1", "admin", "text/plain")
        assert verdict == "deny"
        row = sqlite3.connect(str(tmp_path / "audit.sqlite")).execute(
            "SELECT action, source, decision FROM audit"
        ).fetchone()
        action, source, decision = row
        assert action == "qdistro.clipboard.receive:user1:admin"
        assert decision == 0
        assert "clipboard_receive_default_deny" in source

    def test_cross_silo_allow_rule(self, broker, rules_dir):
        _write_rule(rules_dir, decision="allow",
                    action="qdistro.clipboard.receive:user1:admin")
        broker.rules.reload()
        verdict = broker.CheckClipboardReceive(
            "user1", "admin", "text/plain")
        assert verdict == "allow"

    def test_cross_silo_deny_rule(self, broker, rules_dir):
        _write_rule(rules_dir, decision="deny",
                    action="qdistro.clipboard.receive:user1:admin")
        broker.rules.reload()
        verdict = broker.CheckClipboardReceive(
            "user1", "admin", "text/plain")
        assert verdict == "deny"

    def test_distinct_from_transfer_rule(self, broker, rules_dir):
        # Authoring an allow rule for transfer must NOT bleed into
        # receive — the actions are distinct synthetic strings.
        _write_rule(rules_dir, decision="allow",
                    action="qdistro.clipboard.transfer:user1:admin",
                    name="t")
        broker.rules.reload()
        verdict = broker.CheckClipboardReceive(
            "user1", "admin", "text/plain")
        assert verdict == "deny"


class TestReceiveLineage:
    def test_no_cross_contaminate_denies_and_records_attempt(
            self, broker, rules_dir, fake_source_live, silo_security_registry,
            monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _write_rule(rules_dir, decision="allow",
                    action="qdistro.clipboard.receive:work:home")
        broker.rules.reload()
        _register_source(broker, silo="work")

        verdict = broker.CheckClipboardReceive(
            "work", "home", "text/plain", "", "", "", False,
            SOURCE_PID, SOURCE_START)

        assert verdict == "deny"
        row = broker._lineage_store._conn.execute(
            "SELECT chokepoint, verdict FROM activities"
        ).fetchone()
        assert row == ("clipboard", "deny")
        assert broker._lineage_store._conn.execute(
            "SELECT COUNT(*) FROM edges WHERE predicate='used'"
        ).fetchone()[0] == 1
        assert broker._lineage_store._conn.execute(
            "SELECT COUNT(*) FROM entities WHERE eid LIKE 'clipboard-source:%'"
        ).fetchone()[0] == 1
        assert broker._lineage_store._conn.execute(
            "SELECT COUNT(*) FROM edges WHERE predicate='wasDerivedFrom'"
        ).fetchone()[0] == 0
        audit = broker.audit.recent(1)[0]
        assert audit["decision"] is False
        assert "clipboard_receive_lineage_deny" in audit["source"]

    def test_local_only_same_compartment_receive_records_derivative(
            self, broker, fake_source_live, silo_security_registry,
            monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _register_source(broker, silo="work")

        verdict = broker.CheckClipboardReceive(
            "work", "work", "text/plain", "", "", "", True,
            SOURCE_PID, SOURCE_START)

        assert verdict == "allow"
        row = broker._lineage_store._conn.execute(
            "SELECT chokepoint, verdict FROM activities"
        ).fetchone()
        assert row == ("clipboard", "allow")
        out_eid = broker._lineage_store._conn.execute(
            "SELECT subject FROM edges WHERE predicate='wasDerivedFrom'"
        ).fetchone()[0]
        out = broker._lineage_store.get_entity(out_eid)
        assert out.guards == frozenset({"local-only", "no-cross-contaminate"})
        assert out.compartments == frozenset({"work"})


# ---------------------------------------------------------------------
# qdwin_shell_v1@v15 per-MIME selector
# ---------------------------------------------------------------------

class TestPerMimeSelector:
    def _write_mime_rule(self, rules_dir: Path, *, decision: str,
                         action: str, mime_type: str,
                         name: str) -> None:
        (rules_dir / f"{name}.yaml").write_text(
            f"- name: {name}\n"
            f"  decision: {decision}\n"
            f"  match:\n"
            f"    action: {action!r}\n"
            f"    mime_type: {mime_type!r}\n"
        )

    def test_per_mime_allow_one_deny_other(self, broker, rules_dir):
        # Canonical admin pattern: same silo pair, allow text/plain,
        # deny image/png.
        self._write_mime_rule(rules_dir, decision="allow",
                              action="qdistro.clipboard.receive:user1:admin",
                              mime_type="text/plain",
                              name="10-text-allow")
        self._write_mime_rule(rules_dir, decision="deny",
                              action="qdistro.clipboard.receive:user1:admin",
                              mime_type="image/png",
                              name="20-image-deny")
        broker.rules.reload()
        assert broker.CheckClipboardReceive(
            "user1", "admin", "text/plain") == "allow"
        assert broker.CheckClipboardReceive(
            "user1", "admin", "image/png") == "deny"
        # Unmentioned mime → default deny.
        assert broker.CheckClipboardReceive(
            "user1", "admin", "text/uri-list") == "deny"

    def test_mime_specific_beats_generic_first_match(self, broker, rules_dir):
        # Two rules at the same priority level: more-specific (with
        # mime_type) authored first should win when its mime hits;
        # broader (no mime_type) catches everything else.
        (rules_dir / "10-mime.yaml").write_text(
            "- name: deny-png-only\n"
            "  decision: deny\n"
            "  match:\n"
            "    action: qdistro.clipboard.receive:user1:admin\n"
            "    mime_type: image/png\n"
        )
        (rules_dir / "20-generic.yaml").write_text(
            "- name: allow-pair\n"
            "  decision: allow\n"
            "  match:\n"
            "    action: qdistro.clipboard.receive:user1:admin\n"
        )
        broker.rules.reload()
        assert broker.CheckClipboardReceive(
            "user1", "admin", "image/png") == "deny"
        assert broker.CheckClipboardReceive(
            "user1", "admin", "text/plain") == "allow"

    def test_mime_rule_does_not_match_transfer(self, broker, rules_dir):
        # A mime-typed rule authored on the receive action must not
        # influence transfer (the actions differ AND transfer carries
        # no single mime).
        self._write_mime_rule(rules_dir, decision="allow",
                              action="qdistro.clipboard.receive:user1:admin",
                              mime_type="text/plain",
                              name="r")
        broker.rules.reload()
        # Transfer call → no rule match → default deny.
        assert broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"]) == "deny"

    def test_mime_audit_records_rule_path(self, broker, tmp_path, rules_dir):
        self._write_mime_rule(rules_dir, decision="allow",
                              action="qdistro.clipboard.receive:user1:admin",
                              mime_type="text/plain",
                              name="r")
        broker.rules.reload()
        broker.CheckClipboardReceive("user1", "admin", "text/plain")
        row = sqlite3.connect(str(tmp_path / "audit.sqlite")).execute(
            "SELECT source, decision, rule_path FROM audit").fetchone()
        source, decision, rule_path = row
        assert decision == 1
        assert "clipboard_receive_rule" in source
        assert "mime=text/plain" in source
        assert rule_path is not None and rule_path.endswith("r.yaml")


# ---------------------------------------------------------------------
# Audit shape (mime in source-tag, app_id propagation)
# ---------------------------------------------------------------------

class TestAuditShape:
    def test_audit_carries_mime_and_app_ids(self, broker, tmp_path):
        broker.CheckClipboardReceive(
            "user1", "admin", "text/uri-list",
            source_app_id="qdistro.tier3.user1",
            dest_app_id="qdistro.admin.terminal",
            source_sandbox_engine="qdistro.tier3")
        row = sqlite3.connect(str(tmp_path / "audit.sqlite")).execute(
            "SELECT source FROM audit").fetchone()
        source = row[0]
        assert "mime=text/uri-list" in source
        assert "src_app=qdistro.tier3.user1" in source
        assert "dst_app=qdistro.admin.terminal" in source
        assert "src_engine=qdistro.tier3" in source

    def test_audit_handles_empty_secctx(self, broker, tmp_path):
        broker.CheckClipboardReceive(
            "user1", "admin", "text/plain")
        row = sqlite3.connect(str(tmp_path / "audit.sqlite")).execute(
            "SELECT source FROM audit").fetchone()
        source = row[0]
        # All optional fields default to "(unknown)" / "(none)"
        assert "src_app=(unknown)" in source
        assert "dst_app=(unknown)" in source
        assert "src_engine=(unknown)" in source


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

class TestValidation:
    def test_long_silo_rejected(self, broker):
        with pytest.raises(dbus.DBusException) as exc:
            broker.CheckClipboardReceive(
                "x" * 81, "admin", "text/plain")
        assert "silo identifier too long" in str(exc.value)

    def test_rate_limit_kicks_in(self, broker, tmp_path, rules_dir):
        # Tight limiter so we can trip it without hammering.
        broker.ratelimit = RateLimiter(limit=2, window_s=10.0)
        broker.CheckClipboardReceive("user1", "admin", "text/plain")
        broker.CheckClipboardReceive("user1", "admin", "text/plain")
        with pytest.raises(dbus.DBusException) as exc:
            broker.CheckClipboardReceive("user1", "admin", "text/plain")
        assert "Rate limit exceeded" in str(exc.value)
