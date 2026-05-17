"""Focused unit-level shim for the Option-B same-silo identity gate.

This is the integration-style test for the three broker methods whose
same-silo short-circuit now requires identity_verified=True (see
todo/decisions/secctx-identity-contract.md). It asserts that the gate
fires correctly across CheckClipboardTransfer, CheckClipboardReceive,
and CheckHandoffActivation by exercising the public broker surface
directly — the VM-level coverage that drives qdshell forwarding the
flag end-to-end needs to land separately as a VM bats test (the
existing test fixtures are too heavy to wire a live qdshell+qdwin+broker
chain into a unit test).
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
    def __init__(self, cache_db, audit_db, rules_dir):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        self.pending_signals = []
        self.decided_signals = []

    def _peer_info(self, sender, conn):
        return (ADMIN_UID, 1, QDSHELL_EXE, 0)

    def RequestPending(self, rid):  # type: ignore[override]
        pass

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        pass


@pytest.fixture
def broker(tmp_path: Path) -> _StubBroker:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rules_dir),
    )


# --- same-silo allow gate -------------------------------------------------

class TestSameSiloGate:
    def test_transfer_denies_without_verification(self, broker):
        assert broker.CheckClipboardTransfer(
            "user1", "user1", ["text/plain"]) == "deny"

    def test_transfer_allows_with_verification(self, broker):
        assert broker.CheckClipboardTransfer(
            "user1", "user1", ["text/plain"],
            "", "", "", True) == "allow"

    def test_receive_denies_without_verification(self, broker):
        assert broker.CheckClipboardReceive(
            "user1", "user1", "text/plain") == "deny"

    def test_receive_allows_with_verification(self, broker):
        assert broker.CheckClipboardReceive(
            "user1", "user1", "text/plain",
            "", "", "", True) == "allow"

    def test_handoff_denies_without_verification(self, broker):
        assert broker.CheckHandoffActivation(
            "user1", "user1", "src.app", "dst.app") == "deny"

    def test_handoff_allows_with_verification(self, broker):
        assert broker.CheckHandoffActivation(
            "user1", "user1", "src.app", "dst.app", "", True) == "allow"


# --- audit source labels distinguish verified from unverified -------------

class TestAuditLabels:
    def test_verified_label_in_audit(self, broker):
        broker.CheckClipboardTransfer(
            "user1", "user1", ["text/plain"], "", "", "", True)
        rows = broker.audit.recent(10)
        joined = " ".join(r["source"] for r in rows)
        assert "clipboard_same_silo_verified" in joined

    def test_unverified_falls_through_default_deny_label(self, broker):
        broker.CheckClipboardTransfer(
            "user1", "user1", ["text/plain"])
        rows = broker.audit.recent(10)
        joined = " ".join(r["source"] for r in rows)
        # Same-silo + unverified goes through the cross-silo default-deny path.
        assert "clipboard_default_deny" in joined
