"""Phase 3: broker workflow integration surface.

Exercises the read-only ListWorkflows / ListWorkflowRuns D-Bus methods
(admin-gated) and the registry's GLib-loop-sharing flag, without bringing
up a real system bus. The Broker instance is built via __new__ so we set
only the attributes these methods touch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import dbus
import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "workflow"))

import qdistro_admin_broker as b  # noqa: E402
from workflow_engine import WorkflowEngine  # noqa: E402
from workflow_schema import (  # noqa: E402
    StepDef, StepType, TriggerDef, TriggerType, WorkflowDef,
)
from trigger_registry import TriggerRegistry, DBusSignalTrigger  # noqa: E402
from audit_logger import WorkflowAuditLogger  # noqa: E402

ADMIN = b.ADMIN_UID


def _broker(engine, uid=0):
    br = b.Broker.__new__(b.Broker)
    br.workflow_engine = engine
    br._peer_info = lambda sender, conn: (uid, 1, "x", 0)  # type: ignore
    return br


def _engine_with_wf(name="git-sign"):
    engine = WorkflowEngine()
    engine._workflows[name] = WorkflowDef(
        name=name,
        trigger=TriggerDef(type=TriggerType.PROCESS_SPAWN),
        steps=[StepDef(type=StepType.DELIVER_SECRET, config={"item": "v/d/k"})],
        needs=["vault/dev/key"],
        description="grant signing key for one commit",
        source_path="/etc/qdistro/workflows/git-sign.yaml",
    )
    return engine


class TestListWorkflows:
    def test_empty_when_no_engine(self):
        br = _broker(None)
        assert list(br.ListWorkflows(sender=":1", conn=None)) == []

    def test_returns_defs(self):
        br = _broker(_engine_with_wf())
        rows = br.ListWorkflows(sender=":1", conn=None)
        assert len(rows) == 1
        assert rows[0]["name"] == "git-sign"
        assert rows[0]["trigger_type"] == "process_spawn"
        assert rows[0]["step_count"] == 1
        assert "vault/dev/key" in list(rows[0]["needs"])

    def test_denied_for_non_admin(self):
        br = _broker(_engine_with_wf(), uid=1500)
        with pytest.raises(dbus.DBusException):
            br.ListWorkflows(sender=":1", conn=None)


class TestListWorkflowRuns:
    def test_empty_when_no_engine(self):
        br = _broker(None)
        assert list(br.ListWorkflowRuns(10, sender=":1", conn=None)) == []

    def test_returns_runs(self, tmp_path):
        audit = WorkflowAuditLogger(db_path=str(tmp_path / "wf.sqlite"))
        engine = WorkflowEngine(audit_logger=audit)
        engine._workflows["wf"] = WorkflowDef(
            name="wf", trigger=TriggerDef(type=TriggerType.CRON),
            needs=["v/d/k"],
            steps=[StepDef(type=StepType.DELIVER_SECRET,
                           config={"item": "v/d/k"})])
        run = engine.start_run("wf")
        br = _broker(engine)
        rows = br.ListWorkflowRuns(10, sender=":1", conn=None)
        assert any(r["run_id"] == run.run_id for r in rows)
        assert rows[0]["state"] in ("completed", "running")

    def test_denied_for_non_admin(self):
        br = _broker(None, uid=1500)
        with pytest.raises(dbus.DBusException):
            br.ListWorkflowRuns(10, sender=":1", conn=None)


class TestRegistryLoopSharing:
    def test_dbus_trigger_shares_host_loop(self):
        # With dbus_run_own_loop=False the D-Bus trigger must not spin its
        # own GLib loop (it relies on the broker's).
        reg = TriggerRegistry(dbus_run_own_loop=False)
        wf = WorkflowDef(
            name="sig", trigger=TriggerDef(
                type=TriggerType.DBUS_SIGNAL,
                config={"interface": "org.example.Iface"}),
            steps=[StepDef(type=StepType.RUN_HOOK)])
        trig = reg.register(wf, lambda n, c: None)
        try:
            assert isinstance(trig, DBusSignalTrigger)
            assert trig._run_own_loop is False
            assert trig._loop is None  # no private loop started
        finally:
            reg.unregister_all()
