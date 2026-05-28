"""Unit tests for the real workflow step handlers.

Phase-1 handlers (run_hook, call_broker, call_dbus, wait_for_process)
do real work; each backend is faked at its boundary:

  - run_hook   -> a fake broker.hooks.query
  - call_broker -> a fake broker proxy with whitelisted methods
  - call_dbus  -> a fake ``dbus`` module installed in sys.modules
  - wait_for_process -> real pids (a live child, a reaped child)
"""
from __future__ import annotations

import os
import sys
import time
import types
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "workflow"))

from workflow_schema import StepDef, StepResult, StepType, WorkflowRun  # noqa: E402
from workflow_engine import WorkflowEngine  # noqa: E402


def _result() -> StepResult:
    return StepResult(step_name="s", step_type="t", success=False,
                      started_at=time.time())


def _run(ctx=None) -> WorkflowRun:
    return WorkflowRun(workflow_name="wf", trigger_context=dict(ctx or {}))


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class _FakeHooks:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def query(self, action, event):
        self.calls.append((action, dict(event)))
        return self._resp


class _FakeBroker:
    def __init__(self, hooks_resp=None):
        self.hooks = _FakeHooks(hooks_resp)
        self.calls = []

    def ListRules(self):
        self.calls.append("ListRules")
        return [{"name": "r1"}]

    def RunCacheGc(self):
        self.calls.append("RunCacheGc")
        return 3

    def DecideRequest(self, *a):  # not whitelisted; must never be called
        self.calls.append("DecideRequest")
        raise AssertionError("DecideRequest must not be reachable")


def _engine(broker=None) -> WorkflowEngine:
    return WorkflowEngine(broker_proxy=broker, audit_logger=None)


# ----------------------------------------------------------------------
# run_hook
# ----------------------------------------------------------------------


class TestRunHook:
    def test_allow_succeeds(self):
        eng = _engine(_FakeBroker({"verdict": "allow"}))
        r = _result()
        step = StepDef(type=StepType.RUN_HOOK, config={"hook": "h", "event": {"a": 1}})
        eng._handle_run_hook(step, _run(), r)
        assert r.success
        assert r.details["verdict"] == "allow"
        assert eng._broker.hooks.calls == [("h", {"a": 1})]

    def test_transform_succeeds(self):
        eng = _engine(_FakeBroker({"verdict": "transform", "reason": "redacted"}))
        r = _result()
        eng._handle_run_hook(StepDef(type=StepType.RUN_HOOK, config={"hook": "h"}),
                             _run(), r)
        assert r.success
        assert r.details["reason"] == "redacted"

    def test_deny_fails(self):
        eng = _engine(_FakeBroker({"verdict": "deny", "reason": "nope"}))
        r = _result()
        eng._handle_run_hook(StepDef(type=StepType.RUN_HOOK, config={"hook": "h"}),
                             _run(), r)
        assert not r.success
        assert "deny" in r.error

    def test_null_verdict_fails_closed(self):
        eng = _engine(_FakeBroker(None))  # query() -> None == null/unreachable
        r = _result()
        eng._handle_run_hook(StepDef(type=StepType.RUN_HOOK, config={"hook": "h"}),
                             _run(), r)
        assert not r.success
        assert "no verdict" in r.error

    def test_missing_hook_name(self):
        eng = _engine(_FakeBroker({"verdict": "allow"}))
        r = _result()
        eng._handle_run_hook(StepDef(type=StepType.RUN_HOOK, config={}), _run(), r)
        assert not r.success
        assert "missing 'hook'" in r.error

    def test_no_hook_client(self):
        eng = _engine(broker=object())  # broker without .hooks
        r = _result()
        eng._handle_run_hook(StepDef(type=StepType.RUN_HOOK, config={"hook": "h"}),
                             _run(), r)
        assert not r.success
        assert "no hook client" in r.error


# ----------------------------------------------------------------------
# call_broker
# ----------------------------------------------------------------------


class TestCallBroker:
    def test_whitelisted_method(self):
        broker = _FakeBroker()
        eng = _engine(broker)
        r = _result()
        eng._handle_call_broker(
            StepDef(type=StepType.CALL_BROKER, config={"method": "ListRules"}),
            _run(), r)
        assert r.success
        assert broker.calls == ["ListRules"]
        assert r.details["returned"] == [{"name": "r1"}]

    def test_method_with_args(self):
        broker = _FakeBroker()
        eng = _engine(broker)
        r = _result()
        eng._handle_call_broker(
            StepDef(type=StepType.CALL_BROKER,
                    config={"method": "RunCacheGc", "args": []}),
            _run(), r)
        assert r.success
        assert r.details["returned"] == 3

    def test_non_whitelisted_rejected(self):
        broker = _FakeBroker()
        eng = _engine(broker)
        r = _result()
        eng._handle_call_broker(
            StepDef(type=StepType.CALL_BROKER, config={"method": "DecideRequest"}),
            _run(), r)
        assert not r.success
        assert "not in whitelist" in r.error
        assert "DecideRequest" not in broker.calls

    def test_no_broker(self):
        eng = _engine(broker=None)
        r = _result()
        eng._handle_call_broker(
            StepDef(type=StepType.CALL_BROKER, config={"method": "ListRules"}),
            _run(), r)
        assert not r.success
        assert "no broker proxy" in r.error


# ----------------------------------------------------------------------
# call_dbus
# ----------------------------------------------------------------------


def _install_fake_dbus(monkeypatch, return_value="ok", raises=None):
    recorder = {"calls": []}

    class _Method:
        def __init__(self, name, interface):
            self.name = name
            self.interface = interface

        def __call__(self, *args, **kwargs):
            recorder["calls"].append((self.name, self.interface, args, kwargs))
            if raises is not None:
                raise raises
            return return_value

    class _Obj:
        def get_dbus_method(self, name, dbus_interface=None):
            return _Method(name, dbus_interface)

    class _Bus:
        def get_object(self, bus_name, obj_path):
            recorder["calls"].append(("get_object", bus_name, obj_path))
            return _Obj()

    fake = types.ModuleType("dbus")
    fake.SystemBus = lambda: _Bus()
    fake.SessionBus = lambda: _Bus()
    monkeypatch.setitem(sys.modules, "dbus", fake)
    return recorder


class TestCallDbus:
    def _step(self, **extra):
        cfg = {"bus_name": "org.example", "object_path": "/org/example",
               "interface": "org.example.Iface", "method": "Ping"}
        cfg.update(extra)
        return StepDef(type=StepType.CALL_DBUS, config=cfg)

    def test_success(self, monkeypatch):
        rec = _install_fake_dbus(monkeypatch, return_value="pong")
        eng = _engine()
        r = _result()
        eng._handle_call_dbus(self._step(args=["x", 1]), _run(), r)
        assert r.success
        assert r.details["returned"] == "pong"
        # method call recorded with a bounded timeout kwarg
        call = [c for c in rec["calls"] if c[0] == "Ping"][0]
        assert call[2] == ("x", 1)
        assert "timeout" in call[3]

    def test_timeout_clamped(self, monkeypatch):
        rec = _install_fake_dbus(monkeypatch)
        eng = _engine()
        r = _result()
        eng._handle_call_dbus(self._step(timeout=99999), _run(), r)
        call = [c for c in rec["calls"] if c[0] == "Ping"][0]
        assert call[3]["timeout"] <= 120.0

    def test_missing_fields(self):
        eng = _engine()
        r = _result()
        eng._handle_call_dbus(
            StepDef(type=StepType.CALL_DBUS, config={"bus_name": "org.x"}),
            _run(), r)
        assert not r.success
        assert "required" in r.error

    def test_private_method_rejected(self, monkeypatch):
        _install_fake_dbus(monkeypatch)
        eng = _engine()
        r = _result()
        eng._handle_call_dbus(self._step(method="_secret"), _run(), r)
        assert not r.success
        assert "private" in r.error

    def test_failure_isolated(self, monkeypatch):
        _install_fake_dbus(monkeypatch, raises=RuntimeError("bus down"))
        eng = _engine()
        r = _result()
        eng._handle_call_dbus(self._step(), _run(), r)
        assert not r.success
        assert "bus down" in r.error


# ----------------------------------------------------------------------
# wait_for_process
# ----------------------------------------------------------------------


class TestWaitForProcess:
    def test_reaped_pid_returns_immediately(self):
        p = subprocess.Popen(["true"])
        p.wait()
        eng = _engine()
        r = _result()
        t0 = time.monotonic()
        eng._handle_wait_for_process(
            StepDef(type=StepType.WAIT_FOR_PROCESS, config={"pid": p.pid}),
            _run(), r)
        assert r.success
        assert r.details["exited"] is True
        assert time.monotonic() - t0 < 2.0

    def test_trigger_ref_resolution(self):
        p = subprocess.Popen(["true"])
        p.wait()
        eng = _engine()
        r = _result()
        eng._handle_wait_for_process(
            StepDef(type=StepType.WAIT_FOR_PROCESS, config={"pid": "$trigger.pid"}),
            _run({"pid": p.pid}), r)
        assert r.success
        assert r.details["pid"] == p.pid

    def test_live_process_times_out(self):
        p = subprocess.Popen(["sleep", "30"])
        try:
            eng = _engine()
            r = _result()
            eng._handle_wait_for_process(
                StepDef(type=StepType.WAIT_FOR_PROCESS,
                        config={"pid": p.pid, "timeout": 0.5}),
                _run(), r)
            assert not r.success
            assert "still alive" in r.error
        finally:
            p.kill()
            p.wait()

    def test_process_exits_during_wait(self):
        p = subprocess.Popen(["sleep", "0.4"])
        eng = _engine()
        r = _result()
        eng._handle_wait_for_process(
            StepDef(type=StepType.WAIT_FOR_PROCESS,
                    config={"pid": p.pid, "timeout": 10}),
            _run(), r)
        p.wait()
        assert r.success
        assert r.details["exited"] is True

    def test_unresolved_pid(self):
        eng = _engine()
        r = _result()
        eng._handle_wait_for_process(
            StepDef(type=StepType.WAIT_FOR_PROCESS, config={"pid": "$trigger.pid"}),
            _run({}), r)
        assert not r.success
        assert "unresolved" in r.error

    def test_invalid_pid(self):
        eng = _engine()
        r = _result()
        eng._handle_wait_for_process(
            StepDef(type=StepType.WAIT_FOR_PROCESS, config={"pid": -5}),
            _run(), r)
        assert not r.success
        assert "invalid pid" in r.error
