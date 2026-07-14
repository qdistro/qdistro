"""Fail-safe lifecycle for one pre-created qdwin RDP output slot.

This module does not speak Wayland or RDP.  It is the authority/state boundary
that tells a privileged local executor exactly which ordered operations are
allowed.  The executor must use qdwin's trusted output-management connection
and a broker-owned authenticated external-listener fd; a public RDP listener is
never produced by this state machine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping


_SLOT = re.compile(r"rdp-[0-9]{1,3}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class DisplaySlotError(RuntimeError):
    """A slot transition or authority fact is invalid."""


class SlotPhase(str, Enum):
    DISABLED = "disabled"
    ENABLING = "enabling"
    ACTIVE = "active"
    DRAINING = "draining"
    FAILED_SAFE = "failed-safe"


class ActionKind(str, Enum):
    PEER_BLANK_PANEL = "peer-blank-panel"
    PRIMARY_ENABLE_OUTPUT = "primary-enable-output"
    OPEN_AUTHENTICATED_CARRIER = "open-authenticated-carrier"
    PRIMARY_ENABLE_INPUT = "primary-enable-input"
    PRIMARY_DISABLE_INPUT = "primary-disable-input"
    SYNTHESIZE_RELEASES = "synthesize-releases"
    CLEAR_TRANSFERS = "clear-transfers"
    CLOSE_CARRIER = "close-carrier"
    PRIMARY_DISABLE_OUTPUT = "primary-disable-output"
    PEER_UNBLANK_PANEL = "peer-unblank-panel"


@dataclass(frozen=True)
class HeldInput:
    kind: str
    code: int


@dataclass(frozen=True)
class SlotAction:
    kind: ActionKind
    releases: tuple[HeldInput, ...] = ()


@dataclass(frozen=True)
class DisplaySlotSpec:
    name: str
    max_width: int = 7680
    max_height: int = 4320
    max_scale: int = 2

    def validate(self) -> None:
        if _SLOT.fullmatch(self.name) is None:
            raise DisplaySlotError("RDP slot name is invalid")
        if (self.max_width <= 0 or self.max_height <= 0
                or self.max_width * self.max_height > 67_108_864):
            raise DisplaySlotError("RDP slot mode bound is invalid")
        if self.max_scale <= 0 or self.max_scale > 4:
            raise DisplaySlotError("RDP slot scale bound is invalid")


@dataclass(frozen=True)
class ActiveDisplayGrant:
    primary_machine: str
    peer_machine: str
    trust_domain_id: str
    generation: int
    session_id: str
    slot_name: str
    logical_x: int
    logical_y: int
    width: int
    height: int
    scale: int
    allow_input: bool
    lease_expires_at: int
    heartbeat_ms: int

    @classmethod
    def from_verified_payload(cls, payload: Mapping) -> "ActiveDisplayGrant":
        fields = cls.__dataclass_fields__
        try:
            values = {name: payload[name] for name in fields}
        except (KeyError, TypeError) as exc:
            raise DisplaySlotError(
                "verified display grant is incomplete") from exc
        grant = cls(**values)
        grant.validate()
        return grant

    def validate(self) -> None:
        for value in (
            self.primary_machine, self.peer_machine, self.trust_domain_id,
            self.session_id,
        ):
            if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
                raise DisplaySlotError("display grant identity is invalid")
        if self.primary_machine == self.peer_machine:
            raise DisplaySlotError("display endpoints must differ")
        if _SLOT.fullmatch(self.slot_name) is None:
            raise DisplaySlotError("display grant slot name is invalid")
        for value in (
            self.generation, self.width, self.height, self.scale,
            self.lease_expires_at, self.heartbeat_ms,
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise DisplaySlotError("display grant integer is invalid")
        for value in (self.logical_x, self.logical_y):
            if (not isinstance(value, int) or isinstance(value, bool)
                    or abs(value) > 65_535):
                raise DisplaySlotError("display grant position is invalid")
        if not isinstance(self.allow_input, bool):
            raise DisplaySlotError("display grant input policy is invalid")


class RemoteDisplaySlot:
    """One generation-scoped, heartbeat-leased pre-created RDP head."""

    def __init__(self, spec: DisplaySlotSpec, *, clock: Callable[[], float]):
        spec.validate()
        self.spec = spec
        self.clock = clock
        self.phase = SlotPhase.DISABLED
        self.grant: ActiveDisplayGrant | None = None
        self.last_generation = 0
        self.last_heartbeat = 0.0
        self._held: set[HeldInput] = set()
        self.failure_reason: str | None = None

    @property
    def generation(self) -> int:
        return self.grant.generation if self.grant else self.last_generation

    def _check_generation(self, generation: int) -> None:
        if self.grant is None or generation != self.grant.generation:
            raise DisplaySlotError("display slot generation is stale")

    def _lease_valid(self) -> bool:
        if self.grant is None:
            return False
        now = self.clock()
        heartbeat_deadline = (
            self.last_heartbeat + self.grant.heartbeat_ms / 1000.0)
        return now <= heartbeat_deadline and now < self.grant.lease_expires_at

    def begin_attach(self, payload: Mapping) -> tuple[SlotAction, ...]:
        if self.phase not in {SlotPhase.DISABLED, SlotPhase.FAILED_SAFE}:
            raise DisplaySlotError(
                f"cannot attach display slot from {self.phase.value}")
        grant = ActiveDisplayGrant.from_verified_payload(payload)
        if grant.slot_name != self.spec.name:
            raise DisplaySlotError("display grant targets a different slot")
        if grant.generation <= self.last_generation:
            raise DisplaySlotError("display slot generation must increase")
        if grant.width > self.spec.max_width or grant.height > self.spec.max_height:
            raise DisplaySlotError("display grant exceeds slot mode bound")
        if grant.scale > self.spec.max_scale:
            raise DisplaySlotError("display grant exceeds slot scale bound")
        if grant.width * grant.height > 67_108_864:
            raise DisplaySlotError("display grant pixel count is out of bounds")
        # Weston RDP injects input on the same accepted peer socket. Until an
        # input-filtering RDP shim exists, a display-only grant cannot be
        # enforced server-side and must fail closed rather than trusting client
        # flags. v1 display peers are explicitly console-equivalent.
        if not grant.allow_input:
            raise DisplaySlotError(
                "RDP v1 requires console-equivalent input authority")
        now = self.clock()
        if now >= grant.lease_expires_at:
            raise DisplaySlotError("display lease has already expired")
        self.grant = grant
        self.last_generation = grant.generation
        self.last_heartbeat = now
        self._held.clear()
        self.failure_reason = None
        self.phase = SlotPhase.ENABLING
        return (
            SlotAction(ActionKind.PEER_BLANK_PANEL),
            SlotAction(ActionKind.PRIMARY_ENABLE_OUTPUT),
            SlotAction(ActionKind.OPEN_AUTHENTICATED_CARRIER),
        )

    def carrier_connected(self, generation: int) -> tuple[SlotAction, ...]:
        self._check_generation(generation)
        if self.phase is not SlotPhase.ENABLING:
            raise DisplaySlotError(
                f"carrier cannot connect from {self.phase.value}")
        if not self._lease_valid():
            return self.fail_safe("display lease expired during attach")
        self.phase = SlotPhase.ACTIVE
        return (SlotAction(ActionKind.PRIMARY_ENABLE_INPUT),)

    def heartbeat(self, generation: int) -> bool:
        self._check_generation(generation)
        if self.phase not in {SlotPhase.ENABLING, SlotPhase.ACTIVE}:
            return False
        now = self.clock()
        if now >= self.grant.lease_expires_at:  # type: ignore[union-attr]
            return False
        if not self._lease_valid():
            return False
        self.last_heartbeat = now
        return True

    def input_event(self, generation: int, *, kind: str,
                    code: int, pressed: bool) -> None:
        self._check_generation(generation)
        if self.phase is not SlotPhase.ACTIVE or not self._lease_valid():
            raise DisplaySlotError("display input is disabled")
        if kind not in {"key", "button"}:
            raise DisplaySlotError("held input kind is invalid")
        if (not isinstance(code, int) or isinstance(code, bool)
                or not 0 <= code <= 0xFFFF):
            raise DisplaySlotError("held input code is invalid")
        held = HeldInput(kind, code)
        if pressed:
            self._held.add(held)
        else:
            self._held.discard(held)

    def _teardown_actions(self) -> tuple[SlotAction, ...]:
        releases = tuple(sorted(
            self._held, key=lambda item: (item.kind, item.code)))
        self._held.clear()
        return (
            SlotAction(ActionKind.PRIMARY_DISABLE_INPUT),
            SlotAction(ActionKind.SYNTHESIZE_RELEASES, releases),
            SlotAction(ActionKind.CLEAR_TRANSFERS),
            SlotAction(ActionKind.CLOSE_CARRIER),
            SlotAction(ActionKind.PRIMARY_DISABLE_OUTPUT),
            SlotAction(ActionKind.PEER_UNBLANK_PANEL),
        )

    def begin_detach(self, generation: int) -> tuple[SlotAction, ...]:
        self._check_generation(generation)
        if self.phase not in {SlotPhase.ENABLING, SlotPhase.ACTIVE}:
            raise DisplaySlotError(
                f"cannot detach display slot from {self.phase.value}")
        self.phase = SlotPhase.DRAINING
        return self._teardown_actions()

    def complete_detach(self, generation: int) -> None:
        self._check_generation(generation)
        if self.phase is not SlotPhase.DRAINING:
            raise DisplaySlotError(
                f"cannot complete detach from {self.phase.value}")
        self.grant = None
        self.phase = SlotPhase.DISABLED

    def fail_safe(self, reason: str) -> tuple[SlotAction, ...]:
        if not isinstance(reason, str) or not reason:
            raise DisplaySlotError("display failure reason is required")
        if self.phase in {SlotPhase.DISABLED, SlotPhase.FAILED_SAFE}:
            return ()
        self.phase = SlotPhase.FAILED_SAFE
        self.failure_reason = reason
        return self._teardown_actions()

    def tick(self) -> tuple[SlotAction, ...]:
        if self.phase not in {SlotPhase.ENABLING, SlotPhase.ACTIVE}:
            return ()
        if self._lease_valid():
            return ()
        return self.fail_safe("display lease heartbeat timeout")

    def reset_failed_safe(self) -> None:
        if self.phase is not SlotPhase.FAILED_SAFE:
            raise DisplaySlotError("display slot is not failed-safe")
        self.grant = None
        self.phase = SlotPhase.DISABLED
