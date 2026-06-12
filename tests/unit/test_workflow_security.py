"""Security-gate tests for the workflow engine.

Covers the enforcement added for the engine audit (findings F1-F10):

  F1  conditions are evaluated fail-closed against the firing process
  F4  deliver_secret may only deliver an item declared in ``needs``
  F7  the ``invoker`` role is enforced as a uid constraint
  F2  call_dbus refuses denied bus names + honours the allowlist
  F3  non-auto_run workflows park a PENDING run requiring approval
  F6  concurrent process_spawn fires for one cgroup collapse to one run
  F8  the in-flight bound drops excess fires
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "workflow"))

import condition_eval  # noqa: E402
from workflow_schema import (  # noqa: E402
    RunState, StepDef, StepType, TriggerDef, TriggerType, WorkflowDef,
)
from workflow_engine import WorkflowEngine  # noqa: E402


def _wf(name="wf", *, trigger=TriggerType.CRON, conditions=None, needs=None,
        roles=None, auto_run=False, steps=None):
    if steps is None:
        steps = [StepDef(type=StepType.DELIVER_SECRET,
                         config={"item": (needs or ["vault/dev/key"])[0]})]
    return WorkflowDef(
        name=name, trigger=TriggerDef(type=trigger),
        steps=steps, conditions=conditions or [], needs=needs or [],
        roles=roles or {}, auto_run=auto_run)


# ----------------------------------------------------------------------
# F1 — condition_eval unit
# ----------------------------------------------------------------------


class TestConditionEval:
    def test_empty_passes(self):
        ok, _ = condition_eval.evaluate([], {})
        assert ok

    def test_uid_match_current_process(self):
        ctx = {"pid": os.getpid()}
        ok, reason = condition_eval.evaluate([{"uid": os.getuid()}], ctx)
        assert ok, reason

    def test_uid_mismatch_fails(self):
        ctx = {"pid": os.getpid()}
        # An unrealistic uid that is not ours.
        ok, reason = condition_eval.evaluate([{"uid": os.getuid() + 4242}],
                                             ctx)
        assert not ok and "uid" in reason

    def test_unknown_key_fails_closed(self):
        ok, reason = condition_eval.evaluate([{"phase_of_moon": "full"}],
                                             {"pid": os.getpid()})
        assert not ok and "unknown condition key" in reason

    def test_process_condition_without_pid_fails_closed(self):
        ok, reason = condition_eval.evaluate([{"uid": 0}], {})
        assert not ok and "no pid" in reason

    def test_recycled_pid_fails_closed(self):
        # A starttime anchor that won't match the live process -> reject.
        ctx = {"pid": os.getpid(), "pid_starttime": 1}
        ok, reason = condition_eval.evaluate([{"uid": os.getuid()}], ctx)
        assert not ok and "recycled" in reason

    def test_argv0_basename_match(self):
        ctx = {"pid": os.getpid()}
        # Our argv0 is the python interpreter.
        ok, reason = condition_eval.evaluate([{"argv0": "*python*"}], ctx)
        assert ok, reason

    def test_cgroup_match_no_proc_needed(self):
        ok, _ = condition_eval.evaluate(
            [{"cgroup": "system.slice/qdistro-git-*"}],
            {"cgroup": "system.slice/qdistro-git-sign-1.scope"})
        assert ok

    def test_cgroup_mismatch(self):
        ok, _ = condition_eval.evaluate(
            [{"cgroup": "system.slice/other-*"}],
            {"cgroup": "system.slice/qdistro-git-sign-1.scope"})
        assert not ok


# ----------------------------------------------------------------------
# F1 (pure-logic edge tables) — the condition matcher primitives directly.
# The tests above drive them through evaluate(); these pin the matchers'
# own list-vs-scalar / glob / basename / name-resolution edges so a
# regression in a primitive is caught even if evaluate()'s plumbing hides it.
# ----------------------------------------------------------------------


class TestConditionMatchers:
    # ``want`` may be a scalar or a list; a list matches if ANY element does.
    # Numeric-only cells are hermetic: ints and numeric strings resolve to
    # themselves without any NSS lookup. Name resolution (which DOES hit NSS)
    # is exercised separately below with a stubbed resolver.
    @pytest.mark.parametrize("actual,want,expected", [
        (1000, 1000, True),                 # scalar int
        (1000, "1000", True),               # numeric string resolves to int
        (1000, [999, 1000], True),          # list, second element matches
        (1000, [999, 1001], False),         # list, none match
        (1000, [], False),                  # empty list never matches
    ])
    def test_match_uid_numeric(self, actual, want, expected):
        assert condition_eval._match_uid(actual, want) is expected

    @pytest.mark.parametrize("actual,want,expected", [
        (0, 0, True),                       # scalar int
        (0, "0", True),                     # numeric string (gid/uid symmetry)
        (0, [5, 0], True),                  # list, second element matches
        (0, [5, 7], False),                 # list, none match
        (0, [], False),                     # empty list never matches
    ])
    def test_match_gid_numeric(self, actual, want, expected):
        assert condition_eval._match_gid(actual, want) is expected

    def test_match_uid_name_resolution(self, monkeypatch):
        # Hermetic stub: only "admin" (and the literal int) resolve; any other
        # name resolves to None. Pins that a name resolves+matches, an unknown
        # name fails closed, and a mixed list matches on the resolvable member.
        monkeypatch.setattr(condition_eval._pi, "resolve_uid_name",
                            lambda v: 1000 if v in ("admin", 1000) else None)
        assert condition_eval._match_uid(1000, "admin") is True
        assert condition_eval._match_uid(1000, "ghost") is False
        assert condition_eval._match_uid(1000, ["ghost", 1000]) is True
        assert condition_eval._match_uid(1000, ["ghost"]) is False

    def test_match_gid_name_resolution(self, monkeypatch):
        monkeypatch.setattr(condition_eval._pi, "resolve_gid_name",
                            lambda v: 0 if v in ("root", 0) else None)
        assert condition_eval._match_gid(0, "root") is True
        assert condition_eval._match_gid(0, "ghost") is False
        assert condition_eval._match_gid(0, ["ghost", "root"]) is True

    @pytest.mark.parametrize("actual,want,expected", [
        ("abc", "abc", True),               # exact
        ("abc", "a*", True),                # wildcard
        ("abc", "A*", False),               # fnmatch is case-sensitive here
        ("abc", ["x*", "a*"], True),        # list, one pattern matches
        ("abc", ["x*", "y*"], False),       # list, none match
        ("abc", [], False),                 # empty list
        ("system.slice/qdistro-1.scope", "system.slice/qdistro-*", True),
    ])
    def test_match_glob(self, actual, want, expected):
        assert condition_eval._match_glob(actual, want) is expected

    # argv0 matches on the FULL path OR the basename, so a bare program-name
    # pattern catches an absolute argv0 and a path-glob still works.
    @pytest.mark.parametrize("actual,want,expected", [
        ("/usr/bin/python3", "python3", True),      # basename pattern
        ("/usr/bin/python3", "py*", True),          # basename glob
        ("/usr/bin/python3", "/usr/bin/*", True),   # full-path glob
        ("/usr/bin/python3", "bash", False),        # neither
        ("/usr/bin/python3", ["a", "py*"], True),   # list, basename glob hits
        ("python3", "python3", True),               # already-bare argv0
    ])
    def test_match_argv0(self, actual, want, expected):
        assert condition_eval._match_argv0(actual, want) is expected


# ----------------------------------------------------------------------
# F1/F7 — conditions gate the run (no secret delivered on reject)
# ----------------------------------------------------------------------


class TestConditionsGateRun:
    def _engine(self):
        return WorkflowEngine(audit_logger=None)  # tracking-only delivery

    def test_reject_blocks_run_and_delivery(self):
        eng = self._engine()
        eng._workflows["wf"] = _wf(
            needs=["vault/dev/key"],
            conditions=[{"uid": os.getuid() + 4242}], auto_run=True)
        run = eng.start_run("wf", {"pid": os.getpid()})
        assert run.state == RunState.FAILED
        assert "conditions not satisfied" in run.error
        # No step executed -> no secret tracked.
        assert run.steps_completed == []
        assert run.run_id not in eng._delivered_secrets

    def test_pass_runs(self):
        eng = self._engine()
        eng._workflows["wf"] = _wf(
            needs=["vault/dev/key"],
            conditions=[{"uid": os.getuid()}], auto_run=True)
        run = eng.start_run("wf", {"pid": os.getpid()})
        assert run.state == RunState.COMPLETED

    def test_invoker_role_enforced_as_uid(self):
        # roles: {<current-user>: invoker} should pass; a foreign invoker
        # should be rejected even with no explicit uid condition.
        import pwd as _pwd
        me = _pwd.getpwuid(os.getuid()).pw_name
        eng = self._engine()
        eng._workflows["ok"] = _wf(
            name="ok", needs=["vault/dev/key"],
            roles={me: "invoker"}, auto_run=True)
        run = eng.start_run("ok", {"pid": os.getpid()})
        assert run.state == RunState.COMPLETED, run.error

        eng._workflows["no"] = _wf(
            name="no", needs=["vault/dev/key"],
            roles={"root" if os.getuid() != 0 else "nobody": "invoker"},
            auto_run=True)
        run = eng.start_run("no", {"pid": os.getpid()})
        assert run.state == RunState.FAILED


# ----------------------------------------------------------------------
# F4 — needs allowlist
# ----------------------------------------------------------------------


class TestNeedsEnforced:
    def test_undeclared_item_rejected(self):
        eng = WorkflowEngine(audit_logger=None)
        eng._workflows["wf"] = _wf(
            needs=["vault/dev/allowed"], auto_run=True,
            steps=[StepDef(type=StepType.DELIVER_SECRET,
                           config={"item": "vault/dev/SNEAKY"})])
        run = eng.start_run("wf", {})
        assert run.state == RunState.FAILED
        assert "not in workflow needs" in run.steps_completed[0].error

    def test_empty_needs_rejects_any_delivery(self):
        eng = WorkflowEngine(audit_logger=None)
        eng._workflows["wf"] = _wf(
            needs=[], auto_run=True,
            steps=[StepDef(type=StepType.DELIVER_SECRET,
                           config={"item": "vault/dev/key"})])
        run = eng.start_run("wf", {})
        assert run.state == RunState.FAILED

    def test_declared_item_allowed(self):
        eng = WorkflowEngine(audit_logger=None)
        eng._workflows["wf"] = _wf(needs=["vault/dev/key"], auto_run=True)
        run = eng.start_run("wf", {})
        assert run.state == RunState.COMPLETED


# ----------------------------------------------------------------------
# F2 — call_dbus destination policy
# ----------------------------------------------------------------------


class _Result:
    def __init__(self):
        self.success = False
        self.error = ""
        self.details = {}


def _dbus_step(bus_name):
    return StepDef(type=StepType.CALL_DBUS, config={
        "bus_name": bus_name, "object_path": "/x",
        "interface": "x.Y", "method": "Ping"})


class TestCallDbusPolicy:
    # The destination policy is ENFORCED by default and fail-closed:
    # hard denylist + unique-name rejection + default-deny allowlist.
    # Each test clears the dev escape hatch (QDISTRO_WORKFLOW_DBUS_OPEN) so
    # the enforced default is exercised even if the ambient env sets it.
    def test_denylisted_bus_refused(self, monkeypatch):
        monkeypatch.delenv("QDISTRO_WORKFLOW_DBUS_OPEN", raising=False)
        from workflow_schema import StepResult, WorkflowRun
        eng = WorkflowEngine(audit_logger=None)
        r = StepResult(step_name="s", step_type="call_dbus", success=False)
        eng._handle_call_dbus(_dbus_step("org.qdistro.Pwd1"),
                              WorkflowRun(workflow_name="w"), r)
        assert not r.success and "denied" in r.error

    def test_systemd_refused(self, monkeypatch):
        monkeypatch.delenv("QDISTRO_WORKFLOW_DBUS_OPEN", raising=False)
        from workflow_schema import StepResult, WorkflowRun
        eng = WorkflowEngine(audit_logger=None)
        r = StepResult(step_name="s", step_type="call_dbus", success=False)
        eng._handle_call_dbus(_dbus_step("org.freedesktop.systemd1"),
                              WorkflowRun(workflow_name="w"), r)
        assert not r.success and "denied" in r.error

    def test_denylisted_bus_refused_even_if_allowlisted(self, monkeypatch):
        # The hard denylist wins over the allowlist: a privilege-escalation
        # bus name can never be re-permitted by configuration.
        monkeypatch.delenv("QDISTRO_WORKFLOW_DBUS_OPEN", raising=False)
        monkeypatch.setenv("QDISTRO_WORKFLOW_DBUS_ALLOW", "org.qdistro.Pwd1")
        from workflow_schema import StepResult, WorkflowRun
        eng = WorkflowEngine(audit_logger=None)
        r = StepResult(step_name="s", step_type="call_dbus", success=False)
        eng._handle_call_dbus(_dbus_step("org.qdistro.Pwd1"),
                              WorkflowRun(workflow_name="w"), r)
        assert not r.success and "denied" in r.error

    def test_unique_name_refused(self, monkeypatch):
        # A workflow must not slip past the well-known-name denylist by
        # targeting a denied service's unique connection name (":1.N").
        monkeypatch.delenv("QDISTRO_WORKFLOW_DBUS_OPEN", raising=False)
        from workflow_schema import StepResult, WorkflowRun
        eng = WorkflowEngine(audit_logger=None)
        r = StepResult(step_name="s", step_type="call_dbus", success=False)
        eng._handle_call_dbus(_dbus_step(":1.3"),
                              WorkflowRun(workflow_name="w"), r)
        assert not r.success and "unique connection name" in r.error

    def test_non_allowlisted_refused_by_default(self, monkeypatch):
        # Default-deny: with no allowlist configured, even an otherwise
        # innocuous bus name is refused — nothing is callable by default.
        monkeypatch.delenv("QDISTRO_WORKFLOW_DBUS_OPEN", raising=False)
        monkeypatch.delenv("QDISTRO_WORKFLOW_DBUS_ALLOW", raising=False)
        from workflow_schema import StepResult, WorkflowRun
        eng = WorkflowEngine(audit_logger=None)
        r = StepResult(step_name="s", step_type="call_dbus", success=False)
        eng._handle_call_dbus(_dbus_step("org.example.Other"),
                              WorkflowRun(workflow_name="w"), r)
        assert not r.success and "allowlist" in r.error

    def test_allowlist_blocks_others(self, monkeypatch):
        monkeypatch.delenv("QDISTRO_WORKFLOW_DBUS_OPEN", raising=False)
        monkeypatch.setenv("QDISTRO_WORKFLOW_DBUS_ALLOW", "org.example.Allowed")
        from workflow_schema import StepResult, WorkflowRun
        eng = WorkflowEngine(audit_logger=None)
        r = StepResult(step_name="s", step_type="call_dbus", success=False)
        eng._handle_call_dbus(_dbus_step("org.example.Other"),
                              WorkflowRun(workflow_name="w"), r)
        assert not r.success and "allowlist" in r.error

    def test_allowlisted_dest_permitted(self, monkeypatch):
        # An explicitly-allowlisted, non-denied bus name passes the policy:
        # it is NOT rejected by denylist/unique-name/allowlist checks. With
        # no real dbus available the call fails downstream, but the error is
        # never a policy refusal — proving the policy permitted it.
        monkeypatch.delenv("QDISTRO_WORKFLOW_DBUS_OPEN", raising=False)
        monkeypatch.setenv("QDISTRO_WORKFLOW_DBUS_ALLOW", "org.example.Allowed")
        from workflow_schema import StepResult, WorkflowRun
        eng = WorkflowEngine(audit_logger=None)
        r = StepResult(step_name="s", step_type="call_dbus", success=False)
        eng._handle_call_dbus(_dbus_step("org.example.Allowed"),
                              WorkflowRun(workflow_name="w"), r)
        assert "denied" not in r.error
        assert "allowlist" not in r.error
        assert "unique connection name" not in r.error

    def test_escape_hatch_restores_open(self, monkeypatch):
        # Dev escape hatch (QDISTRO_WORKFLOW_DBUS_OPEN=1) reverts to the
        # historical wide-open behavior: even a normally-denied bus name is
        # NOT rejected by policy — it only fails later at the real dbus call
        # (no dbus module faked here), proving policy didn't block it.
        monkeypatch.setenv("QDISTRO_WORKFLOW_DBUS_OPEN", "1")
        from workflow_schema import StepResult, WorkflowRun
        eng = WorkflowEngine(audit_logger=None)
        r = StepResult(step_name="s", step_type="call_dbus", success=False)
        eng._handle_call_dbus(_dbus_step("org.qdistro.Pwd1"),
                              WorkflowRun(workflow_name="w"), r)
        # Policy did not reject it; any failure is from the actual call,
        # never the denylist/allowlist.
        assert "denied" not in r.error
        assert "allowlist" not in r.error


# ----------------------------------------------------------------------
# F3 — approval gate
# ----------------------------------------------------------------------


class TestApprovalGate:
    def test_non_auto_run_parks_pending(self):
        eng = WorkflowEngine(audit_logger=None)
        eng._workflows["wf"] = _wf(needs=["vault/dev/key"], auto_run=False)
        eng._on_trigger("wf", {})
        pending = eng.list_pending_runs()
        assert len(pending) == 1
        assert pending[0].state == RunState.PENDING
        # Nothing executed yet.
        assert pending[0].steps_completed == []

    def test_approve_executes(self):
        eng = WorkflowEngine(audit_logger=None)
        eng._workflows["wf"] = _wf(needs=["vault/dev/key"], auto_run=False)
        eng._on_trigger("wf", {})
        run_id = eng.list_pending_runs()[0].run_id
        assert eng.approve_run(run_id) is True
        # Dispatched to the pool — wait for completion.
        for _ in range(200):
            r = eng.get_run(run_id)
            if r and r.state == RunState.COMPLETED:
                break
            time.sleep(0.01)
        assert eng.get_run(run_id).state == RunState.COMPLETED
        # Double-approve is a no-op.
        assert eng.approve_run(run_id) is False
        eng.shutdown()

    def test_approve_unknown_run(self):
        eng = WorkflowEngine(audit_logger=None)
        assert eng.approve_run("nope") is False

    def test_repeated_fire_does_not_flood_pending(self):
        # A cron-style workflow that keeps firing while unapproved must
        # collapse to a single pending run, not accumulate one per tick.
        eng = WorkflowEngine(audit_logger=None)
        eng._workflows["wf"] = _wf(needs=["vault/dev/key"], auto_run=False)
        eng._on_trigger("wf", {})
        eng._on_trigger("wf", {})
        eng._on_trigger("wf", {})
        assert len(eng.list_pending_runs()) == 1


# ----------------------------------------------------------------------
# F6/F8 — dedup + in-flight bound
# ----------------------------------------------------------------------


class TestDedupAndBound:
    def test_duplicate_cgroup_fires_collapse(self):
        gate = threading.Event()
        started = threading.Event()

        class _BlockEngine(WorkflowEngine):
            def _handle_deliver_secret(self, step, run, result, wf=None):
                started.set()
                gate.wait(2.0)
                result.success = True

        eng = _BlockEngine(audit_logger=None)
        eng._workflows["wf"] = _wf(needs=["vault/dev/key"], auto_run=True,
                                   trigger=TriggerType.PROCESS_SPAWN)
        ctx = {"cgroup": "system.slice/x.scope", "pid": os.getpid()}
        eng._on_trigger("wf", ctx)
        assert started.wait(2.0)
        # Second fire for the same cgroup while the first is in flight is
        # collapsed.
        eng._on_trigger("wf", dict(ctx))
        assert len(eng.list_runs()) == 1
        gate.set()
        eng.shutdown()

    def test_inflight_bound_drops(self):
        eng = WorkflowEngine(audit_logger=None)
        eng._workflows["wf"] = _wf(needs=["vault/dev/key"], auto_run=True,
                                   trigger=TriggerType.PROCESS_SPAWN)
        eng._inflight = eng._max_inflight  # simulate saturation
        eng._on_trigger("wf", {"cgroup": "c", "pid": os.getpid()})
        assert eng.list_runs() == []  # dropped, nothing scheduled
