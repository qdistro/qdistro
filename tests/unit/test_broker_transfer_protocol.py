"""New transfer routing never substitutes owners, actors or acceptance outcomes."""
import json
from types import SimpleNamespace

import dbus
import pytest
import qdistro_admin_broker as b
import transfer_protocol as wire
from test_broker_sendto import _StubBroker

CAPS = dict(version=1, instance_id='instance', kinds=['text'], max_bytes=1024,
            encoding='utf-8', available=True, confirmation_required=False, reason='')
SERVICE = 'org.qdistro.Notebook.uid2000'


class Relay:
    def __init__(self):
        self.capabilities = dict(CAPS)
        self.forwarded = []
        self.queries = []
        self.send_error = None
        self.receipt = wire.receipt('instance', 'receiver-secret-id', 'staged', '')
        self.envelope_error = None
    def GetTransferCapabilities(self, service, **kw):
        assert kw['timeout'] == 3.0
        if self.envelope_error:
            raise self.envelope_error
        return json.dumps(dict(owner=':1.8', capabilities=self.capabilities))
    def ForwardTransfer(self, *args, **kw):
        self.forwarded.append(args)
        assert args[:3] == (SERVICE, ':1.8', 'instance') and kw['timeout'] == 3.0
        if self.send_error:
            raise self.send_error
        return json.dumps(self.receipt)
    def GetTransferStatus(self, *args, **kw):
        self.queries.append(args)
        assert kw['timeout'] == 3.0
        return json.dumps(self.receipt)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    rules = tmp_path / 'rules'; rules.mkdir()
    broker = _StubBroker(str(tmp_path/'cache'), str(tmp_path/'audit'), str(rules))
    broker.set_peer(2000, pid=100, exe='/usr/bin/app', start=123)
    relay = Relay()
    class Bus:
        owner = ':1.3'
        objects = []
        def get_name_owner(self, name):
            return self.owner
        def get_object(self, owner, path, **kw):
            assert owner == ':1.3' and kw == {'introspect':False}
            self.objects.append(owner)
            return relay
    bus = Bus()
    monkeypatch.setattr(b.dbus, 'SystemBus', lambda: bus)
    return SimpleNamespace(broker=broker, relay=relay, bus=bus)


def send(setup, **changes):
    results, errors = [], []
    setup.broker.RelayTransfer(changes.get('uid',2000), SERVICE,
                              changes.get('instance','instance'), changes.get('kind','text'),
                              changes.get('payload','hello'), results.append, errors.append)
    return results, errors


def test_opaque_receipt_queries_receiver_without_exposing_routing(setup):
    results, errors = send(setup)
    assert not errors
    result = json.loads(results[0]); token = result['transfer_id']
    assert result['state'] == 'staged' and token != 'receiver-secret-id'
    setup.relay.receipt['state'] = 'applied'
    queried = json.loads(setup.broker.GetTransferStatus(token))
    assert queried['state'] == 'applied' and queried['transfer_id'] == token
    assert setup.relay.queries[0] == (SERVICE, ':1.8', 'instance', 'receiver-secret-id')
    assert 'hello' not in repr(setup.broker._transfer_receipts.rows)


@pytest.mark.parametrize('change', [dict(uid=2001), dict(pid=101), dict(exe='/other'), dict(start=124)])
def test_wrong_actor_denied_before_any_receiver_query(setup, change):
    token = json.loads(send(setup)[0][0])['transfer_id']
    peer = dict(uid=2000,pid=100,exe='/usr/bin/app',start=123); peer.update(change)
    setup.broker.set_peer(**peer)
    before = len(setup.bus.objects)
    with pytest.raises(dbus.DBusException, match='another actor'):
        setup.broker.GetTransferStatus(token)
    assert len(setup.bus.objects) == before and not setup.relay.queries


@pytest.mark.parametrize('cause', ['timeout','malformed','instance_changed'])
def test_discovery_uncertainty_is_unknown_and_never_sent(setup, cause):
    if cause == 'timeout': setup.relay.envelope_error = TimeoutError()
    if cause == 'malformed': setup.relay.capabilities['version'] = True
    if cause == 'instance_changed': setup.relay.capabilities['instance_id'] = 'restarted'
    result = json.loads(send(setup)[0][0])
    assert result['state'] == 'unknown' and not setup.relay.forwarded


@pytest.mark.parametrize('change', [dict(payload='x\x00'), dict(payload='x'*(wire.MAX_BYTES+1)),
                                    dict(kind='url'), dict(payload='x'*1025)])
def test_known_admission_rejection_never_sends(setup, change):
    results, errors = send(setup, **change)
    assert not errors and json.loads(results[0])['state'] == 'rejected'
    assert not setup.relay.forwarded


def test_dispatch_timeout_is_unknown_and_status_never_repeats_send(setup):
    setup.relay.send_error = TimeoutError()
    result = json.loads(send(setup)[0][0])
    assert result['state'] == 'unknown' and len(setup.relay.forwarded) == 1
    assert json.loads(setup.broker.GetTransferStatus(result['transfer_id']))['state'] == 'unknown'
    assert len(setup.relay.forwarded) == 1


def test_relay_restart_never_redirects_status(setup):
    token = json.loads(send(setup)[0][0])['transfer_id']
    setup.bus.owner = ':1.4'
    assert json.loads(setup.broker.GetTransferStatus(token))['state'] == 'unknown'
    assert not setup.relay.queries and len(setup.relay.forwarded) == 1


def test_cross_uid_reuses_one_shot_approval_and_revalidates_actor(setup):
    replies, errors = send(setup, uid=3000)
    assert not replies and not errors and not setup.relay.forwarded
    request = next(iter(setup.broker._pending.values()))
    assert request.one_shot and request.decision is None
    setup.broker.set_peer(2000,pid=100,exe='/usr/bin/app',start=124)
    request.waiters[0][0](True)
    assert json.loads(replies[0])['state'] == 'unknown'
    assert not setup.relay.forwarded


def test_cross_uid_rechecks_active_after_approval(setup):
    replies, _ = send(setup, uid=3000)
    request = next(iter(setup.broker._pending.values()))
    setup.broker._silo_state_override = lambda uid: 'Frozen'
    request.waiters[0][0](True)
    assert json.loads(replies[0])['state'] == 'unknown' and not setup.relay.forwarded


def test_ledger_capacity_keeps_staged_and_expiry_does_not_refresh():
    now = [0]
    ledger = wire.ReceiptLedger(clock=lambda:now[0], capacity=1, ttl=600)
    actor=(1,2,'app',3)
    token,row=ledger.reserve(actor,(), 'instance')
    with pytest.raises(ValueError, match='capacity'):
        ledger.reserve(actor,(), 'instance')
    now[0]=599
    assert ledger.get(token,actor) is row
    now[0]=600
    assert ledger.get(token,actor) is None


def test_terminal_ledger_entry_can_be_evicted():
    ledger=wire.ReceiptLedger(capacity=1)
    actor=(1,2,'app',3)
    old,row=ledger.reserve(actor,(),'instance');row['receipt']['state']='applied'
    new,_=ledger.reserve(actor,(),'instance')
    assert old!=new and ledger.get(old,actor) is None


@pytest.mark.parametrize('mutation', [dict(version=True), dict(state='queued'),
                                      dict(instance_id='restart'), dict(reason='x' * 161)])
def test_malformed_dispatch_ack_is_unknown_and_never_retried(setup, mutation):
    setup.relay.receipt.update(mutation)
    result = json.loads(send(setup)[0][0])
    assert result['state'] == 'unknown'
    queried = json.loads(setup.broker.GetTransferStatus(result['transfer_id']))
    assert queried['state'] == 'unknown'
    assert len(setup.relay.forwarded) == 1 and not setup.relay.queries


def test_receipt_expiry_during_admin_prompt_prevents_dispatch(setup):
    now = [0]
    setup.broker._transfer_receipts = wire.ReceiptLedger(clock=lambda: now[0], ttl=600)
    results, errors = send(setup, uid=3000)
    assert not results and not errors
    req = next(iter(setup.broker._pending.values()))
    now[0] = 600
    req.waiters[0][0](True)
    assert json.loads(results[0])['state'] == 'unknown'
    assert not setup.relay.forwarded
