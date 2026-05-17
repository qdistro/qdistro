"""Tests for qdistro_admin_broker.Broker.CheckHandoffActivation.

Cross-silo window-activation policy gate added in spec/09. Same shape
as CheckClipboardTransfer (spec/10): same-silo trivial allow, cross-
silo via the rules engine + default-deny, every call audited.
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
QDSHELL_EXE = "/usr/bin/qdshell"


class _StubBroker(Broker):
    def __init__(self, cache_db, audit_db, rules_dir,
                 *, ratelimit_limit=10_000, ratelimit_window_s=1.0):
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
        self.pending_signals = []
        self.decided_signals = []

    def set_peer(self, uid, pid=100, exe=QDSHELL_EXE, start=0):
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


def _write_handoff_rule(rules_dir: Path, *, decision: str,
                        source: str, dest: str,
                        name: str = "ho") -> None:
    action = f"qdistro.handoff.activate:{source}:{dest}"
    (rules_dir / f"{name}.yaml").write_text(
        f"- name: {name}\n"
        f"  decision: {decision}\n"
        f"  match:\n"
        f"    action: {action!r}\n"
    )


class TestSameSilo:
    # Option-B contract: same-silo allow requires identity_verified=True.
    # See todo/decisions/secctx-identity-contract.md.

    def test_admin_to_admin_when_verified(self, broker):
        assert broker.CheckHandoffActivation(
            "admin", "admin", "src.app", "dst.app", "", True) == "allow"

    def test_user1_to_user1_when_verified(self, broker):
        assert broker.CheckHandoffActivation(
            "user1", "user1", "x", "y", "", True) == "allow"

    def test_same_silo_writes_audit(self, broker):
        broker.CheckHandoffActivation("user2", "user2", "a", "b", "", True)
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert rows[0]["decision"] is True
        assert rows[0]["action"] == "qdistro.handoff.activate:user2:user2"
        assert "handoff_same_silo" in rows[0]["source"]
        assert "src_app=a" in rows[0]["source"]
        assert "dst_app=b" in rows[0]["source"]

    def test_same_silo_ignores_rules_when_verified(self, broker, rules_dir):
        _write_handoff_rule(rules_dir, decision="deny",
                            source="user1", dest="user1")
        broker.rules.reload()
        assert broker.CheckHandoffActivation(
            "user1", "user1", "x", "y", "", True) == "allow"

    def test_same_silo_unverified_falls_through_to_deny(self, broker):
        # Same-silo without identity_verified now goes through the
        # cross-silo policy path. With no rule, that's default-deny.
        assert broker.CheckHandoffActivation(
            "user1", "user1", "x", "y") == "deny"


class TestCrossSilo:
    def test_default_deny(self, broker):
        assert broker.CheckHandoffActivation(
            "user1", "admin", "a", "b") == "deny"

    def test_allow_rule(self, broker, rules_dir):
        _write_handoff_rule(rules_dir, decision="allow",
                            source="user1", dest="admin")
        broker.rules.reload()
        assert broker.CheckHandoffActivation(
            "user1", "admin", "x", "y") == "allow"

    def test_directional(self, broker, rules_dir):
        _write_handoff_rule(rules_dir, decision="allow",
                            source="user1", dest="admin", name="up")
        broker.rules.reload()
        assert broker.CheckHandoffActivation(
            "user1", "admin", "x", "y") == "allow"
        assert broker.CheckHandoffActivation(
            "admin", "user1", "x", "y") == "deny"

    def test_audit_default_deny_rule_path_null(self, broker):
        broker.CheckHandoffActivation("user1", "admin", "src", "dst")
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert rows[0]["decision"] is False
        assert "handoff_default_deny" in rows[0]["source"]
        assert "src_app=src" in rows[0]["source"]
        assert "dst_app=dst" in rows[0]["source"]
        assert rows[0]["rule_path"] is None

    def test_audit_rule_path_set_when_rule_fires(self, broker, rules_dir):
        _write_handoff_rule(rules_dir, decision="allow",
                            source="user1", dest="admin", name="r")
        broker.rules.reload()
        broker.CheckHandoffActivation("user1", "admin", "src", "dst")
        rows = broker.audit.recent(10)
        assert "handoff_rule" in rows[0]["source"]
        assert rows[0]["rule_path"].endswith("r.yaml")

    def test_unknown_app_id_audited_as_unknown(self, broker):
        broker.CheckHandoffActivation("user1", "admin", "", "")
        rows = broker.audit.recent(10)
        assert "src_app=(unknown)" in rows[0]["source"]
        assert "dst_app=(unknown)" in rows[0]["source"]


# --- secctx-aware rules (qdwin §6.10 / qdwin_shell_v1@v13) ----------------


class TestSecctxRulesHandoff:
    def test_app_id_specific_allow(self, broker, rules_dir):
        (rules_dir / "tier3-user1.yaml").write_text(
            "- name: tier3-user1\n"
            "  decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.handoff.activate:user1:admin'\n"
            "    app_id: 'qdistro.tier3.user1'\n"
        )
        broker.rules.reload()
        assert broker.CheckHandoffActivation(
            "user1", "admin",
            "qdistro.tier3.user1", "com.example.Target",
            "qdistro.tier3") == "allow"
        # Different app_id under same silo → no rule match → deny.
        assert broker.CheckHandoffActivation(
            "user1", "admin",
            "qdistro.tier3.user2", "com.example.Target",
            "qdistro.tier3") == "deny"

    def test_sandbox_engine_blanket_deny(self, broker, rules_dir):
        (rules_dir / "block-tier2.yaml").write_text(
            "- name: block-tier2\n"
            "  decision: deny\n"
            "  match: {sandbox_engine: 'qdistro.tier2'}\n"
        )
        broker.rules.reload()
        assert broker.CheckHandoffActivation(
            "tier2-app", "admin",
            "com.example.App", "com.example.Target",
            "qdistro.tier2") == "deny"

    def test_audit_records_engine(self, broker):
        broker.CheckHandoffActivation(
            "user1", "admin",
            "qdistro.tier3.user1", "com.example.Receiver",
            "qdistro.tier3")
        rows = broker.audit.recent(10)
        assert "src_engine=qdistro.tier3" in rows[0]["source"]

    def test_audit_marks_engine_unknown_when_not_propagated(self, broker):
        broker.CheckHandoffActivation("user1", "admin", "x", "y")
        rows = broker.audit.recent(10)
        assert "src_engine=(unknown)" in rows[0]["source"]

    def test_legacy_rule_matches_secctx_caller(self, broker, rules_dir):
        _write_handoff_rule(rules_dir, decision="allow",
                            source="user1", dest="admin")
        broker.rules.reload()
        assert broker.CheckHandoffActivation(
            "user1", "admin",
            "qdistro.tier3.user1", "com.example.Target",
            "qdistro.tier3") == "allow"


class TestInputValidation:
    def test_long_silo_rejected(self, broker):
        import dbus
        # 80-char cap matches CheckClipboardTransfer; vm-<63-char> = 66.
        with pytest.raises(dbus.DBusException):
            broker.CheckHandoffActivation("a" * 81, "admin", "x", "y")

    def test_long_app_id_capped(self, broker):
        # App-ids over 128 chars are silently truncated, not rejected.
        broker.CheckHandoffActivation("user1", "user1",
                                      "x" * 1000, "y" * 1000,
                                      "", True)
        rows = broker.audit.recent(1)
        # Audit row stays bounded.
        assert len(rows[0]["source"]) < 4096


class TestRateLimit:
    def test_rejects(self, tmp_path, rules_dir):
        b = _StubBroker(
            str(tmp_path / "approvals.sqlite"),
            str(tmp_path / "audit.sqlite"),
            str(rules_dir),
            ratelimit_limit=2,
            ratelimit_window_s=10.0,
        )
        assert b.CheckHandoffActivation(
            "user1", "user1", "a", "b", "", True) == "allow"
        assert b.CheckHandoffActivation(
            "user1", "user1", "a", "b", "", True) == "allow"
        import dbus
        with pytest.raises(dbus.DBusException):
            b.CheckHandoffActivation(
                "user1", "user1", "a", "b", "", True)
