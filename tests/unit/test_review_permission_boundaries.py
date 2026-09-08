"""Regressions for selector preservation, auditable fast gates and bounded hooks."""
import concurrent.futures
import os
import queue
import socket
import sqlite3
import struct
import threading
import time
from types import SimpleNamespace

import pytest

import qdistro_admin_broker as B
from qdistro_admin_cache import ApprovalCache, SCHEMA
from qdistro_hook_client import HookClient
from test_broker_check_permission import _StubBroker


@pytest.fixture
def broker(tmp_path):
    rules = tmp_path / 'rules'
    rules.mkdir()
    b = _StubBroker(str(tmp_path / 'cache.db'), str(tmp_path / 'audit.db'), str(rules))
    yield b
    b._io_pool.shutdown(wait=True)
    if hasattr(b, '_hook_pool'):
        b._hook_pool.shutdown(wait=True)


@pytest.mark.parametrize('selector,value,other', [
    ('mime_type', 'text/plain', 'image/png'),
    ('app_id', 'approved.app', 'different.app'),
    ('sandbox_engine', 'qdistro.tier2', 'other.engine'),
])
@pytest.mark.parametrize('argv', [[], ['/bin/tool', 'approved']])
def test_rule_scope_never_creates_independent_approval(broker, tmp_path, selector, value, other, argv):
    rule = tmp_path / 'rules' / 'allow.yaml'
    rule.write_text(f'- name: scoped\n  decision: allow\n  scope: 1h\n  match:\n    action: review.transfer\n    {selector}: {value}\n')
    broker.rules.reload()
    details = {selector: value, **{f'argv[{i:02d}]': arg for i, arg in enumerate(argv)}}
    rid = broker._enqueue(2000, 0, '/bin/tool', 0, 'review.transfer', details, delegated=False)
    assert broker._pending[rid].decision is True
    assert broker.cache.lookup_detail(2000, 'review.transfer', '/bin/tool', argv or None) is None
    def check(details):
        return broker._decide_check(uid=2000, pid=0, exe='/bin/tool', action_s='review.transfer',
                                    details=details, lin_app=details.get('app_id', ''),
                                    lin_engine=details.get('sandbox_engine', ''))
    assert check({**details, selector: other}) == 'unknown'
    rule.unlink()
    broker.rules.reload()
    assert check(details) == 'unknown'
    broker.cache.store(2000, 'review.transfer', '/bin/tool', '1h', True, 1000, argv=argv or None)
    assert check(details) == 'allow'  # A real human grant remains independent.


def test_migrate_ambiguous_rule_rows_only_once(tmp_path):
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.replace(",\n    provenance   TEXT NOT NULL DEFAULT 'human'", ''))
    for uid in (0, 1000):
        conn.execute("INSERT INTO approvals(caller_uid,action,match_kind,match_value,decision,created_at,approver_uid,scope) VALUES(2000,?,'exe_only','/bin/tool',1,1,?,'forever_exe')", (f'by{uid}', uid))
    conn.execute("INSERT INTO approvals(caller_uid,action,match_kind,match_value,decision,created_at,approver_uid,scope) VALUES(2000,'root-deny','exe_only','/bin/tool',0,1,0,'forever_exe')")
    conn.commit()
    conn.close()
    cache = ApprovalCache(path)
    assert cache.lookup_detail(2000, 'by0', '/bin/tool') is None
    assert cache.lookup_detail(2000, 'by1000', '/bin/tool') is not None
    assert cache.lookup_detail(2000, 'root-deny', '/bin/tool')['decision'] == 0
    cache.store(2000, 'new-root-human', '/bin/tool', 'forever_exe', True, 0)
    cache._conn.close()
    reopened = ApprovalCache(path)
    assert reopened.lookup_detail(2000, 'new-root-human', '/bin/tool') is not None
    assert reopened.lookup_detail(2000, 'root-deny', '/bin/tool')['decision'] == 0


@pytest.mark.parametrize('portal', [False, True])
@pytest.mark.parametrize('source', ['rule', 'cache'])
@pytest.mark.parametrize('allowed', [True, False])
def test_fast_decisions_are_audited(broker, tmp_path, monkeypatch, source, allowed, portal):
    if source == 'rule':
        (tmp_path / 'rules' / 'rule.yaml').write_text(f'- name: gate\n  decision: {"allow" if allowed else "deny"}\n  match:\n    action: review.audit\n')
        broker.rules.reload()
    else:
        broker.cache.store(2000, 'review.audit', '/bin/tool', 'forever_exe', allowed, 1000)
    broker.set_peer(2000, pid=123, exe='/bin/tool')
    if portal:
        monkeypatch.setattr(broker, '_require_root_helper_peer', lambda *args: (0, 500, '/bin/portal', 5))
        monkeypatch.setattr(B, '_read_proc_identity', lambda pid: ('/bin/tool', 42))
        monkeypatch.setattr(B, '_read_proc_uid', lambda pid: 2000)
        result = broker.CheckPermissionForClient('review.audit', {'mime_type': 'text/plain'}, 123, 42)
    else:
        result = broker.CheckPermission('review.audit', {'mime_type': 'text/plain'})
    assert result == ('allow' if allowed else 'deny')
    row, = broker.audit.recent()
    assert row['caller_pid'] == 123 and row['caller_uid'] == 2000
    assert row['action'] == 'review.audit' and row['decision'] == allowed
    assert row['source'] == source
    assert 'text/plain' in row['context']
    if source == 'cache':
        assert '"grant_id":' in row['context']
    else:
        assert row['rule_path'].endswith('rule.yaml')


def test_required_audit_failure_denies_fast_grant(broker, monkeypatch):
    broker.cache.store(2000, 'review.audit', '/bin/tool', 'forever_exe', True, 1000)
    broker.set_peer(2000, exe='/bin/tool')
    monkeypatch.setattr(B, 'AUDIT_REQUIRED', True)
    def fail(**kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(broker.audit, 'log', fail)
    assert broker.CheckPermission('review.audit', {}) == 'deny'


def test_rule_request_audit_keeps_source_token_and_selector_context(broker,
                                                                    tmp_path):
    (tmp_path / 'rules' / 'rule.yaml').write_text(
        '- name: gate\n  decision: allow\n  match:\n'
        '    action: review.request\n    mime_type: text/plain\n')
    broker.rules.reload()

    rid = broker._enqueue(
        2000, 123, '/bin/tool', 0, 'review.request',
        {'mime_type': 'text/plain'}, delegated=False)

    assert broker._pending[rid].decision is True
    row, = broker.audit.recent()
    assert row['source'] == 'rule'
    assert row['rule_path'].endswith('rule.yaml')
    assert row['context'] == (
        '{"app_id": "", "mime_type": "text/plain", '
        '"sandbox_engine": ""}')


def install_hooks(broker, monkeypatch, query):
    callbacks = queue.Queue()
    broker.hooks = SimpleNamespace(enabled=True, query=query)
    broker._hook_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    broker._hook_slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(B.GLib, 'idle_add', lambda fn, *args: callbacks.put((fn, args)))
    return callbacks


def drain_until(callbacks, broker, rid):
    deadline = time.monotonic() + 2
    while broker._pending[rid].decision is None and rid not in broker.pending_signals:
        fn, args = callbacks.get(timeout=max(.01, deadline - time.monotonic()))
        fn(*args)


def test_slow_hook_does_not_block_other_gates_or_overwrite_admin(broker, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def query(*args):
        entered.set()
        assert release.wait(2)
        return {'verdict': 'allow'}
    callbacks = install_hooks(broker, monkeypatch, query)
    try:
        start = time.monotonic()
        rid = broker._enqueue(2000, 0, '/bin/tool', 0, 'slow', {}, delegated=False)
        assert time.monotonic() - start < .2
        assert entered.wait(1)
        broker.cache.store(2000, 'cached', '/bin/tool', 'forever_exe', True, 1000)
        broker.set_peer(2000, exe='/bin/tool')
        start = time.monotonic()
        assert broker.CheckPermission('cached', {}) == 'allow'
        assert broker.CheckPermission('slow', {}) == 'unknown'
        assert time.monotonic() - start < .2
        saturated = broker._enqueue(2000, 0, '/bin/tool', 0, 'overflow', {}, delegated=False)
        assert saturated in broker.pending_signals
        broker._pending[rid].decision = False  # Admin denial wins over late hook.
    finally:
        release.set()
    fn, args = callbacks.get(timeout=2)
    fn(*args)
    assert broker._pending[rid].decision is False


@pytest.mark.parametrize('verdict,expected', [('allow', True), ('deny', False), ('transform', None), ('garbage', None)])
def test_async_hook_requires_binary_verdict_and_live_identity(broker, monkeypatch, verdict, expected):
    callbacks = install_hooks(broker, monkeypatch, lambda *args: {'verdict': verdict})
    monkeypatch.setattr(B, '_read_proc_identity', lambda pid: ('/bin/tool', 42))
    monkeypatch.setattr(B, '_read_proc_uid', lambda pid: 2000)
    rid = broker._enqueue(2000, 0, '/bin/tool', 42, 'hook', {}, delegated=False)
    drain_until(callbacks, broker, rid)
    assert broker._pending[rid].decision is expected
    if expected is not None:
        row, = broker.audit.recent()
        assert 'hook' in row['source'] and row['decision'] == expected
    else:
        assert rid in broker.pending_signals


def test_late_hook_rejects_recycled_pid(broker, monkeypatch):
    callbacks = install_hooks(broker, monkeypatch, lambda *args: {'verdict': 'allow'})
    monkeypatch.setattr(B, '_read_proc_identity', lambda pid: ('/bin/tool', 43))
    monkeypatch.setattr(B, '_read_proc_uid', lambda pid: 2000)
    rid = broker._enqueue(2000, 0, '/bin/tool', 42, 'hook', {}, delegated=False)
    drain_until(callbacks, broker, rid)
    assert broker._pending[rid].decision is False


def test_partial_hook_frame_has_whole_operation_deadline(tmp_path):
    path = str(tmp_path / 'hook.sock')
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    finished = threading.Event()
    def serve():
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            try:
                for byte in struct.pack('!I', 100) + b'x' * 100:
                    conn.sendall(bytes([byte]))
                    if finished.wait(.025):
                        break
            except BrokenPipeError:
                pass
    worker = threading.Thread(target=serve)
    worker.start()
    try:
        start = time.monotonic()
        assert HookClient(path, timeout_s=.12).query('slow', {}) is None
        assert time.monotonic() - start < .3
    finally:
        finished.set()
        worker.join(1)
        server.close()
    assert not worker.is_alive()


def test_real_audit_writer_contention_is_bounded_and_denies(broker, monkeypatch):
    broker.cache.store(2000, 'review.audit', '/bin/tool', 'forever_exe', True, 1000)
    broker.set_peer(2000, exe='/bin/tool')
    monkeypatch.setattr(B, 'AUDIT_REQUIRED', True)
    competitor = sqlite3.connect(broker.audit.db_path)
    competitor.execute('BEGIN IMMEDIATE')
    try:
        start = time.monotonic()
        assert broker.CheckPermission('review.audit', {}) == 'deny'
        assert time.monotonic() - start < .2
    finally:
        competitor.rollback()
        competitor.close()


def test_rule_installed_during_hook_wins(broker, monkeypatch, tmp_path):
    callbacks = install_hooks(broker, monkeypatch, lambda *args: {'verdict': 'allow'})
    monkeypatch.setattr(B, '_read_proc_identity', lambda pid: ('/bin/tool', 42))
    monkeypatch.setattr(B, '_read_proc_uid', lambda pid: 2000)
    rid = broker._enqueue(2000, 0, '/bin/tool', 42, 'hook', {}, delegated=False)
    (tmp_path / 'rules' / 'deny.yaml').write_text('- name: deny\n  decision: deny\n  match:\n    action: hook\n')
    broker.rules.reload()
    drain_until(callbacks, broker, rid)
    assert broker._pending[rid].decision is False
    row, = broker.audit.recent()
    assert 'rule' in row['source'] and row['decision'] is False


def test_hook_cannot_authorize_changed_lineage(broker, monkeypatch):
    callbacks = install_hooks(broker, monkeypatch, lambda *args: {'verdict': 'allow'})
    monkeypatch.setattr(B, '_read_proc_identity', lambda pid: ('/bin/tool', 42))
    monkeypatch.setattr(B, '_read_proc_uid', lambda pid: 2000)
    monkeypatch.setattr(broker, '_lineage_selectors', lambda *args: ('engine', 'old.app'))
    rid = broker._enqueue(2000, 0, '/bin/tool', 42, 'hook', {}, delegated=False)
    monkeypatch.setattr(broker, '_lineage_selectors', lambda *args: ('engine', 'new.app'))
    drain_until(callbacks, broker, rid)
    assert broker._pending[rid].decision is False


def test_glib_dispatches_cached_caller_while_hook_is_blocked(broker, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def query(*args):
        entered.set()
        assert release.wait(2)
        return {'verdict': 'allow'}
    broker.hooks = SimpleNamespace(enabled=True, query=query)
    broker._hook_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    broker._hook_slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(B, '_read_proc_identity', lambda pid: ('/bin/tool', 42))
    monkeypatch.setattr(B, '_read_proc_uid', lambda pid: 2000)
    broker.cache.store(2000, 'cached', '/bin/tool', 'forever_exe', True, 1000)
    broker.set_peer(2000, exe='/bin/tool')
    loop = B.GLib.MainLoop()
    results = []
    original = broker._apply_hook
    def complete(rid, future):
        try:
            return original(rid, future)
        finally:
            loop.quit()
    monkeypatch.setattr(broker, '_apply_hook', complete)
    rid = broker._enqueue(2000, 0, '/bin/tool', 42, 'slow', {}, delegated=False)
    assert entered.wait(1)
    def second_caller():
        start = time.monotonic()
        results.append((broker.CheckPermission('cached', {}), time.monotonic() - start))
        release.set()
        return False
    B.GLib.idle_add(second_caller)
    expired = []
    def expire():
        expired.append(True)
        release.set()
        loop.quit()
        return False
    timer = B.GLib.timeout_add(1000, expire)
    try:
        loop.run()
        assert not expired
        assert results[0][0] == 'allow' and results[0][1] < .2
        assert broker._pending[rid].decision is True
    finally:
        release.set()
        if not expired:
            B.GLib.source_remove(timer)
