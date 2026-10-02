"""Exercise workflow call_broker against the real broker authorization boundary."""
import threading
from pathlib import Path
from types import SimpleNamespace

import dbus
import pytest

import qdistro_admin_broker as b
import workflow_engine as w
from workflow_schema import StepDef, StepResult, StepType, WorkflowRun


def broker(tmp_path):
    br = b.Broker.__new__(b.Broker)
    br.rules = b.RulesEngine(str(tmp_path / 'rules'))
    br._lock = threading.Lock()
    br._pending = {}
    br.audit = None
    br.workflow_engine = None
    return br


def call(engine, method, args=()):
    result = StepResult(step_name='probe', step_type='call_broker', success=False,
                        started_at=0)
    engine._handle_call_broker(
        StepDef(type=StepType.CALL_BROKER, config={'method': method, 'args': list(args)}),
        WorkflowRun(workflow_name='probe'), result,
    )
    return result


def test_production_setup_calls_real_broker_without_a_dbus_sender(tmp_path, monkeypatch):
    br = broker(tmp_path)
    monkeypatch.setattr(b, 'WORKFLOW_ENABLED', True)
    monkeypatch.setattr(b, '_workflow_dir_candidates', lambda: [str(Path(w.__file__).parent)])
    monkeypatch.setattr(b.os, 'getuid', lambda: 0)
    monkeypatch.setattr(w.WorkflowEngine, 'load_workflows', lambda self: [])
    monkeypatch.setattr(w.WorkflowEngine, 'register_triggers', lambda self: None)
    import audit_logger
    import pwd_secret_source
    monkeypatch.setattr(audit_logger, 'WorkflowAuditLogger', lambda **kwargs: None)
    monkeypatch.setattr(pwd_secret_source, 'PwdSecretSource', lambda: None)
    # Internal calls must not try resolving None, nor synchronously call our
    # own exported broker through D-Bus (which would deadlock its main loop).
    monkeypatch.setattr(b.dbus, 'SystemBus', lambda: pytest.fail('unexpected system bus lookup'))
    br._setup_workflow_engine()
    assert isinstance(br.workflow_engine, w.WorkflowEngine)
    for method in ('ListRules', 'GetPending', 'ListWorkflows'):
        result = call(br.workflow_engine, method)
        assert result.success, (method, result.error)
        assert result.details['method'] == method


@pytest.mark.parametrize('method', ['DecideRequest', 'SaveRule', 'RevokeApproval',
                                  'GetRunChannelEnv', 'RequestPermission'])
def test_internal_proxy_cannot_reach_privileged_mutation(tmp_path, monkeypatch, method):
    br = broker(tmp_path)
    proxy = b._WorkflowBrokerProxy(br)
    engine = w.WorkflowEngine(broker_proxy=proxy)
    with pytest.raises(AttributeError):
        getattr(proxy, method)
    result = call(engine, method)
    assert not result.success
    assert 'not in whitelist' in result.error
    monkeypatch.setattr(b.os, 'getuid', lambda: 0)
    # The auth boundary independently restricts the Python capability too.
    with pytest.raises(dbus.DBusException, match='not permitted'):
        br._require_admin_control_peer(b._WORKFLOW_SENDER, None, method)


@pytest.mark.parametrize('sender', [None, ':1.999', 'token-repr'])
def test_external_or_missing_sender_keeps_normal_authentication(tmp_path, monkeypatch, sender):
    br = broker(tmp_path)
    if sender == 'token-repr':
        sender = str(b._WORKFLOW_SENDER)
    calls = []
    class BusDaemon:
        def GetConnectionUnixUser(self, value):
            calls.append(value)
            if value is None:
                raise TypeError('Expected a string or unicode object')
            return 2000
        def GetConnectionUnixProcessID(self, value):
            return 123456789
    daemon = BusDaemon()
    monkeypatch.setattr(b.dbus, 'SystemBus', lambda: SimpleNamespace(get_object=lambda *a: daemon))
    monkeypatch.setattr(b.dbus, 'Interface', lambda obj, interface: obj)
    with pytest.raises((dbus.DBusException, TypeError)):
        br.ListRules(sender=sender)
    assert calls == [sender]


def test_nonroot_internal_engine_does_not_acquire_root_privileges(tmp_path, monkeypatch):
    br = broker(tmp_path)
    monkeypatch.setattr(b.os, 'getuid', lambda: 2000)
    result = call(w.WorkflowEngine(broker_proxy=b._WorkflowBrokerProxy(br)), 'ListRules')
    assert not result.success
    assert 'not permitted' in result.error
