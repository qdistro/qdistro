"""Approval identity across reload and queued-worker dispatch (no backends)."""
import json
import os
import threading

import dbus
import pytest

import qdistro_admin_broker as broker_module
from audit_logger import WorkflowAuditLogger
from workflow_engine import WorkflowEngine
from workflow_schema import RunState, StepDef, StepType, TriggerDef, TriggerType, WorkflowDef, WorkflowPlan


def definition():
    return WorkflowDef("wf", TriggerDef(TriggerType.CRON, {"schedule": "* * * * *"}),
                       [StepDef(StepType.RUN_HOOK, config={"hook": "reviewed", "event": {"to": "dev"}})],
                       needs=["vault/dev/key"])


class RecordingEngine(WorkflowEngine):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.executed = []

    def _execute_steps(self, wf, run):
        self.executed.append(WorkflowPlan.capture(wf).digest)


@pytest.fixture
def engine():
    eng = RecordingEngine(max_concurrent_runs=1)
    eng._workflows = {"wf": definition()}
    try:
        yield eng
    finally:
        eng.shutdown()


def reload(engine, monkeypatch, definitions):
    monkeypatch.setattr(engine._loader, "load", lambda: definitions)
    monkeypatch.setattr(engine._loader, "load_errors", lambda: [])
    assert engine.load_workflows() == []


def test_snapshot_is_detached_and_canonical():
    wf = definition()
    plan = WorkflowPlan.capture(wf)
    wf.steps[0].config["event"]["to"] = "changed"
    returned = plan.definition()
    returned.steps[0].config["event"]["to"] = "also changed"
    assert plan.definition().steps[0].config["event"]["to"] == "dev"
    original = definition()
    original.description = "new label"
    original.source_path = "/moved/file.yaml"
    original.steps[0].config = dict(reversed(list(original.steps[0].config.items())))
    assert WorkflowPlan.capture(original).digest == plan.digest


@pytest.mark.parametrize("field", ["needs", "steps", "conditions", "roles", "trigger", "auto_run", "removed"])
def test_reload_invalidates_material_changes(engine, monkeypatch, field):
    run = engine._enqueue_pending("wf", {})
    digest = engine.preview_run(run.run_id)["definition_digest"]
    changed = definition()
    if field == "needs":
        changed.needs.append("vault/other/key")
    elif field == "steps":
        changed.steps[0].config["hook"] = "unreviewed"
    elif field == "conditions":
        changed.conditions = [{"uid": 1234}]
    elif field == "roles":
        changed.roles = {"dev": "invoker"}
    elif field == "trigger":
        changed.trigger.config["schedule"] = "0 * * * *"
    elif field == "auto_run":
        changed.auto_run = True
    reload(engine, monkeypatch, [] if field == "removed" else [changed])
    assert run.state == RunState.FAILED, run
    assert engine.preview_run(run.run_id) == {}
    assert not engine.approve_run(run.run_id, digest)
    assert not engine.approve_run(run.run_id), "legacy approval released invalidated run"
    assert engine.executed == []


def test_unchanged_reload_preserves_identity_and_dispatches_once(engine, monkeypatch):
    run = engine._enqueue_pending("wf", {})
    preview = engine.preview_run(run.run_id)
    changed = definition()
    changed.description = "display only"
    changed.source_path = "/new/location"
    reload(engine, monkeypatch, [changed])
    assert engine.preview_run(run.run_id) == preview
    assert not engine.approve_run(run.run_id, "wrong digest")
    assert run.state == RunState.PENDING
    assert engine.approve_run(run.run_id, preview["definition_digest"])
    assert not engine.approve_run(run.run_id, preview["definition_digest"])
    engine._run_pool.shutdown(wait=True)
    assert engine.executed == [preview["definition_digest"]]
    assert run.state == RunState.COMPLETED


def test_reload_after_queue_submission_cannot_substitute(engine, monkeypatch):
    occupied = threading.Event()
    release = threading.Event()

    def block_worker():
        occupied.set()
        assert release.wait(5), "test did not release worker"

    blocker = engine._run_pool.submit(block_worker)
    assert occupied.wait(5), "worker never occupied"
    try:
        run = engine._enqueue_pending("wf", {})
        digest = engine.preview_run(run.run_id)["definition_digest"]
        assert engine.approve_run(run.run_id, digest)
        assert not run.execution_started, "approval unexpectedly bypassed queued worker"
        changed = definition()
        changed.steps[0].config["hook"] = "unreviewed"
        reload(engine, monkeypatch, [changed])
        assert run.state == RunState.FAILED
        # Returning to A cannot resurrect an approval revoked during B.
        reload(engine, monkeypatch, [definition()])
    finally:
        release.set()
        blocker.result(timeout=5)
        engine._run_pool.shutdown(wait=True)
    assert engine.executed == [], "queued approval executed after revocation"


def test_execution_keeps_live_identity_check(engine):
    engine._workflows["wf"].conditions = [{"uid": os.getuid()}]
    run = engine._enqueue_pending("wf", {"pid": os.getpid(), "pid_starttime": 1})
    assert engine.approve_run(run.run_id, engine.preview_run(run.run_id)["definition_digest"])
    engine._run_pool.shutdown(wait=True)
    assert run.state == RunState.FAILED and "recycled" in run.error, run
    assert engine.executed == []


def make_broker(engine, audit, uid=broker_module.ADMIN_UID):
    br = broker_module.Broker.__new__(broker_module.Broker)
    br.workflow_engine = engine
    br.audit = audit
    br._require_admin_control_peer = lambda sender, conn, method: (uid, 42, "/admin", 123)
    return br


class RecordingAudit:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def log(self, **row):
        if self.fail:
            raise OSError("audit unavailable")
        self.rows.append(row)


def test_preview_approval_and_execution_share_audited_digest(tmp_path):
    durable = WorkflowAuditLogger(str(tmp_path / "audit.sqlite"))
    engine = RecordingEngine(audit_logger=durable)
    engine._workflows = {"wf": definition()}
    audit = RecordingAudit()
    br = make_broker(engine, audit)
    try:
        run = engine._enqueue_pending("wf", {})
        preview = json.loads(br.PreviewWorkflowRun(run.run_id))
        assert preview["definition"]["steps"][0]["config"]["hook"] == "reviewed"
        assert preview["definition"]["needs"] == ["vault/dev/key"]
        digest = preview["definition_digest"]
        assert not br.ApproveWorkflowPlan(run.run_id, "stale")
        assert audit.rows == []
        assert br.ApproveWorkflowPlan(run.run_id, digest)
        engine._run_pool.shutdown(wait=True)
        assert engine.executed == [digest]
        assert durable.recent_runs()[0]["definition_digest"] == digest
        assert f"definition_digest={digest}" in audit.rows[0]["source"]
        row = durable._conn.execute("SELECT details FROM workflow_audit WHERE event_type='run_approval'").fetchone()
        assert json.loads(row[0]) == {"definition_digest": digest,
                                     "approver": {"uid": broker_module.ADMIN_UID, "pid": 42,
                                                  "exe": "/admin", "start_time": 123}}
    finally:
        engine.shutdown()
        durable.close()


def test_strict_approval_fails_closed_on_audit_error(engine):
    br = make_broker(engine, RecordingAudit(fail=True))
    run = engine._enqueue_pending("wf", {})
    digest = engine.preview_run(run.run_id)["definition_digest"]
    assert not br.ApproveWorkflowPlan(run.run_id, digest)
    assert run.state == RunState.PENDING and engine.executed == []


def test_strict_surfaces_require_control_peer(engine):
    br = make_broker(engine, RecordingAudit())
    def deny(*args):
        raise dbus.DBusException("control peer denied")
    br._require_admin_control_peer = deny
    with pytest.raises(dbus.DBusException):
        br.PreviewWorkflowRun("any")
    with pytest.raises(dbus.DBusException):
        br.ApproveWorkflowPlan("any", "digest")


def test_legacy_approval_binds_audited_digest_and_rejects_drift(engine):
    audit = RecordingAudit()
    br = make_broker(engine, audit)
    run = engine._enqueue_pending("wf", {})
    digest = run.plan.digest
    assert br.ApproveWorkflowRun(run.run_id)
    engine._run_pool.shutdown(wait=True)
    assert engine.executed == [digest]
    assert f"definition_digest={digest}" in audit.rows[0]["source"]
    assert audit.rows[0]["approver_uid"] == broker_module.ADMIN_UID


def test_audit_migrates_existing_database_without_losing_history(tmp_path):
    import sqlite3
    from audit_logger import SCHEMA
    path = str(tmp_path / "old.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO workflow_runs(run_id, workflow_name, state) VALUES ('old', 'wf', 'completed')")
    conn.commit()
    conn.close()
    audit = WorkflowAuditLogger(path)
    try:
        assert audit.recent_runs()[0]["run_id"] == "old"
        assert audit.recent_runs()[0]["definition_digest"] == ""
        audit.log_plan_binding("old", "digest")
        assert audit.recent_runs()[0]["definition_digest"] == "digest"
    finally:
        audit.close()


def test_workflow_loader_rejects_noncanonical_configuration(tmp_path):
    from workflow_loader import WorkflowLoader
    path = tmp_path / "bad.yaml"
    path.write_text("name: wf\ntrigger: {type: cron}\nsteps:\n  - type: run_hook\n    date: 2026-09-29\n")
    loader = WorkflowLoader(system_dir=str(tmp_path), user_dir="")
    assert loader.load() == []
    assert "unsupported workflow configuration type: date" in loader.load_errors()[0]


def test_auto_run_queue_cannot_execute_reloaded_approval_required_plan(engine, monkeypatch):
    occupied = threading.Event()
    release = threading.Event()
    def block_worker():
        occupied.set()
        assert release.wait(5)
    blocker = engine._run_pool.submit(block_worker)
    assert occupied.wait(5)
    try:
        engine._workflows["wf"].auto_run = True
        engine._on_trigger("wf", {})
        reload(engine, monkeypatch, [definition()])
    finally:
        release.set()
        blocker.result(timeout=5)
        engine._run_pool.shutdown(wait=True)
    assert engine.executed == [], "auto-run queue bypassed approval requirement after reload"
    assert len(engine.list_runs()) == 1
    assert engine.list_runs()[0].state == RunState.FAILED


def test_reload_does_not_splice_into_already_executing_run(engine, monkeypatch):
    executing = threading.Event()
    release = threading.Event()
    captured = []
    def execute(wf, run):
        executing.set()
        assert release.wait(5)
        captured.append(WorkflowPlan.capture(wf).digest)
    monkeypatch.setattr(engine, "_execute_steps", execute)
    run = engine._enqueue_pending("wf", {})
    digest = run.plan.digest
    assert engine.approve_run(run.run_id, digest)
    assert executing.wait(5), "approved worker never began execution"
    try:
        changed = definition()
        changed.steps[0].config["hook"] = "new"
        reload(engine, monkeypatch, [changed])
        assert run.execution_started and run.state == RunState.RUNNING
    finally:
        release.set()
        engine._run_pool.shutdown(wait=True)
    assert captured == [digest]
    assert run.state == RunState.COMPLETED
