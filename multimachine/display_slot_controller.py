"""Trusted orchestration boundary for one leased remote-display slot.

``RemoteDisplaySlot`` defines the only legal state transitions and action
ordering.  This controller executes those actions serially, converts every
partial attach, carrier loss, lease timeout, and local operation failure into
the same best-effort fail-safe teardown, and emits secret-free audit events.

The executor is deliberately local and narrow.  A primary implementation talks
to trusted qdwin output management and owns the carrier child; a peer
implementation reserves/blanks the physical panel and owns FreeRDP.  Neither
may reinterpret grant policy or reorder actions.
"""
from __future__ import annotations

import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from .remote_display_slot import (
    DisplaySlotError,
    RemoteDisplaySlot,
    SlotAction,
    SlotPhase,
)


class DisplaySlotExecutor(Protocol):
    """Privileged local operations behind the pure slot state machine."""

    def perform(self, action: SlotAction, grant: Mapping) -> None:
        """Complete one action or raise; teardown actions must be idempotent."""

    def fail_safe_confirmed(self, slot_name: str) -> bool:
        """Return true only when input/carrier/output/panel are locally safe."""


class DisplayActionEndpoint(Protocol):
    """One local half of the distributed display transaction."""

    def perform(self, action: SlotAction, grant: Mapping) -> None:
        """Execute a role-appropriate action or raise."""

    def safe_state_confirmed(self, slot_name: str) -> bool:
        """Confirm this endpoint has released every resource for the slot."""


class SplitDisplaySlotExecutor:
    """Route the state machine's fixed actions to primary, peer, or carrier.

    Cross-machine RPC/authentication belongs below the endpoint interfaces.  A
    caller cannot choose the destination: the action enum has one fixed owner,
    so a compromised peer cannot turn a panel request into primary output or
    input authority.
    """

    _PEER_ACTIONS = frozenset({
        "peer-blank-panel", "peer-unblank-panel",
    })
    _CARRIER_ACTIONS = frozenset({
        "open-authenticated-carrier", "close-carrier",
    })
    _PRIMARY_ACTIONS = frozenset({
        "primary-enable-output", "primary-enable-input",
        "primary-disable-input", "synthesize-releases", "clear-transfers",
        "primary-disable-output",
    })

    def __init__(self, *, primary: DisplayActionEndpoint,
                 peer: DisplayActionEndpoint,
                 carrier: DisplayActionEndpoint):
        self.primary = primary
        self.peer = peer
        self.carrier = carrier

    def perform(self, action: SlotAction, grant: Mapping) -> None:
        kind = action.kind.value
        if kind in self._PRIMARY_ACTIONS:
            endpoint = self.primary
        elif kind in self._PEER_ACTIONS:
            endpoint = self.peer
        elif kind in self._CARRIER_ACTIONS:
            endpoint = self.carrier
        else:  # defensive if the state machine grows without routing policy
            raise DisplayControllerError(
                f"display action has no executor owner: {kind}")
        endpoint.perform(action, grant)

    def fail_safe_confirmed(self, slot_name: str) -> bool:
        # Evaluate every endpoint even if one is unsafe so all three probes run
        # and can leave audit evidence.
        results = [
            self.primary.safe_state_confirmed(slot_name),
            self.carrier.safe_state_confirmed(slot_name),
            self.peer.safe_state_confirmed(slot_name),
        ]
        return all(results)


class PrimaryDisplayEndpoint:
    """Split primary output mutation from other primary-local operations.

    Output enable/disable must round-trip through authenticated qdshell. Input
    gating, held releases, and transfer cleanup stay in the primary controller
    runtime. This prevents the shell mailbox from growing into a general remote
    action interface.
    """

    _SHELL_ACTIONS = frozenset({
        "primary-enable-output", "primary-disable-output",
    })
    _LOCAL_ACTIONS = frozenset({
        "primary-enable-input", "primary-disable-input",
        "synthesize-releases", "clear-transfers",
    })

    def __init__(self, *, shell_layout: DisplayActionEndpoint,
                 local: DisplayActionEndpoint):
        self.shell_layout = shell_layout
        self.local = local

    def perform(self, action: SlotAction, grant: Mapping) -> None:
        kind = action.kind.value
        if kind in self._SHELL_ACTIONS:
            self.shell_layout.perform(action, grant)
        elif kind in self._LOCAL_ACTIONS:
            self.local.perform(action, grant)
        else:
            raise DisplayControllerError(
                f"action is not owned by the primary endpoint: {kind}")

    def safe_state_confirmed(self, slot_name: str) -> bool:
        shell_safe = self.shell_layout.safe_state_confirmed(slot_name)
        local_safe = self.local.safe_state_confirmed(slot_name)
        return shell_safe and local_safe


class DisplayControllerError(RuntimeError):
    """A controller transition failed and fail-safe handling was attempted."""


@dataclass(frozen=True)
class ControllerEvent:
    timestamp: float
    generation: int
    phase: str
    kind: str
    action: str | None = None
    ok: bool = True
    detail: str | None = None


class DisplaySlotController:
    """Serialize one slot's grant, executor, heartbeat, and failure lifecycle."""

    def __init__(self, slot: RemoteDisplaySlot, executor: DisplaySlotExecutor,
                 *, clock: Callable[[], float] = time.time,
                 audit: Callable[[ControllerEvent], None] | None = None):
        self.slot = slot
        self.executor = executor
        self.clock = clock
        self.audit = audit or (lambda _event: None)
        self._grant: dict | None = None
        self._lock = threading.RLock()

    @property
    def phase(self) -> SlotPhase:
        return self.slot.phase

    @property
    def generation(self) -> int:
        return self.slot.generation

    def _emit(self, kind: str, *, action: SlotAction | None = None,
              ok: bool = True, detail: str | None = None) -> None:
        self.audit(ControllerEvent(
            timestamp=self.clock(), generation=self.slot.generation,
            phase=self.slot.phase.value, kind=kind,
            action=action.kind.value if action else None,
            ok=ok, detail=detail))

    def _perform(self, action: SlotAction) -> None:
        if self._grant is None:
            raise DisplayControllerError("display controller has no active grant")
        self._emit("action-start", action=action)
        try:
            self.executor.perform(action, self._grant)
        except Exception as exc:
            self._emit("action-failed", action=action, ok=False,
                       detail=type(exc).__name__)
            raise
        self._emit("action-complete", action=action)

    def _perform_teardown(self, actions: tuple[SlotAction, ...]) -> list[str]:
        failures: list[str] = []
        # Never stop a safety sequence at its first failed cleanup.  In
        # particular, failure to synthesize releases must not leave the output
        # or peer panel owned indefinitely.
        for action in actions:
            try:
                self._perform(action)
            except Exception as exc:
                failures.append(f"{action.kind.value}:{type(exc).__name__}")
        return failures

    def _fail_safe(self, reason: str) -> list[str]:
        actions = self.slot.fail_safe(reason)
        self._emit("fail-safe", ok=False, detail=reason)
        return self._perform_teardown(actions)

    def attach(self, verified_payload: Mapping) -> None:
        """Execute a verified attach through carrier-connected/input-enabled."""
        with self._lock:
            if self._grant is not None:
                raise DisplayControllerError(
                    "display controller already owns a grant")
            self._grant = deepcopy(dict(verified_payload))
            try:
                actions = self.slot.begin_attach(self._grant)
                self._emit("attach-begin")
                for action in actions:
                    self._perform(action)
                for action in self.slot.carrier_connected(self.slot.generation):
                    self._perform(action)
                self._emit("attach-complete")
            except Exception as exc:
                failures = self._fail_safe(
                    f"attach failed at {type(exc).__name__}")
                # Validation can reject before the slot leaves DISABLED. There
                # is then no admitted authority to retain and no failed-safe
                # state to reset; a corrected grant may be tried normally.
                if self.slot.phase is SlotPhase.DISABLED:
                    self._grant = None
                suffix = f"; teardown failures={failures}" if failures else ""
                raise DisplayControllerError(
                    f"display attach failed: {type(exc).__name__}{suffix}") from exc

    def heartbeat(self, generation: int) -> bool:
        with self._lock:
            try:
                accepted = self.slot.heartbeat(generation)
            except DisplaySlotError:
                self._emit("heartbeat-rejected", ok=False, detail="stale-generation")
                raise
            if accepted:
                self._emit("heartbeat")
                return True
            failures = self._fail_safe("display heartbeat rejected")
            if failures:
                raise DisplayControllerError(
                    f"heartbeat fail-safe teardown failed: {failures}")
            return False

    def record_input(self, generation: int, *, kind: str,
                     code: int, pressed: bool) -> None:
        with self._lock:
            self.slot.input_event(
                generation, kind=kind, code=code, pressed=pressed)

    def carrier_lost(self, generation: int, *, reason: str = "carrier lost") -> None:
        with self._lock:
            if generation != self.slot.generation:
                self._emit("carrier-loss-ignored", ok=False,
                           detail="stale-generation")
                return
            failures = self._fail_safe(reason)
            if failures:
                raise DisplayControllerError(
                    f"carrier-loss teardown failed: {failures}")

    def tick(self) -> bool:
        """Poll the heartbeat/absolute lease; return true when fail-safe fired."""
        with self._lock:
            actions = self.slot.tick()
            if not actions:
                return False
            self._emit("lease-timeout", ok=False)
            failures = self._perform_teardown(actions)
            if failures:
                raise DisplayControllerError(
                    f"lease-timeout teardown failed: {failures}")
            return True

    def detach(self, generation: int) -> None:
        with self._lock:
            actions = self.slot.begin_detach(generation)
            self._emit("detach-begin")
            failures = self._perform_teardown(actions)
            if failures:
                # The state was DRAINING. Mark it FAILED_SAFE without repeating
                # already attempted idempotent actions; reset remains blocked
                # until the executor independently confirms local safe state.
                self.slot.fail_safe("detach operation failed")
                self._emit("detach-failed", ok=False, detail=",".join(failures))
                raise DisplayControllerError(
                    f"display detach had cleanup failures: {failures}")
            self.slot.complete_detach(generation)
            self._grant = None
            self._emit("detach-complete")

    def reset_failed_safe(self) -> None:
        """Permit a newer grant only after local state is independently safe."""
        with self._lock:
            if self.slot.phase is not SlotPhase.FAILED_SAFE:
                raise DisplayControllerError("display slot is not failed-safe")
            if not self.executor.fail_safe_confirmed(self.slot.spec.name):
                self._emit("safe-state-unconfirmed", ok=False)
                raise DisplayControllerError(
                    "local display fail-safe state is not confirmed")
            self.slot.reset_failed_safe()
            self._grant = None
            self._emit("failed-safe-reset")
