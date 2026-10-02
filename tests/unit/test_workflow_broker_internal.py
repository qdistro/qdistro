"""Exercise workflow call_broker against the real broker authorization boundary."""
import threading
import queue
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


@pytest.mark.parametrize('failure', [None, RuntimeError, TimeoutError])
def test_worker_step_dispatches_to_broker_thread_and_preserves_errors(tmp_path, monkeypatch, failure):
    br = broker(tmp_path)
    proxy = b._WorkflowBrokerProxy(br)
    engine = w.WorkflowEngine(broker_proxy=proxy)
    monkeypatch.setattr(b.os, 'getuid', lambda: 0)
    owner = threading.get_ident()
    pending = queue.Queue()
    calls = []
    original = br.ListRules
    def list_rules(*args, **kwargs):
        calls.append(threading.get_ident())
        if failure:
            raise failure('maintenance refused')
        return original(*args, **kwargs)
    br.ListRules = list_rules
    monkeypatch.setattr(b.GLib, 'idle_add', lambda callback: pending.put(callback))
    results = []
    thread = threading.Thread(target=lambda: results.append(call(engine, 'ListRules')))
    thread.start()
    try:
        callback = pending.get(timeout=2)
        assert calls == []  # worker cannot touch broker state before dispatch
        assert callback() is False  # GLib removes this one-shot callback
    finally:
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert calls == [owner]
    assert len(results) == 1
    assert results[0].success is (failure is None)
    if failure:
        assert 'maintenance refused' in results[0].error


def test_dispatch_timeout_cancels_queued_maintenance_without_late_effect(tmp_path, monkeypatch):
    br = broker(tmp_path)
    proxy = b._WorkflowBrokerProxy(br)
    pending = queue.Queue()
    calls = []
    br.RunCacheGc = lambda **kwargs: calls.append('gc')
    monkeypatch.setattr(b.GLib, 'idle_add', lambda callback: pending.put(callback))
    monkeypatch.setattr(b, '_WORKFLOW_DISPATCH_TIMEOUT_S', 0.02)
    results = []
    engine = w.WorkflowEngine(broker_proxy=proxy)
    thread = threading.Thread(target=lambda: results.append(call(engine, 'RunCacheGc')))
    thread.start()
    callback = pending.get(timeout=2)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(results) == 1 and not results[0].success
    assert 'queued operation canceled' in results[0].error
    assert callback() is False
    assert calls == []


def test_running_dispatch_timeout_reports_unconfirmed_outcome(tmp_path, monkeypatch):
    br = broker(tmp_path)
    proxy = b._WorkflowBrokerProxy(br)
    pending = queue.Queue()
    finished = threading.Event()
    started = threading.Event()
    future_type = b.concurrent.futures.Future
    class WaitUntilRunningFuture(future_type):
        def result(self, timeout=None):
            # Scheduling the owner callback is not the operation being timed.
            # Open the unchanged 20-ms timeout only after maintenance begins.
            assert started.wait(timeout=2)
            return super().result(timeout=timeout)
    monkeypatch.setattr(b.concurrent.futures, 'Future', WaitUntilRunningFuture)
    calls = []
    def maintenance(**kwargs):
        # The worker's bounded wait expires while the broker action is live.
        started.set()
        assert finished.wait(timeout=2)
        calls.append('gc')
        return 1
    br.RunCacheGc = maintenance
    monkeypatch.setattr(b.GLib, 'idle_add', lambda callback: pending.put(callback))
    monkeypatch.setattr(b, '_WORKFLOW_DISPATCH_TIMEOUT_S', 0.02)
    results = []
    engine = w.WorkflowEngine(broker_proxy=proxy)
    def step():
        try:
            results.append(call(engine, 'RunCacheGc'))
        finally:
            finished.set()
    thread = threading.Thread(target=step)
    thread.start()
    callback = pending.get(timeout=2)
    assert callback() is False
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert calls == ['gc']
    assert len(results) == 1 and not results[0].success
    assert 'operation already running; outcome unconfirmed' in results[0].error


def test_worker_reload_preserves_policy_until_mainloop_dispatch(tmp_path, monkeypatch):
    br = broker(tmp_path)
    rules_dir = tmp_path / 'rules'
    rules_dir.mkdir()
    rule_file = rules_dir / 'deny.yaml'
    rule_file.write_text('- name: guarded\n  decision: deny\n  match:\n    action: guarded\n')
    br.rules.reload()
    assert [r.decision for r in br.rules.rules()] == ['deny']
    br.RulesReloaded = lambda count: None
    proxy = b._WorkflowBrokerProxy(br)
    pending = queue.Queue()
    owner = threading.get_ident()
    file_reads = []
    import builtins
    original_open = builtins.open
    def tracked_open(file, *args, **kwargs):
        if str(file) == str(rule_file):
            file_reads.append(threading.get_ident())
        return original_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', tracked_open)
    monkeypatch.setattr(b.os, 'getuid', lambda: 0)
    monkeypatch.setattr(b.GLib, 'idle_add', lambda callback: pending.put(callback))
    results = []
    thread = threading.Thread(target=lambda: results.append(proxy.ReloadRules()))
    thread.start()
    callback = pending.get(timeout=2)
    # Real RulesEngine.reload clears policy before reading files. While the
    # worker waits, the explicit deny must still be present and no worker may
    # enter that disk-I/O window. The eventual real reload runs on our owner.
    assert [r.decision for r in br.rules.rules()] == ['deny']
    assert file_reads == []
    assert callback() is False
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert file_reads == [owner]
    assert [r.decision for r in br.rules.rules()] == ['deny']
    assert int(results[0][0]) == 1


def test_pending_signal_from_worker_is_emitted_on_owner_thread(tmp_path, monkeypatch):
    br = broker(tmp_path)
    owner = threading.get_ident()
    calls = []
    # The real signal accepts exactly two arguments, not a sender keyword.
    br.WorkflowRunPending = lambda run_id, name: calls.append((threading.get_ident(), run_id, name))
    proxy = b._WorkflowBrokerProxy(br)
    pending = queue.Queue()
    monkeypatch.setattr(b.GLib, 'idle_add', lambda callback: pending.put(callback))
    thread = threading.Thread(target=lambda: proxy.WorkflowRunPending('run-1', 'workflow'))
    thread.start()
    callback = pending.get(timeout=2)
    assert calls == []
    assert callback() is False
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert calls == [(owner, 'run-1', 'workflow')]
