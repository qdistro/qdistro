"""Tests for Broker.VerifyClientIdentity (Option-B identity contract).

VerifyClientIdentity is the broker's defence against the same-uid spoof
window left open by qdwin's self-asserted secctx strings. qdshell
forwards the (pid, starttime, uid, exe, selinux_label) tuple that qdwin
captured at secctx-bind time; this method re-resolves the live process
against /proc and returns True only when everything still matches.

See todo/decisions/secctx-identity-contract.md (Option B).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest import mock

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
        self._peer_uid = ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = QDSHELL_EXE
        self._peer_start = 0
        self.pending_signals = []
        self.decided_signals = []

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

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


# --- /proc-shape mocks ----------------------------------------------------

def _patch_proc(exe: str, starttime: int, uid: int | None,
                label: str = ""):
    """Install patched versions of the three /proc helpers used by
    VerifyClientIdentity. Returns the patch context manager stack.
    """
    return mock.patch.multiple(
        B,
        _read_proc_identity=lambda pid: (exe, starttime),
        _read_proc_uid=lambda pid: uid,
        _read_proc_selinux_label=lambda pid: label,
    )


# --- matching identity → True --------------------------------------------

class TestMatching:
    def test_all_axes_match(self, broker):
        with _patch_proc(exe="/usr/bin/python3", starttime=12345,
                          uid=1000, label="unconfined_u:unconfined_r:t1"):
            assert broker.VerifyClientIdentity(
                4242, 12345, 1000,
                "/usr/bin/python3",
                "unconfined_u:unconfined_r:t1",
                "qdistro.tier3", "qdistro.tier3.user1", "user1") is True

    def test_no_selinux_either_side_still_passes(self, broker):
        # SELinux off / unconfined: both sides empty → that axis is
        # skipped; pid+starttime+exe+uid still gate the result.
        with _patch_proc(exe="/usr/bin/python3", starttime=999,
                          uid=1000, label=""):
            assert broker.VerifyClientIdentity(
                100, 999, 1000, "/usr/bin/python3", "",
                "", "", "") is True

    def test_empty_exe_skips_exe_check(self, broker):
        # qdwin couldn't read /proc/<pid>/exe at bind time; we don't
        # treat the empty claim as a wildcard-match either, but we also
        # don't fail the verify on that axis alone.
        with _patch_proc(exe="/usr/bin/python3", starttime=42,
                          uid=1000, label=""):
            assert broker.VerifyClientIdentity(
                100, 42, 1000, "", "", "", "", "") is True


# --- mismatches → False ---------------------------------------------------

class TestMismatch:
    def test_starttime_mismatch_returns_false(self, broker):
        # The PID was recycled — same pid, different starttime. The
        # canonical anti-reuse check.
        with _patch_proc(exe="/usr/bin/python3", starttime=99999,
                          uid=1000, label=""):
            assert broker.VerifyClientIdentity(
                4242, 12345, 1000, "/usr/bin/python3", "",
                "", "", "") is False

    def test_exe_mismatch_returns_false(self, broker):
        with _patch_proc(exe="/usr/bin/evil", starttime=42,
                          uid=1000, label=""):
            assert broker.VerifyClientIdentity(
                100, 42, 1000, "/usr/bin/python3", "",
                "", "", "") is False

    def test_uid_mismatch_returns_false(self, broker):
        with _patch_proc(exe="/usr/bin/python3", starttime=42,
                          uid=2000, label=""):
            assert broker.VerifyClientIdentity(
                100, 42, 1000, "/usr/bin/python3", "",
                "", "", "") is False

    def test_selinux_label_mismatch_returns_false(self, broker):
        with _patch_proc(exe="/usr/bin/python3", starttime=42,
                          uid=1000,
                          label="staff_u:staff_r:other_t"):
            assert broker.VerifyClientIdentity(
                100, 42, 1000, "/usr/bin/python3",
                "unconfined_u:unconfined_r:t1",
                "", "", "") is False

    def test_process_gone_returns_false(self, broker):
        # _read_proc_identity returns (exe="?", 0) when the proc is gone.
        with _patch_proc(exe="?", starttime=0, uid=None, label=""):
            assert broker.VerifyClientIdentity(
                999999, 42, 1000, "/usr/bin/python3", "",
                "", "", "") is False


# --- audit trail ----------------------------------------------------------

class TestAudit:
    def test_writes_audit_row_on_allow(self, broker):
        with _patch_proc(exe="/u/bin/python3", starttime=7,
                          uid=1000, label=""):
            broker.VerifyClientIdentity(
                42, 7, 1000, "/u/bin/python3", "",
                "qdistro.tier3", "qdistro.tier3.user1", "user1")
        rows = broker.audit.recent(10)
        assert any("verify_client_identity" in r["source"] for r in rows)
        assert any("reasons=ok" in r["source"] for r in rows)

    def test_writes_audit_row_on_deny(self, broker):
        with _patch_proc(exe="/u/bin/python3", starttime=99,
                          uid=1000, label=""):
            broker.VerifyClientIdentity(
                42, 7, 1000, "/u/bin/python3", "",
                "", "", "")
        rows = broker.audit.recent(10)
        joined = " ".join(r["source"] for r in rows)
        assert "starttime-mismatch" in joined
