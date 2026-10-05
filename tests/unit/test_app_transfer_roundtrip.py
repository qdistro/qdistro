"""Actual transfer handlers across broker, relay, SDK and a Qt editor.

Only the two D-Bus transports are in-process fakes. These tests do not claim
installed system/session-bus policy or VM GUI acceptance.
"""
from __future__ import annotations

import json
import threading

import dbus
import pytest
import qdistro_admin_broker as B
import qdistro_app as sdk
import qdistro_user_relay as R
from PyQt6.QtWidgets import QMainWindow, QTextEdit
from qdistro_app.transfers import TransferController
from qnotebook import qdistro_integration as notebook
from test_send_to_broker import _StubBroker


class _Broker(_StubBroker):
    def _peer_info(self, sender, conn):
        if sender == ":admin":
            return B.ADMIN_UID, 100, "/usr/bin/admin", 1
        return 2000, 201, "/usr/bin/qfileman", 2


class _Receiver(sdk.AppReceiver):
    def __init__(self, dispatcher):
        self._transfers = TransferController(dispatcher.capabilities, dispatcher.stage)


class _Relay(R.UserRelay):
    def __init__(self, bus, uid):
        self._bus, self._uid = bus, uid


class _RootConnection:
    def get_unix_user(self, sender):
        assert sender == ":broker"
        return 0


class _ReceiverProxy:
    def __init__(self, receiver):
        self.receiver = receiver
        self.receive_count = 0

    def GetTransferCapabilities(self, **kwargs):
        return self.receiver.GetTransferCapabilities()

    def ReceiveTransfer(self, instance, kind, payload, **kwargs):
        self.receive_count += 1
        return self.receiver.ReceiveTransfer(instance, kind, payload, sender=":relay.session")

    def GetTransferStatus(self, instance, token, **kwargs):
        return self.receiver.GetTransferStatus(instance, token, sender=":relay.session")


class _SessionBus:
    def __init__(self, service, proxy):
        self.service, self.proxy, self.owner = service, proxy, ":receiver.1"

    def get_name_owner(self, service):
        assert service == self.service
        return self.owner

    def get_object(self, owner, path, **kwargs):
        assert owner == ":receiver.1", "relay must address pinned unique receiver"
        assert path == sdk.AppReceiver.OBJ_PATH
        return self.proxy


class _RelayProxy:
    def __init__(self, relay):
        self.relay = relay

    def __getattr__(self, name):
        def invoke(*args, **kwargs):
            return getattr(self.relay, name)(*args, sender=":broker", conn=_RootConnection())
        return invoke


class _BrokerProxy:
    def __init__(self, broker):
        self.broker = broker

    def GetTransferCapabilities(self, *args, **kwargs):
        return self.broker.GetTransferCapabilities(*args, sender=":caller", conn=None)

    def RelayTransfer(self, *args, **kwargs):
        replies, errors = [], []
        self.broker.RelayTransfer(*args, replies.append, errors.append, sender=":caller", conn=None)
        if self.broker._pending:
            assert not replies, "cross-UID transfer must wait for approval"
            rid = next(iter(self.broker._pending))
            self.broker.DecideRequest(rid, "allow", "once", sender=":admin", conn=None)
        if errors:
            raise errors[0]
        assert len(replies) == 1
        return replies[0]

    def GetTransferStatus(self, *args, **kwargs):
        return self.broker.GetTransferStatus(*args, sender=":caller", conn=None)


class _SystemBus:
    def __init__(self, broker, relay, uid):
        self.broker, self.relay, self.uid = broker, relay, uid
        self.owner = ":relay.system.1"

    def get_name_owner(self, service):
        assert service == f"org.qdistro.UserRelay.uid{self.uid}"
        return self.owner

    def get_object(self, owner, path, **kwargs):
        if owner == B.BUS_NAME:
            return self.broker
        assert owner == ":relay.system.1", "broker must address pinned unique relay"
        assert path == R.OBJ_PATH
        return self.relay


@pytest.fixture
def route(qapp, tmp_path, monkeypatch, request):
    uid = request.param
    window = QMainWindow()
    window.editor = QTextEdit(window)
    window.setCentralWidget(window.editor)
    window.editor.setPlainText("Existing note\n")
    window._current_page = "Page.md"
    window._qdistro_autoconfirm = True
    dispatcher = notebook.NotebookTransfers(window)
    window._qdistro_transfers = dispatcher
    receiver = _Receiver(dispatcher)
    receiver_proxy = _ReceiverProxy(receiver)
    service = f"org.qdistro.QNotebook.uid{uid}"
    session = _SessionBus(service, receiver_proxy)
    relay = _Relay(session, uid)
    rules = tmp_path / "rules"
    rules.mkdir()
    broker = _Broker(str(tmp_path / "cache.sqlite"), str(tmp_path / "audit.sqlite"), str(rules))
    system = _SystemBus(_BrokerProxy(broker), _RelayProxy(relay), uid)
    monkeypatch.setattr(dbus, "SystemBus", lambda: system)
    # DecideRequest's TOCTOU re-check reads live /proc for the pending
    # caller's pid; pin the seam to the stubbed peer identity so the fake
    # pid 201 does not collide with a real host process (CallerGone).
    identities = {100: ("/usr/bin/admin", 1), 201: ("/usr/bin/qfileman", 2)}
    monkeypatch.setattr(B, "_read_proc_identity",
                        lambda pid: identities.get(int(pid), ("?", 0)))
    yield window, broker, system, session, receiver_proxy, uid, service
    dispatcher.shutdown()
    qapp.processEvents()
    window.close()


@pytest.mark.integration
@pytest.mark.parametrize("route", [2000, 3000], indirect=True)
def test_staged_then_applied_means_editor_insertion(route, qapp):
    window, broker, _, _, proxy, uid, service = route
    capabilities = sdk.get_transfer_capabilities(uid, service)
    assert capabilities["max_bytes"] == 256 * 1024
    assert capabilities["confirmation_required"] is True
    payload = "Exact UTF-8 text: Žluťoučký\n"
    before = window.editor.toPlainText()
    result = []
    # Delivery originates outside the Qt event thread. A queued pump must own
    # confirmation, with no modal dialog or insertion before the staged reply.
    thread = threading.Thread(target=lambda: result.append(sdk.send_transfer(
        uid, service, capabilities["instance_id"], "text/plain", payload)))
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive(), "staging blocked on GUI confirmation"
    assert result[0]["state"] == "staged", result
    assert window.editor.toPlainText() == before
    qapp.processEvents()
    receipt = sdk.get_transfer_status(result[0]["transfer_id"])
    assert receipt["state"] == "applied", receipt
    assert payload in window.editor.toPlainText()
    assert "saving is separate" in receipt["reason"]
    assert proxy.receive_count == 1
    assert len(broker.pending_signals) == (1 if uid == 3000 else 0)
    assert not window._qdistro_inbox


@pytest.mark.integration
@pytest.mark.parametrize("route", [2000], indirect=True)
@pytest.mark.parametrize("confirmation,expected", [(False, "declined"), (True, "failed")])
def test_decline_and_missing_page_never_report_applied(route, qapp, confirmation, expected):
    window, _, _, _, proxy, uid, service = route
    window._qdistro_autoconfirm = confirmation
    if confirmation:
        window._current_page = None
    before = window.editor.toPlainText()
    capabilities = sdk.get_transfer_capabilities(uid, service)
    receipt = sdk.send_transfer(uid, service, capabilities["instance_id"], "text/plain", "Payload")
    assert receipt["state"] == "staged", receipt
    qapp.processEvents()
    assert sdk.get_transfer_status(receipt["transfer_id"])["state"] == expected
    assert window.editor.toPlainText() == before
    assert proxy.receive_count == 1


@pytest.mark.integration
@pytest.mark.parametrize("route", [2000], indirect=True)
def test_relay_restart_makes_receipt_unknown_without_second_delivery(route, qapp):
    _, _, system, _, proxy, uid, service = route
    capabilities = sdk.get_transfer_capabilities(uid, service)
    receipt = sdk.send_transfer(uid, service, capabilities["instance_id"], "text/plain", "Payload")
    assert receipt["state"] == "staged", receipt
    qapp.processEvents()
    system.owner = ":relay.system.2"
    assert sdk.get_transfer_status(receipt["transfer_id"])["state"] == "unknown"
    assert proxy.receive_count == 1


@pytest.mark.integration
@pytest.mark.parametrize("route", [2000, 3000], indirect=True)
def test_multibyte_limit_is_measured_before_delivery(route, qapp):
    window, _, _, _, proxy, uid, service = route
    caps = sdk.get_transfer_capabilities(uid, service)
    at_limit = "ž" * (caps["max_bytes"] // 2)
    oversized = sdk.send_transfer(uid, service, caps["instance_id"], "text/plain", at_limit + "ž")
    assert oversized["state"] == "rejected", oversized
    assert proxy.receive_count == 0
    receipt = sdk.send_transfer(uid, service, caps["instance_id"], "text/plain", at_limit)
    assert receipt["state"] == "staged", receipt
    qapp.processEvents()
    assert sdk.get_transfer_status(receipt["transfer_id"])["state"] == "applied"
    assert at_limit in window.editor.toPlainText()
    assert proxy.receive_count == 1


@pytest.mark.integration
@pytest.mark.parametrize("route", [2000], indirect=True)
def test_full_inbox_rejects_before_staging_without_losing_existing_work(route, qapp):
    window, _, _, _, proxy, uid, service = route
    caps = sdk.get_transfer_capabilities(uid, service)
    handles = []
    for index in range(notebook.MAX_PENDING_DROPS):
        receipt = sdk.send_transfer(uid, service, caps["instance_id"], "text/plain", f"Payload {index}")
        assert receipt["state"] == "staged", receipt
        handles.append(receipt["transfer_id"])
    assert sdk.get_transfer_capabilities(uid, service)["available"] is False
    overflow = sdk.send_transfer(uid, service, caps["instance_id"], "text/plain", "Overflow")
    assert overflow["state"] == "rejected", overflow
    assert proxy.receive_count == notebook.MAX_PENDING_DROPS
    qapp.processEvents()
    assert all(sdk.get_transfer_status(handle)["state"] == "applied" for handle in handles)
    assert "Overflow" not in window.editor.toPlainText()
    assert not window._qdistro_inbox


@pytest.mark.integration
@pytest.mark.parametrize("route", [2000, 3000], indirect=True)
@pytest.mark.parametrize("suffix", [',"state":"applied"', ',"unused":NaN',
                                    ',"nested":{"key":1,"key":2}'])
def test_malformed_receiver_receipt_is_unknown_through_relay_broker_and_sdk(route, monkeypatch, suffix):
    window, _, _, _, proxy, uid, service = route
    caps = sdk.get_transfer_capabilities(uid, service)
    before = window.editor.toPlainText()
    raw = json.dumps({"version": 1, "instance_id": caps["instance_id"],
                      "transfer_id": "receiver-id", "state": "failed", "reason": ""})[:-1] + suffix + '}'
    calls = []
    def receive(*args, **kwargs):
        calls.append(args)
        return raw
    monkeypatch.setattr(proxy, "ReceiveTransfer", receive)
    receipt = sdk.send_transfer(uid, service, caps["instance_id"], "text/plain", "Payload")
    assert receipt["state"] == "unknown", receipt
    assert sdk.get_transfer_status(receipt["transfer_id"])["state"] == "unknown"
    assert len(calls) == 1, "an ambiguous receipt must not trigger another delivery"
    assert window.editor.toPlainText() == before


@pytest.mark.integration
@pytest.mark.parametrize("route", [2000, 3000], indirect=True)
@pytest.mark.parametrize("suffix", [',"state":"applied"', ',"unused":NaN',
                                    ',"nested":{"key":1,"key":2}'])
def test_malformed_status_receipt_cannot_promote_staged_work(route, monkeypatch, suffix):
    _, _, _, _, proxy, uid, service = route
    caps = sdk.get_transfer_capabilities(uid, service)
    receipt = sdk.send_transfer(uid, service, caps["instance_id"], "text/plain", "Payload")
    assert receipt["state"] == "staged", receipt
    queries = []
    def status(instance, receiver_id, **kwargs):
        queries.append(receiver_id)
        return json.dumps({"version": 1, "instance_id": instance, "transfer_id": receiver_id,
                           "state": "failed", "reason": ""})[:-1] + suffix + '}'
    monkeypatch.setattr(proxy, "GetTransferStatus", status)
    for _ in range(2):
        assert sdk.get_transfer_status(receipt["transfer_id"])["state"] == "unknown"
    assert len(queries) == 2
    assert proxy.receive_count == 1, "querying an uncertain disposition must never resend"
