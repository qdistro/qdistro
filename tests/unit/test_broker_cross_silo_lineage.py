"""Cross-silo source-pid lineage on the clipboard/handoff gates (P1-1).

The clipboard/handoff gates are called by qdshell, not by the source app,
so the D-Bus peer is qdshell — resolving *that* pid would attest qdshell.
qdshell instead relays the source app's kernel-authenticated
``(pid, starttime)`` (the tuple qdwin captured via SO_PEERCRED at
secctx-bind and already feeds to VerifyClientIdentity). The broker resolves
that pid against the launch-record store and uses the launcher-attested
silo/app/engine for the cross-silo rule lookup.

These tests prove:

- shadow mode (default): the claimed source strings still drive the
  decision — nothing changes before enforce is switched on.
- enforce mode: a cross-silo decision requires an *attested* source. No
  source pid, no launch record, a recycled pid, or a forged silo claim all
  fail closed; only a registered + verified source whose attested silo
  satisfies a rule is allowed, and the claim is overridden by the attested
  value.
- under enforce, the same-silo `identity_verified` shortcut also needs
  launch-record verification before it can bypass rule evaluation.
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
from qdistro_lineage_store import LineageStore  # noqa: E402

ADMIN_UID = 1000            # qdshell, the D-Bus caller of these gates
QDSHELL_PID = 900
QDSHELL_EXE = "/usr/bin/qdshell"

SOURCE_PID = 55501          # the silo app whose data is moving
SOURCE_EXE = "/usr/bin/firefox"
SOURCE_UID = 4001
SOURCE_START = 778899


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
        self._lineage_store = LineageStore(str(Path(audit_db).with_name("lineage.sqlite")))
        self.hooks = type("_NoHooks", (), {"query": lambda self, *a: None})()
        # The gates' D-Bus caller is always qdshell (admin uid).
        self._peer = (ADMIN_UID, QDSHELL_PID, QDSHELL_EXE, 1)

    def _peer_info(self, sender, conn):
        return self._peer

    def _clipboard_receive_lineage(self, **_kwargs):
        return True, "stubbed"


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
def fake_source_live(monkeypatch):
    """SOURCE_PID resolves to a known live process; unknown pids are gone."""
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
def clean_silo_security_registry(tmp_path, monkeypatch):
    p = tmp_path / "silo-security.toml"
    p.write_text(
        "[silo.user1]\n"
        "guards = []\n"
        "compartments = ['user1']\n"
        "conflict_classes = ['clipboard']\n"
        "\n"
        "[silo.admin]\n"
        "guards = []\n"
        "compartments = ['admin']\n"
        "conflict_classes = ['clipboard']\n"
    )
    p.chmod(0o644)
    monkeypatch.setenv("QDISTRO_SILO_SECURITY", str(p))
    return p


def _transfer_rule(rules_dir: Path, *, src="user1", dst="admin",
                   decision="allow"):
    (rules_dir / "xfer.yaml").write_text(
        f"- name: xfer\n"
        f"  decision: {decision}\n"
        f"  match:\n"
        f"    action: 'qdistro.clipboard.transfer:{src}:{dst}'\n")


def _register_source(broker, *, silo="user1", engine="qdistro.tier3",
                     app_id="qdistro.tier3.user1", starttime=SOURCE_START):
    return broker.launch_records.register(
        silo=silo, uid=SOURCE_UID, pid=SOURCE_PID, starttime=starttime,
        exe=SOURCE_EXE, sandbox_engine=engine, app_id=app_id)


def _journal_decision_line(capsys, gate: str) -> str:
    """Return the single `[broker] clipboard/<gate> cross-silo decision:` line
    from captured stdout (the journal observability surface)."""
    marker = f"[broker] clipboard/{gate} cross-silo decision:"
    lines = [ln for ln in capsys.readouterr().out.splitlines() if marker in ln]
    assert len(lines) == 1, f"expected exactly one {marker!r} line, got {lines!r}"
    return lines[0]


def _transfer(broker, *, src, dst, identity_verified=False,
              source_pid=0, source_starttime=0):
    return broker.CheckClipboardTransfer(
        src, dst, ["text/plain"], "", "", "", identity_verified,
        source_pid, source_starttime)


# --- shadow mode (default) --------------------------------------------

class TestShadow:
    def test_claimed_src_drives_decision(self, broker, rules_dir,
                                         fake_source_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        # No source pid, no record — legacy behaviour: the claimed src
        # "user1" matches the rule.
        assert _transfer(broker, src="user1", dst="admin") == "allow"

    def test_shadow_never_hard_denies_without_pid(self, broker, rules_dir,
                                                  fake_source_live,
                                                  monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        # Even with a forged claim and no attestation, shadow keeps the
        # legacy verdict (here: allow) — it only logs the divergence.
        assert _transfer(broker, src="user1", dst="admin",
                         source_pid=SOURCE_PID) == "allow"


# --- enforce mode ------------------------------------------------------

class TestEnforce:
    def test_no_source_pid_cross_silo_denied(self, broker, rules_dir,
                                             fake_source_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        # A cross-silo decision with no attested source → deny.
        assert _transfer(broker, src="user1", dst="admin") == "deny"

    def test_source_pid_no_record_denied(self, broker, rules_dir,
                                         fake_source_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        # Source pid relayed but never registered → unverified → deny.
        assert _transfer(broker, src="user1", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START) == "deny"

    def test_registered_source_allows(self, broker, rules_dir,
                                      fake_source_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        _register_source(broker, silo="user1")
        # Attested silo "user1" satisfies the user1:admin rule.
        assert _transfer(broker, src="user1", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START) == "allow"

    def test_forged_silo_overridden_by_attestation(self, broker, rules_dir,
                                                    fake_source_live,
                                                    monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        # Rule only allows the privileged-looking "admin:admin" pairing.
        _transfer_rule(rules_dir, src="admin", dst="admin")
        broker.rules.reload()
        # The source is really silo "user1"; the caller forges src="admin"
        # to hit the admin:admin rule. Enforce overrides the claim with the
        # attested silo → action becomes user1:admin → no rule → deny.
        _register_source(broker, silo="user1")
        assert _transfer(broker, src="admin", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START) == "deny"

    def test_recycled_source_pid_denied(self, broker, rules_dir,
                                        fake_source_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        # Record minted for an old starttime; live process now differs from
        # the relayed starttime → recycled-pid drift → deny.
        _register_source(broker, silo="user1", starttime=SOURCE_START)
        assert _transfer(broker, src="user1", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START + 1) == "deny"

    def test_same_silo_verified_without_source_denied(self, broker, rules_dir,
                                                      fake_source_live,
                                                      monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        broker.rules.reload()
        # Same silo + identity_verified is not enough under enforcement:
        # qdshell must relay the source pid/starttime so the broker can bind
        # the claimed silo to a verified launch record.
        assert _transfer(broker, src="user1", dst="user1",
                         identity_verified=True) == "deny"

    def test_same_silo_verified_registered_source_allows(
            self, broker, rules_dir, fake_source_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        broker.rules.reload()
        _register_source(broker, silo="user1")
        assert _transfer(broker, src="user1", dst="user1",
                         identity_verified=True,
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START) == "allow"


# --- the other two gates share the helper; spot-check enforce paths ----

class TestReceiveAndHandoff:
    def test_receive_no_source_denied(self, broker, rules_dir,
                                      fake_source_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        (rules_dir / "r.yaml").write_text(
            "- name: r\n  decision: allow\n  match:\n"
            "    action: 'qdistro.clipboard.receive:user1:admin'\n")
        broker.rules.reload()
        assert broker.CheckClipboardReceive(
            "user1", "admin", "text/plain", "", "", "", False, 0, 0) == "deny"

    def test_receive_registered_source_allows(self, broker, rules_dir,
                                              fake_source_live,
                                              clean_silo_security_registry,
                                              monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        (rules_dir / "r.yaml").write_text(
            "- name: r\n  decision: allow\n  match:\n"
            "    action: 'qdistro.clipboard.receive:user1:admin'\n")
        broker.rules.reload()
        _register_source(broker, silo="user1")
        assert broker.CheckClipboardReceive(
            "user1", "admin", "text/plain", "", "", "", False,
            SOURCE_PID, SOURCE_START) == "allow"

    def test_handoff_no_source_denied(self, broker, rules_dir,
                                      fake_source_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        (rules_dir / "h.yaml").write_text(
            "- name: h\n  decision: allow\n  match:\n"
            "    action: 'qdistro.handoff.activate:user1:admin'\n")
        broker.rules.reload()
        assert broker.CheckHandoffActivation(
            "user1", "admin", "", "", "", False, 0, 0) == "deny"

    def test_handoff_registered_source_allows(self, broker, rules_dir,
                                             fake_source_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        (rules_dir / "h.yaml").write_text(
            "- name: h\n  decision: allow\n  match:\n"
            "    action: 'qdistro.handoff.activate:user1:admin'\n")
        broker.rules.reload()
        _register_source(broker, silo="user1")
        assert broker.CheckHandoffActivation(
            "user1", "admin", "", "", "", False,
            SOURCE_PID, SOURCE_START) == "allow"

    def test_receive_same_silo_registered_source_allows(
            self, broker, fake_source_live, clean_silo_security_registry,
            monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _register_source(broker, silo="user1")
        assert broker.CheckClipboardReceive(
            "user1", "user1", "text/plain", "", "", "", True,
            SOURCE_PID, SOURCE_START) == "allow"

    def test_handoff_same_silo_registered_source_allows(
            self, broker, fake_source_live, monkeypatch):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _register_source(broker, silo="user1")
        assert broker.CheckHandoffActivation(
            "user1", "user1", "", "", "", True,
            SOURCE_PID, SOURCE_START) == "allow"


class TestAttestedAttributionLogged:
    """The audit row and the journal line must carry the *attested* source
    identity the decision was made against, never the qdshell-relayed claim.

    Regression guard for the handoff path: _cross_silo_source() writes the
    resolved identity back into sapp_raw/seng_raw (used for the rule match),
    and CheckHandoffActivation must refresh its sapp/seng display values from
    those raws before auditing/journalling — otherwise a forged app_id/engine
    claim is logged even though the rule was decided against the attested one.
    Transfer/Receive bind sapp/seng directly to the resolved return and are
    covered here too so the journal-line format itself is pinned.
    """

    def test_handoff_logs_attested_app_engine_not_claim(
            self, broker, rules_dir, fake_source_live, monkeypatch, capsys):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        (rules_dir / "h.yaml").write_text(
            "- name: h\n  decision: allow\n  match:\n"
            "    action: 'qdistro.handoff.activate:user1:admin'\n")
        broker.rules.reload()
        # Attested identity differs from the forged claim below.
        _register_source(broker, silo="user1",
                         app_id="qdistro.tier3.user1", engine="qdistro.tier3")
        verdict = broker.CheckHandoffActivation(
            "user1", "admin",
            "forged.evil.app", "dst.app", "forged.engine", False,
            SOURCE_PID, SOURCE_START)
        assert verdict == "allow"
        # Audit row carries the attested identity, never the forged claim.
        row = broker.audit.recent(10)[0]["source"]
        assert "src_app=qdistro.tier3.user1" in row
        assert "src_engine=qdistro.tier3" in row
        assert "forged.evil.app" not in row
        assert "forged.engine" not in row
        # The journal line (the s110/s112 observability surface) agrees.
        # Isolate it: _cross_silo_source() also prints an "overridden"
        # diagnostic that legitimately names the forged claim, so assert on
        # the decision line itself, not all of stdout.
        jline = _journal_decision_line(capsys, "handoff")
        assert "verdict=allow" in jline
        assert "src_app=qdistro.tier3.user1" in jline
        assert "src_engine=qdistro.tier3" in jline
        assert "forged.evil.app" not in jline
        assert "forged.engine" not in jline

    def test_transfer_journal_line_format(
            self, broker, rules_dir, fake_source_live, monkeypatch, capsys):
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        _register_source(broker, silo="user1",
                         app_id="qdistro.tier3.user1", engine="qdistro.tier3")
        assert _transfer(broker, src="user1", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START) == "allow"
        jline = _journal_decision_line(capsys, "transfer")
        assert "user1 -> admin" in jline
        assert "verdict=allow" in jline
        assert "src_app=qdistro.tier3.user1" in jline
        assert "src_engine=qdistro.tier3" in jline


# --- enforce edge cases ------------------------------------------------

class TestEnforceEdges:
    def test_deny_rule_still_denies_verified_source(self, broker, rules_dir,
                                                    fake_source_live,
                                                    monkeypatch):
        # A verified source whose silo matches an explicit *deny* rule is
        # denied — attestation feeds the rule, it does not bypass it.
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _transfer_rule(rules_dir, src="user1", dst="admin", decision="deny")
        broker.rules.reload()
        _register_source(broker, silo="user1")
        assert _transfer(broker, src="user1", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START) == "deny"

    def test_verified_source_no_rule_default_denies(self, broker, rules_dir,
                                                    fake_source_live,
                                                    monkeypatch):
        # Verified source but no rule for its attested silo pair → the
        # cross-silo default-deny still applies.
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _transfer_rule(rules_dir, src="otheruser", dst="admin")
        broker.rules.reload()
        _register_source(broker, silo="user1")
        assert _transfer(broker, src="user1", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START) == "deny"

    def test_source_starttime_zero_still_resolves(self, broker, rules_dir,
                                                  fake_source_live,
                                                  monkeypatch):
        # When the relayed starttime is 0 (qdwin could not read it) the
        # drift check is skipped, but the launch-record lookup still anchors
        # on the live starttime, so a registered source resolves + allows.
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        _register_source(broker, silo="user1")
        assert _transfer(broker, src="user1", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=0) == "allow"

    def test_source_proc_gone_denied(self, broker, rules_dir, monkeypatch):
        # The relayed pid names no live process (starttime read → 0) →
        # unresolvable → cross-silo deny.
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        monkeypatch.setattr(pi, "read_exe_and_starttime",
                            lambda pid: ("?", 0))
        monkeypatch.setattr(pi, "read_uid", lambda pid: None)
        monkeypatch.setattr(pi, "read_selinux_label", lambda pid: "")
        monkeypatch.setattr(pi, "read_cgroup", lambda pid: "")
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        assert _transfer(broker, src="user1", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START) == "deny"

    def test_registered_source_exe_mismatch_denied(self, broker, rules_dir,
                                                   fake_source_live,
                                                   monkeypatch):
        # Record carries a different exe than the live process → resolver
        # axis mismatch → unverified → deny (anti exe-swap).
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        broker.launch_records.register(
            silo="user1", uid=SOURCE_UID, pid=SOURCE_PID,
            starttime=SOURCE_START, exe="/usr/bin/evil",
            sandbox_engine="qdistro.tier3", app_id="qdistro.tier3.user1")
        assert _transfer(broker, src="user1", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START) == "deny"

    def test_hard_deny_writes_audit_row(self, broker, rules_dir,
                                        fake_source_live, monkeypatch):
        # A cross-silo deny for want of an attested source leaves a
        # forensic row (findings Q#7).
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", True)
        _transfer_rule(rules_dir, src="user1", dst="admin")
        broker.rules.reload()
        _transfer(broker, src="user1", dst="admin")  # no source pid
        rows = broker.audit.recent(20)
        assert any("qdistro.lineage.source_deny:clipboard.transfer"
                   in str(r["action"]) for r in rows)

    def test_shadow_does_not_override_diverging_claim(self, broker, rules_dir,
                                                      fake_source_live,
                                                      monkeypatch):
        # Shadow: the source is really user1 but the caller claims "admin"
        # to hit an admin:admin rule. Shadow keeps the legacy verdict
        # (allow) — it only logs the divergence; enforce is what overrides.
        monkeypatch.setattr(B, "LINEAGE_ENFORCE", False)
        _transfer_rule(rules_dir, src="admin", dst="admin")
        broker.rules.reload()
        _register_source(broker, silo="user1")
        assert _transfer(broker, src="admin", dst="admin",
                         source_pid=SOURCE_PID,
                         source_starttime=SOURCE_START) == "allow"
