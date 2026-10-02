"""Receipt ownership, bounded admission, and broker-mediated SDK contract."""
from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import qdistro_app as sdk
from qdistro_app import app_receiver
from qdistro_app.transfers import MAX_RECEIPTS, RECEIPT_TTL_S, TransferController


def capabilities(**overrides):
    return {"kinds": ["text/plain", "text/markdown"], "max_bytes": 8,
            "encoding": "utf-8", "confirmation_required": True,
            "available": True, "reason": "", **overrides}


@pytest.fixture
def controller():
    admitted = []
    completions = []
    clock = [0.0]
    def admit(kind, payload, complete):
        admitted.append((kind, payload))
        completions.append(complete)
        return {"state": "staged", "reason": "awaiting user"}
    instance = TransferController(capabilities, admit, clock=lambda: clock[0])
    return instance, admitted, completions, clock


def test_staged_is_not_applied_and_first_terminal_wins(controller):
    receiver, admitted, completions, _ = controller
    receipt = receiver.receive(receiver.instance_id, "text/plain", "secret", ":1.4")
    assert admitted == [("text/plain", "secret")]
    assert receipt["state"] == "staged" and receipt["transfer_id"]
    assert set(receipt) == {"version", "instance_id", "transfer_id", "state", "reason"}
    assert "secret" not in json.dumps(receipt)
    assert receiver.status(receiver.instance_id, receipt["transfer_id"], ":1.4") == receipt
    assert completions[0]("applied", "inserted in editor")
    assert not completions[0]("failed", "late callback")
    terminal = receiver.status(receiver.instance_id, receipt["transfer_id"], ":1.4")
    assert terminal["state"] == "applied" and terminal["reason"] == "inserted in editor"


@pytest.mark.parametrize("state", ["staged", "rejected", "unknown"])
def test_completion_only_accepts_terminal_application_outcomes(controller, state):
    receiver, _, completions, _ = controller
    receipt = receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")
    with pytest.raises(ValueError, match="completion state"):
        completions[0](state)
    assert receiver.status(receiver.instance_id, receipt["transfer_id"], ":1.4")["state"] == "staged"


@pytest.mark.parametrize("result", [{"state": "staged"}, {"state": "rejected"}, RuntimeError("payload detail")])
def test_synchronous_completion_cannot_be_overwritten(result):
    def admit(kind, payload, complete):
        complete("applied", "inserted")
        if isinstance(result, Exception):
            raise result
        return result
    receiver = TransferController(capabilities, admit)
    receipt = receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")
    assert receipt["state"] == "applied" and receipt["reason"] == "inserted"


@pytest.mark.parametrize("kind,payload,reason", [
    ("image/png", "text", "kind"), ("text/plain", "123456789", "byte limit"),
    ("text/plain", "é" * 5, "byte limit"), ("text/plain", "nul\x00", "NUL"),
    ("text/plain", "\ud800", "UTF-8"),
])
def test_invalid_transfer_never_reaches_callback(controller, kind, payload, reason):
    receiver, admitted, _, _ = controller
    receipt = receiver.receive(receiver.instance_id, kind, payload, ":1.4")
    assert receipt["state"] == "rejected" and reason in receipt["reason"], receipt
    assert admitted == [] and len(receiver._receipts) == 0


def test_exact_utf8_limit_is_accepted(controller):
    receiver, admitted, _, _ = controller
    assert receiver.receive(receiver.instance_id, "text/plain", "é" * 4, ":1.4")["state"] == "staged"
    assert admitted == [("text/plain", "é" * 4)]


@pytest.mark.parametrize("overrides", [
    {"max_bytes": True}, {"max_bytes": 1024 * 1024 + 1}, {"max_bytes": 0},
    {"kinds": ["text/*"]}, {"kinds": "text/plain"}, {"kinds": []},
    {"confirmation_required": 1}, {"available": "yes"}, {"encoding": "latin-1"},
    {"version": 2}, {"version": True},
])
def test_invalid_capabilities_never_imply_acceptance(overrides):
    admitted = []
    receiver = TransferController(lambda: capabilities(**overrides),
                                  lambda *args: admitted.append(args))
    assert receiver.capabilities()["version"] == 0
    assert receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")["state"] == "unknown"
    assert admitted == []


def test_unavailable_receiver_refuses_synchronously():
    admitted = []
    receiver = TransferController(lambda: capabilities(available=False, reason="not ready"),
                                  lambda *args: admitted.append(args))
    receipt = receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")
    assert receipt["state"] == "rejected" and receipt["reason"] == "not ready"
    assert admitted == []


def test_receipt_scoped_to_authenticated_sender_and_receiver_instance(controller):
    receiver, admitted, _, _ = controller
    receipt = receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")
    assert receiver.status(receiver.instance_id, receipt["transfer_id"], ":1.5")["state"] == "unknown"
    assert receiver.status("different-instance", receipt["transfer_id"], ":1.4")["state"] == "unknown"
    assert receiver.status(receiver.instance_id, "missing", ":1.4")["state"] == "unknown"
    assert receiver.receive("different-instance", "text/plain", "text", ":1.4")["state"] == "unknown"
    assert receiver.receive(receiver.instance_id, "text/plain", "text", "")["state"] == "rejected"
    assert len(admitted) == 1
    restarted = TransferController(capabilities, lambda *args: {"state": "staged"})
    assert restarted.instance_id != receiver.instance_id
    assert restarted.status(receiver.instance_id, receipt["transfer_id"], ":1.4")["state"] == "unknown"


def test_expiration_cannot_evict_outstanding_staged_obligations(controller):
    receiver, admitted, completions, clock = controller
    receipts = [receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")
                for _ in range(MAX_RECEIPTS)]
    assert all(row["state"] == "staged" for row in receipts)
    clock[0] = RECEIPT_TTL_S + 1
    assert receiver.status(receiver.instance_id, receipts[0]["transfer_id"], ":1.4")["state"] == "unknown"
    refused = receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")
    assert refused["state"] == "rejected" and "capacity" in refused["reason"]
    assert len(admitted) == MAX_RECEIPTS and len(receiver._receipts) == MAX_RECEIPTS
    completions[0]("declined")
    assert receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")["state"] == "staged"
    assert len(receiver._receipts) == MAX_RECEIPTS
    assert receiver.status(receiver.instance_id, receipts[0]["transfer_id"], ":1.4")["state"] == "unknown"


def test_terminal_receipts_may_be_evicted_without_evicting_staged(controller):
    receiver, _, completions, _ = controller
    receipts = [receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")
                for _ in range(MAX_RECEIPTS)]
    completions[-1]("applied")
    assert receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")["state"] == "staged"
    assert receiver.status(receiver.instance_id, receipts[-1]["transfer_id"], ":1.4")["state"] == "unknown"
    assert receiver.status(receiver.instance_id, receipts[0]["transfer_id"], ":1.4")["state"] == "staged"


def test_competing_thread_completions_cannot_change_first_terminal(controller):
    receiver, _, completions, _ = controller
    receipt = receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")
    barrier = threading.Barrier(3)
    def complete(state):
        barrier.wait(timeout=5)
        return completions[0](state)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(complete, state) for state in ("applied", "declined")]
        barrier.wait(timeout=5)
        assert sum(f.result(timeout=5) for f in futures) == 1
    final = receiver.status(receiver.instance_id, receipt["transfer_id"], ":1.4")
    assert final["state"] in {"applied", "declined"}
    assert not completions[0]("failed")
    assert receiver.status(receiver.instance_id, receipt["transfer_id"], ":1.4") == final


@pytest.mark.parametrize("result", [None, {"state": "applied"}, {"state": []}, RuntimeError("secret")])
def test_callback_failure_is_terminal_without_leaking_exception_payload(result):
    def admit(*args):
        if isinstance(result, Exception):
            raise result
        return result
    receiver = TransferController(capabilities, admit)
    receipt = receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")
    assert receipt["state"] == "failed"
    assert "secret" not in json.dumps(receipt)


@pytest.fixture
def receiver(monkeypatch):
    monkeypatch.setattr(sdk.dbus.service, "BusName", lambda *args, **kwargs: object())
    monkeypatch.setattr(sdk.dbus.service.Object, "__init__", lambda *args: None)
    admitted = []
    completions = []
    def admit(kind, payload, complete):
        admitted.append((kind, payload))
        completions.append(complete)
        return {"state": "staged"}
    obj = sdk.AppReceiver("org.qdistro.Test.uid1000", lambda *args: None,
                          bus=object(), transfer_capabilities=capabilities, on_transfer=admit)
    return obj, admitted, completions


def test_app1_transfer_does_not_broadcast_payload_or_change_legacy_arrival(receiver, monkeypatch):
    obj, admitted, completions = receiver
    monkeypatch.setattr(obj, "PayloadReceived", lambda *args: pytest.fail("new receipt broadcast payload"))
    contract = json.loads(obj.GetTransferCapabilities())
    receipt = json.loads(obj.ReceiveTransfer(contract["instance_id"], "text/plain", "secret", sender=":1.4"))
    assert receipt["state"] == "staged" and admitted == [("text/plain", "secret")]
    assert obj.last_received is None and obj.GetLastReceived() == ""
    completions[0]("applied")
    assert json.loads(obj.GetTransferStatus(contract["instance_id"], receipt["transfer_id"], sender=":1.4"))["state"] == "applied"
    assert json.loads(obj.GetTransferStatus(contract["instance_id"], receipt["transfer_id"], sender=":1.5"))["state"] == "unknown"
    assert obj.ReceiveTransfer._dbus_sender_keyword == "sender"
    assert obj.GetTransferStatus._dbus_sender_keyword == "sender"


def test_register_app_forwards_receipt_opt_in(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "fake")
    monkeypatch.setattr(app_receiver, "is_session_bus_available", lambda: True)
    claimed = []
    def factory(**kwargs):
        claimed.append(kwargs)
        return "registered"
    monkeypatch.setattr(app_receiver, "AppReceiver", factory)
    callback = lambda *args: {"state": "staged"}
    assert app_receiver.register_app("Test", transfer_capabilities=capabilities, on_transfer=callback,
                                     install_glib_mainloop=False) == "registered"
    assert claimed[0]["transfer_capabilities"] is capabilities
    assert claimed[0]["on_transfer"] is callback


class BrokerProxy:
    def __init__(self):
        self.calls = []
        self.reply = "{}"
        self.error = None

    def __getattr__(self, name):
        def invoke(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self.error:
                raise self.error
            return self.reply
        return invoke


@pytest.fixture
def broker(monkeypatch):
    proxy = BrokerProxy()
    class Bus:
        def get_object(self, name, path):
            assert name == "org.qdistro.AdminBroker1" and path == "/org/qdistro/AdminBroker1"
            return proxy
    monkeypatch.setattr(sdk.dbus, "SystemBus", Bus)
    return proxy


def test_sdk_client_wire_arguments_and_timeouts(broker):
    broker.reply = json.dumps(capabilities(version=1, instance_id="receiver-instance"))
    contract = sdk.get_transfer_capabilities(3000, "org.qdistro.Test.uid3000")
    assert contract["version"] == 1
    assert broker.calls[-1] == ("GetTransferCapabilities", (3000, "org.qdistro.Test.uid3000"),
                               {"dbus_interface": "org.qdistro.AdminBroker1", "timeout": 3.0})
    receipt = {"version": 1, "instance_id": "receiver-instance", "transfer_id": "broker-handle",
               "state": "staged", "reason": ""}
    broker.reply = json.dumps(receipt)
    assert sdk.send_transfer(3000, "org.qdistro.Test.uid3000", "receiver-instance", "text/plain", "text", timeout=17) == receipt
    assert broker.calls[-1] == ("RelayTransfer", (3000, "org.qdistro.Test.uid3000", "receiver-instance", "text/plain", "text"),
                               {"dbus_interface": "org.qdistro.AdminBroker1", "timeout": 17.0})
    receipt["state"] = "applied"
    broker.reply = json.dumps(receipt)
    assert sdk.get_transfer_status("broker-handle") == receipt
    assert broker.calls[-1] == ("GetTransferStatus", ("broker-handle",),
                               {"dbus_interface": "org.qdistro.AdminBroker1", "timeout": 3.0})


@pytest.mark.parametrize("error", [TimeoutError("reply timeout"), sdk.dbus.DBusException("disconnected")])
def test_sdk_unconfirmed_send_is_unknown_and_never_retried(broker, error):
    broker.error = error
    receipt = sdk.send_transfer(3000, "org.qdistro.Test.uid3000", "instance", "text/plain", "text")
    assert receipt["state"] == "unknown"
    assert len(broker.calls) == 1 and broker.calls[0][0] == "RelayTransfer"
    assert sdk.get_transfer_status("known-handle")["state"] == "unknown"
    assert sdk.get_transfer_capabilities(3000, "org.qdistro.Test.uid3000")["version"] == 0
    assert [call[0] for call in broker.calls] == ["RelayTransfer", "GetTransferStatus", "GetTransferCapabilities"]


@pytest.mark.parametrize("reply", ["bad json", "[]", '{"version":true,"state":"applied"}',
                                    '{"version":1,"state":[],"instance_id":"x","transfer_id":"y"}',
                                    '{"version":1,"state":"applied","instance_id":"other","transfer_id":"handle"}'])
def test_malformed_or_wrong_instance_reply_never_claims_application(broker, reply):
    broker.reply = reply
    assert sdk.send_transfer(3000, "org.qdistro.Test.uid3000", "instance", "text/plain", "text")["state"] == "unknown"


def test_menu_capabilities_are_broker_queried_for_both_uid_cases(monkeypatch):
    own_uid = os.geteuid()
    queried = []
    rows = [(own_uid, "org.qdistro.Local", "Local"), (own_uid + 1, "org.qdistro.Peer", "Peer")]
    monkeypatch.setattr(app_receiver, "list_receivers", lambda: rows)
    monkeypatch.setattr(app_receiver, "_probe_silo", lambda *args: "")
    def probe(uid, service):
        queried.append((uid, service))
        return sdk.normalize_capabilities(capabilities(version=1, instance_id=service))
    monkeypatch.setattr(app_receiver, "get_transfer_capabilities", probe)
    monkeypatch.setattr(app_receiver, "_probe_can_receive", lambda *args: pytest.fail("known contract used legacy probe"))
    menu = app_receiver.send_to_menu_targets(kind="text/plain")
    assert queried == [(r[0], r[1]) for r in rows]
    assert len(menu) == 2 and all(row["capability_state"] == "known" for row in menu)
    assert app_receiver.send_to_menu_targets(kind="image/png") == []


def test_unknown_menu_candidate_never_claims_versioned_capability(monkeypatch):
    monkeypatch.setattr(app_receiver, "list_receivers", lambda: [(3000, "org.qdistro.Legacy", "Legacy")])
    monkeypatch.setattr(app_receiver, "_probe_silo", lambda *args: "")
    monkeypatch.setattr(app_receiver, "_probe_can_receive", lambda *args: True)
    monkeypatch.setattr(app_receiver, "get_transfer_capabilities", lambda *args: sdk.unknown_capabilities())
    row = app_receiver.send_to_menu_targets(kind="text/plain")[0]
    assert row["capability_state"] == "unknown" and row["capabilities"]["version"] == 0
    assert "kinds" not in row["capabilities"]


@pytest.mark.integration
def test_real_app1_session_bus_scopes_receipts_to_authenticated_connection(tmp_path):
    """The bus supplies sender identity; a second connection cannot query it."""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    executable = shutil.which("dbus-run-session")
    assert executable, "dbus-run-session is required for App1 sender isolation coverage"
    script = r'''
import json
import os
import threading
import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from qdistro_app import APP1_IFACE, APP1_OBJ_PATH, AppReceiver
DBusGMainLoop(set_as_default=True)
loop = GLib.MainLoop()
ready = threading.Event()
server = dbus.SessionBus(private=True)
completed = []
capabilities = lambda: {"kinds": ["text/plain"], "max_bytes": 8, "encoding": "utf-8",
                       "confirmation_required": True, "available": True, "reason": ""}
def admit(kind, payload, complete):
    completed.append(complete)
    return {"state": "staged"}
receiver = AppReceiver("org.qdistro.TransferTest.uid1000", lambda *args: None, bus=server,
                       transfer_capabilities=capabilities, on_transfer=admit)
GLib.idle_add(lambda: ready.set() or False)
thread = threading.Thread(target=loop.run)
thread.start()
assert ready.wait(5), "service loop did not become ready"
client = dbus.bus.BusConnection(os.environ["DBUS_SESSION_BUS_ADDRESS"])
other = dbus.bus.BusConnection(os.environ["DBUS_SESSION_BUS_ADDRESS"])
def proxy(connection):
    return dbus.Interface(connection.get_object("org.qdistro.TransferTest.uid1000", APP1_OBJ_PATH), APP1_IFACE)
try:
    original = proxy(client)
    foreign = proxy(other)
    caps = json.loads(original.GetTransferCapabilities(timeout=3))
    receipt = json.loads(original.ReceiveTransfer(caps["instance_id"], "text/plain", "text", timeout=3))
    assert receipt["state"] == "staged", receipt
    assert json.loads(foreign.GetTransferStatus(caps["instance_id"], receipt["transfer_id"], timeout=3))["state"] == "unknown"
    assert json.loads(original.GetTransferStatus(caps["instance_id"], receipt["transfer_id"], timeout=3))["state"] == "staged"
    assert completed[0]("applied", "editor insertion")
    assert json.loads(original.GetTransferStatus(caps["instance_id"], receipt["transfer_id"], timeout=3))["state"] == "applied"
    assert json.loads(foreign.GetTransferStatus(caps["instance_id"], receipt["transfer_id"], timeout=3))["state"] == "unknown"
    print("authenticated sender isolation and application completion passed")
finally:
    GLib.idle_add(loop.quit)
    thread.join(timeout=5)
    assert not thread.is_alive(), "service loop did not stop"
    receiver.remove_from_connection()
    client.close()
    other.close()
'''
    env = dict(os.environ, PYTHONPATH=str(Path(sdk.__file__).resolve().parents[1]))
    result = subprocess.run([executable, sys.executable, "-c", script], env=env,
                            text=True, capture_output=True, timeout=15)
    evidence = tmp_path / "private-bus-evidence.txt"
    evidence.write_text(result.stdout + result.stderr)
    assert result.returncode == 0, f"private App1 bus failed: {result.stdout}\n{result.stderr}"
    assert "authenticated sender isolation and application completion passed" in result.stdout


def test_async_menu_candidates_do_not_probe_any_receiver(monkeypatch):
    monkeypatch.setattr(app_receiver, "list_receivers", lambda: [(1000, "org.qdistro.Test", "Test")])
    for method in ("get_transfer_capabilities", "_probe_silo", "_probe_can_receive"):
        monkeypatch.setattr(app_receiver, method, lambda *args: pytest.fail("per-target GUI probe"))
    row = app_receiver.send_to_menu_targets(kind="text/plain", probe_capabilities=False)[0]
    assert row["service"] == "org.qdistro.Test"
    assert row["capability_state"] == "unknown" and row["capabilities"]["version"] == 0
    assert row["silo"] == ""


def test_receiver_without_opt_in_remains_unknown(receiver):
    obj, _, _ = receiver
    obj._transfers = TransferController()
    assert json.loads(obj.GetTransferCapabilities())["version"] == 0
    assert json.loads(obj.ReceiveTransfer(obj._transfers.instance_id, "text/plain", "text", sender=":1.4"))["state"] == "unknown"


def test_receiver_rejection_is_an_explicit_terminal_receipt():
    receiver = TransferController(capabilities, lambda *args: {"state": "rejected", "reason": "inbox full"})
    receipt = receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4")
    assert receipt["state"] == "rejected" and receipt["reason"] == "inbox full"
    assert receiver.status(receiver.instance_id, receipt["transfer_id"], ":1.4") == receipt


@pytest.mark.parametrize("reply", [" " * 8193 + "{}", "é" * 4097,
                                    '{"version":1,"version":0}',
                                    '{"version":NaN}',
                                    '{"version":1,"state":"applied","instance_id":"bad\\u0000instance","transfer_id":"handle"}'])
def test_untrusted_wire_metadata_is_bounded_and_strict(broker, reply):
    broker.reply = reply
    assert sdk.get_transfer_capabilities(3000, "org.qdistro.Test")["version"] == 0
    assert sdk.send_transfer(3000, "org.qdistro.Test", "instance", "text/plain", "text")["state"] == "unknown"
    assert sdk.get_transfer_status("handle")["state"] == "unknown"


def test_capability_identities_and_sender_names_reject_controls(controller):
    assert sdk.normalize_capabilities(capabilities(version=1, instance_id="bad\x00instance"))["version"] == 0
    assert sdk.normalize_capabilities(capabilities(version=1, instance_id="bad\ud800instance"))["version"] == 0
    receiver, admitted, _, _ = controller
    assert receiver.receive(receiver.instance_id, "text/plain", "text", ":1.4\x00")["state"] == "rejected"
    assert admitted == []
