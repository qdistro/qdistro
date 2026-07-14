"""D-Bus-independent authorization tests for the qdshell layout facade."""
from __future__ import annotations

import json
import threading

from multimachine.display_shell_mailbox import (
    DisplayShellInputMailbox,
    DisplayShellMailbox,
)
from multimachine.display_shell_service import (
    DBUS_NAME,
    DBUS_PATH,
    DisplayShellServiceCore,
)
from multimachine.remote_display_slot import ActionKind, SlotAction


def _grant() -> dict:
    return {
        "generation": 9, "session_id": "display-session",
        "slot_name": "rdp-0", "logical_x": 1280, "logical_y": 0,
        "width": 1280, "height": 800, "scale": 1,
        "lease_expires_at": 4_000_000_000,
    }


def test_only_trusted_qdshell_can_claim_or_acknowledge() -> None:
    ready = threading.Event()
    mailbox = DisplayShellMailbox(on_pending=ready.set)
    core = DisplayShellServiceCore(
        mailbox, trusted_sender=lambda sender: sender == ":1.shell")
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            mailbox.perform(
                SlotAction(ActionKind.PRIMARY_ENABLE_OUTPUT), _grant())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=publish)
    worker.start()
    assert ready.wait(1)
    assert core.claim_layout(":1.attacker") == ""
    assert not core.acknowledge_layout(
        ":1.attacker", request_id="x" * 32,
        generation=9, result="applied")
    encoded = core.claim_layout(":1.shell")
    request = json.loads(encoded)
    assert request["slot_name"] == "rdp-0"
    assert core.claim_layout(":1.shell") == ""
    assert not core.acknowledge_layout(
        ":1.shell", request_id=request["request_id"],
        generation=8, result="applied")
    assert core.acknowledge_layout(
        ":1.shell", request_id=request["request_id"],
        generation=9, result="applied")
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert errors == []


def test_empty_sender_and_malformed_ack_fail_closed() -> None:
    mailbox = DisplayShellMailbox()
    core = DisplayShellServiceCore(mailbox, trusted_sender=lambda _sender: True)
    assert core.claim_layout("") == ""
    assert not core.acknowledge_layout(
        "", request_id="a" * 32, generation=1, result="applied")
    assert not core.acknowledge_layout(
        ":1.shell", request_id="a" * 32, generation=1, result="bogus")


def test_bus_identity_is_dedicated_to_display_transactions() -> None:
    assert DBUS_NAME == "org.qdistro.MultiMachineDisplay1"
    assert DBUS_PATH == "/org/qdistro/MultiMachineDisplay1"


def test_only_trusted_qdshell_can_complete_input_gate() -> None:
    ready = threading.Event()
    layout = DisplayShellMailbox()
    input_mailbox = DisplayShellInputMailbox(on_pending=ready.set)
    core = DisplayShellServiceCore(
        layout, input_mailbox=input_mailbox,
        trusted_sender=lambda sender: sender == ":1.shell")
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            input_mailbox.perform(
                SlotAction(ActionKind.PRIMARY_ENABLE_INPUT), _grant())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=publish)
    worker.start()
    assert ready.wait(1)
    assert core.claim_input(":1.attacker") == ""
    encoded = core.claim_input(":1.shell")
    request = json.loads(encoded)
    assert request["enabled"] is True
    assert not core.acknowledge_input(
        ":1.attacker", request_id=request["request_id"],
        generation=9, result="applied")
    assert core.acknowledge_input(
        ":1.shell", request_id=request["request_id"],
        generation=9, result="applied")
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert errors == []
