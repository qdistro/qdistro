"""Tests for qdistro_admin_broker.Broker.CheckClipboardTransfer.

CheckClipboardTransfer is the cross-silo clipboard policy gate added in
spec/10. The broker:

  - Returns "allow" or "deny" (never "unknown" — clipboard is a
    synchronous user action).
  - Same-silo transfers are unconditionally allowed (audit row written).
  - Cross-silo transfers consult the rules engine via the synthetic
    action `qdistro.clipboard.transfer:<source>:<dest>`. Default-deny
    when no rule matches.
  - Each call writes one audit row regardless of decision; the joined
    mime types live in the audit `source` field for the History tab.

Mirrors the test_broker_check_permission.py harness shape.
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


def _write_clipboard_rule(rules_dir: Path, *, decision: str,
                          source: str, dest: str,
                          name: str = "clip") -> None:
    action = f"qdistro.clipboard.transfer:{source}:{dest}"
    (rules_dir / f"{name}.yaml").write_text(
        f"- name: {name}\n"
        f"  decision: {decision}\n"
        f"  match:\n"
        f"    action: {action!r}\n"
    )


# --- same-silo: always allowed -------------------------------------------

class TestSameSilo:
    # Option-B contract (todo/decisions/secctx-identity-contract.md):
    # same-silo allow now requires identity_verified=True from qdshell.
    # Without it the gate falls through to the cross-silo policy path
    # (default-deny), which is exercised by TestSameSiloUnverified below.

    def test_admin_to_admin(self, broker):
        assert broker.CheckClipboardTransfer(
            "admin", "admin", ["text/plain"],
            "", "", "", True) == "allow"

    def test_user1_to_user1(self, broker):
        assert broker.CheckClipboardTransfer(
            "user1", "user1", ["text/plain", "text/html"],
            "", "", "", True) == "allow"

    def test_same_silo_writes_audit(self, broker):
        broker.CheckClipboardTransfer("user2", "user2", ["text/plain"],
                                      "", "", "", True)
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert rows[0]["decision"] is True
        assert rows[0]["action"] == "qdistro.clipboard.transfer:user2:user2"
        assert "clipboard_same_silo" in rows[0]["source"]

    def test_same_silo_ignores_rules(self, broker, rules_dir):
        # Even an explicit deny rule for the same-silo action shouldn't
        # fire — verified-same-silo is short-circuit allow.
        _write_clipboard_rule(rules_dir, decision="deny",
                              source="user1", dest="user1")
        broker.rules.reload()
        assert broker.CheckClipboardTransfer(
            "user1", "user1", ["text/plain"],
            "", "", "", True) == "allow"


class TestSameSiloUnverified:
    """Option-B gate: same-silo without identity_verified falls through."""

    def test_falls_through_to_default_deny(self, broker):
        assert broker.CheckClipboardTransfer(
            "user1", "user1", ["text/plain"]) == "deny"

    def test_falls_through_explicit_false(self, broker):
        assert broker.CheckClipboardTransfer(
            "admin", "admin", ["text/plain"],
            "", "", "", False) == "deny"

    def test_can_be_overridden_by_explicit_rule(self, broker, rules_dir):
        # An admin rule that explicitly allows the same-silo synthetic
        # action still lets the unverified transfer through — this is
        # the documented escape hatch for callers that can't (yet)
        # propagate identity_verified.
        _write_clipboard_rule(rules_dir, decision="allow",
                              source="user1", dest="user1")
        broker.rules.reload()
        assert broker.CheckClipboardTransfer(
            "user1", "user1", ["text/plain"]) == "allow"


# --- cross-silo: rule lookup, default deny -------------------------------

class TestCrossSilo:
    def test_default_deny_when_no_rule(self, broker):
        assert broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"]) == "deny"

    def test_allow_when_rule_matches(self, broker, rules_dir):
        _write_clipboard_rule(rules_dir, decision="allow",
                              source="user1", dest="admin")
        broker.rules.reload()
        assert broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"]) == "allow"

    def test_deny_rule_matches(self, broker, rules_dir):
        _write_clipboard_rule(rules_dir, decision="deny",
                              source="user1", dest="user2")
        broker.rules.reload()
        assert broker.CheckClipboardTransfer(
            "user1", "user2", ["text/plain"]) == "deny"

    def test_directional_rules(self, broker, rules_dir):
        # user1 → admin allowed; admin → user1 NOT allowed (asymmetric).
        _write_clipboard_rule(rules_dir, decision="allow",
                              source="user1", dest="admin", name="up")
        broker.rules.reload()
        assert broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"]) == "allow"
        assert broker.CheckClipboardTransfer(
            "admin", "user1", ["text/plain"]) == "deny"

    def test_audit_row_includes_mime_and_source_label(self, broker):
        broker.CheckClipboardTransfer("user1", "admin",
                                      ["text/plain", "text/html"])
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        # Default-deny path tags source label distinctly from rule path.
        assert "clipboard_default_deny" in rows[0]["source"]
        assert "text/plain" in rows[0]["source"]
        assert "text/html" in rows[0]["source"]
        assert rows[0]["decision"] is False
        assert rows[0]["rule_path"] is None

    def test_audit_row_records_rule_path_when_rule_fires(
            self, broker, rules_dir):
        _write_clipboard_rule(rules_dir, decision="allow",
                              source="user1", dest="admin", name="r")
        broker.rules.reload()
        broker.CheckClipboardTransfer("user1", "admin", ["text/plain"])
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "clipboard_rule" in rows[0]["source"]
        assert rows[0]["rule_path"] is not None
        assert rows[0]["rule_path"].endswith("r.yaml")


# --- secctx-aware rules (qdwin §6.10 / qdwin_shell_v1@v13) ----------------


class TestSecctxRules:
    def test_app_id_specific_allow(self, broker, rules_dir):
        # Only qdistro.tier3.user1 specifically allowed; other tier-3
        # silo identities still hit default-deny.
        (rules_dir / "tier3-user1.yaml").write_text(
            "- name: tier3-user1\n"
            "  decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.clipboard.transfer:user1:admin'\n"
            "    app_id: 'qdistro.tier3.user1'\n"
        )
        broker.rules.reload()
        assert broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"],
            "qdistro.tier3.user1", "", "qdistro.tier3") == "allow"
        # Wrong app_id under same silo prefix → no rule match → deny.
        assert broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"],
            "qdistro.tier3.user2", "", "qdistro.tier3") == "deny"
        # No app_id (legacy / unsandboxed caller) → no rule match → deny.
        assert broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"]) == "deny"

    def test_sandbox_engine_blanket_deny(self, broker, rules_dir):
        # "Block all tier-2 podman apps from clipboard out, regardless
        # of silo or app_id." Authored on the engine selector alone.
        (rules_dir / "block-tier2.yaml").write_text(
            "- name: block-tier2\n"
            "  decision: deny\n"
            "  match:\n"
            "    sandbox_engine: 'qdistro.tier2'\n"
        )
        # Also a permissive same-action rule below it; engine deny
        # wins because it matches first when the file sorts first.
        (rules_dir / "z-allow.yaml").write_text(
            "- name: z\n"
            "  decision: allow\n"
            "  match: {action: 'qdistro.clipboard.transfer:tier2-app:admin'}\n"
        )
        broker.rules.reload()
        assert broker.CheckClipboardTransfer(
            "tier2-app", "admin", ["text/plain"],
            "com.example.App", "", "qdistro.tier2") == "deny"
        # tier-3 caller doesn't trip the tier-2 deny → allow rule fires.
        assert broker.CheckClipboardTransfer(
            "tier2-app", "admin", ["text/plain"],
            "qdistro.tier3.user1", "", "qdistro.tier3") == "allow"

    def test_secctx_attributes_audited(self, broker, rules_dir):
        broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"],
            "qdistro.tier3.user1", "com.example.Receiver", "qdistro.tier3")
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "src_app=qdistro.tier3.user1" in rows[0]["source"]
        assert "dst_app=com.example.Receiver" in rows[0]["source"]
        assert "src_engine=qdistro.tier3" in rows[0]["source"]

    def test_audit_marks_unknown_when_not_propagated(self, broker):
        broker.CheckClipboardTransfer("user1", "admin", ["text/plain"])
        rows = broker.audit.recent(10)
        assert "src_app=(unknown)" in rows[0]["source"]
        assert "dst_app=(unknown)" in rows[0]["source"]
        assert "src_engine=(unknown)" in rows[0]["source"]

    def test_legacy_rule_still_matches_secctx_caller(self, broker, rules_dir):
        # Pre-v13 rule (no app_id selector) keeps allowing even when
        # caller now propagates secctx fields. Backwards compat.
        _write_clipboard_rule(rules_dir, decision="allow",
                              source="user1", dest="admin")
        broker.rules.reload()
        assert broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"],
            "qdistro.tier3.user1", "", "qdistro.tier3") == "allow"

    def test_tier4_vm_specific_allow(self, broker, rules_dir):
        # Tier-4 silos resolved by qdshell as `vm-<vm>`. Admin authors
        # `qdistro.clipboard.transfer:vm-foo:admin` → allow; other VMs
        # still hit default-deny.
        (rules_dir / "tier4-vm-foo.yaml").write_text(
            "- name: tier4-vm-foo\n"
            "  decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.clipboard.transfer:vm-foo:admin'\n"
            "    app_id: 'qdistro.tier4.foo'\n"
        )
        broker.rules.reload()
        assert broker.CheckClipboardTransfer(
            "vm-foo", "admin", ["text/plain"],
            "qdistro.tier4.foo", "", "qdistro.tier4") == "allow"
        # Different tier-4 VM → no rule match → default-deny.
        assert broker.CheckClipboardTransfer(
            "vm-bar", "admin", ["text/plain"],
            "qdistro.tier4.bar", "", "qdistro.tier4") == "deny"

    def test_tier4_engine_blanket_deny(self, broker, rules_dir):
        # "Block all tier-4 VMs from clipboard out by default": single
        # engine-level rule that catches every vm-* source.
        (rules_dir / "block-tier4.yaml").write_text(
            "- name: block-tier4\n"
            "  decision: deny\n"
            "  match:\n"
            "    sandbox_engine: 'qdistro.tier4'\n"
        )
        broker.rules.reload()
        assert broker.CheckClipboardTransfer(
            "vm-foo", "admin", ["text/plain"],
            "qdistro.tier4.foo", "", "qdistro.tier4") == "deny"
        assert broker.CheckClipboardTransfer(
            "vm-bar", "admin", ["text/plain"],
            "qdistro.tier4.bar", "", "qdistro.tier4") == "deny"
        # Tier-3 caller untouched.
        assert broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"],
            "qdistro.tier3.user1", "", "qdistro.tier3") == "deny"  # default

    def test_tier5_vm5_silo_audited(self, broker, rules_dir):
        # Tier-5 silos resolve as `vm5-<vm>`; audit row records the
        # source identity faithfully even when no rule matches.
        broker.CheckClipboardTransfer(
            "vm5-app", "admin", ["text/plain"],
            "qdistro.tier5.app", "", "qdistro.tier5")
        rows = broker.audit.recent(1)
        assert len(rows) == 1
        assert "src_app=qdistro.tier5.app" in rows[0]["source"]
        assert "src_engine=qdistro.tier5" in rows[0]["source"]


# --- input validation ----------------------------------------------------

class TestInputValidation:
    def test_long_silo_rejected(self, broker):
        import dbus
        # Cap is 80 chars to fit `vm-` + 63-char tier-4 vm_name max.
        with pytest.raises(dbus.DBusException):
            broker.CheckClipboardTransfer("a" * 81, "admin", ["text/plain"])

    def test_long_dest_rejected(self, broker):
        import dbus
        with pytest.raises(dbus.DBusException):
            broker.CheckClipboardTransfer("user1", "b" * 81, ["text/plain"])

    def test_max_length_silo_accepted(self, broker):
        # 80 chars exactly is at the boundary — must be accepted so
        # tier-4 vm-<63-char-vm-name> (66 chars) cleanly fits.
        long_silo = "vm-" + ("a" * 63)
        assert len(long_silo) == 66
        # Cross-silo + no rule: default-deny is fine; we only assert
        # that the call isn't rejected at the input-validation gate.
        assert broker.CheckClipboardTransfer(
            long_silo, "admin", ["text/plain"]) == "deny"

    def test_empty_mime_list_ok(self, broker):
        # Some clients (e.g. drag-and-drop start) set selection with no
        # mime types yet; gate must still return a decision.
        assert broker.CheckClipboardTransfer(
            "user1", "user1", [], "", "", "", True) == "allow"
        assert broker.CheckClipboardTransfer(
            "user1", "admin", []) == "deny"

    def test_mime_list_capped(self, broker):
        # 50 mime types — broker caps to 32 in the audit but still
        # returns a decision.
        broker.CheckClipboardTransfer(
            "user1", "user1", [f"text/x-{i}" for i in range(50)])
        rows = broker.audit.recent(1)
        # No exception, and audit source field stays bounded.
        assert len(rows[0]["source"]) < 4096

    def test_strips_whitespace(self, broker):
        assert broker.CheckClipboardTransfer(
            "  user1  ", "  user1  ", ["text/plain"],
            "", "", "", True) == "allow"


# --- rate-limit ---------------------------------------------------------

class TestRateLimit:
    def test_rate_limit_rejects(self, tmp_path, rules_dir):
        b = _StubBroker(
            str(tmp_path / "approvals.sqlite"),
            str(tmp_path / "audit.sqlite"),
            str(rules_dir),
            ratelimit_limit=2,
            ratelimit_window_s=10.0,
        )
        # First two pass.
        assert b.CheckClipboardTransfer(
            "user1", "user1", [], "", "", "", True) == "allow"
        assert b.CheckClipboardTransfer(
            "user1", "user1", [], "", "", "", True) == "allow"
        # Third trips rate-limit.
        import dbus
        with pytest.raises(dbus.DBusException):
            b.CheckClipboardTransfer(
                "user1", "user1", [], "", "", "", True)
