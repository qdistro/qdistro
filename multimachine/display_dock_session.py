"""Broker-side owner for one primary display dock-session slot.

The per-window :mod:`multimachine.mm_broker` owns viewer proxy toplevels.  A
display dock is a different authority: it temporarily adds a peer panel to the
primary compositor's global workspace.  This module keeps that authority in a
separate, generation-scoped owner and composes the already narrow endpoints
without letting callers choose or reorder individual actions.

The cryptographic launcher verifies a display grant before this owner sees its
payload.  The owner then drives :class:`DisplaySlotController`, schedules peer
renewals ahead of the independently enforced deadline, turns carrier death or
renewal failure into the controller's complete fail-safe teardown, and exposes
only attach/detach/poll/reset/shutdown lifecycle operations.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from .display_slot_controller import (
    ControllerEvent,
    DisplayActionEndpoint,
    DisplaySlotController,
    PrimaryDisplayEndpoint,
    SplitDisplaySlotExecutor,
)
from .remote_display_slot import (
    ActionKind,
    ActiveDisplayGrant,
    DisplaySlotSpec,
    RemoteDisplaySlot,
    SlotAction,
    SlotPhase,
)


class CarrierSession(Protocol):
    """One authenticated, generation-bound carrier process group."""

    def ready(self) -> bool: ...
    def alive(self) -> bool: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class DockSessionStatus:
    slot_name: str
    phase: str
    generation: int
    session_id: str | None
    next_heartbeat: float | None


class PrimarySafetyEndpoint:
    """Bind controller safety actions to primary-local enforcement hooks.

    There are intentionally no permissive defaults.  Production wiring must
    provide the input gate, held-input release, transfer clear, and independent
    safe-state probe.  This prevents the VM gate's old boolean input adapter
    from accidentally becoming a product implementation.
    """

    def __init__(self, *,
                 set_input_enabled: Callable[[bool], None] | None = None,
                 input_gate: DisplayActionEndpoint | None = None,
                 synthesize_releases: Callable[[tuple], None],
                 clear_transfers: Callable[[], None],
                 safe_probe: Callable[[str], bool]):
        if (set_input_enabled is None) == (input_gate is None):
            raise ValueError(
                "primary safety needs exactly one real input gate adapter")
        self.set_input_enabled = set_input_enabled
        self.input_gate = input_gate
        self.synthesize_releases = synthesize_releases
        self.clear_transfers = clear_transfers
        self.safe_probe = safe_probe

    def perform(self, action: SlotAction, _grant: Mapping) -> None:
        if action.kind is ActionKind.PRIMARY_ENABLE_INPUT:
            if self.input_gate is not None:
                self.input_gate.perform(action, _grant)
            else:
                assert self.set_input_enabled is not None
                self.set_input_enabled(True)
        elif action.kind is ActionKind.PRIMARY_DISABLE_INPUT:
            if self.input_gate is not None:
                self.input_gate.perform(action, _grant)
            else:
                assert self.set_input_enabled is not None
                self.set_input_enabled(False)
        elif action.kind is ActionKind.SYNTHESIZE_RELEASES:
            self.synthesize_releases(action.releases)
        elif action.kind is ActionKind.CLEAR_TRANSFERS:
            self.clear_transfers()
        else:
            raise RuntimeError(
                f"primary safety endpoint cannot perform {action.kind.value}")

    def safe_state_confirmed(self, slot_name: str) -> bool:
        input_safe = (True if self.input_gate is None
                      else self.input_gate.safe_state_confirmed(slot_name))
        return input_safe and self.safe_probe(slot_name) is True


class PanelControlEndpoint:
    """Controller adapter for the authenticated primary panel Unix socket."""

    def __init__(self, *, command: Callable[[str], Mapping],
                 safe_probe: Callable[[str], bool]):
        self.command = command
        self.safe_probe = safe_probe
        self.active_generation: int | None = None

    def _request(self, kind: str, expected: str) -> None:
        response = self.command(kind)
        if response != {"ok": True, "result": expected}:
            raise RuntimeError(f"panel {kind} failed: {dict(response)!r}")

    def perform(self, action: SlotAction, grant: Mapping) -> None:
        generation = int(grant["generation"])
        slot_name = str(grant["slot_name"])
        if action.kind is ActionKind.PEER_BLANK_PANEL:
            if self.active_generation is not None:
                raise RuntimeError("another panel generation is already reserved")
            self._request("reserve", "reserved")
            self.active_generation = generation
        elif action.kind is ActionKind.PEER_UNBLANK_PANEL:
            # A peer-owned expiry makes the generation terminal and may close
            # the socket before source cleanup arrives.  Independent safe state
            # is authoritative in that case; otherwise release must round-trip.
            if not self.safe_probe(slot_name):
                self._request("release", "released")
            self.active_generation = None
            if not self.safe_probe(slot_name):
                raise RuntimeError("peer panel did not restore safe ownership")
        else:
            raise RuntimeError(
                f"panel endpoint cannot perform {action.kind.value}")

    def heartbeat(self, generation: int, grant: Mapping) -> None:
        if (generation != int(grant["generation"])
                or generation != self.active_generation):
            raise RuntimeError("panel heartbeat targets another generation")
        self._request("heartbeat", "renewed")

    def safe_state_confirmed(self, slot_name: str) -> bool:
        return (self.active_generation is None
                and self.safe_probe(slot_name) is True)


class CarrierSupervisorEndpoint:
    """Own exactly one authenticated carrier process group per generation."""

    def __init__(self, *, start: Callable[[Mapping], CarrierSession],
                 safe_probe: Callable[[str], bool]):
        self.start = start
        self.safe_probe = safe_probe
        self.session: CarrierSession | None = None
        self.active_generation: int | None = None

    def perform(self, action: SlotAction, grant: Mapping) -> None:
        generation = int(grant["generation"])
        if action.kind is ActionKind.OPEN_AUTHENTICATED_CARRIER:
            if self.session is not None:
                raise RuntimeError("another carrier generation is active")
            session = self.start(grant)
            self.session = session
            self.active_generation = generation
            if not session.ready() or not session.alive():
                try:
                    session.close()
                finally:
                    self.session = None
                    self.active_generation = None
                raise RuntimeError("authenticated carrier did not become ready")
        elif action.kind is ActionKind.CLOSE_CARRIER:
            session = self.session
            self.session = None
            self.active_generation = None
            if session is not None:
                session.close()
            if not self.safe_probe(str(grant["slot_name"])):
                raise RuntimeError("carrier resources remain active after close")
        else:
            raise RuntimeError(
                f"carrier endpoint cannot perform {action.kind.value}")

    def alive(self, generation: int) -> bool:
        return (self.session is not None
                and self.active_generation == generation
                and self.session.alive())

    def safe_state_confirmed(self, slot_name: str) -> bool:
        return (self.session is None and self.active_generation is None
                and self.safe_probe(slot_name) is True)


class DisplayDockSession:
    """Single lifecycle owner for one primary compositor display slot."""

    def __init__(self, *, slot: DisplaySlotSpec,
                 shell_layout: DisplayActionEndpoint,
                 primary_local: DisplayActionEndpoint,
                 peer_panel: DisplayActionEndpoint,
                 carrier: CarrierSupervisorEndpoint,
                 clock: Callable[[], float] = time.time,
                 audit: Callable[[ControllerEvent], None] | None = None):
        self.clock = clock
        self.carrier = carrier
        self._grant: ActiveDisplayGrant | None = None
        self._next_heartbeat: float | None = None
        self._lock = threading.RLock()
        self.controller = DisplaySlotController(
            slot=RemoteDisplaySlot(slot, clock=clock),
            executor=SplitDisplaySlotExecutor(
                primary=PrimaryDisplayEndpoint(
                    shell_layout=shell_layout, local=primary_local),
                peer=peer_panel, carrier=carrier),
            clock=clock, audit=audit)

    @property
    def phase(self) -> SlotPhase:
        return self.controller.phase

    @property
    def generation(self) -> int:
        return self.controller.generation

    def status(self) -> DockSessionStatus:
        with self._lock:
            return DockSessionStatus(
                slot_name=self.controller.slot.spec.name,
                phase=self.phase.value,
                generation=self.generation,
                session_id=None if self._grant is None else self._grant.session_id,
                next_heartbeat=self._next_heartbeat)

    def attach(self, verified_payload: Mapping) -> None:
        with self._lock:
            grant = ActiveDisplayGrant.from_verified_payload(verified_payload)
            if grant.slot_name != self.controller.slot.spec.name:
                raise ValueError("display grant targets another broker slot")
            self.controller.attach(verified_payload)
            self._grant = grant
            # Renew at half the peer-owned deadline, leaving one full half
            # interval for scheduling jitter and a bounded control round-trip.
            self._next_heartbeat = (
                self.clock() + grant.heartbeat_ms / 2000.0)

    def heartbeat(self, generation: int) -> bool:
        with self._lock:
            accepted = self.controller.heartbeat(generation)
            if accepted and self._grant is not None:
                self._next_heartbeat = (
                    self.clock() + self._grant.heartbeat_ms / 2000.0)
            return accepted

    def poll(self) -> bool:
        """Service carrier health, controller lease, and scheduled renewal.

        Return true when this call caused a safety transition.  Exceptions from
        fail-safe cleanup remain visible to the supervising daemon.
        """
        with self._lock:
            if self.phase not in {SlotPhase.ENABLING, SlotPhase.ACTIVE}:
                return False
            generation = self.generation
            if not self.carrier.alive(generation):
                self.controller.carrier_lost(
                    generation, reason="authenticated carrier process lost")
                self._next_heartbeat = None
                return True
            if self.controller.tick():
                self._next_heartbeat = None
                return True
            if (self.phase is SlotPhase.ACTIVE
                    and self._next_heartbeat is not None
                    and self.clock() >= self._next_heartbeat):
                self.heartbeat(generation)
            return self.phase is SlotPhase.FAILED_SAFE

    def detach(self, generation: int) -> None:
        with self._lock:
            self.controller.detach(generation)
            self._grant = None
            self._next_heartbeat = None

    def reset_failed_safe(self) -> None:
        with self._lock:
            self.controller.reset_failed_safe()
            self._grant = None
            self._next_heartbeat = None

    def restore_last_generation(self, generation: int) -> None:
        """Seed replay state after external startup recovery reached safe."""
        with self._lock:
            self.controller.slot.restore_last_generation(generation)

    def shutdown(self) -> None:
        """Best-effort normal detach; failed cleanup remains FAILED_SAFE."""
        with self._lock:
            if self.phase in {SlotPhase.ENABLING, SlotPhase.ACTIVE}:
                self.detach(self.generation)
            self._next_heartbeat = None

    def serve(self, stop: threading.Event, *, poll_interval: float = 0.05) -> None:
        if poll_interval <= 0 or poll_interval > 1:
            raise ValueError("display dock poll interval is out of bounds")
        try:
            while not stop.wait(poll_interval):
                self.poll()
        finally:
            self.shutdown()
