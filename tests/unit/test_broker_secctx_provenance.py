"""Tests for Option A secctx launcher-gated provenance tracking.

When SECCTX_LAUNCHER_GATED is True (production default), the broker
annotates every clipboard / handoff audit entry with
secctx_provenance=launcher_gated so admins can filter decisions by trust
level. When False, the tag is 'advisory' and a warning is emitted for
same-silo checks without identity verification.

See todo/decisions/secctx-identity-contract.md.
"""
from __future__ import annotations

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


ADMIN_UID = B.ADMIN_UID  # 1000
QDSHELL_EXE = "/usr/bin/qdshell"


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
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []

    def set_peer(self, uid: int, pid: int = 100, exe: str = QDSHELL_EXE,
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


# --- secctx_provenance in audit rows (clipboard transfer) ------------------

class TestClipboardTransferProvenance:
    def test_same_silo_verified_includes_launcher_gated(self, broker):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", True):
            broker.CheckClipboardTransfer(
                "user1", "user1", ["text/plain"],
                "", "", "", True)
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "secctx_provenance=launcher_gated" in rows[0]["source"]

    def test_same_silo_verified_advisory_when_gate_off(self, broker):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", False):
            broker.CheckClipboardTransfer(
                "user1", "user1", ["text/plain"],
                "", "", "", True)
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "secctx_provenance=advisory" in rows[0]["source"]

    def test_cross_silo_includes_provenance(self, broker):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", True):
            broker.CheckClipboardTransfer(
                "user1", "admin", ["text/plain"])
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "secctx_provenance=launcher_gated" in rows[0]["source"]

    def test_cross_silo_advisory_provenance(self, broker):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", False):
            broker.CheckClipboardTransfer(
                "user1", "admin", ["text/plain"])
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "secctx_provenance=advisory" in rows[0]["source"]


# --- secctx_provenance in audit rows (clipboard receive) -------------------

class TestClipboardReceiveProvenance:
    def test_same_silo_verified_includes_provenance(self, broker):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", True):
            broker.CheckClipboardReceive(
                "user1", "user1", "text/plain",
                "", "", "", True)
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "secctx_provenance=launcher_gated" in rows[0]["source"]

    def test_cross_silo_includes_provenance(self, broker):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", True):
            broker.CheckClipboardReceive(
                "user1", "admin", "text/plain")
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "secctx_provenance=launcher_gated" in rows[0]["source"]


# --- secctx_provenance in audit rows (handoff activation) ------------------

class TestHandoffActivationProvenance:
    def test_same_silo_verified_includes_provenance(self, broker):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", True):
            broker.CheckHandoffActivation(
                "user1", "user1", "app1", "app2",
                "", True)
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "secctx_provenance=launcher_gated" in rows[0]["source"]

    def test_cross_silo_includes_provenance(self, broker):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", True):
            broker.CheckHandoffActivation(
                "user1", "admin", "app1", "app2")
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "secctx_provenance=launcher_gated" in rows[0]["source"]


# --- advisory warning on unverified same-silo -----------------------------

class TestAdvisoryWarning:
    def test_clipboard_transfer_warns_when_advisory(self, broker, capsys):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", False):
            broker.CheckClipboardTransfer(
                "user1", "user1", ["text/plain"],
                "", "", "", False)
        captured = capsys.readouterr()
        assert "WARN secctx advisory" in captured.out
        assert "clipboard transfer" in captured.out

    def test_clipboard_transfer_no_warn_when_launcher_gated(
            self, broker, capsys):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", True):
            broker.CheckClipboardTransfer(
                "user1", "user1", ["text/plain"],
                "", "", "", False)
        captured = capsys.readouterr()
        assert "WARN secctx advisory" not in captured.out

    def test_clipboard_receive_warns_when_advisory(self, broker, capsys):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", False):
            broker.CheckClipboardReceive(
                "user1", "user1", "text/plain",
                "", "", "", False)
        captured = capsys.readouterr()
        assert "WARN secctx advisory" in captured.out
        assert "clipboard receive" in captured.out

    def test_handoff_warns_when_advisory(self, broker, capsys):
        with mock.patch.object(B, "SECCTX_LAUNCHER_GATED", False):
            broker.CheckHandoffActivation(
                "user1", "user1", "app1", "app2",
                "", False)
        captured = capsys.readouterr()
        assert "WARN secctx advisory" in captured.out
        assert "handoff activation" in captured.out


# --- config reader ---------------------------------------------------------

class TestConfigReader:
    def test_default_is_true(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            assert B._read_secctx_launcher_gated() is True

    def test_env_true(self):
        with mock.patch.dict("os.environ",
                             {"QDISTRO_SECCTX_LAUNCHER_GATED": "1"}):
            assert B._read_secctx_launcher_gated() is True

    def test_env_false(self):
        with mock.patch.dict("os.environ",
                             {"QDISTRO_SECCTX_LAUNCHER_GATED": "0"}):
            assert B._read_secctx_launcher_gated() is False

    def test_conf_file(self, tmp_path):
        conf = tmp_path / "broker.conf"
        conf.write_text("secctx_launcher_gated = true\n")
        with (mock.patch.dict("os.environ", {}, clear=True),
              mock.patch.object(B, "_BROKER_CONF_PATH", str(conf))):
            assert B._read_secctx_launcher_gated() is True

    def test_conf_file_false(self, tmp_path):
        conf = tmp_path / "broker.conf"
        conf.write_text("secctx_launcher_gated = false\n")
        with (mock.patch.dict("os.environ", {}, clear=True),
              mock.patch.object(B, "_BROKER_CONF_PATH", str(conf))):
            assert B._read_secctx_launcher_gated() is False
