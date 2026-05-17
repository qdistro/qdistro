"""Unit tests for admin app tabs (Rules, History) and enhanced functionality.

Tests the Rules tab, History tab, tray icon, keyboard shortcuts, and error
handling added in P07 admin app polish.

These tests never construct a real BrokerBridge (which would try to bind to
the system bus during __init__); they use a MagicMock spec to stand in for
the broker. A QApplication is bootstrapped under the offscreen platform so
no display is required.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("dbus")
pytest.importorskip("PyQt6")

# Make qdistro/admin_app/ importable as a top-level module so we don't have
# to package the admin app to test it. The conftest already hangs off
# qdistro/tests/unit/; walk up two levels to get the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "admin_app"))


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    """One QApplication for the whole module; widgets need an app."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def fake_broker():
    """A MagicMock standing in for BrokerBridge.

    Tabs only call into list_rules / save_rule / reload_rules / list_history
    / list_cache / get_pending / decide; everything is read off the mock so
    we don't need a real dbus connection.
    """
    from qdistro_admin_app import BrokerBridge
    b = MagicMock(spec=BrokerBridge)
    b.list_rules.return_value = []
    b.list_history.return_value = []
    b.list_cache.return_value = []
    b.get_pending.return_value = []
    return b


# -- BrokerBridge surface ----------------------------------------------------

class TestBrokerBridgeSurface:
    """Verify that BrokerBridge advertises the methods the tabs rely on.

    We're not exercising D-Bus here; we just confirm the class advertises
    the API the rest of the admin app code calls. A regression where
    someone deletes list_rules() would be caught at import.
    """

    def test_list_rules_method_exists(self):
        from qdistro_admin_app import BrokerBridge
        assert callable(getattr(BrokerBridge, "list_rules", None))

    def test_save_rule_method_exists(self):
        from qdistro_admin_app import BrokerBridge
        assert callable(getattr(BrokerBridge, "save_rule", None))

    def test_reload_rules_method_exists(self):
        from qdistro_admin_app import BrokerBridge
        assert callable(getattr(BrokerBridge, "reload_rules", None))

    def test_list_history_method_exists(self):
        from qdistro_admin_app import BrokerBridge
        assert callable(getattr(BrokerBridge, "list_history", None))


# -- RulesTab ----------------------------------------------------------------

class TestRulesTab:

    def test_rules_tab_initializes(self, fake_broker):
        from qdistro_admin_app import RulesTab
        tab = RulesTab(fake_broker)
        assert tab.broker is fake_broker
        assert tab.table is not None
        assert tab.model is not None
        # Columns header label sanity.
        assert tab.model.columnCount() == len(RulesTab.COLUMNS)

    def test_rules_tab_refresh_populates_model(self, fake_broker):
        from qdistro_admin_app import RulesTab
        fake_broker.list_rules.return_value = [
            {
                "name": "Test Rule",
                "decision": "allow",
                "source_path": "/etc/qdistro/rules.d/test.yaml",
                "uid": 2000,
                "action": "test.action",
                "exe": "/bin/test",
                "scope": "once",
                "rationale": "Test rule",
            },
        ]
        tab = RulesTab(fake_broker)
        tab.refresh()
        fake_broker.list_rules.assert_called_once()
        assert tab.model.rowCount() == 1
        # First column is name.
        assert tab.model.item(0, 0).text() == "Test Rule"

    def test_rules_tab_refresh_multiple_rules(self, fake_broker):
        from qdistro_admin_app import RulesTab
        fake_broker.list_rules.return_value = [
            {"name": f"R{i}", "decision": "allow", "source_path": "",
             "uid": -1, "action": "", "exe": "", "scope": "", "rationale": ""}
            for i in range(5)
        ]
        tab = RulesTab(fake_broker)
        tab.refresh()
        assert tab.model.rowCount() == 5

    def test_rules_tab_renders_dont_care_sentinels(self, fake_broker):
        """uid=-1, empty action/exe/scope render as human-readable defaults."""
        from qdistro_admin_app import RulesTab
        fake_broker.list_rules.return_value = [
            {"name": "wildcard", "decision": "deny", "source_path": "",
             "uid": -1, "action": "", "exe": "", "scope": "", "rationale": ""},
        ]
        tab = RulesTab(fake_broker)
        tab.refresh()
        # Find the uid column index; it's column 2 in COLUMNS.
        uid_text = tab.model.item(0, 2).text()
        assert uid_text == "any"

    def test_rules_tab_handles_dbus_error(self, fake_broker):
        from qdistro_admin_app import RulesTab
        import dbus
        fake_broker.list_rules.side_effect = dbus.DBusException("nope")
        tab = RulesTab(fake_broker)
        with patch("qdistro_admin_app.QMessageBox") as mock_msgbox:
            tab.refresh()
            mock_msgbox.warning.assert_called_once()


# -- HistoryTab --------------------------------------------------------------

class TestHistoryTab:

    def test_history_tab_initializes(self, fake_broker):
        from qdistro_admin_app import HistoryTab
        tab = HistoryTab(fake_broker)
        assert tab.broker is fake_broker
        assert tab.table is not None
        assert tab.model is not None

    def test_history_tab_requests_100_entries(self, fake_broker):
        """The History tab caps requests at MAX_HISTORY_ENTRIES (100)."""
        from qdistro_admin_app import HistoryTab, MAX_HISTORY_ENTRIES
        HistoryTab(fake_broker)  # refresh() runs in __init__
        # First call (auto-refresh on construction) goes with the cap.
        fake_broker.list_history.assert_called_with(MAX_HISTORY_ENTRIES)
        assert MAX_HISTORY_ENTRIES == 100

    def test_history_tab_renders_rows(self, fake_broker):
        from qdistro_admin_app import HistoryTab
        fake_broker.list_history.return_value = [
            {
                "ts": 1700000000,
                "caller_uid": 2000,
                "caller_pid": 1234,
                "caller_exe": "/bin/test",
                "action": "test.act",
                "decision": True,
                "scope": "once",
                "source": "prompt",
                "approver_uid": 1000,
                "rule_path": None,
                "request_id": 1,
                "selinux_subj_type": None,
                "argv": ["/bin/test", "--flag"],
            },
        ]
        tab = HistoryTab(fake_broker)  # auto-refresh
        assert tab.model.rowCount() == 1
        # decision rendered as "Allow"
        # decision is at index 4 in COLUMNS.
        assert tab.model.item(0, 4).text() == "Allow"

    def test_history_tab_renders_deny(self, fake_broker):
        from qdistro_admin_app import HistoryTab
        fake_broker.list_history.return_value = [
            {
                "ts": 1700000001, "caller_uid": 2001, "caller_pid": 0,
                "caller_exe": "", "action": "x", "decision": False,
                "scope": None, "source": "rule", "approver_uid": None,
                "rule_path": "/x", "request_id": None,
                "selinux_subj_type": None, "argv": [],
            },
        ]
        tab = HistoryTab(fake_broker)
        assert tab.model.item(0, 4).text() == "Deny"

    def test_history_tab_handles_dbus_error(self, fake_broker):
        from qdistro_admin_app import HistoryTab
        import dbus
        fake_broker.list_history.side_effect = dbus.DBusException("offline")
        with patch("qdistro_admin_app.QMessageBox") as mock_msgbox:
            HistoryTab(fake_broker)
            mock_msgbox.warning.assert_called_once()


# -- RuleEditorDialog --------------------------------------------------------

class TestRuleEditorDialog:

    def test_dialog_initializes_empty(self, fake_broker):
        from qdistro_admin_app import RuleEditorDialog
        dlg = RuleEditorDialog(fake_broker, {})
        assert dlg.broker is fake_broker
        # The empty case uses the default YAML template — verify it parses.
        text = dlg.yaml_editor.toPlainText()
        assert "decision" in text
        assert "match" in text

    def test_dialog_initializes_with_rule(self, fake_broker):
        from qdistro_admin_app import RuleEditorDialog
        rule = {
            "name": "Edit me", "decision": "allow",
            "source_path": "/etc/qdistro/rules.d/foo.yaml",
            "uid": 2000, "action": "x.y", "exe": "/bin/z",
            "scope": "1h", "rationale": "because",
        }
        dlg = RuleEditorDialog(fake_broker, rule)
        # filename pre-filled from basename of source_path.
        assert dlg.filename_line.text() == "foo.yaml"
        # YAML body contains the rule name.
        assert "Edit me" in dlg.yaml_editor.toPlainText()


# -- ErrorDetailsWidget ------------------------------------------------------

class TestErrorDetailsWidget:

    def test_initial_state(self):
        from qdistro_admin_app import ErrorDetailsWidget
        w = ErrorDetailsWidget("oops")
        assert w.error_message == "oops"
        assert w.expanded is False

    def test_toggle_expand_collapse(self):
        from qdistro_admin_app import ErrorDetailsWidget
        w = ErrorDetailsWidget("oops")
        w._toggle_visibility(True)
        assert w.expanded is True
        w._toggle_visibility(False)
        assert w.expanded is False


# -- DetailPane --------------------------------------------------------------

class TestDetailPane:

    def test_pane_has_error_widget(self):
        from qdistro_admin_app import DetailPane
        pane = DetailPane()
        assert hasattr(pane, "error_widget")

    def test_show_request_without_error_keeps_widget_hidden(self):
        from qdistro_admin_app import DetailPane
        pane = DetailPane()
        pane.show_request({
            "id": 1, "uid": 2000, "pid": 99, "action": "x", "exe": "/x",
        })
        assert pane.error_widget.isHidden()

    def test_show_request_with_error_reveals_widget(self):
        from qdistro_admin_app import DetailPane
        pane = DetailPane()
        pane.show_request({
            "id": 2, "uid": 2000, "pid": 99, "action": "x", "exe": "/x",
            "details": {"error": "ScopeNotPermitted: nope"},
        })
        assert pane.error_widget.isVisible() or not pane.error_widget.isHidden()
        # The widget's text should carry the error message.
        assert "ScopeNotPermitted" in pane.error_widget.error_text.toPlainText()


# -- Module constants --------------------------------------------------------

def test_module_constants():
    from qdistro_admin_app import HISTORY_FILE, MAX_HISTORY_ENTRIES
    assert MAX_HISTORY_ENTRIES == 100
    assert HISTORY_FILE == "/var/lib/qdistro/admin-history.jsonl"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
