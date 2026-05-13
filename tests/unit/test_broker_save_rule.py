"""Tests for qdistro_admin_broker.Broker.SaveRule.

SaveRule is the broker-side surface for the admin app's "Rule from
this..." flow (spec/25 §Phase-2). It validates a YAML rule, writes
to the rules dir, and hot-reloads the engine. Admin/root only.

Mirrors the test_broker_check_permission.py harness shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("dbus")

import dbus  # noqa: E402

from test_broker_check_permission import (  # noqa: E402
    _StubBroker, ADMIN_UID, NON_ADMIN_UID,
)


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


VALID_BODY = (
    "- name: from-test\n"
    "  decision: allow\n"
    "  match:\n"
    "    action: 'qdistro.view-stream.subscribe:test'\n"
    "    uid: 1234\n"
)


class TestSaveRuleAccess:
    def test_admin_succeeds(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        path = broker.SaveRule("from-test.yaml", VALID_BODY)
        assert Path(path).exists()
        assert Path(path).read_text(encoding="utf-8") == VALID_BODY

    def test_admin_save_emits_rules_reloaded(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        broker.SaveRule("from-test.yaml", VALID_BODY)
        # The signal fires once per successful SaveRule, carrying the
        # post-reload rule count. Subscribers (qdshell) re-check live
        # cross-silo state when this lands.
        assert len(broker.rules_reloaded_signals) == 1
        assert broker.rules_reloaded_signals[0] == 1
        broker.SaveRule("from-test-2.yaml", VALID_BODY)
        assert len(broker.rules_reloaded_signals) == 2
        assert broker.rules_reloaded_signals[1] == 2

    def test_failed_save_does_not_emit(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.SaveRule("from-bad.yaml", "not: a: list:\n[")
        assert broker.rules_reloaded_signals == []

    def test_reload_rules_emits_rules_reloaded(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        # Pre-seed a file directly so ReloadRules has something to load.
        (rules_dir / "external.yaml").write_text(VALID_BODY)
        broker.ReloadRules()
        assert len(broker.rules_reloaded_signals) == 1
        assert broker.rules_reloaded_signals[0] == 1

    def test_root_succeeds(self, broker):
        broker.set_peer(uid=0)
        broker.SaveRule("from-root.yaml", VALID_BODY)

    def test_non_admin_refused(self, broker):
        broker.set_peer(uid=NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.SaveRule("from-bad.yaml", VALID_BODY)
        assert "AccessDenied" in str(ei.value) or \
               "restricted" in str(ei.value).lower()


class TestSaveRuleValidation:
    def test_rejects_path_traversal(self, broker):
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.SaveRule("../escape.yaml", VALID_BODY)
        assert "RulesEngineRefused" in str(ei.value) or \
               "match" in str(ei.value).lower()

    def test_rejects_bad_extension(self, broker):
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.SaveRule("from-bad.txt", VALID_BODY)

    def test_rejects_invalid_yaml(self, broker):
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.SaveRule("from-bad.yaml", "not: a: list:\n[")

    def test_rejects_unknown_decision(self, broker):
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.SaveRule(
                "from-bad.yaml",
                "- decision: maybe\n"
                "  match:\n"
                "    action: 'x'\n",
            )


class TestListRules:
    """ListRules — admin-only enumeration of loaded rules for the
    Rules tab in the admin app. Round-trips name / decision /
    source_path / match selectors / scope / rationale. The "don't
    care" sentinel is uid=-1 / action="" / exe="" / scope="".
    """
    def test_admin_empty(self, broker):
        broker.set_peer(uid=ADMIN_UID)
        assert broker.ListRules() == []

    def test_non_admin_refused(self, broker):
        broker.set_peer(uid=NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.ListRules()
        assert "AccessDenied" in str(ei.value) or \
               "restricted" in str(ei.value).lower()

    def test_after_save(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        broker.SaveRule(
            "list-test.yaml",
            "- name: list-test-rule\n"
            "  decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.view-stream.subscribe:lr'\n"
            "    uid: 4242\n"
            "  scope: 24h\n"
            "  rationale: testing list\n",
        )
        rows = broker.ListRules()
        assert len(rows) == 1
        r = rows[0]
        assert str(r["name"]) == "list-test-rule"
        assert str(r["decision"]) == "allow"
        assert str(r["action"]) == "qdistro.view-stream.subscribe:lr"
        assert int(r["uid"]) == 4242
        assert str(r["exe"]) == ""             # not specified -> ""
        assert str(r["scope"]) == "24h"
        assert str(r["rationale"]) == "testing list"
        assert "list-test.yaml" in str(r["source_path"])

    def test_dont_care_uid_renders_minus_one(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        broker.SaveRule(
            "any-uid.yaml",
            "- decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.view-stream.subscribe:any'\n",
        )
        rows = broker.ListRules()
        assert len(rows) == 1
        assert int(rows[0]["uid"]) == -1


class TestSaveRuleHotReload:
    def test_save_then_match(self, broker, rules_dir):
        # Pre-condition: no rule, action returns "unknown".
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(
            "qdistro.view-stream.subscribe:hotrl", {}) == "unknown"
        # Save an allow rule for that action.
        broker.set_peer(uid=ADMIN_UID)
        broker.SaveRule("hot-reload.yaml",
            "- decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.view-stream.subscribe:hotrl'\n"
            f"    uid: {NON_ADMIN_UID}\n")
        # Post-condition: same action now allows without a separate
        # broker.rules.reload() call — SaveRule does it for us.
        broker.set_peer(uid=NON_ADMIN_UID)
        assert broker.CheckPermission(
            "qdistro.view-stream.subscribe:hotrl", {}) == "allow"
