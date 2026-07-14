"""R9 trusted controller ordering, failure, heartbeat, and reset tests."""
from __future__ import annotations

import pytest

from multimachine.display_slot_controller import (
    ControllerEvent,
    DisplayControllerError,
    DisplaySlotController,
    PrimaryDisplayEndpoint,
    SplitDisplaySlotExecutor,
)
from multimachine.remote_display_slot import (
    ActionKind,
    DisplaySlotError,
    DisplaySlotSpec,
    RemoteDisplaySlot,
    SlotAction,
    SlotPhase,
)


class Clock:
    def __init__(self, value: float = 1000):
        self.value = value

    def __call__(self) -> float:
        return self.value


class Executor:
    def __init__(self):
        self.actions: list[SlotAction] = []
        self.fail: set[ActionKind] = set()
        self.confirmed = True

    def perform(self, action: SlotAction, grant) -> None:
        assert grant["session_id"] == "display-session"
        self.actions.append(action)
        if action.kind in self.fail:
            raise OSError(action.kind.value)

    def fail_safe_confirmed(self, slot_name: str) -> bool:
        assert slot_name == "rdp-0"
        return self.confirmed

    def safe_state_confirmed(self, slot_name: str) -> bool:
        return self.fail_safe_confirmed(slot_name)


def grant(*, generation: int = 7, expires: int = 5000,
          heartbeat_ms: int = 1000) -> dict:
    return {
        "primary_machine": "primary", "peer_machine": "peer",
        "trust_domain_id": "owner-machines", "generation": generation,
        "session_id": "display-session", "slot_name": "rdp-0",
        "logical_x": 1280, "logical_y": 0, "width": 1280, "height": 800,
        "scale": 1, "allow_input": True, "lease_expires_at": expires,
        "heartbeat_ms": heartbeat_ms,
    }


def controller(clock: Clock | None = None):
    clock = clock or Clock()
    executor = Executor()
    events: list[ControllerEvent] = []
    slot = RemoteDisplaySlot(DisplaySlotSpec("rdp-0"), clock=clock)
    return DisplaySlotController(
        slot, executor, clock=clock, audit=events.append), executor, events


def kinds(executor: Executor) -> list[ActionKind]:
    return [action.kind for action in executor.actions]


ATTACH = [
    ActionKind.PEER_BLANK_PANEL,
    ActionKind.PRIMARY_ENABLE_OUTPUT,
    ActionKind.OPEN_AUTHENTICATED_CARRIER,
    ActionKind.PRIMARY_ENABLE_INPUT,
]
TEARDOWN = [
    ActionKind.PRIMARY_DISABLE_INPUT,
    ActionKind.SYNTHESIZE_RELEASES,
    ActionKind.CLEAR_TRANSFERS,
    ActionKind.CLOSE_CARRIER,
    ActionKind.PRIMARY_DISABLE_OUTPUT,
    ActionKind.PEER_UNBLANK_PANEL,
]


def test_attach_and_detach_execute_the_only_safe_order() -> None:
    owner, executor, events = controller()
    owner.attach(grant())
    assert owner.phase is SlotPhase.ACTIVE
    assert kinds(executor) == ATTACH
    owner.record_input(7, kind="key", code=30, pressed=True)
    owner.record_input(7, kind="button", code=272, pressed=True)
    owner.detach(7)
    assert owner.phase is SlotPhase.DISABLED
    assert kinds(executor) == ATTACH + TEARDOWN
    release_action = executor.actions[5]
    assert [(item.kind, item.code) for item in release_action.releases] == [
        ("button", 272), ("key", 30),
    ]
    assert events[-1].kind == "detach-complete"


def test_rejected_grant_does_not_wedge_the_disabled_controller() -> None:
    owner, executor, _events = controller()
    invalid = grant()
    invalid["slot_name"] = "rdp-1"
    with pytest.raises(DisplayControllerError, match="display attach failed"):
        owner.attach(invalid)
    assert owner.phase is SlotPhase.DISABLED
    owner.attach(grant())
    assert owner.phase is SlotPhase.ACTIVE
    assert kinds(executor) == ATTACH


@pytest.mark.parametrize("failed", ATTACH)
def test_every_partial_attach_failure_runs_complete_teardown(failed) -> None:
    owner, executor, _events = controller()
    executor.fail.add(failed)
    with pytest.raises(DisplayControllerError, match="display attach failed"):
        owner.attach(grant())
    assert owner.phase is SlotPhase.FAILED_SAFE
    attempted = kinds(executor)
    assert failed in attempted
    for action in TEARDOWN:
        assert action in attempted


def test_teardown_continues_after_multiple_local_cleanup_failures() -> None:
    owner, executor, _events = controller()
    owner.attach(grant())
    executor.fail.update({
        ActionKind.SYNTHESIZE_RELEASES,
        ActionKind.PRIMARY_DISABLE_OUTPUT,
    })
    with pytest.raises(DisplayControllerError, match="cleanup failures"):
        owner.detach(7)
    assert owner.phase is SlotPhase.FAILED_SAFE
    assert kinds(executor)[-6:] == TEARDOWN


def test_heartbeat_timeout_tears_down_and_requires_confirmed_reset() -> None:
    clock = Clock()
    owner, executor, events = controller(clock)
    owner.attach(grant(heartbeat_ms=500))
    clock.value += 0.4
    assert owner.heartbeat(7)
    clock.value += 0.6
    assert owner.tick()
    assert owner.phase is SlotPhase.FAILED_SAFE
    assert kinds(executor)[-6:] == TEARDOWN
    assert any(event.kind == "lease-timeout" for event in events)
    executor.confirmed = False
    with pytest.raises(DisplayControllerError, match="not confirmed"):
        owner.reset_failed_safe()
    executor.confirmed = True
    owner.reset_failed_safe()
    assert owner.phase is SlotPhase.DISABLED
    owner.attach(grant(generation=8))
    assert owner.phase is SlotPhase.ACTIVE


def test_carrier_loss_is_fail_safe_but_stale_loss_cannot_kill_new_generation() -> None:
    owner, executor, _events = controller()
    owner.attach(grant())
    owner.carrier_lost(7)
    assert owner.phase is SlotPhase.FAILED_SAFE
    owner.reset_failed_safe()
    owner.attach(grant(generation=8))
    before = list(executor.actions)
    owner.carrier_lost(7)
    assert owner.phase is SlotPhase.ACTIVE
    assert executor.actions == before


def test_stale_heartbeat_and_input_are_rejected_without_mutation() -> None:
    owner, executor, _events = controller()
    owner.attach(grant())
    before = list(executor.actions)
    with pytest.raises(DisplaySlotError, match="generation is stale"):
        owner.heartbeat(6)
    with pytest.raises(DisplaySlotError, match="generation is stale"):
        owner.record_input(6, kind="key", code=30, pressed=True)
    assert owner.phase is SlotPhase.ACTIVE
    assert executor.actions == before


def test_audit_contains_no_grant_secrets_or_certificate_pins() -> None:
    owner, _executor, events = controller()
    payload = grant()
    payload.update({
        "carrier_secret_sha256": "a" * 64,
        "primary_tls_cert_sha256": "b" * 64,
        "peer_tls_cert_sha256": "c" * 64,
    })
    owner.attach(payload)
    text = repr(events)
    assert "a" * 64 not in text
    assert "b" * 64 not in text
    assert "c" * 64 not in text


def test_split_executor_routes_each_action_to_its_fixed_authority_owner() -> None:
    primary = Executor()
    peer = Executor()
    carrier = Executor()
    split = SplitDisplaySlotExecutor(
        primary=primary, peer=peer, carrier=carrier)
    owner = DisplaySlotController(
        RemoteDisplaySlot(DisplaySlotSpec("rdp-0"), clock=Clock()), split,
        clock=Clock())
    owner.attach(grant())
    owner.detach(7)
    assert kinds(peer) == [
        ActionKind.PEER_BLANK_PANEL, ActionKind.PEER_UNBLANK_PANEL,
    ]
    assert kinds(carrier) == [
        ActionKind.OPEN_AUTHENTICATED_CARRIER, ActionKind.CLOSE_CARRIER,
    ]
    assert kinds(primary) == [
        ActionKind.PRIMARY_ENABLE_OUTPUT,
        ActionKind.PRIMARY_ENABLE_INPUT,
        ActionKind.PRIMARY_DISABLE_INPUT,
        ActionKind.SYNTHESIZE_RELEASES,
        ActionKind.CLEAR_TRANSFERS,
        ActionKind.PRIMARY_DISABLE_OUTPUT,
    ]


def test_split_safe_confirmation_checks_all_three_endpoints() -> None:
    primary = Executor()
    peer = Executor()
    carrier = Executor()
    split = SplitDisplaySlotExecutor(
        primary=primary, peer=peer, carrier=carrier)
    primary.confirmed = False
    carrier.confirmed = False
    assert not split.fail_safe_confirmed("rdp-0")


def test_primary_endpoint_exposes_only_output_actions_to_qdshell() -> None:
    shell = Executor()
    local = Executor()
    primary = PrimaryDisplayEndpoint(shell_layout=shell, local=local)
    payload = grant()
    for kind in (
        ActionKind.PRIMARY_ENABLE_OUTPUT,
        ActionKind.PRIMARY_ENABLE_INPUT,
        ActionKind.PRIMARY_DISABLE_INPUT,
        ActionKind.SYNTHESIZE_RELEASES,
        ActionKind.CLEAR_TRANSFERS,
        ActionKind.PRIMARY_DISABLE_OUTPUT,
    ):
        primary.perform(SlotAction(kind), payload)
    assert kinds(shell) == [
        ActionKind.PRIMARY_ENABLE_OUTPUT,
        ActionKind.PRIMARY_DISABLE_OUTPUT,
    ]
    assert kinds(local) == [
        ActionKind.PRIMARY_ENABLE_INPUT,
        ActionKind.PRIMARY_DISABLE_INPUT,
        ActionKind.SYNTHESIZE_RELEASES,
        ActionKind.CLEAR_TRANSFERS,
    ]
    with pytest.raises(DisplayControllerError, match="not owned"):
        primary.perform(SlotAction(ActionKind.PEER_BLANK_PANEL), payload)
