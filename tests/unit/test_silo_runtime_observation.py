"""Runtime evidence must never mutate lifecycle or admit destructive actions."""
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
import qdistro_session_manager as sm
from test_session_manager import _FakeOps


def store_at(tmp_path):
    ops = _FakeOps()
    store = sm._SiloStore(ops, config_path=tmp_path / 'silos.yaml')
    store.create('work', 2000)
    return store, ops


def test_unresolved_start_remains_unknown_and_undeletable(tmp_path):
    store, ops = store_at(tmp_path)
    def unresolved(unit):
        raise sm.StartNotCancelled('injected')
    ops.systemctl_start = unresolved
    with pytest.raises(sm.StartNotCancelled):
        store.start('work')
    ops.observe_silo = lambda *args: ('stopped', 'inactive snapshot')
    store.observe_runtime_once()
    row = store.get('work').to_dict()
    assert row['state'] == sm.State.ACTIVE
    assert row['observed_status'] == 'unknown'
    assert 'unresolved' in row['observed_reason']
    with pytest.raises(sm.SiloBusy):
        store.delete('work')
    calls = []
    ops.systemctl_start = calls.append
    store.start('work')
    # The retry probes liveness first: observe_silo above reports
    # 'stopped' — no pending job, inactive unit, empty workload cgroup —
    # proof the queued start never materialized, so the retry relaunches
    # (the old silent no-op was the defect) and the flag clears on the
    # way through STOPPED.
    assert calls == ['qdshell-session-work@2000.service']
    assert store.get('work').start_unresolved is False


def test_generation_discards_late_probe_and_list_remains_responsive(tmp_path):
    store, ops = store_at(tmp_path)
    began, release = threading.Event(), threading.Event()
    def delayed(*args):
        began.set()
        assert release.wait(3)
        return 'launcher-running', 'old generation'
    ops.observe_silo = delayed
    worker = threading.Thread(target=store.observe_runtime_once)
    worker.start()
    try:
        assert began.wait(3)
        assert len(store.list_silos()) == 1
        store.start('work')
        generation = store.get('work').operation_generation
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert store.get('work').operation_generation == generation
    assert store.get('work').to_dict()['observed_status'] == 'unknown'


def test_probe_failure_staleness_restart_and_launcher_death(tmp_path):
    store, ops = store_at(tmp_path)
    store.start('work')
    ops.observe_silo = lambda *args: ('launcher-running', 'unit active')
    store.observe_runtime_once()
    assert store.get('work').to_dict()['observed_status'] == 'launcher-running'
    ops.observe_silo = lambda *args: ('failed', 'unit failed')
    store.observe_runtime_once()
    assert store.get('work').to_dict()['observed_status'] == 'failed'
    assert store.get('work').state == sm.State.ACTIVE
    store.get('work').observed_monotonic = time.monotonic() - 31
    assert store.get('work').to_dict()['observed_status'] == 'unknown'
    def timeout(*args):
        raise subprocess.TimeoutExpired('probe', 3)
    ops.observe_silo = timeout
    store.observe_runtime_once()
    assert store.get('work').to_dict()['observed_status'] == 'unknown'
    loaded = sm._SiloStore(ops, config_path=tmp_path / 'silos.yaml')
    assert loaded.get('work').to_dict()['observed_status'] == 'unknown'
    assert loaded.get('work').state == sm.State.ACTIVE


def test_ordinary_start_failure_is_visible(tmp_path):
    store, ops = store_at(tmp_path)
    def failed(unit):
        raise sm.SessionError('injected failure')
    ops.systemctl_start = failed
    with pytest.raises(sm.SessionError):
        store.start('work')
    assert store.get('work').to_dict()['observed_status'] == 'failed'
    assert store.get('work').state == sm.State.STOPPED


def test_remaining_validity_uses_monotonic_age_and_expires(tmp_path, monkeypatch):
    store, ops = store_at(tmp_path)
    silo = store.get('work')
    silo.observed_monotonic = 100.0
    silo.observed_status = 'launcher-running'
    monkeypatch.setattr(sm.time, 'monotonic', lambda: 105.0)
    assert silo.to_dict()['observed_ttl_seconds'] == 25.0
    monkeypatch.setattr(sm.time, 'monotonic', lambda: 130.0)
    assert silo.to_dict()['observed_ttl_seconds'] == 0.0
    assert silo.to_dict()['observed_status'] == 'unknown'
    # Even an anomalous backwards sample age cannot grant fresh evidence.
    monkeypatch.setattr(sm.time, 'monotonic', lambda: 95.0)
    assert silo.to_dict()['observed_ttl_seconds'] == 0.0
    assert silo.to_dict()['observed_status'] == 'unknown'


@pytest.mark.parametrize('job,active,exists,running,expected', [
    ('0', 'inactive', 0, 'false', 'unknown'),
    ('0', 'failed', 0, 'true', 'unknown'),
    ('0', 'inactive', 125, '', 'unknown'),
    ('0', 'inactive', 1, '', 'stopped'),
    ('', 'inactive', 1, '', 'stopped'),
    ('0', 'failed', 1, '', 'failed'),
    ('0', 'active', 0, 'true', 'launcher-running'),
    ('0', 'active', 1, '', 'unknown'),
    ('42', 'inactive', 1, '', 'unknown'),
])
def test_system_ops_container_and_job_evidence(monkeypatch, job, active, exists, running, expected):
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs['timeout'] == 3
        if cmd[0] == 'systemctl':
            return SimpleNamespace(returncode=0, stdout=f'LoadState=loaded\nActiveState={active}\nJob={job}\n')
        if any('container exists' in c for c in cmd):
            # the PMRC verdict: the completed chain relays podman's own rc
            return SimpleNamespace(returncode=0, stdout=f'PMRC={exists}\n')
        assert 'inspect' in cmd
        return SimpleNamespace(returncode=0, stdout=running)
    monkeypatch.setattr(sm.subprocess, 'run', run)
    status, _ = sm._SystemOps().observe_silo('work', 1000, sm.KIND_TIER2_TEMPLATE)
    assert status == expected
    if job not in ('', '0'):
        assert len(calls) == 1


def test_native_descendant_cgroup_prevents_false_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(sm, 'CGROUP_ROOT', tmp_path)
    cgroup = tmp_path / 'work'
    cgroup.mkdir()
    (cgroup / 'cgroup.events').write_text('populated 1\nfrozen 0\n')
    monkeypatch.setattr(sm.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='LoadState=loaded\nActiveState=inactive\nJob=0\n'))
    status, _ = sm._SystemOps().observe_silo('work', 2000, sm.KIND_TIER3_USER)
    assert status == 'unknown'
