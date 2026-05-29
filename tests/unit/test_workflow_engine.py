"""Unit tests for the workflow engine skeleton.

Covers YAML loading + validation, step execution ordering,
failure-mid-step cleanup, and audit trail generation. Pure Python,
no D-Bus or real filesystem watches.
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

# Put workflow/ on sys.path so imports work like they do in production
# (flat module names, not package-qualified).
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "workflow"))

from workflow_schema import (  # noqa: E402
    RunState,
    StepDef,
    StepResult,
    StepType,
    TriggerDef,
    TriggerType,
    WorkflowDef,
    WorkflowRun,
)
from workflow_loader import WorkflowLoader  # noqa: E402
from trigger_registry import (  # noqa: E402
    CronTrigger,
    DBusSignalTrigger,
    ProcessSpawnTrigger,
    QbusEventTrigger,
    TriggerRegistry,
)
from audit_logger import WorkflowAuditLogger  # noqa: E402
from workflow_engine import WorkflowEngine  # noqa: E402


# ======================================================================
# Test doubles for step-handler backends (faked at the boundary)
# ======================================================================


class _FakeHooks:
    """Stand-in for broker.hooks (HookClient)."""

    enabled = True

    def __init__(self, verdict="allow"):
        self._verdict = verdict
        self.calls = []

    def query(self, action, event):
        self.calls.append((action, dict(event)))
        if self._verdict is None:
            return None
        return {"verdict": self._verdict}


class _FakeBroker:
    """Minimal broker proxy exposing hooks + a few whitelisted methods."""

    def __init__(self, verdict="allow"):
        self.hooks = _FakeHooks(verdict)
        self.calls = []

    def ListRules(self):
        self.calls.append("ListRules")
        return [{"name": "r1"}]

    def ListCache(self):
        self.calls.append("ListCache")
        return []

    def RunCacheGc(self):
        self.calls.append("RunCacheGc")
        return 0


def _install_fake_dbus(monkeypatch, return_value="ok", raises=None):
    """Install a fake ``dbus`` module so call_dbus runs without a real bus.

    Records the last call on the returned recorder for assertions.
    """
    import types

    recorder = {"calls": []}

    class _Method:
        def __init__(self, name, interface):
            self._name = name
            self._interface = interface

        def __call__(self, *args, **kwargs):
            recorder["calls"].append(
                (self._name, self._interface, args, kwargs))
            if raises is not None:
                raise raises
            return return_value

    class _Obj:
        def get_dbus_method(self, name, dbus_interface=None):
            return _Method(name, dbus_interface)

    class _Bus:
        def get_object(self, bus_name, obj_path, introspect=True):
            recorder["calls"].append(
                ("get_object", bus_name, obj_path, introspect))
            return _Obj()

    fake = types.ModuleType("dbus")
    fake.SystemBus = lambda: _Bus()
    fake.SessionBus = lambda: _Bus()
    monkeypatch.setitem(sys.modules, "dbus", fake)
    return recorder


# ======================================================================
# Schema tests
# ======================================================================


class TestTriggerDef:
    def test_from_dict_valid(self):
        d = {"type": "cron", "schedule": "0 * * * *", "interval_seconds": 3600}
        t = TriggerDef.from_dict(d)
        assert t.type == TriggerType.CRON
        assert t.config["schedule"] == "0 * * * *"
        assert t.config["interval_seconds"] == 3600

    def test_from_dict_all_types(self):
        for tt in TriggerType:
            t = TriggerDef.from_dict({"type": tt.value})
            assert t.type == tt

    def test_from_dict_missing_type(self):
        with pytest.raises(ValueError, match="'type' field"):
            TriggerDef.from_dict({"schedule": "daily"})

    def test_from_dict_invalid_type(self):
        with pytest.raises(ValueError, match="trigger.type must be one of"):
            TriggerDef.from_dict({"type": "magic_wand"})

    def test_from_dict_not_mapping(self):
        with pytest.raises(ValueError, match="trigger must be a mapping"):
            TriggerDef.from_dict("cron")  # type: ignore[arg-type]


class TestStepDef:
    def test_longhand(self):
        s = StepDef.from_dict({"type": "run_hook", "hook": "my_hook"})
        assert s.type == StepType.RUN_HOOK
        assert s.config["hook"] == "my_hook"

    def test_shorthand(self):
        s = StepDef.from_dict({
            "deliver_secret": {"item": "vault/key", "as": "ssh_agent_socket"},
        })
        assert s.type == StepType.DELIVER_SECRET
        assert s.config["item"] == "vault/key"

    def test_shorthand_scalar(self):
        s = StepDef.from_dict({"wait_for_process": "$trigger.pid"})
        assert s.type == StepType.WAIT_FOR_PROCESS
        assert s.config["value"] == "$trigger.pid"

    def test_all_types(self):
        for st in StepType:
            s = StepDef.from_dict({"type": st.value})
            assert s.type == st

    def test_invalid_type(self):
        with pytest.raises(ValueError, match="step type must be one of"):
            StepDef.from_dict({"type": "teleport"})

    def test_missing_type_and_no_shorthand(self):
        with pytest.raises(ValueError, match="step must have a 'type' field"):
            StepDef.from_dict({"hook": "something"})

    def test_not_mapping(self):
        with pytest.raises(ValueError, match="step must be a mapping"):
            StepDef.from_dict("run_hook")  # type: ignore[arg-type]

    def test_step_name(self):
        s = StepDef.from_dict({"type": "run_hook", "name": "my-step"})
        assert s.name == "my-step"


class TestWorkflowDef:
    def _minimal(self, **overrides):
        base = {
            "name": "test-wf",
            "trigger": {"type": "cron", "interval_seconds": 60},
            "steps": [{"type": "run_hook", "hook": "noop"}],
        }
        base.update(overrides)
        return base

    def test_minimal_valid(self):
        wf = WorkflowDef.from_dict(self._minimal())
        assert wf.name == "test-wf"
        assert wf.trigger.type == TriggerType.CRON
        assert len(wf.steps) == 1

    def test_full_fields(self):
        wf = WorkflowDef.from_dict(self._minimal(
            conditions=[{"uid": "dev"}],
            needs=["vault/dev/key"],
            roles={"dev": "invoker", "admin": "approver"},
            description="A test workflow",
        ))
        assert wf.conditions == [{"uid": "dev"}]
        assert wf.needs == ["vault/dev/key"]
        assert wf.roles == {"dev": "invoker", "admin": "approver"}
        assert wf.description == "A test workflow"

    def test_roles_as_list(self):
        wf = WorkflowDef.from_dict(self._minimal(
            roles=[{"dev": "invoker"}, {"admin": "approver"}],
        ))
        assert wf.roles == {"dev": "invoker", "admin": "approver"}

    def test_missing_name(self):
        d = self._minimal()
        del d["name"]
        with pytest.raises(ValueError, match="non-empty 'name'"):
            WorkflowDef.from_dict(d)

    def test_missing_trigger(self):
        d = self._minimal()
        del d["trigger"]
        with pytest.raises(ValueError, match="'trigger' field"):
            WorkflowDef.from_dict(d)

    def test_missing_steps(self):
        d = self._minimal()
        del d["steps"]
        with pytest.raises(ValueError, match="non-empty 'steps'"):
            WorkflowDef.from_dict(d)

    def test_empty_steps(self):
        with pytest.raises(ValueError, match="non-empty 'steps'"):
            WorkflowDef.from_dict(self._minimal(steps=[]))

    def test_unknown_top_level_keys(self):
        with pytest.raises(ValueError, match="unknown top-level keys"):
            WorkflowDef.from_dict(self._minimal(magic="spell"))

    def test_bad_condition_type(self):
        with pytest.raises(ValueError, match="conditions.*must be a mapping"):
            WorkflowDef.from_dict(self._minimal(conditions=["not-a-dict"]))

    def test_bad_needs_type(self):
        with pytest.raises(ValueError, match="needs.*must be a string"):
            WorkflowDef.from_dict(self._minimal(needs=[123]))


class TestWorkflowRun:
    def test_lifecycle(self):
        run = WorkflowRun(workflow_name="test")
        assert run.state == RunState.PENDING
        assert run.started_at == 0.0

        run.mark_running()
        assert run.state == RunState.RUNNING
        assert run.started_at > 0

        run.mark_completed()
        assert run.state == RunState.COMPLETED
        assert run.completed_at >= run.started_at

    def test_mark_failed(self):
        run = WorkflowRun(workflow_name="test")
        run.mark_running()
        run.mark_failed("step exploded")
        assert run.state == RunState.FAILED
        assert run.error == "step exploded"
        assert run.completed_at > 0


# ======================================================================
# YAML loader tests
# ======================================================================


class TestWorkflowLoader:
    def _write_yaml(self, tmp_path: Path, name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return p

    def test_load_single_workflow(self, tmp_path):
        self._write_yaml(tmp_path, "wf1.yaml", """\
            name: backup
            trigger:
              type: cron
              interval_seconds: 3600
            steps:
              - type: run_hook
                hook: backup_hook
        """)
        loader = WorkflowLoader(system_dir=str(tmp_path), user_dir="")
        wfs = loader.load()
        assert len(wfs) == 1
        assert wfs[0].name == "backup"
        assert wfs[0].trigger.type == TriggerType.CRON
        assert wfs[0].steps[0].type == StepType.RUN_HOOK
        assert loader.load_errors() == []

    def test_load_multiple_in_file(self, tmp_path):
        self._write_yaml(tmp_path, "multi.yaml", """\
            - name: wf-a
              trigger:
                type: cron
                interval_seconds: 60
              steps:
                - type: run_hook
            - name: wf-b
              trigger:
                type: dbus_signal
                interface: org.example
              steps:
                - type: call_dbus
                  method: Foo
        """)
        loader = WorkflowLoader(system_dir=str(tmp_path), user_dir="")
        wfs = loader.load()
        assert len(wfs) == 2
        names = {wf.name for wf in wfs}
        assert names == {"wf-a", "wf-b"}

    def test_user_overrides_system(self, tmp_path):
        sys_dir = tmp_path / "system"
        usr_dir = tmp_path / "user"
        sys_dir.mkdir()
        usr_dir.mkdir()
        self._write_yaml(sys_dir, "wf.yaml", """\
            name: shared
            trigger:
              type: cron
              interval_seconds: 3600
            steps:
              - type: run_hook
                hook: system_hook
        """)
        self._write_yaml(usr_dir, "wf.yaml", """\
            name: shared
            trigger:
              type: cron
              interval_seconds: 60
            steps:
              - type: run_hook
                hook: user_hook
        """)
        loader = WorkflowLoader(system_dir=str(sys_dir),
                                user_dir=str(usr_dir))
        wfs = loader.load()
        assert len(wfs) == 1
        assert wfs[0].steps[0].config.get("hook") == "user_hook"

    def test_invalid_yaml_records_error(self, tmp_path):
        (tmp_path / "bad.yaml").write_text(
            ": invalid yaml {{{}}}}", encoding="utf-8")
        loader = WorkflowLoader(system_dir=str(tmp_path), user_dir="")
        wfs = loader.load()
        assert len(wfs) == 0
        errors = loader.load_errors()
        assert len(errors) == 1
        assert "parse failed" in errors[0]

    def test_schema_error_records_error(self, tmp_path):
        self._write_yaml(tmp_path, "bad_schema.yaml", """\
            name: no-trigger
            steps:
              - type: run_hook
        """)
        loader = WorkflowLoader(system_dir=str(tmp_path), user_dir="")
        wfs = loader.load()
        assert len(wfs) == 0
        errors = loader.load_errors()
        assert any("trigger" in e for e in errors)

    def test_nonexistent_dir(self):
        loader = WorkflowLoader(
            system_dir="/nonexistent/path/9999",
            user_dir="/also/nonexistent/path/9999",
        )
        wfs = loader.load()
        assert wfs == []
        assert loader.load_errors() == []

    def test_empty_file_skipped(self, tmp_path):
        (tmp_path / "empty.yaml").write_text("", encoding="utf-8")
        loader = WorkflowLoader(system_dir=str(tmp_path), user_dir="")
        wfs = loader.load()
        assert wfs == []
        assert loader.load_errors() == []

    def test_non_yaml_files_ignored(self, tmp_path):
        (tmp_path / "readme.txt").write_text("not yaml", encoding="utf-8")
        (tmp_path / "script.py").write_text("pass", encoding="utf-8")
        self._write_yaml(tmp_path, "valid.yaml", """\
            name: only-this
            trigger:
              type: cron
            steps:
              - type: run_hook
        """)
        loader = WorkflowLoader(system_dir=str(tmp_path), user_dir="")
        wfs = loader.load()
        assert len(wfs) == 1
        assert wfs[0].name == "only-this"


# ======================================================================
# Trigger registry tests
# ======================================================================


class TestTriggerRegistry:
    def _wf(self, name: str, trigger_type: str = "cron") -> WorkflowDef:
        return WorkflowDef(
            name=name,
            trigger=TriggerDef(type=TriggerType(trigger_type)),
            steps=[StepDef(type=StepType.RUN_HOOK)],
        )

    def test_register_and_unregister(self):
        registry = TriggerRegistry()
        calls = []
        wf = self._wf("test-wf")
        trigger = registry.register(wf, lambda n, c: calls.append((n, c)))
        assert trigger.active
        assert "test-wf" in registry.active_triggers()

        registry.unregister("test-wf")
        assert not trigger.active
        assert "test-wf" not in registry.active_triggers()

    def test_unregister_all(self):
        registry = TriggerRegistry()
        for name in ("a", "b", "c"):
            registry.register(self._wf(name), lambda n, c: None)
        assert len(registry.active_triggers()) == 3

        registry.unregister_all()
        assert len(registry.active_triggers()) == 0

    def test_re_register_stops_old(self):
        registry = TriggerRegistry()
        calls = []
        wf = self._wf("test-wf")
        t1 = registry.register(wf, lambda n, c: calls.append(1))
        t2 = registry.register(wf, lambda n, c: calls.append(2))
        assert not t1.active
        assert t2.active

    def test_all_trigger_types(self):
        registry = TriggerRegistry()
        for tt in TriggerType:
            wf = self._wf(f"wf-{tt.value}", tt.value)
            trigger = registry.register(wf, lambda n, c: None)
            assert trigger.active
        registry.unregister_all()

    def test_cron_trigger_fires(self):
        """CronTrigger with minimum interval should fire."""
        calls = []

        def cb(name, ctx):
            calls.append((name, ctx))

        trigger = CronTrigger(
            "test-cron",
            TriggerDef(type=TriggerType.CRON,
                       config={"interval_seconds": 1.0}),
            cb,
        )
        trigger.start()
        assert trigger.active

        import time
        time.sleep(1.5)
        trigger.stop()
        assert not trigger.active
        # Should have fired at least once.
        assert len(calls) >= 1
        assert calls[0][0] == "test-cron"
        assert "trigger_type" in calls[0][1]


# ======================================================================
# Audit logger tests
# ======================================================================


class TestWorkflowAuditLogger:
    def test_run_lifecycle(self, tmp_path):
        db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=db)

        audit.log_run_start("run-001", "test-wf", {"reason": "cron"})
        runs = audit.recent_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run-001"
        assert runs[0]["state"] == "running"

        audit.log_step("run-001", "step-0", "run_hook", True, 1.0, 2.0)
        steps = audit.run_steps("run-001")
        assert len(steps) == 1
        assert steps[0]["success"]

        audit.log_run_complete("run-001", "test-wf")
        runs = audit.recent_runs()
        assert runs[0]["state"] == "completed"

        audit.close()

    def test_failed_run(self, tmp_path):
        db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=db)

        audit.log_run_start("run-002", "test-wf", {})
        audit.log_step("run-002", "step-0", "deliver_secret",
                       False, 1.0, 2.0, error="vault unreachable")
        audit.log_secret_scrub("run-002", "test-wf", "vault/key")
        audit.log_run_failed("run-002", "test-wf", "step-0 failed")

        runs = audit.recent_runs()
        assert runs[0]["state"] == "failed"
        assert runs[0]["error"] == "step-0 failed"

        steps = audit.run_steps("run-002")
        assert not steps[0]["success"]
        assert steps[0]["error"] == "vault unreachable"

        audit.close()

    def test_gc(self, tmp_path):
        db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=db)

        audit.log_run_start("old-run", "wf", {})
        audit.log_step("old-run", "s", "run_hook", True, 1.0, 2.0)
        audit.log_run_complete("old-run", "wf")

        # GC everything older than 0 seconds from now.
        deleted = audit.gc(0)
        assert deleted >= 1
        assert audit.recent_runs() == []
        assert audit.run_steps("old-run") == []

        audit.close()


# ======================================================================
# Engine tests — step execution ordering
# ======================================================================


class TestWorkflowEngineSteps:
    def _make_engine(self, tmp_path, workflows_yaml: str):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(
            textwrap.dedent(workflows_yaml), encoding="utf-8")
        audit_db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=audit_db)
        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = WorkflowEngine(
            broker_proxy=_FakeBroker(),
            audit_logger=audit,
            loader=loader,
        )
        engine.load_workflows()
        return engine, audit

    def test_simple_run(self, tmp_path):
        engine, audit = self._make_engine(tmp_path, """\
            name: test-wf
            trigger:
              type: cron
            steps:
              - type: run_hook
                hook: hook_a
              - type: run_hook
                hook: hook_b
        """)
        run = engine.start_run("test-wf")
        assert run.state == RunState.COMPLETED
        assert len(run.steps_completed) == 2
        assert all(s.success for s in run.steps_completed)

        # Verify audit trail.
        runs = audit.recent_runs()
        assert len(runs) == 1
        assert runs[0]["state"] == "completed"
        steps = audit.run_steps(run.run_id)
        assert len(steps) == 2

    def test_step_ordering(self, tmp_path):
        engine, _audit = self._make_engine(tmp_path, """\
            name: ordered
            trigger:
              type: cron
            needs:
              - vault/key
            steps:
              - type: deliver_secret
                name: first
                item: vault/key
              - type: run_hook
                name: second
                hook: my_hook
              - type: call_broker
                name: third
                method: ListRules
        """)
        run = engine.start_run("ordered")
        assert run.state == RunState.COMPLETED
        names = [s.step_name for s in run.steps_completed]
        assert names == ["first", "second", "third"]
        types = [s.step_type for s in run.steps_completed]
        assert types == ["deliver_secret", "run_hook", "call_broker"]

    def test_all_step_types(self, tmp_path, monkeypatch):
        # The default call_dbus policy is fail-closed default-deny; allowlist
        # the test bus so the call_dbus step in this end-to-end run succeeds.
        monkeypatch.delenv("QDISTRO_WORKFLOW_DBUS_OPEN", raising=False)
        monkeypatch.setenv("QDISTRO_WORKFLOW_DBUS_ALLOW", "org.example")
        _install_fake_dbus(monkeypatch, return_value="pong")
        # A reaped pid: pidfd_open/kill(0) report it gone immediately.
        import subprocess
        p = subprocess.Popen(["true"])
        p.wait()
        dead_pid = p.pid

        engine, _audit = self._make_engine(tmp_path, f"""\
            name: all-types
            trigger:
              type: cron
            needs:
              - vault/secret
            steps:
              - type: deliver_secret
                item: vault/secret
              - type: wait_for_process
                pid: {dead_pid}
              - type: run_hook
                hook: example
              - type: call_dbus
                bus_name: org.example
                object_path: /org/example
                interface: org.example.Iface
                method: Ping
              - type: call_broker
                method: ListRules
        """)
        run = engine.start_run("all-types")
        assert run.state == RunState.COMPLETED, run.error
        assert len(run.steps_completed) == 5
        assert all(s.success for s in run.steps_completed)

    def test_unknown_workflow_raises(self, tmp_path):
        engine, _audit = self._make_engine(tmp_path, """\
            name: real
            trigger:
              type: cron
            steps:
              - type: run_hook
        """)
        with pytest.raises(ValueError, match="unknown workflow"):
            engine.start_run("nonexistent")


# ======================================================================
# Engine tests — failure mid-step + cleanup
# ======================================================================


class _FailingEngine(WorkflowEngine):
    """Engine subclass that fails on a specific step by name."""

    def __init__(self, fail_step: str, **kwargs):
        super().__init__(**kwargs)
        self._fail_step = fail_step
        self.scrubbed_secrets: list[str] = []

    def _handle_run_hook(self, step, run, result, wf=None):
        if step.config.get("hook") == self._fail_step:
            result.success = False
            result.error = f"simulated failure in {self._fail_step}"
            return
        super()._handle_run_hook(step, run, result, wf)

    def _cleanup_secrets(self, run_id, workflow_name):
        # Track what would be scrubbed.
        secrets = self._delivered_secrets.get(run_id, [])
        self.scrubbed_secrets.extend(secrets)
        super()._cleanup_secrets(run_id, workflow_name)


class TestFailureMidStep:
    def test_failure_stops_execution(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: fail-wf
            trigger:
              type: cron
            steps:
              - type: run_hook
                name: ok-step
                hook: good_hook
              - type: run_hook
                name: bad-step
                hook: exploding_hook
              - type: run_hook
                name: never-reached
                hook: unreachable
        """), encoding="utf-8")

        audit_db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=audit_db)
        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = _FailingEngine(
            fail_step="exploding_hook",
            broker_proxy=_FakeBroker(),
            audit_logger=audit,
            loader=loader,
        )
        engine.load_workflows()
        run = engine.start_run("fail-wf")

        assert run.state == RunState.FAILED
        assert "exploding_hook" in run.error
        # Only 2 steps executed (the third was never reached).
        assert len(run.steps_completed) == 2
        assert run.steps_completed[0].success
        assert not run.steps_completed[1].success

        # Audit recorded the failure.
        runs = audit.recent_runs()
        assert runs[0]["state"] == "failed"

    def test_secrets_scrubbed_on_failure(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: secret-fail
            trigger:
              type: cron
            needs:
              - vault/dev/ssh-key
              - vault/dev/api-token
            steps:
              - type: deliver_secret
                item: vault/dev/ssh-key
              - type: deliver_secret
                item: vault/dev/api-token
              - type: run_hook
                hook: exploding_hook
        """), encoding="utf-8")

        audit_db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=audit_db)
        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = _FailingEngine(
            fail_step="exploding_hook",
            broker_proxy=None,
            audit_logger=audit,
            loader=loader,
        )
        engine.load_workflows()
        run = engine.start_run("secret-fail")

        assert run.state == RunState.FAILED
        # Both secrets should have been scrubbed.
        assert "vault/dev/ssh-key" in engine.scrubbed_secrets
        assert "vault/dev/api-token" in engine.scrubbed_secrets

    def test_deliver_secret_missing_item(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: bad-secret
            trigger:
              type: cron
            steps:
              - type: deliver_secret
        """), encoding="utf-8")

        audit_db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=audit_db)
        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = WorkflowEngine(
            broker_proxy=None, audit_logger=audit, loader=loader,
        )
        engine.load_workflows()
        run = engine.start_run("bad-secret")
        assert run.state == RunState.FAILED
        assert "missing 'item'" in run.error


# ======================================================================
# Engine tests — audit trail generation
# ======================================================================


class TestAuditTrailGeneration:
    def test_successful_run_audit(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: audited-wf
            trigger:
              type: cron
            steps:
              - type: run_hook
                name: step-a
                hook: a
              - type: call_broker
                name: step-b
                method: ListRules
        """), encoding="utf-8")

        audit_db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=audit_db)
        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = WorkflowEngine(
            broker_proxy=_FakeBroker(), audit_logger=audit, loader=loader,
        )
        engine.load_workflows()
        run = engine.start_run("audited-wf")

        assert run.state == RunState.COMPLETED

        runs = audit.recent_runs()
        assert len(runs) == 1
        assert runs[0]["run_id"] == run.run_id
        assert runs[0]["workflow_name"] == "audited-wf"
        assert runs[0]["state"] == "completed"

        steps = audit.run_steps(run.run_id)
        assert len(steps) == 2
        assert steps[0]["step_name"] == "step-a"
        assert steps[0]["step_type"] == "run_hook"
        assert steps[0]["success"]
        assert steps[1]["step_name"] == "step-b"
        assert steps[1]["step_type"] == "call_broker"
        assert steps[1]["success"]

    def test_failed_run_audit_trail(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: fail-audit
            trigger:
              type: cron
            needs:
              - vault/key
            steps:
              - type: deliver_secret
                item: vault/key
              - type: run_hook
                hook: bad
        """), encoding="utf-8")

        audit_db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=audit_db)
        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = _FailingEngine(
            fail_step="bad",
            broker_proxy=None, audit_logger=audit, loader=loader,
        )
        engine.load_workflows()
        run = engine.start_run("fail-audit")

        assert run.state == RunState.FAILED
        runs = audit.recent_runs()
        assert runs[0]["state"] == "failed"
        assert "bad" in (runs[0]["error"] or "")

        steps = audit.run_steps(run.run_id)
        assert len(steps) == 2
        assert steps[0]["success"]   # deliver_secret succeeded
        assert not steps[1]["success"]  # run_hook failed

    def test_trigger_context_in_audit(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: ctx-wf
            trigger:
              type: process_spawn
            steps:
              - type: run_hook
        """), encoding="utf-8")

        audit_db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=audit_db)
        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = WorkflowEngine(
            broker_proxy=None, audit_logger=audit, loader=loader,
        )
        engine.load_workflows()
        run = engine.start_run("ctx-wf", {"pid": 1234, "exe": "/usr/bin/git"})

        runs = audit.recent_runs()
        import json
        ctx = json.loads(runs[0]["trigger_context"])
        assert ctx["pid"] == 1234
        assert ctx["exe"] == "/usr/bin/git"


# ======================================================================
# Engine lifecycle tests
# ======================================================================


class TestEngineLifecycle:
    def test_register_triggers_and_shutdown(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: trigger-wf
            trigger:
              type: cron
              interval_seconds: 9999
            steps:
              - type: run_hook
        """), encoding="utf-8")

        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = WorkflowEngine(
            broker_proxy=None, audit_logger=None, loader=loader,
        )
        engine.load_workflows()
        engine.register_triggers()

        triggers = engine._registry.active_triggers()
        assert "trigger-wf" in triggers
        assert triggers["trigger-wf"].active

        engine.shutdown()
        assert not triggers["trigger-wf"].active

    def test_on_trigger_dispatches_async(self, tmp_path):
        """A trigger fire must not block on the run — it dispatches to the
        worker pool and returns immediately (so the broker main loop /
        watcher threads stay free)."""
        import threading
        import time

        started = threading.Event()
        release = threading.Event()

        class _BlockingEngine(WorkflowEngine):
            def _handle_run_hook(self, step, run, result, wf=None):
                started.set()
                release.wait(2.0)
                result.success = True

        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: async-wf
            auto_run: true
            trigger:
              type: cron
              interval_seconds: 9999
            steps:
              - type: run_hook
                hook: slow
        """), encoding="utf-8")
        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = _BlockingEngine(broker_proxy=None, audit_logger=None,
                                 loader=loader)
        engine.load_workflows()

        engine._on_trigger("async-wf", {"k": "v"})
        # The fire returned promptly while the hook is still blocked.
        assert started.wait(2.0)
        assert not release.is_set()
        # Run is in-flight, not yet complete.
        assert all(r.state == RunState.RUNNING for r in engine.list_runs())
        release.set()
        for _ in range(100):
            runs = engine.list_runs()
            if runs and runs[0].state == RunState.COMPLETED:
                break
            time.sleep(0.02)
        assert engine.list_runs()[0].state == RunState.COMPLETED
        engine.shutdown()

    def test_list_runs(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: list-test
            trigger:
              type: cron
            steps:
              - type: run_hook
        """), encoding="utf-8")

        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = WorkflowEngine(
            broker_proxy=None, audit_logger=None, loader=loader,
        )
        engine.load_workflows()

        assert engine.list_runs() == []
        run1 = engine.start_run("list-test")
        run2 = engine.start_run("list-test")
        runs = engine.list_runs()
        assert len(runs) == 2
        run_ids = {r.run_id for r in runs}
        assert run1.run_id in run_ids
        assert run2.run_id in run_ids

        fetched = engine.get_run(run1.run_id)
        assert fetched is not None
        assert fetched.run_id == run1.run_id


# ======================================================================
# Regression tests for codex review findings
# ======================================================================


class TestCodexReviewFixes:
    """Tests for issues found during codex review."""

    def test_successful_run_scrubs_secrets(self, tmp_path):
        """P1: successful runs must scrub delivered secrets too."""
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            name: secret-ok
            trigger:
              type: cron
            needs:
              - vault/dev/ssh-key
            steps:
              - type: deliver_secret
                item: vault/dev/ssh-key
              - type: run_hook
                hook: safe_hook
        """), encoding="utf-8")

        audit_db = str(tmp_path / "audit.sqlite")
        audit = WorkflowAuditLogger(db_path=audit_db)
        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = WorkflowEngine(
            broker_proxy=_FakeBroker(), audit_logger=audit, loader=loader,
        )
        engine.load_workflows()
        run = engine.start_run("secret-ok")

        assert run.state == RunState.COMPLETED
        # After successful completion, the delivered secrets tracking
        # should be cleaned up.
        assert run.run_id not in engine._delivered_secrets

    def test_cron_trigger_clamps_zero_interval(self):
        """P1: CronTrigger must clamp zero/negative intervals."""
        calls = []
        trigger = CronTrigger(
            "test-zero",
            TriggerDef(type=TriggerType.CRON,
                       config={"interval_seconds": 0}),
            lambda n, c: calls.append(1),
        )
        trigger.start()
        assert trigger.active
        import time
        # With the clamp, a 0-second interval becomes 1s minimum.
        # Sleep briefly and verify we don't get a flood of calls.
        time.sleep(0.3)
        trigger.stop()
        # With clamping to 1s, zero calls in 0.3s is expected.
        assert len(calls) == 0

    def test_cron_trigger_clamps_negative_interval(self):
        """P1: CronTrigger must clamp negative intervals."""
        calls = []
        trigger = CronTrigger(
            "test-neg",
            TriggerDef(type=TriggerType.CRON,
                       config={"interval_seconds": -10}),
            lambda n, c: calls.append(1),
        )
        trigger.start()
        assert trigger.active
        import time
        time.sleep(0.3)
        trigger.stop()
        assert len(calls) == 0

    def test_register_triggers_removes_stale(self, tmp_path):
        """P2: reload + register must remove triggers for deleted workflows."""
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            - name: wf-a
              trigger:
                type: cron
                interval_seconds: 9999
              steps:
                - type: run_hook
            - name: wf-b
              trigger:
                type: cron
                interval_seconds: 9999
              steps:
                - type: run_hook
        """), encoding="utf-8")

        loader = WorkflowLoader(system_dir=str(wf_dir), user_dir="")
        engine = WorkflowEngine(
            broker_proxy=None, audit_logger=None, loader=loader,
        )
        engine.load_workflows()
        engine.register_triggers()
        assert "wf-a" in engine._registry.active_triggers()
        assert "wf-b" in engine._registry.active_triggers()

        # Remove wf-b from YAML and reload.
        (wf_dir / "test.yaml").write_text(textwrap.dedent("""\
            - name: wf-a
              trigger:
                type: cron
                interval_seconds: 9999
              steps:
                - type: run_hook
        """), encoding="utf-8")
        engine.load_workflows()
        engine.register_triggers()

        assert "wf-a" in engine._registry.active_triggers()
        assert "wf-b" not in engine._registry.active_triggers()

        engine.shutdown()

    def test_scalar_needs_becomes_list(self):
        """P2: scalar needs value must not split into characters."""
        wf = WorkflowDef.from_dict({
            "name": "scalar-needs",
            "trigger": {"type": "cron"},
            "steps": [{"type": "run_hook"}],
            "needs": "vault/dev/key",
        })
        assert wf.needs == ["vault/dev/key"]

    def test_list_needs_still_works(self):
        """Ensure list needs still work after the scalar fix."""
        wf = WorkflowDef.from_dict({
            "name": "list-needs",
            "trigger": {"type": "cron"},
            "steps": [{"type": "run_hook"}],
            "needs": ["vault/a", "vault/b"],
        })
        assert wf.needs == ["vault/a", "vault/b"]

    def test_needs_bad_type_rejected(self):
        """Ensure non-string/non-list needs is rejected."""
        with pytest.raises(ValueError, match="needs must be a list or string"):
            WorkflowDef.from_dict({
                "name": "bad-needs",
                "trigger": {"type": "cron"},
                "steps": [{"type": "run_hook"}],
                "needs": 42,
            })
