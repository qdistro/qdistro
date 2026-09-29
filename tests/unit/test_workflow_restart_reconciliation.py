"""Real SQLite restart history cannot resurrect approval or replay work."""
from audit_logger import WorkflowAuditLogger
from workflow_engine import WorkflowEngine


def test_restart_expires_pending_and_marks_running_unknown_without_execution(tmp_path, monkeypatch):
    path = str(tmp_path / "audit.sqlite")
    old = WorkflowAuditLogger(path)
    old.log_run_pending("pending", "wf", {"pid":123})
    old.log_plan_binding("pending", "old-digest")
    old.log_run_start("running", "wf", {})
    old.log_cleanup_state("running", "pending", 1, "")
    old.log_run_queued("queued", "wf", {}, "digest")
    old.close()
    audit = WorkflowAuditLogger(path)
    executions = []
    monkeypatch.setattr(WorkflowEngine, "_execute_existing_run", lambda *a: executions.append(a))
    engine = WorkflowEngine(audit_logger=audit, own_dbus_loop=False)
    records = {r["run_id"]:r for r in engine.recent_runs()}
    assert records["pending"]["state"] == "expired"
    assert records["running"]["state"] == "interrupted"
    assert records["queued"]["state"] == "interrupted"
    assert records["running"]["cleanup_state"] == "unknown"
    assert records["running"]["cleanup_pending"] == 1
    assert "requires review" in records["running"]["cleanup_error"]
    assert not engine.approve_run("pending", "old-digest")
    assert not engine.list_runs() and executions == []
    assert audit._conn.execute("SELECT count(*) FROM workflow_audit WHERE event_type='run_reconciled'").fetchone()[0] == 3
    engine.shutdown(); audit.close()
    again = WorkflowAuditLogger(path)
    engine2 = WorkflowEngine(audit_logger=again, own_dbus_loop=False)
    assert again._conn.execute("SELECT count(*) FROM workflow_audit WHERE event_type='run_reconciled'").fetchone()[0] == 3
    assert executions == []
    engine2.shutdown(); again.close()


def test_partial_auto_admission_audit_does_not_leave_running_row(tmp_path, monkeypatch):
    from workflow_schema import WorkflowDef, TriggerDef, TriggerType
    audit = WorkflowAuditLogger(str(tmp_path / "audit.sqlite"))
    engine = WorkflowEngine(audit_logger=audit, own_dbus_loop=False)
    engine._workflows["wf"] = WorkflowDef(name="wf", auto_run=True, steps=[],
                                          trigger=TriggerDef(type=TriggerType.CRON))
    events = audit._log_event
    def fail_queue(*args, **kwargs):
        if args[2] == "run_queued":
            raise RuntimeError("queued event unavailable")
        return events(*args, **kwargs)
    monkeypatch.setattr(audit, "_log_event", fail_queue)
    submissions = []
    monkeypatch.setattr(engine._run_pool, "submit", lambda *args: submissions.append(args))
    engine._on_trigger("wf", {})
    assert submissions == [] and engine._inflight == 0
    records = audit.recent_runs()
    assert len(records) == 1 and records[0]["state"] == "failed"
    assert "admission audit failed" in records[0]["error"]
    engine.shutdown(); audit.close()


def test_history_gc_preserves_unresolved_cleanup(tmp_path):
    audit = WorkflowAuditLogger(str(tmp_path / "audit.sqlite"))
    audit.log_run_start("residue", "wf", {})
    audit.log_run_complete("residue", "wf")
    audit.log_cleanup_state("residue", "unresolved", 1, "requires review")
    audit.log_run_start("clean", "wf", {})
    audit.log_run_complete("clean", "wf")
    audit.log_cleanup_state("clean", "confirmed", 0, "")
    audit._conn.execute("UPDATE workflow_runs SET started_at=1")
    assert audit.gc(1) == 1
    assert [row["run_id"] for row in audit.recent_runs()] == ["residue"]
    audit.close()
