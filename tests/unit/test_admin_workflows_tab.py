"""Tests for the admin app Workflows tab (Phase 4).

Pure PyQt6 widget-logic tests with a stubbed BrokerBridge — no D-Bus
daemon. Skipped when PyQt6 isn't importable (same pattern as the other
admin-app tests). NOTE: this exercises the model/refresh logic only;
the actual rendered GUI must be validated in a running admin app.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "admin_app"))
sys.path.insert(0, str(_ROOT / "broker"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qdistro_admin_app import WorkflowsTab  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Stub QMessageBox so info/warning popups don't block the offscreen
    event loop during widget tests."""
    import qdistro_admin_app as app_mod
    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(app_mod.QMessageBox, name,
                            staticmethod(lambda *a, **k: None))


def _broker(workflows=None, runs=None):
    br = MagicMock()
    br.bus = MagicMock()
    br.list_workflows.return_value = workflows or []
    br.list_workflow_runs.return_value = runs or []
    return br


def test_populates_workflows_and_runs(qapp):
    wf = {"name": "git-sign", "trigger_type": "process_spawn",
          "description": "sign one commit", "needs": ["vault/dev/key"],
          "step_count": 2, "source_path": "/etc/qdistro/workflows/g.yaml"}
    run = {"run_id": "abc123", "workflow_name": "git-sign",
           "state": "completed", "started_at": 1716800000.0,
           "completed_at": 1716800005.0, "error": ""}
    tab = WorkflowsTab(_broker([wf], [run]))
    assert tab._wf_model.rowCount() == 1
    assert tab._wf_model.item(0, 0).text() == "git-sign"
    assert tab._wf_model.item(0, 1).text() == "process_spawn"
    assert tab._wf_model.item(0, 3).text() == "vault/dev/key"
    assert tab._runs_model.rowCount() == 1
    assert tab._runs_model.item(0, 0).text() == "abc123"
    assert tab._runs_model.item(0, 2).text() == "completed"
    # A completed run shows a formatted finish timestamp, not "-".
    assert tab._runs_model.item(0, 4).text() != "-"


def test_empty_lists_render(qapp):
    tab = WorkflowsTab(_broker([], []))
    assert tab._wf_model.rowCount() == 0
    assert tab._runs_model.rowCount() == 0


def test_running_run_has_no_finish_ts(qapp):
    run = {"run_id": "r1", "workflow_name": "wf", "state": "running",
           "started_at": 1716800000.0, "completed_at": 0.0, "error": ""}
    tab = WorkflowsTab(_broker([], [run]))
    assert tab._runs_model.item(0, 4).text() == "-"


def test_refresh_repopulates(qapp):
    br = _broker([], [])
    tab = WorkflowsTab(br)
    assert tab._wf_model.rowCount() == 0
    br.list_workflows.return_value = [
        {"name": "x", "trigger_type": "cron", "description": "",
         "needs": [], "step_count": 1, "source_path": ""}]
    tab.refresh()
    assert tab._wf_model.rowCount() == 1


def test_reload_signal_triggers_refresh(qapp):
    br = _broker([], [])
    tab = WorkflowsTab(br)
    br.list_workflows.return_value = [
        {"name": "y", "trigger_type": "cron", "description": "",
         "needs": [], "step_count": 1, "source_path": ""}]
    tab._on_reloaded(3)
    assert tab._wf_model.rowCount() == 1


def _pending_run(run_id="p1"):
    return {"run_id": run_id, "workflow_name": "wf", "state": "pending",
            "started_at": 1716800000.0, "completed_at": 0.0, "error": ""}


def test_approve_selected_calls_broker(qapp):
    br = _broker([], [_pending_run("p1")])
    br.approve_workflow_run.return_value = True
    tab = WorkflowsTab(br)
    # Select the pending run row, then approve.
    tab.runs_table.selectRow(0)
    tab.approve_selected()
    br.approve_workflow_run.assert_called_once_with("p1")


def test_approve_skips_non_pending(qapp):
    run = {"run_id": "done", "workflow_name": "wf", "state": "completed",
           "started_at": 1716800000.0, "completed_at": 1716800005.0,
           "error": ""}
    br = _broker([], [run])
    tab = WorkflowsTab(br)
    tab.runs_table.selectRow(0)
    tab.approve_selected()
    br.approve_workflow_run.assert_not_called()


def test_approve_with_no_selection_is_noop(qapp):
    br = _broker([], [_pending_run("p1")])
    tab = WorkflowsTab(br)
    # No row selected.
    tab.approve_selected()
    br.approve_workflow_run.assert_not_called()


def test_pending_signal_triggers_refresh(qapp):
    br = _broker([], [])
    tab = WorkflowsTab(br)
    br.list_workflow_runs.return_value = [_pending_run("p9")]
    # WorkflowRunPending is wired to the same handler as RulesReloaded.
    tab._on_reloaded("p9", "wf")
    assert tab._runs_model.rowCount() == 1
    assert tab._runs_model.item(0, 2).text() == "pending"
