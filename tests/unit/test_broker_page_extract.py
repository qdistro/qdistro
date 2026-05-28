"""Tests for qdistro_admin_broker.Broker.PageExtract.

PageExtract is the broker side of the browser "Send to…" share-to flow
(Bridge Phase 9c). The browser bridge — running as the browser's uid —
forwards a page-extract action as a single JSON string; the broker:

  - Resolves the SOURCE silo name from the authenticated D-Bus caller
    (`_peer_info` uid → username), NOT from the JSON body.
  - Takes the DESTINATION (`dest_uid`) from the body as an opaque silo
    identifier (free-form string per the bridge/extension schema).
  - Same-user (source silo == dest silo): allow without a rule, audited.
  - Cross-user: rules-engine lookup on the synthetic action
    `qdistro.share_to:<source>:<dest>` (content_type exposed as the
    `mime_type` selector). Default-DENY when no rule matches.
  - Reply is a JSON string the bridge decodes into a dict:
    `{"ok": true}` / `{"ok": false, "error": ...}`.
  - Each call writes one audit row regardless of decision.

Mirrors the test_broker_clipboard_transfer.py harness shape.
"""
from __future__ import annotations

import json
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


BROWSER_EXE = "/usr/bin/qdistro-browser-bridge"

# Test uids deliberately have no /etc/passwd entry, so the broker's
# uid→name resolution falls back to the deterministic `uid:<n>` form.
# That fallback IS the source silo name in these tests.
SRC_UID = 2001
SRC_SILO = B._username_for_uid(SRC_UID)  # "uid:2001" on a clean box
DEST_SILO = "dev-user"  # opaque destination name, as the bridge sends


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
        # Default: caller is the browser running as SRC_UID.
        self._peer_uid = SRC_UID
        self._peer_pid = 1
        self._peer_exe = BROWSER_EXE
        self._peer_start = 0
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []

    def set_peer(self, uid: int, pid: int = 100, exe: str = BROWSER_EXE,
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


def _body(**kw) -> str:
    """Build a request body matching what the bridge's
    `_handle_page_extract` sends (url/title/selected_text/dest_uid/
    content_type/parent_exe/extension_id)."""
    payload = {
        "url": "https://example.com/article",
        "title": "An Article",
        "selected_text": "some selected text",
        "dest_uid": DEST_SILO,
        "content_type": "url",
        "parent_exe": "/usr/bin/firefox",
        "extension_id": "ext-abc",
    }
    payload.update(kw)
    return json.dumps(payload)


def _write_share_rule(rules_dir: Path, *, decision: str,
                      source: str, dest: str,
                      content_type: str | None = None,
                      name: str = "share") -> None:
    action = f"qdistro.share_to:{source}:{dest}"
    lines = [
        f"- name: {name}",
        f"  decision: {decision}",
        "  match:",
        f"    action: {action!r}",
    ]
    if content_type is not None:
        lines.append(f"    mime_type: {content_type!r}")
    (rules_dir / f"{name}.yaml").write_text("\n".join(lines) + "\n")


# --- same-user: always allowed, no rule needed --------------------------

class TestSameUser:
    def test_same_user_allowed(self, broker):
        # dest == the caller's own resolved silo name.
        reply = json.loads(broker.PageExtract(_body(dest_uid=SRC_SILO)))
        assert reply == {"ok": True}

    def test_same_user_ignores_deny_rule(self, broker, rules_dir):
        # Same-user is short-circuit allow even if a deny rule exists.
        _write_share_rule(rules_dir, decision="deny",
                          source=SRC_SILO, dest=SRC_SILO)
        broker.rules.reload()
        reply = json.loads(broker.PageExtract(_body(dest_uid=SRC_SILO)))
        assert reply == {"ok": True}

    def test_same_user_writes_audit(self, broker):
        broker.PageExtract(_body(dest_uid=SRC_SILO))
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert rows[0]["decision"] is True
        assert rows[0]["action"] == f"qdistro.share_to:{SRC_SILO}:{SRC_SILO}"
        assert "share_to_same_user" in rows[0]["source"]

    def test_uid_shaped_dest_normalizes_to_username(self, broker):
        # A caller that sends the destination as a numeric uid string
        # ("2001") rather than the username must still be recognised as
        # same-user: the broker normalizes a uid-shaped dest into the
        # same namespace as the resolved source, so the bypass and the
        # rule-action shape compare like with like.
        reply = json.loads(broker.PageExtract(_body(dest_uid=str(SRC_UID))))
        assert reply == {"ok": True}
        rows = broker.audit.recent(10)
        assert rows[0]["action"] == f"qdistro.share_to:{SRC_SILO}:{SRC_SILO}"
        assert "share_to_same_user" in rows[0]["source"]


# --- cross-user: rule lookup, default deny ------------------------------

class TestCrossUser:
    def test_default_deny_when_no_rule(self, broker):
        reply = json.loads(broker.PageExtract(_body(dest_uid=DEST_SILO)))
        assert reply == {"ok": False, "error": "policy_denied"}

    def test_allow_when_rule_matches(self, broker, rules_dir):
        _write_share_rule(rules_dir, decision="allow",
                          source=SRC_SILO, dest=DEST_SILO)
        broker.rules.reload()
        reply = json.loads(broker.PageExtract(_body(dest_uid=DEST_SILO)))
        assert reply == {"ok": True}

    def test_deny_rule_matches(self, broker, rules_dir):
        _write_share_rule(rules_dir, decision="deny",
                          source=SRC_SILO, dest=DEST_SILO)
        broker.rules.reload()
        reply = json.loads(broker.PageExtract(_body(dest_uid=DEST_SILO)))
        assert reply == {"ok": False, "error": "policy_denied"}

    def test_directional_rules(self, broker, rules_dir):
        # SRC -> DEST allowed; the reverse direction (a caller resolving
        # to DEST_SILO sending back to SRC_SILO) is NOT covered, so it
        # default-denies. We can't easily make the peer resolve to
        # DEST_SILO, so assert the asymmetry via a different dest.
        _write_share_rule(rules_dir, decision="allow",
                          source=SRC_SILO, dest=DEST_SILO, name="up")
        broker.rules.reload()
        assert json.loads(
            broker.PageExtract(_body(dest_uid=DEST_SILO))) == {"ok": True}
        # Same source, different dest with no rule → default-deny.
        assert json.loads(
            broker.PageExtract(_body(dest_uid="other-user"))) == {
                "ok": False, "error": "policy_denied"}

    def test_content_type_selector(self, broker, rules_dir):
        # Allow url, but text_selection still hits default-deny.
        _write_share_rule(rules_dir, decision="allow",
                          source=SRC_SILO, dest=DEST_SILO,
                          content_type="url")
        broker.rules.reload()
        assert json.loads(broker.PageExtract(
            _body(dest_uid=DEST_SILO, content_type="url"))) == {"ok": True}
        assert json.loads(broker.PageExtract(_body(
            dest_uid=DEST_SILO, content_type="text_selection"))) == {
                "ok": False, "error": "policy_denied"}


# --- source identity is the caller, NOT the JSON body -------------------

class TestSourceIsCaller:
    def test_body_source_uid_is_ignored(self, broker, rules_dir):
        # A rule allows SRC_SILO -> DEST_SILO. Even if the body injects a
        # different source field, the synthetic action is built from the
        # authenticated caller's resolved silo, so the rule fires.
        _write_share_rule(rules_dir, decision="allow",
                          source=SRC_SILO, dest=DEST_SILO)
        broker.rules.reload()
        body = _body(dest_uid=DEST_SILO, source_uid="9999",
                     source="evil-silo")
        assert json.loads(broker.PageExtract(body)) == {"ok": True}

    def test_spoofed_source_cannot_borrow_other_rule(self, broker,
                                                     rules_dir):
        # Rule only allows a DIFFERENT source ("admin") -> DEST_SILO. The
        # real caller resolves to SRC_SILO and tries to claim "admin" in
        # the body; the gate must default-deny because the AUTHENTICATED
        # caller's silo has no matching rule.
        _write_share_rule(rules_dir, decision="allow",
                          source="admin", dest=DEST_SILO)
        broker.rules.reload()
        body = _body(dest_uid=DEST_SILO, source_uid="0", source="admin")
        assert json.loads(broker.PageExtract(body)) == {
            "ok": False, "error": "policy_denied"}
        # Audit row records the REAL caller's resolved silo in the action.
        rows = broker.audit.recent(1)
        assert rows[0]["action"] == f"qdistro.share_to:{SRC_SILO}:{DEST_SILO}"


# --- audit emission -----------------------------------------------------

class TestAudit:
    def test_default_deny_audit(self, broker):
        broker.PageExtract(_body(dest_uid=DEST_SILO, content_type="url"))
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "share_to_default_deny" in rows[0]["source"]
        assert "content_type=url" in rows[0]["source"]
        assert f"dest={DEST_SILO}" in rows[0]["source"]
        assert rows[0]["decision"] is False
        assert rows[0]["rule_path"] is None

    def test_rule_path_recorded_when_rule_fires(self, broker, rules_dir):
        _write_share_rule(rules_dir, decision="allow",
                          source=SRC_SILO, dest=DEST_SILO, name="r")
        broker.rules.reload()
        broker.PageExtract(_body(dest_uid=DEST_SILO))
        rows = broker.audit.recent(10)
        assert len(rows) == 1
        assert "share_to_rule" in rows[0]["source"]
        assert rows[0]["rule_path"] is not None
        assert rows[0]["rule_path"].endswith("r.yaml")

    def test_audit_records_extension_and_parent_exe(self, broker):
        broker.PageExtract(_body(
            dest_uid=DEST_SILO, extension_id="ext-xyz",
            parent_exe="/usr/bin/chromium"))
        rows = broker.audit.recent(1)
        assert "ext=ext-xyz" in rows[0]["source"]
        assert "parent_exe=/usr/bin/chromium" in rows[0]["source"]

    def test_audit_never_contains_page_text(self, broker):
        broker.PageExtract(_body(
            dest_uid=DEST_SILO,
            selected_text="SECRET-PAGE-CONTENT-DO-NOT-LOG"))
        rows = broker.audit.recent(1)
        assert "SECRET-PAGE-CONTENT-DO-NOT-LOG" not in rows[0]["source"]


# --- malformed body -----------------------------------------------------

class TestMalformedBody:
    def test_not_json(self, broker):
        reply = json.loads(broker.PageExtract("not-json{"))
        assert reply == {"ok": False, "error": "malformed_body"}

    def test_not_an_object(self, broker):
        reply = json.loads(broker.PageExtract(json.dumps(["a", "b"])))
        assert reply == {"ok": False, "error": "malformed_body"}

    def test_missing_url(self, broker):
        body = json.dumps({"dest_uid": DEST_SILO, "content_type": "url"})
        reply = json.loads(broker.PageExtract(body))
        assert reply == {"ok": False, "error": "missing_url"}

    def test_empty_url(self, broker):
        reply = json.loads(broker.PageExtract(_body(url="")))
        assert reply == {"ok": False, "error": "missing_url"}

    def test_missing_dest(self, broker):
        reply = json.loads(broker.PageExtract(_body(dest_uid="")))
        assert reply == {"ok": False, "error": "missing_dest"}

    def test_dest_too_long(self, broker):
        reply = json.loads(broker.PageExtract(_body(dest_uid="d" * 81)))
        assert reply == {"ok": False, "error": "bad_dest"}

    def test_dest_max_length_accepted(self, broker):
        # 80 chars exactly is at the boundary — must not be rejected at
        # the input gate (cross-user + no rule → default-deny is fine).
        reply = json.loads(broker.PageExtract(_body(dest_uid="d" * 80)))
        assert reply == {"ok": False, "error": "policy_denied"}

    def test_malformed_body_writes_no_audit(self, broker):
        broker.PageExtract("not-json{")
        assert broker.audit.recent(10) == []


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
        assert json.loads(
            b.PageExtract(_body(dest_uid=SRC_SILO))) == {"ok": True}
        assert json.loads(
            b.PageExtract(_body(dest_uid=SRC_SILO))) == {"ok": True}
        import dbus
        with pytest.raises(dbus.DBusException):
            b.PageExtract(_body(dest_uid=SRC_SILO))
