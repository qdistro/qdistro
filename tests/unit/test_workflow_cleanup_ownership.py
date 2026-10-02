"""Cleanup owns failures independently of task state and survives audit retry."""
import pytest

import workflow_engine as we
from audit_logger import WorkflowAuditLogger
from secret_delivery import DeliveryError, DeliveryHandle, TmpfsMountDelivery
from workflow_schema import StepDef, StepType, TriggerDef, TriggerType, WorkflowDef, RunState


def setup_engine(tmp_path, monkeypatch, *, fail=1, partial=False, lifetime="workflow_exit"):
    audit = WorkflowAuditLogger(str(tmp_path / "audit.sqlite"))
    engine = we.WorkflowEngine(audit_logger=audit, secret_source=lambda item, **kw: b"dummy",
                               own_dbus_loop=False)
    class Handle(DeliveryHandle):
        method = "test"
        attempts = 0
        def deliver(self):
            if partial:
                raise DeliveryError("partial delivery")
        def _revoke(self):
            self.attempts += 1
            if self.attempts <= fail:
                raise DeliveryError("revocation denied")
    handles = []
    def factory(method, secret, config):
        handle = Handle(secret); handles.append(handle); return handle
    monkeypatch.setattr(we, "make_delivery", factory)
    engine._workflows["wf"] = WorkflowDef(
        name="wf", trigger=TriggerDef(type=TriggerType.CRON), needs=["vault/dev/key"],
        steps=[StepDef(type=StepType.DELIVER_SECRET, config={"item": "vault/dev/key",
                        "delivery_method": "env", "var": "SECRET", "scrub_on": lifetime})])
    return engine, audit, handles


def test_task_success_keeps_failed_cleanup_owned_then_retry_audits_once(tmp_path, monkeypatch):
    engine, audit, handles = setup_engine(tmp_path, monkeypatch)
    run = engine.start_run("wf")
    assert run.state == RunState.COMPLETED and run.cleanup_state == "unresolved"
    assert run.cleanup_pending == 1 and handles[0]._secret.wiped
    assert engine._delivery_handles[run.run_id] == handles
    assert audit.recent_runs()[0]["cleanup_state"] == "unresolved"
    assert audit._conn.execute("SELECT count(*) FROM workflow_audit WHERE event_type='secret_scrub'").fetchone()[0] == 0
    engine.scrub_all_runs(); engine.scrub_all_runs()
    assert handles[0].attempts == 2 and run.cleanup_state == "confirmed"
    assert run.run_id not in engine._delivery_handles
    assert audit._conn.execute("SELECT count(*) FROM workflow_audit WHERE event_type='secret_scrub'").fetchone()[0] == 1
    engine.shutdown(); audit.close()


@pytest.mark.parametrize("partial,lifetime,stopping", [(True,"workflow_exit",False),
                         (False,"step_exit",False), (False,"workflow_exit",True)])
def test_partial_immediate_and_late_cleanup_failures_remain_owned(tmp_path, monkeypatch,
                                                                 partial, lifetime, stopping):
    engine, audit, handles = setup_engine(tmp_path, monkeypatch, fail=99,
                                          partial=partial, lifetime=lifetime)
    engine._stopping = stopping
    run = engine.start_run("wf")
    for _ in range(10):
        engine.scrub_all_runs()
    assert handles[0].attempts == 3
    assert not handles[0].scrubbed and handles[0]._secret.wiped
    assert engine._delivery_handles[run.run_id] == handles
    assert run.cleanup_state == "unresolved" and run.cleanup_pending == 1
    assert "exhausted" in run.cleanup_error
    assert audit.recent_runs()[0]["cleanup_state"] == "unresolved"
    assert audit._conn.execute("SELECT count(*) FROM workflow_audit WHERE event_type='secret_scrub'").fetchone()[0] == 0
    if partial:
        assert run.state == RunState.FAILED
    engine.shutdown(); audit.close()


def test_scrub_audit_failure_keeps_confirmed_handle_owned_and_deduplicates_retry(tmp_path, monkeypatch):
    engine, audit, handles = setup_engine(tmp_path, monkeypatch, fail=0)
    original = audit.log_secret_scrub
    calls = []
    def uncertain(*args):
        original(*args)
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("audit acknowledgment lost")
    monkeypatch.setattr(audit, "log_secret_scrub", uncertain)
    run = engine.start_run("wf")
    assert handles[0].scrubbed and run.cleanup_state == "unresolved"
    assert engine._delivery_handles[run.run_id] == handles
    engine.scrub_all_runs()
    assert handles[0].attempts == 1 and run.cleanup_state == "confirmed"
    assert run.run_id not in engine._delivery_handles
    assert audit._conn.execute("SELECT count(*) FROM workflow_audit WHERE event_type='secret_scrub'").fetchone()[0] == 1
    engine.shutdown(); audit.close()


def test_real_tmpfs_backend_failure_is_owned_until_retry(tmp_path, monkeypatch):
    engine, audit, _ = setup_engine(tmp_path, monkeypatch)
    attempts = []
    def unmount(target):
        attempts.append(target)
        if len(attempts) == 1:
            raise DeliveryError("busy mount")
    handles = []
    def factory(method, secret, config):
        handle = TmpfsMountDelivery(secret, runtime_root=str(tmp_path / "rt"),
                                   mounter=(lambda *args: None, unmount))
        handles.append(handle); return handle
    monkeypatch.setattr(we, "make_delivery", factory)
    run = engine.start_run("wf")
    assert run.cleanup_state == "unresolved" and handles[0]._mounted
    engine.scrub_all_runs()
    assert run.cleanup_state == "confirmed" and handles[0].scrubbed
    assert len(attempts) == 2
    engine.shutdown(); audit.close()


def test_immediate_cleanup_preserves_earlier_workflow_lifetime(tmp_path, monkeypatch):
    engine, audit, handles = setup_engine(tmp_path, monkeypatch, fail=0)
    wf = engine._workflows["wf"]
    wf.steps.append(StepDef(type=StepType.DELIVER_SECRET, config={"item":"vault/dev/key",
                           "delivery_method":"env", "var":"SECRET", "scrub_on":"step_exit"}))
    observed = []
    wf.steps.append(StepDef(type=StepType.RUN_HOOK, config={"hook":"probe"}))
    def probe(step, run, result, wf=None):
        observed.append([h.scrubbed for h in handles]); result.success = True
    monkeypatch.setattr(engine, "_handle_run_hook", probe)
    run = engine.start_run("wf")
    assert observed == [[False, True]]
    assert run.state == RunState.COMPLETED and run.cleanup_state == "confirmed"
    engine.shutdown(); audit.close()


def test_cleanup_status_audit_failure_retains_retry_and_live_visibility(tmp_path, monkeypatch):
    engine, audit, handles = setup_engine(tmp_path, monkeypatch, fail=0)
    original = audit.log_cleanup_state
    def fail_terminal(run_id, state, pending, error):
        if state == "confirmed":
            raise RuntimeError("status persistence unavailable")
        original(run_id, state, pending, error)
    monkeypatch.setattr(audit, "log_cleanup_state", fail_terminal)
    run = engine.start_run("wf")
    assert handles[0].scrubbed and run.cleanup_state == "unresolved"
    assert run.run_id in engine._cleanup_pending_runs
    assert engine.recent_runs()[0]["cleanup_state"] == "unresolved"
    monkeypatch.setattr(audit, "log_cleanup_state", original)
    engine.scrub_all_runs()
    assert run.cleanup_state == "confirmed" and run.run_id not in engine._cleanup_pending_runs
    engine.shutdown(); audit.close()
