"""Tests for qdistro_admin_broker.Broker.DeleteRule.

DeleteRule is the broker-side surface for the admin app's "Delete
rule" button. It re-applies path safety fail-closed, unlinks the
YAML rule file from the rules dir, writes a broker-side audit row,
and hot-reloads the engine. Admin/root only.

Replaces the admin app's former direct ``os.remove`` of rule files
(security-hardening-carryforward §"Broker and rules"). Mirrors the
test_broker_save_rule.py harness.
"""
from __future__ import annotations

import os
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


def _seed_rule(broker, filename: str = "to-delete.yaml") -> str:
    """Save a rule as admin and return its source_path from ListRules."""
    broker.set_peer(uid=ADMIN_UID)
    broker.SaveRule(filename, VALID_BODY)
    rows = broker.ListRules()
    assert rows, "expected the seeded rule to be loaded"
    src = str(rows[0]["source_path"])
    assert Path(src).exists()
    return src


class TestDeleteRuleAccess:
    def test_admin_deletes(self, broker):
        src = _seed_rule(broker)
        broker.set_peer(uid=ADMIN_UID)
        deleted, count, errors = broker.DeleteRule(src, "from-test")
        assert bool(deleted) is True
        assert int(count) == 0          # nothing left after delete
        assert list(errors) == []
        assert not Path(src).exists()
        assert broker.ListRules() == []

    def test_root_deletes(self, broker):
        src = _seed_rule(broker)
        broker.set_peer(uid=0)
        deleted, _count, _errors = broker.DeleteRule(src, "from-test")
        assert bool(deleted) is True
        assert not Path(src).exists()

    def test_non_admin_refused(self, broker):
        src = _seed_rule(broker)
        broker.set_peer(uid=NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.DeleteRule(src, "from-test")
        assert "AccessDenied" in str(ei.value) or \
               "restricted" in str(ei.value).lower()
        # File is untouched.
        assert Path(src).exists()

    def test_delete_emits_rules_reloaded(self, broker):
        src = _seed_rule(broker)
        broker.set_peer(uid=ADMIN_UID)
        before = len(broker.rules_reloaded_signals)
        broker.DeleteRule(src, "from-test")
        assert len(broker.rules_reloaded_signals) == before + 1
        # Post-delete count is carried in the signal.
        assert broker.rules_reloaded_signals[-1] == 0


class TestDeleteRulePathSafety:
    def test_rejects_missing_file(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        target = str(rules_dir / "nope.yaml")
        with pytest.raises(dbus.DBusException) as ei:
            broker.DeleteRule(target, "nope")
        assert "RulesEngineRefused" in str(ei.value) or \
               "not found" in str(ei.value).lower()

    def test_rejects_empty_path(self, broker):
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.DeleteRule("", "x")
        assert "RulesEngineRefused" in str(ei.value) or \
               "empty" in str(ei.value).lower()

    def test_rejects_path_escape(self, broker, rules_dir, tmp_path):
        # A real file outside rules.d that the client tries to delete via
        # a rules.d-prefixed traversal path.
        outside = tmp_path / "secret.yaml"
        outside.write_text(VALID_BODY)
        broker.set_peer(uid=ADMIN_UID)
        escape = str(rules_dir / ".." / "secret.yaml")
        with pytest.raises(dbus.DBusException) as ei:
            broker.DeleteRule(escape, "secret")
        assert "outside" in str(ei.value).lower() or \
               "RulesEngineRefused" in str(ei.value)
        # The outside file must survive a refused delete.
        assert outside.exists()

    def test_rejects_symlink(self, broker, rules_dir, tmp_path):
        # A symlink living inside rules.d pointing at an outside file.
        outside = tmp_path / "target.yaml"
        outside.write_text(VALID_BODY)
        link = rules_dir / "link.yaml"
        os.symlink(outside, link)
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.DeleteRule(str(link), "link")
        assert "symlink" in str(ei.value).lower() or \
               "RulesEngineRefused" in str(ei.value)
        # Neither the symlink's target nor (importantly) the target file
        # should be unlinked by a refused delete.
        assert outside.exists()

    def test_rejects_directory(self, broker, rules_dir):
        sub = rules_dir / "subdir"
        sub.mkdir()
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.DeleteRule(str(sub), "subdir")
        assert sub.exists()


class TestDeleteRuleAudit:
    def _rows(self, broker):
        return broker.audit.recent(limit=50)

    def test_success_audited_with_caller_identity(self, broker):
        src = _seed_rule(broker)
        broker.set_peer(uid=ADMIN_UID, pid=4321, exe="/usr/bin/admin-app")
        broker.DeleteRule(src, "from-test")
        rows = [r for r in self._rows(broker)
                if r["action"] == "qdistro.rules.delete"]
        assert rows, "expected a delete audit row"
        row = rows[0]
        assert int(row["caller_uid"]) == ADMIN_UID
        assert int(row["caller_pid"]) == 4321
        assert row["caller_exe"] == "/usr/bin/admin-app"
        assert bool(row["decision"]) is True
        assert "from-test" in str(row["source"])
        assert str(row["rule_path"]).endswith("to-delete.yaml")

    def test_non_admin_refusal_audited(self, broker):
        src = _seed_rule(broker)
        broker.set_peer(uid=NON_ADMIN_UID, pid=99, exe="/usr/bin/rogue")
        with pytest.raises(dbus.DBusException):
            broker.DeleteRule(src, "from-test")
        rows = [r for r in self._rows(broker)
                if r["action"] == "qdistro.rules.delete"]
        assert rows, "expected a refusal audit row"
        row = rows[0]
        assert int(row["caller_uid"]) == NON_ADMIN_UID
        assert bool(row["decision"]) is False
        assert "non-admin" in str(row["source"]).lower()

    def test_path_escape_refusal_audited(self, broker, rules_dir, tmp_path):
        outside = tmp_path / "secret.yaml"
        outside.write_text(VALID_BODY)
        broker.set_peer(uid=ADMIN_UID)
        escape = str(rules_dir / ".." / "secret.yaml")
        with pytest.raises(dbus.DBusException):
            broker.DeleteRule(escape, "secret")
        rows = [r for r in self._rows(broker)
                if r["action"] == "qdistro.rules.delete"]
        assert rows
        assert bool(rows[0]["decision"]) is False


class TestDeleteRuleFailClosed:
    """The broker-side audit row is the whole point of DeleteRule, so an
    audit-write failure must abort the delete (fail-closed) rather than
    silently unlink the file with no forensic record."""

    def test_audit_failure_aborts_before_unlink(self, broker, monkeypatch):
        src = _seed_rule(broker)
        broker.set_peer(uid=ADMIN_UID)

        def _boom(**_kw):
            raise RuntimeError("audit disk full")

        monkeypatch.setattr(broker.audit, "log", _boom)
        with pytest.raises(dbus.DBusException) as ei:
            broker.DeleteRule(src, "from-test")
        assert "audit" in str(ei.value).lower() or \
               "RulesEngineRefused" in str(ei.value)
        # File must still be present — audit failure aborts the unlink.
        assert Path(src).exists()

    def test_unlink_error_refused_and_audited(self, broker, monkeypatch):
        src = _seed_rule(broker)
        broker.set_peer(uid=ADMIN_UID)
        real_unlink = os.unlink

        def _fail_unlink(path, *, dir_fd=None):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(os, "unlink", _fail_unlink)
        with pytest.raises(dbus.DBusException) as ei:
            broker.DeleteRule(src, "from-test")
        monkeypatch.setattr(os, "unlink", real_unlink)
        assert "RulesEngineRefused" in str(ei.value) or \
               "unlink" in str(ei.value).lower()
        # Both an intent (True, before unlink) and a failure (False) row
        # should be present so the failed attempt is forensically visible.
        rows = [r for r in broker.audit.recent(limit=50)
                if r["action"] == "qdistro.rules.delete"]
        assert any(bool(r["decision"]) is False for r in rows)
