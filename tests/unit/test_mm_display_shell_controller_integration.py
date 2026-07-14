"""Controller → authenticated shell claim → exact tagged ack integration."""
from __future__ import annotations

import json
import threading

from multimachine.display_shell_mailbox import DisplayShellMailbox
from multimachine.display_shell_service import DisplayShellServiceCore
from multimachine.display_slot_controller import (
    DisplaySlotController,
    PrimaryDisplayEndpoint,
    SplitDisplaySlotExecutor,
)
from multimachine.remote_display_slot import (
    ActionKind,
    DisplaySlotSpec,
    RemoteDisplaySlot,
    SlotAction,
    SlotPhase,
)


class Endpoint:
    def __init__(self):
        self.actions: list[ActionKind] = []

    def perform(self, action: SlotAction, _grant) -> None:
        self.actions.append(action.kind)

    def safe_state_confirmed(self, _slot_name: str) -> bool:
        return True

    def heartbeat(self, _generation: int, _grant) -> None:
        pass


def grant() -> dict:
    return {
        "primary_machine": "primary", "peer_machine": "peer",
        "trust_domain_id": "owner-machines", "generation": 90,
        "session_id": "display-session", "slot_name": "rdp-0",
        "logical_x": 1280, "logical_y": 0, "width": 1280, "height": 800,
        "scale": 1, "allow_input": True,
        "lease_expires_at": 4_000_000_000, "heartbeat_ms": 1000,
    }


def test_controller_blocks_on_exact_qdshell_apply_before_input_enable() -> None:
    pending = threading.Event()
    mailbox = DisplayShellMailbox(on_pending=pending.set)
    service = DisplayShellServiceCore(
        mailbox, trusted_sender=lambda sender: sender == ":1.qdshell")
    primary_local = Endpoint()
    peer = Endpoint()
    carrier = Endpoint()
    primary = PrimaryDisplayEndpoint(
        shell_layout=mailbox, local=primary_local)
    executor = SplitDisplaySlotExecutor(
        primary=primary, peer=peer, carrier=carrier)
    controller = DisplaySlotController(
        RemoteDisplaySlot(DisplaySlotSpec("rdp-0"), clock=lambda: 1000),
        executor, clock=lambda: 1000)
    errors: list[BaseException] = []

    def attach() -> None:
        try:
            controller.attach(grant())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=attach)
    worker.start()
    assert pending.wait(1)
    # Peer reservation happened, but carrier/input cannot start until qdshell
    # reports the exact atomic output transaction as applied.
    assert peer.actions == [ActionKind.PEER_BLANK_PANEL]
    assert carrier.actions == []
    assert primary_local.actions == []
    request = json.loads(service.claim_layout(":1.qdshell"))
    assert request["enabled"] is True
    assert service.acknowledge_layout(
        ":1.qdshell", request_id=request["request_id"],
        generation=90, result="applied")
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert errors == []
    assert controller.phase is SlotPhase.ACTIVE
    assert carrier.actions == [ActionKind.OPEN_AUTHENTICATED_CARRIER]
    assert primary_local.actions == [ActionKind.PRIMARY_ENABLE_INPUT]
