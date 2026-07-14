"""R9 fail-safe pre-created RDP output-slot lifecycle."""
from __future__ import annotations

from dataclasses import asdict

import pytest

from multimachine.remote_display_slot import (
    ActionKind,
    DisplaySlotError,
    DisplaySlotSpec,
    HeldInput,
    RemoteDisplaySlot,
    SlotPhase,
)


class Clock:
    def __init__(self, value=1_800_000_000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def payload(**changes):
    value = {
        "primary_machine": "laptop",
        "peer_machine": "workstation",
        "trust_domain_id": "owner-machines",
        "generation": 90,
        "session_id": "dock-session-r9",
        "slot_name": "rdp-0",
        "logical_x": 1920,
        "logical_y": 0,
        "width": 2560,
        "height": 1440,
        "scale": 1,
        "allow_input": True,
        "lease_expires_at": 1_800_000_000 + 3600,
        "heartbeat_ms": 2000,
    }
    value.update(changes)
    return value


def kinds(actions):
    return tuple(action.kind for action in actions)


def active_slot():
    clock = Clock()
    slot = RemoteDisplaySlot(DisplaySlotSpec("rdp-0"), clock=clock)
    attach = slot.begin_attach(payload())
    assert kinds(attach) == (
        ActionKind.PEER_BLANK_PANEL,
        ActionKind.PRIMARY_ENABLE_OUTPUT,
        ActionKind.OPEN_AUTHENTICATED_CARRIER,
    )
    assert kinds(slot.carrier_connected(90)) == (
        ActionKind.PRIMARY_ENABLE_INPUT,
    )
    return slot, clock


def test_attach_order_never_exposes_input_before_panel_and_output_authority():
    slot, _ = active_slot()
    assert slot.phase is SlotPhase.ACTIVE
    assert slot.generation == 90


def test_read_only_grant_never_admits_rdp_input() -> None:
    clock = Clock()
    slot = RemoteDisplaySlot(DisplaySlotSpec("rdp-0"), clock=clock)
    slot.begin_attach(payload(allow_input=False))
    assert slot.carrier_connected(90) == ()
    assert slot.phase is SlotPhase.ACTIVE
    with pytest.raises(DisplaySlotError, match="input is disabled"):
        slot.input_event(90, kind="key", code=30, pressed=True)
    assert kinds(slot.begin_detach(90))[0] is ActionKind.PRIMARY_DISABLE_INPUT


def test_detach_order_cuts_input_and_carrier_before_output_and_panel_release():
    slot, _ = active_slot()
    slot.input_event(90, kind="key", code=30, pressed=True)
    slot.input_event(90, kind="button", code=272, pressed=True)
    actions = slot.begin_detach(90)
    assert kinds(actions) == (
        ActionKind.PRIMARY_DISABLE_INPUT,
        ActionKind.SYNTHESIZE_RELEASES,
        ActionKind.CLEAR_TRANSFERS,
        ActionKind.CLOSE_CARRIER,
        ActionKind.PRIMARY_DISABLE_OUTPUT,
        ActionKind.PEER_UNBLANK_PANEL,
    )
    assert actions[1].releases == (
        HeldInput("button", 272), HeldInput("key", 30))
    assert slot.phase is SlotPhase.DRAINING
    slot.complete_detach(90)
    assert slot.phase is SlotPhase.DISABLED


def test_heartbeat_timeout_runs_same_fail_safe_teardown():
    slot, clock = active_slot()
    slot.input_event(90, kind="key", code=42, pressed=True)
    clock.advance(2.1)
    actions = slot.tick()
    assert kinds(actions)[0] is ActionKind.PRIMARY_DISABLE_INPUT
    assert kinds(actions)[-1] is ActionKind.PEER_UNBLANK_PANEL
    assert actions[1].releases == (HeldInput("key", 42),)
    assert slot.phase is SlotPhase.FAILED_SAFE
    assert slot.failure_reason == "display lease heartbeat timeout"


def test_heartbeats_renew_only_before_deadline_and_absolute_expiry_wins():
    slot, clock = active_slot()
    clock.advance(1.5)
    assert slot.heartbeat(90)
    clock.advance(1.5)
    assert slot.heartbeat(90)
    clock.value = payload()["lease_expires_at"]
    assert not slot.heartbeat(90)
    assert slot.tick()


def test_stale_generation_cannot_connect_input_heartbeat_or_detach():
    clock = Clock()
    slot = RemoteDisplaySlot(DisplaySlotSpec("rdp-0"), clock=clock)
    slot.begin_attach(payload())
    for operation in (
        lambda: slot.carrier_connected(89),
        lambda: slot.heartbeat(89),
        lambda: slot.input_event(89, kind="key", code=30, pressed=True),
        lambda: slot.begin_detach(89),
    ):
        with pytest.raises(DisplaySlotError, match="generation is stale"):
            operation()


def test_redock_requires_strictly_new_generation_even_after_fail_safe():
    slot, _ = active_slot()
    slot.fail_safe("peer lost")
    slot.reset_failed_safe()
    with pytest.raises(DisplaySlotError, match="generation must increase"):
        slot.begin_attach(payload(generation=90))
    slot.begin_attach(payload(generation=91))
    assert slot.generation == 91


def test_restart_restores_last_generation_before_accepting_grants() -> None:
    slot = RemoteDisplaySlot(DisplaySlotSpec("rdp-0"), clock=Clock())
    slot.restore_last_generation(90)
    assert slot.generation == 90
    with pytest.raises(DisplaySlotError, match="generation must increase"):
        slot.begin_attach(payload(generation=90))
    slot.begin_attach(payload(generation=91))
    with pytest.raises(DisplaySlotError, match="recovery is invalid"):
        slot.restore_last_generation(92)


@pytest.mark.parametrize(("changes", "message"), [
    ({"slot_name": "rdp-1"}, "different slot"),
    ({"width": 9000}, "mode bound"),
    ({"height": 5000}, "mode bound"),
    ({"scale": 3}, "scale bound"),
])
def test_slot_rejects_unenforceable_or_out_of_pool_grant(changes, message):
    slot = RemoteDisplaySlot(DisplaySlotSpec("rdp-0"), clock=Clock())
    with pytest.raises(DisplaySlotError, match=message):
        slot.begin_attach(payload(**changes))


def test_verified_payload_projection_does_not_accept_missing_field():
    value = payload()
    value.pop("heartbeat_ms")
    slot = RemoteDisplaySlot(DisplaySlotSpec("rdp-0"), clock=Clock())
    with pytest.raises(DisplaySlotError, match="incomplete"):
        slot.begin_attach(value)


def test_action_records_are_serializable_for_audit():
    slot, _ = active_slot()
    action = slot.begin_detach(90)[0]
    assert asdict(action) == {"kind": ActionKind.PRIMARY_DISABLE_INPUT,
                              "releases": ()}
