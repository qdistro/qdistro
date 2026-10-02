"""Transfer methods authenticate root and pin the session connection owner."""
import json
from types import SimpleNamespace

import dbus
import pytest
import qdistro_user_relay as relay_module
import transfer_protocol as wire
from test_broker_transfer_protocol import CAPS, SERVICE
from test_user_relay import _StubUserRelay


class Receiver:
    def __init__(self):
        self.caps = dict(CAPS)
        self.calls = []
        self.result = wire.receipt('instance', 'private', 'staged', '')

    def GetTransferCapabilities(self, **kw):
        assert kw['timeout'] == 3.0
        return json.dumps(self.caps)

    def ReceiveTransfer(self, *args, **kw):
        assert kw['timeout'] == 3.0
        self.calls.append(args)
        return json.dumps(self.result)

    def GetTransferStatus(self, *args, **kw):
        assert kw['timeout'] == 3.0
        self.calls.append(args)
        return json.dumps(self.result)


@pytest.fixture
def setup():
    receiver = Receiver()

    class Bus:
        owner = ':1.8'
        looked_up = []

        def get_name_owner(self, name):
            assert name == SERVICE
            return self.owner

        def get_object(self, owner, path, **kw):
            assert owner == ':1.8' and kw == {'introspect': False}
            assert path == relay_module.APP1_OBJ_PATH
            self.looked_up.append(owner)
            return receiver

    bus = Bus()
    return SimpleNamespace(relay=_StubUserRelay(bus), bus=bus, receiver=receiver,
                           root=SimpleNamespace(get_unix_user=lambda sender: 0))


@pytest.mark.parametrize('method,args', [
    ('GetTransferCapabilities', (SERVICE,)),
    ('ForwardTransfer', (SERVICE, ':1.8', 'instance', 'text', 'hello')),
    ('GetTransferStatus', (SERVICE, ':1.8', 'instance', 'private')),
])
@pytest.mark.parametrize('uid', [1000, 2000])
def test_new_methods_require_authenticated_root_before_session_lookup(setup, method, args, uid):
    conn = SimpleNamespace(get_unix_user=lambda sender: uid)
    with pytest.raises(dbus.DBusException, match='root'):
        getattr(setup.relay, method)(*args, sender=':2.1', conn=conn)
    assert not setup.bus.looked_up


def test_same_session_connection_sends_and_queries_pinned_receiver(setup):
    auth = dict(sender=':2.1', conn=setup.root)
    caps = json.loads(setup.relay.GetTransferCapabilities(SERVICE, **auth))
    assert caps == dict(owner=':1.8', capabilities=CAPS)
    result = json.loads(setup.relay.ForwardTransfer(SERVICE, ':1.8', 'instance', 'text', 'hello', **auth))
    assert result['state'] == 'staged'
    setup.receiver.result['state'] = 'applied'
    status = json.loads(setup.relay.GetTransferStatus(SERVICE, ':1.8', 'instance', 'private', **auth))
    assert status['state'] == 'applied'
    assert setup.receiver.calls == [('instance', 'text', 'hello'), ('instance', 'private')]
    assert setup.bus.looked_up == [':1.8'] * 3


@pytest.mark.parametrize('change', ['owner', 'instance'])
def test_restart_never_redirects_or_sends(setup, change):
    if change == 'owner':
        setup.bus.owner = ':1.9'
    else:
        setup.receiver.caps['instance_id'] = 'restart'
    with pytest.raises(dbus.DBusException, match='changed'):
        setup.relay.ForwardTransfer(SERVICE, ':1.8', 'instance', 'text', 'hello',
                                   sender=':2.1', conn=setup.root)
    assert not setup.receiver.calls


@pytest.mark.parametrize('mutation', [dict(version=True), dict(transfer_id='different'), dict(state='queued')])
def test_malformed_receipt_is_not_application_success(setup, mutation):
    setup.receiver.result.update(mutation)
    with pytest.raises(ValueError):
        setup.relay.GetTransferStatus(SERVICE, ':1.8', 'instance', 'private',
                                     sender=':2.1', conn=setup.root)
