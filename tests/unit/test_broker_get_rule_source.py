"""Tests for qdistro_admin_broker.Broker.GetRuleSource.

GetRuleSource is the broker-side, read-only surface for the admin
app's "Edit rule" path. Editing an existing rule must show the actual
on-disk YAML (comments + any keys ListRules does not project) so a
round-trip save does not regenerate from the projected fields and lose
them (security-hardening-carryforward §"Broker and rules").

The admin app cannot read /etc/qdistro/rules.d directly (root-owned),
so reading the file is a broker RPC — the same reason DeleteRule
exists. It re-applies the SAME fail-closed path safety as DeleteRule
(realpath containment inside the canonical rules.d, reject symlink /
escape / missing / non-regular). Admin/root only. Read-only, so no
audit row. Mirrors the test_broker_delete_rule.py harness.
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


# A body deliberately carrying a comment and a key ListRules does not
# project, to prove the raw text (not a regenerated view) is returned.
RAW_BODY = (
    "# this comment must survive a round-trip\n"
    "- name: from-test\n"
    "  decision: allow\n"
    "  match:\n"
    "    action: 'qdistro.view-stream.subscribe:test'\n"
    "    uid: 1234\n"
    "  future_unknown_key: keep-me\n"
)


def _seed_rule(broker, filename: str = "to-read.yaml") -> str:
    """Write a rule file directly into the rules dir and return its path.

    GetRuleSource serves the verbatim bytes of any .yaml/.yml file
    inside rules.d (it does NOT require the file to currently parse —
    an admin must be able to open a malformed rule to fix it)."""
    path = Path(broker.rules._dir) / filename
    path.write_text(RAW_BODY, encoding="utf-8")
    return str(path)


class TestGetRuleSourceAccess:
    def test_admin_reads_raw_text_verbatim(self, broker):
        src = _seed_rule(broker)
        broker.set_peer(uid=ADMIN_UID)
        text = str(broker.GetRuleSource(src))
        # Exact file content, byte-for-byte (comments + unknown keys).
        assert text == RAW_BODY
        assert "# this comment must survive" in text
        assert "future_unknown_key: keep-me" in text

    def test_root_reads(self, broker):
        src = _seed_rule(broker)
        broker.set_peer(uid=0)
        assert str(broker.GetRuleSource(src)) == RAW_BODY

    def test_non_admin_refused(self, broker):
        src = _seed_rule(broker)
        broker.set_peer(uid=NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.GetRuleSource(src)
        assert "AccessDenied" in str(ei.value) or \
               "restricted" in str(ei.value).lower()

    def test_read_does_not_modify_or_delete(self, broker):
        src = _seed_rule(broker)
        broker.set_peer(uid=ADMIN_UID)
        broker.GetRuleSource(src)
        # Read-only: file untouched, still present, content unchanged.
        assert Path(src).exists()
        assert Path(src).read_text(encoding="utf-8") == RAW_BODY


class TestGetRuleSourcePathSafety:
    def test_rejects_missing_file(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        target = str(rules_dir / "nope.yaml")
        with pytest.raises(dbus.DBusException) as ei:
            broker.GetRuleSource(target)
        assert "RulesEngineRefused" in str(ei.value) or \
               "not found" in str(ei.value).lower()

    def test_rejects_empty_path(self, broker):
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.GetRuleSource("")
        assert "RulesEngineRefused" in str(ei.value) or \
               "empty" in str(ei.value).lower()

    def test_rejects_path_escape(self, broker, rules_dir, tmp_path):
        # A real secret outside rules.d that the client tries to read via
        # a rules.d-prefixed traversal path.
        outside = tmp_path / "secret.yaml"
        outside.write_text("- name: secret\n  decision: deny\n")
        broker.set_peer(uid=ADMIN_UID)
        escape = str(rules_dir / ".." / "secret.yaml")
        with pytest.raises(dbus.DBusException) as ei:
            broker.GetRuleSource(escape)
        assert "outside" in str(ei.value).lower() or \
               "RulesEngineRefused" in str(ei.value)

    def test_rejects_absolute_path_outside_rules_d(self, broker, tmp_path):
        # Cannot read an arbitrary file (e.g. /etc/shadow analogue) by
        # supplying its plain absolute path.
        outside = tmp_path / "outside" / "shadowy.yaml"
        outside.parent.mkdir()
        outside.write_text("root:x:0:0\n")
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.GetRuleSource(str(outside))
        assert "outside" in str(ei.value).lower() or \
               "RulesEngineRefused" in str(ei.value)

    def test_rejects_symlink(self, broker, rules_dir, tmp_path):
        # A symlink living inside rules.d pointing at an outside secret:
        # following it would exfiltrate the target's contents.
        outside = tmp_path / "target.txt"
        outside.write_text("super secret\n")
        link = rules_dir / "link.yaml"
        os.symlink(outside, link)
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.GetRuleSource(str(link))
        assert "symlink" in str(ei.value).lower() or \
               "RulesEngineRefused" in str(ei.value)

    def test_rejects_directory(self, broker, rules_dir):
        sub = rules_dir / "subdir"
        sub.mkdir()
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.GetRuleSource(str(sub))

    def test_rejects_non_yaml_file_inside_rules_d(self, broker, rules_dir):
        # A non-.yaml regular file living inside rules.d must be refused:
        # the RPC serves rule files only, so it cannot be used to read an
        # arbitrary secret an admin happened to drop in rules.d.
        stray = rules_dir / "secret.txt"
        stray.write_text("api_key=supersecret\n")
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.GetRuleSource(str(stray))
        assert "not a YAML rule file" in str(ei.value) or \
               "RulesEngineRefused" in str(ei.value)
        assert "supersecret" not in str(ei.value)

    def test_serves_malformed_rule_for_repair(self, broker, rules_dir):
        # A .yaml file that fails to parse must still be served verbatim
        # so an admin can open and fix it in the editor.
        bad = rules_dir / "broken.yaml"
        bad.write_text("- name: broken\n  decision: not-a-valid-decision\n")
        broker.set_peer(uid=ADMIN_UID)
        text = str(broker.GetRuleSource(str(bad)))
        assert "not-a-valid-decision" in text
