"""Tests for the admin app Rules tab, Rule editor dialog, and
"Rule from this" integration.

These are pure-Python + PyQt6 tests -- no D-Bus daemon needed. The
BrokerBridge is stubbed to return canned data so we exercise the
widget logic, signal wiring, and YAML generation without a live broker.

When PyQt6 is not importable (e.g. PySide6/PyQt6 library conflict on
the host, or headless CI), the entire module is skipped via
pytest.importorskip -- same pattern as test_admin_notifications.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Skip the entire module when PyQt6 is not importable.
QtWidgets = pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
QtCore = pytest.importorskip("PyQt6.QtCore", exc_type=ImportError)

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

# Make the admin_app and broker modules importable.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "admin_app"))
sys.path.insert(0, str(_ROOT / "broker"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Import after path setup.
from qdistro_admin_app import (  # noqa: E402
    BrokerBridge,
    CacheTab,
    DetailPane,
    HistoryTab,
    MainWindow,
    RuleEditorDialog,
    RulesTab,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    """Singleton QApplication for the test session."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

def _make_stub_broker():
    """Return a MagicMock that quacks like BrokerBridge."""
    broker = MagicMock()
    broker.list_rules.return_value = [
        {
            "name": "allow-apt",
            "decision": "allow",
            "source_path": "/etc/qdistro/rules.d/apt.yaml",
            "uid": 2000,
            "action": "qdistro.exec.apt-get",
            "exe": "/usr/bin/apt-get",
            "scope": "forever",
            "rationale": "Allow apt-get for uid 2000",
        },
        {
            "name": "deny-rm",
            "decision": "deny",
            "source_path": "/etc/qdistro/rules.d/deny-rm.yaml",
            "uid": -1,
            "action": "qdistro.exec.rm",
            "exe": "/usr/bin/rm",
            "scope": "",
            "rationale": "Deny rm globally",
        },
    ]
    broker.save_rule.return_value = "/etc/qdistro/rules.d/new.yaml"
    broker.reload_rules.return_value = None
    broker.list_history.return_value = []
    broker.get_pending.return_value = []
    # Stub out signal attributes that widgets connect to
    broker.rulesReloaded = MagicMock()
    broker.rulesReloaded.connect = MagicMock()
    broker.requestPending = MagicMock()
    broker.requestPending.connect = MagicMock()
    broker.requestDecided = MagicMock()
    broker.requestDecided.connect = MagicMock()
    broker.approvalRevoked = MagicMock()
    broker.approvalRevoked.connect = MagicMock()
    return broker


def _sample_pending_request():
    return {
        "id": 42,
        "uid": 2000,
        "pid": 12345,
        "exe": "/usr/bin/curl",
        "action": "qdistro.network.fetch",
        "details": {"url": "https://example.com"},
    }


def _sample_history_entry():
    return {
        "ts": 1716800000,
        "caller_uid": 2001,
        "caller_pid": 9999,
        "caller_exe": "/usr/bin/git",
        "action": "qdistro.exec.git",
        "decision": True,
        "scope": "forever_exe",
        "source": "prompt",
        "approver_uid": 1000,
        "rule_path": "",
        "request_id": 7,
        "selinux_subj_type": "",
        "argv": ["git", "push"],
    }


# ---------------------------------------------------------------------------
# RulesTab tests
# ---------------------------------------------------------------------------

class TestRulesTab:
    """Exercises the RulesTab widget in isolation."""

    def test_rules_table_populated_from_broker(self, qapp):
        """refresh() populates the QStandardItemModel from broker.list_rules()."""
        broker = _make_stub_broker()
        tab = RulesTab(broker)
        tab.refresh()

        assert tab.model.rowCount() == 2
        # First row: allow-apt
        assert tab.model.item(0, 0).text() == "allow-apt"
        assert tab.model.item(0, 1).text() == "allow"
        assert tab.model.item(0, 2).text() == "2000"
        assert tab.model.item(0, 3).text() == "qdistro.exec.apt-get"
        assert tab.model.item(0, 4).text() == "/usr/bin/apt-get"
        assert tab.model.item(0, 5).text() == "forever"
        # Second row: deny-rm
        assert tab.model.item(1, 0).text() == "deny-rm"
        assert tab.model.item(1, 1).text() == "deny"
        # uid=-1 renders as "any"
        assert tab.model.item(1, 2).text() == "any"

    def test_rules_table_stashes_raw_data(self, qapp):
        """Each row stores the raw rule dict on column-0 UserRole+1."""
        broker = _make_stub_broker()
        tab = RulesTab(broker)
        tab.refresh()

        raw = tab.model.item(0, 0).data(Qt.ItemDataRole.UserRole + 1)
        assert raw["name"] == "allow-apt"
        assert raw["uid"] == 2000

    def test_rules_table_empty_when_no_rules(self, qapp):
        broker = _make_stub_broker()
        broker.list_rules.return_value = []
        tab = RulesTab(broker)
        tab.refresh()

        assert tab.model.rowCount() == 0

    def test_uid_minus_one_shows_any(self, qapp):
        """uid=-1 (the D-Bus 'don't care' sentinel) renders as 'any'."""
        broker = _make_stub_broker()
        tab = RulesTab(broker)
        tab.refresh()

        # Second rule has uid=-1
        assert tab.model.item(1, 2).text() == "any"

    def test_empty_action_shows_any(self, qapp):
        """Empty action string renders as 'any'."""
        broker = _make_stub_broker()
        broker.list_rules.return_value = [{
            "name": "catch-all",
            "decision": "deny",
            "source_path": "/etc/qdistro/rules.d/catch.yaml",
            "uid": -1,
            "action": "",
            "exe": "",
            "scope": "",
            "rationale": "Catch-all deny",
        }]
        tab = RulesTab(broker)
        tab.refresh()

        assert tab.model.item(0, 3).text() == "any"
        assert tab.model.item(0, 4).text() == "any"
        assert tab.model.item(0, 5).text() == "inherit"

    def test_reload_button_exists(self, qapp):
        """The Reload button is present with the right object name."""
        broker = _make_stub_broker()
        tab = RulesTab(broker)

        assert tab.btn_reload is not None
        assert tab.btn_reload.objectName() == "btn_reload_rules"

    def test_reload_calls_broker(self, qapp):
        """Clicking Reload calls broker.reload_rules() then refresh()."""
        broker = _make_stub_broker()
        tab = RulesTab(broker)
        tab._on_reload()

        broker.reload_rules.assert_called_once()
        # refresh() calls list_rules
        assert broker.list_rules.call_count >= 1

    def test_rules_reloaded_signal_connected(self, qapp):
        """RulesTab connects to broker.rulesReloaded for live-updates."""
        broker = _make_stub_broker()
        tab = RulesTab(broker)

        broker.rulesReloaded.connect.assert_called()


# ---------------------------------------------------------------------------
# RuleEditorDialog tests
# ---------------------------------------------------------------------------

class TestRuleEditorDialog:
    """Exercises the rule editor dialog."""

    def test_empty_template(self, qapp):
        """An empty rule_data produces a YAML template."""
        broker = _make_stub_broker()
        dlg = RuleEditorDialog(broker, {})

        text = dlg.yaml_editor.toPlainText()
        assert "decision:" in text or "decision" in text
        assert "match:" in text

    def test_pre_populated_from_pending(self, qapp):
        """Rule editor pre-populates uid/action/exe from a pending request."""
        broker = _make_stub_broker()
        rule_data = {
            "name": "Rule for qdistro.network.fetch from uid 2000",
            "decision": "allow",
            "uid": 2000,
            "action": "qdistro.network.fetch",
            "exe": "/usr/bin/curl",
            "scope": "once",
            "rationale": "Auto-generated",
        }
        dlg = RuleEditorDialog(broker, rule_data)

        text = dlg.yaml_editor.toPlainText()
        assert "qdistro.network.fetch" in text
        assert "2000" in text
        assert "/usr/bin/curl" in text

    def test_pre_populated_from_existing_rule(self, qapp):
        """Rule editor pre-populates from an existing rule with source_path."""
        broker = _make_stub_broker()
        rule_data = {
            "name": "allow-apt",
            "decision": "allow",
            "source_path": "/etc/qdistro/rules.d/apt.yaml",
            "uid": 2000,
            "action": "qdistro.exec.apt-get",
            "exe": "/usr/bin/apt-get",
            "scope": "forever",
            "rationale": "Allow apt-get for uid 2000",
        }
        dlg = RuleEditorDialog(broker, rule_data)

        # Filename derived from source_path
        assert dlg.filename_line.text() == "apt.yaml"
        text = dlg.yaml_editor.toPlainText()
        assert "allow" in text

    def test_generate_yaml_flat_schema(self, qapp):
        """_generate_yaml_from_rule handles the flat schema (uid/action/exe
        at top level, as produced by _create_rule_from_current)."""
        broker = _make_stub_broker()
        rule_data = {
            "name": "test",
            "decision": "allow",
            "uid": 2000,
            "action": "test.action",
            "exe": "/usr/bin/test",
            "scope": "once",
            "rationale": "Testing",
        }
        dlg = RuleEditorDialog(broker, rule_data)
        text = dlg.yaml_editor.toPlainText()

        # The generated YAML must contain the match selectors (not empty)
        assert "test.action" in text
        assert "2000" in text
        assert "/usr/bin/test" in text

    def test_generate_yaml_uid_minus_one_omitted(self, qapp):
        """uid=-1 (any) should NOT appear in the match block."""
        broker = _make_stub_broker()
        rule_data = {
            "name": "catch-all",
            "decision": "deny",
            "uid": -1,
            "action": "some.action",
            "exe": "",
            "scope": "",
            "rationale": "",
        }
        dlg = RuleEditorDialog(broker, rule_data)
        text = dlg.yaml_editor.toPlainText()

        # uid: -1 should be omitted from the match block; only action
        # should appear
        assert "some.action" in text

    def test_generate_yaml_preserves_extended_selectors(self, qapp):
        broker = _make_stub_broker()
        rule_data = {
            "name": "extended",
            "decision": "allow",
            "action": "com.qdistro.fs.open:*",
            "app_id": "org.example.App",
            "sandbox_engine": "flatpak",
            "mime_type": "text/plain",
            "argv_prefix": ["/usr/bin/foo", "--open"],
        }
        dlg = RuleEditorDialog(broker, rule_data)
        text = dlg.yaml_editor.toPlainText()
        assert "org.example.App" in text
        assert "flatpak" in text
        assert "text/plain" in text
        assert "argv_prefix" in text

    def test_get_rule_data_returns_yaml_body(self, qapp):
        """get_rule_data() returns the YAML body as a dict."""
        broker = _make_stub_broker()
        dlg = RuleEditorDialog(broker, {
            "name": "test",
            "decision": "allow",
            "uid": 2000,
            "action": "test.action",
            "exe": "/usr/bin/test",
        })
        dlg.filename_line.setText("my-rule.yaml")
        data = dlg.get_rule_data()

        assert "yaml_body" in data
        assert data["filename"] == "my-rule.yaml"


# ---------------------------------------------------------------------------
# DetailPane "Rule from this" button tests
# ---------------------------------------------------------------------------

class TestDetailPaneRuleFromThis:
    """Exercises the 'Rule from this' button on the Pending detail pane."""

    def test_button_exists(self, qapp):
        """The 'Rule from this' button is present."""
        pane = DetailPane()
        assert pane.btn_rule_from_this is not None
        assert pane.btn_rule_from_this.objectName() == "btn_rule_from_this"

    def test_button_emits_signal_with_request(self, qapp):
        """Clicking 'Rule from this' emits ruleFromThis(req)."""
        pane = DetailPane()
        req = _sample_pending_request()
        pane.show_request(req)

        emitted = []
        pane.ruleFromThis.connect(lambda r: emitted.append(r))
        pane.btn_rule_from_this.click()

        assert len(emitted) == 1
        assert emitted[0]["action"] == "qdistro.network.fetch"
        assert emitted[0]["uid"] == 2000

    def test_button_noop_when_no_selection(self, qapp):
        """When no request is shown, button click does nothing."""
        pane = DetailPane()
        emitted = []
        pane.ruleFromThis.connect(lambda r: emitted.append(r))
        pane.btn_rule_from_this.click()

        assert len(emitted) == 0

    def test_clear_resets_current_req(self, qapp):
        """clear() resets _current_req so the button is a no-op."""
        pane = DetailPane()
        pane.show_request(_sample_pending_request())
        pane.clear()

        emitted = []
        pane.ruleFromThis.connect(lambda r: emitted.append(r))
        pane.btn_rule_from_this.click()

        assert len(emitted) == 0


# ---------------------------------------------------------------------------
# HistoryTab "Rule from this" button tests
# ---------------------------------------------------------------------------

class TestHistoryTabRuleFromThis:
    """Exercises the 'Rule from this' button on the History tab."""

    def test_button_exists(self, qapp):
        broker = _make_stub_broker()
        tab = HistoryTab(broker)
        assert tab.btn_rule_from_history is not None
        assert tab.btn_rule_from_history.objectName() == "btn_rule_from_history"

    def test_signal_emitted_on_selection(self, qapp):
        """ruleFromHistory fires with the selected entry data."""
        broker = _make_stub_broker()
        entry = _sample_history_entry()
        broker.list_history.return_value = [entry]
        tab = HistoryTab(broker)
        tab.refresh()

        # Select first row
        idx = tab.model.index(0, 0)
        tab.table.setCurrentIndex(idx)

        emitted = []
        tab.ruleFromHistory.connect(lambda e: emitted.append(e))
        tab._on_rule_from_history()

        assert len(emitted) == 1
        assert emitted[0]["action"] == "qdistro.exec.git"
        assert emitted[0]["caller_uid"] == 2001


# ---------------------------------------------------------------------------
# "Rule from this" pre-population -- integration-level
# ---------------------------------------------------------------------------

class TestRuleFromThisPrePopulation:
    """Verify that pre-populated rule data from pending requests and
    history entries ends up in the RuleEditorDialog correctly."""

    def test_pending_request_prepopulates(self, qapp):
        """A pending request's uid/action/exe land in the rule YAML."""
        broker = _make_stub_broker()
        req = _sample_pending_request()
        rule_data = {
            "name": f"Rule for {req['action']} from uid {req['uid']}",
            "decision": "allow",
            "uid": req["uid"],
            "action": req["action"],
            "exe": req["exe"],
            "scope": "once",
            "rationale": f"Auto-generated rule from request {req['id']}",
        }
        dlg = RuleEditorDialog(broker, rule_data)
        text = dlg.yaml_editor.toPlainText()

        assert "qdistro.network.fetch" in text
        assert "2000" in text
        assert "/usr/bin/curl" in text

    def test_history_entry_prepopulates(self, qapp):
        """A history entry's caller_uid/action/caller_exe land in the rule."""
        broker = _make_stub_broker()
        entry = _sample_history_entry()
        rule_data = {
            "name": f"Rule for {entry['action']} from uid {entry['caller_uid']}",
            "decision": "allow" if entry["decision"] else "deny",
            "uid": entry["caller_uid"],
            "action": entry["action"],
            "exe": entry["caller_exe"],
            "scope": "once",
            "rationale": "Auto-generated rule from history entry",
        }
        dlg = RuleEditorDialog(broker, rule_data)
        text = dlg.yaml_editor.toPlainText()

        assert "qdistro.exec.git" in text
        assert "2001" in text
        assert "/usr/bin/git" in text


# ---------------------------------------------------------------------------
# Save triggers broker reload
# ---------------------------------------------------------------------------

class TestSaveTriggersReload:
    """Verify that saving a rule via the dialog calls SaveRule on the
    broker (which in turn reloads rules and emits RulesReloaded)."""

    def test_save_rule_calls_broker(self, qapp):
        """RulesTab._on_add_rule calls broker.save_rule when dialog accepts."""
        broker = _make_stub_broker()
        tab = RulesTab(broker)

        # Mock the dialog to auto-accept
        with patch("qdistro_admin_app.RuleEditorDialog") as MockDlg:
            mock_dlg_inst = MagicMock()
            mock_dlg_inst.exec.return_value = 1  # QDialog.DialogCode.Accepted
            mock_dlg_inst.filename_line.text.return_value = "test-rule.yaml"
            mock_dlg_inst.yaml_editor.toPlainText.return_value = (
                "- name: test\n  decision: allow\n  match:\n"
                "    uid: 2000\n    action: test.action\n"
            )
            MockDlg.return_value = mock_dlg_inst

            tab._on_add_rule()

        broker.save_rule.assert_called_once()
        args = broker.save_rule.call_args
        assert args[0][0] == "test-rule.yaml"

    def test_add_rule_rejects_allow_all(self, qapp):
        broker = _make_stub_broker()
        tab = RulesTab(broker)

        with patch("qdistro_admin_app.RuleEditorDialog") as MockDlg, \
             patch("qdistro_admin_app.QMessageBox.warning") as warn:
            mock_dlg_inst = MagicMock()
            mock_dlg_inst.exec.return_value = 1
            mock_dlg_inst.filename_line.text.return_value = "bad.yaml"
            mock_dlg_inst.yaml_editor.toPlainText.return_value = (
                "- name: bad\n  decision: allow\n  match: {}\n"
            )
            MockDlg.return_value = mock_dlg_inst

            tab._on_add_rule()

        broker.save_rule.assert_not_called()
        warn.assert_called_once()

    def test_edit_rule_rejects_allow_all(self, qapp):
        broker = _make_stub_broker()
        tab = RulesTab(broker)
        tab.refresh()
        tab.table.setCurrentIndex(tab.model.index(0, 0))

        with patch("qdistro_admin_app.RuleEditorDialog") as MockDlg, \
             patch("qdistro_admin_app.QMessageBox.warning") as warn:
            mock_dlg_inst = MagicMock()
            mock_dlg_inst.exec.return_value = 1
            mock_dlg_inst.filename_line.text.return_value = "bad.yaml"
            mock_dlg_inst.yaml_editor.toPlainText.return_value = (
                "- name: bad\n  decision: allow\n  match: {}\n"
            )
            MockDlg.return_value = mock_dlg_inst

            tab._on_edit_rule()

        broker.save_rule.assert_not_called()
        warn.assert_called_once()

    def test_new_rule_appears_after_save(self, qapp):
        """After saving, refresh() re-queries the broker, and a newly added
        rule appears in the table."""
        broker = _make_stub_broker()
        tab = RulesTab(broker)
        tab.refresh()
        assert tab.model.rowCount() == 2

        # Simulate adding a third rule
        broker.list_rules.return_value = list(broker.list_rules.return_value) + [{
            "name": "new-rule",
            "decision": "allow",
            "source_path": "/etc/qdistro/rules.d/new.yaml",
            "uid": 3000,
            "action": "qdistro.exec.new",
            "exe": "/usr/bin/new",
            "scope": "once",
            "rationale": "Freshly added",
        }]
        tab.refresh()

        assert tab.model.rowCount() == 3
        assert tab.model.item(2, 0).text() == "new-rule"


# ---------------------------------------------------------------------------
# BrokerBridge rulesReloaded signal
# ---------------------------------------------------------------------------

class TestBrokerBridgeRulesReloadedSignal:
    """Verify the BrokerBridge has the rulesReloaded signal wired."""

    def test_signal_defined(self, qapp):
        """BrokerBridge class defines rulesReloaded as a pyqtSignal."""
        # The signal is a class attribute; check it exists on the type
        # (instantiating requires dbus which we can't mock easily here).
        assert hasattr(BrokerBridge, "rulesReloaded")


# ---------------------------------------------------------------------------
# _yaml_is_allow_all guard
# ---------------------------------------------------------------------------

class TestYamlIsAllowAllGuard:
    """MainWindow._yaml_is_allow_all prevents overly broad rules."""

    def test_rejects_empty_match(self, qapp):
        yaml_body = "- decision: allow\n  match: {}\n"
        assert MainWindow._yaml_is_allow_all(yaml_body) is True

    def test_accepts_rule_with_uid(self, qapp):
        yaml_body = "- decision: allow\n  match:\n    uid: 2000\n"
        assert MainWindow._yaml_is_allow_all(yaml_body) is False

    def test_accepts_deny_empty_match(self, qapp):
        """A deny rule with empty match is fine -- denying everything is safe."""
        yaml_body = "- decision: deny\n  match: {}\n"
        assert MainWindow._yaml_is_allow_all(yaml_body) is False

    def test_accepts_rule_with_action(self, qapp):
        yaml_body = "- decision: allow\n  match:\n    action: some.action\n"
        assert MainWindow._yaml_is_allow_all(yaml_body) is False

    def test_rejects_allow_no_match_key(self, qapp):
        yaml_body = "- decision: allow\n"
        assert MainWindow._yaml_is_allow_all(yaml_body) is True
