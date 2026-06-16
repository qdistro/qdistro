"""Dock-session **generation** + **display lease** reference state machine.

Productizes the D3 logic sim (``todo/multi-machine/08-probe-d3-generation-sim.py``,
PASS) and encodes the lease model + the dock state x concern table from
``06-docking-and-unmount-lifecycle.md`` (codex r3/r5). This is the *contract*
the eventual qdwin/qbus daemon code must satisfy; the unit tests pin it. It is
pure-Python with an injected clock so lease-expiry is deterministic in tests.

Governing rule (06, codex r3): **stale pixels are allowed for at most a short
bounded blanking interval; stale input is never allowed.** Two mechanisms
enforce it: the generation guard (reject anything from an old dock generation)
and the display lease (time-bounded, heartbeat-renewed, fail-safe on both sides
independently).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class DockState(str, Enum):
    STANDALONE = "standalone"
    DOCKING = "docking"
    DOCKED = "docked"
    UNDOCKING = "undocking"
    FAILED_SAFE = "failed-safe"


# Message kinds carried over the dock link. ``input`` is special: stale input is
# *never* applied (06), so the guard treats it as never-tolerable.
INPUT_KINDS = frozenset({"input_motion", "input_button", "input_key",
                         "input_axis", "input_modifiers"})
PIXEL_KINDS = frozenset({"frame"})
CONTROL_KINDS = frozenset({"clipboard_offer", "dnd", "proxy_map", "proxy_unmap",
                           "configure"})


# --------------------------------------------------------------------------
# Dock state x concern authority table (06, codex r5). Each cell names who is
# authoritative. "primary" = laptop; "local" = the secondary's own qdwin/seat.
# Encodes the table verbatim so policy is testable, not prose.
# --------------------------------------------------------------------------
_CONCERN_TABLE: dict[str, dict[DockState, str]] = {
    "composition": {
        DockState.STANDALONE: "local", DockState.DOCKING: "local->primary",
        DockState.DOCKED: "primary", DockState.UNDOCKING: "primary->local",
        DockState.FAILED_SAFE: "local",
    },
    "seat": {
        DockState.STANDALONE: "local", DockState.DOCKING: "local",
        DockState.DOCKED: "primary", DockState.UNDOCKING: "drain->local",
        DockState.FAILED_SAFE: "local",
    },
    "secondary_keyboard": {
        DockState.STANDALONE: "active", DockState.DOCKING: "dormant",
        DockState.DOCKED: "dormant", DockState.UNDOCKING: "->active",
        DockState.FAILED_SAFE: "active",
    },
    "clipboard": {
        DockState.STANDALONE: "local", DockState.DOCKING: "local",
        DockState.DOCKED: "primary", DockState.UNDOCKING: "clear",
        DockState.FAILED_SAFE: "local",
    },
    "lock": {
        DockState.STANDALONE: "secondary", DockState.DOCKING: "secondary",
        DockState.DOCKED: "laptop", DockState.UNDOCKING: "laptop->secondary",
        DockState.FAILED_SAFE: "secondary",
    },
    "secure_attention": {
        DockState.STANDALONE: "secondary", DockState.DOCKING: "secondary",
        DockState.DOCKED: "laptop", DockState.UNDOCKING: "laptop",
        DockState.FAILED_SAFE: "secondary",
    },
    "new_presence_permission": {
        DockState.STANDALONE: "local-presence", DockState.DOCKING: "forward/queue",
        DockState.DOCKED: "forward-to-primary", DockState.UNDOCKING: "queue",
        DockState.FAILED_SAFE: "deny/queue",
    },
}


def concern_authority(concern: str, state: DockState) -> str:
    try:
        return _CONCERN_TABLE[concern][state]
    except KeyError as e:
        raise KeyError(f"unknown concern/state: {concern}/{state}") from e


# --------------------------------------------------------------------------
@dataclass
class DisplayLease:
    """A time-bounded lease for who drives a physically-attached panel (06).

    The secondary's display agent grants the *primary* a lease for the
    secondary's monitor and blanks the local output while leased. Renewed by
    heartbeats; on renewal failure the secondary revokes and returns the
    monitor to its local qdwin. Both sides fail safe independently.
    """

    output_id: int
    generation: int
    ttl: float                      # seconds; lease invalid if not renewed within
    granted_at: float
    last_heartbeat: float
    owner: str = "primary"
    revoked: bool = False

    def is_valid(self, now: float) -> bool:
        return (not self.revoked) and (now - self.last_heartbeat) <= self.ttl

    def renew(self, now: float, generation: int) -> bool:
        """Heartbeat. A heartbeat from a stale generation must not renew."""
        if self.revoked or generation != self.generation:
            return False
        self.last_heartbeat = now
        return True

    def revoke(self) -> None:
        self.revoked = True


@dataclass
class DockSession:
    """The authoritative dock state machine. One per peer link.

    ``clock`` returns monotonic seconds (injected for deterministic tests).
    """

    clock: Callable[[], float]
    lease_ttl: float = 2.0
    state: DockState = DockState.STANDALONE
    generation: int = 0
    lease: DisplayLease | None = None
    applied: list[tuple[int, str]] = field(default_factory=list)
    rejected: list[tuple[int, str, str]] = field(default_factory=list)  # (gen, kind, reason)
    _gen_counter: "itertools.count" = field(
        default_factory=lambda: itertools.count(1), repr=False)

    # ---- transitions (each fail-closed) ---------------------------------
    def begin_dock(self) -> int:
        """standalone/failed-safe -> docking; mint a fresh generation."""
        if self.state not in (DockState.STANDALONE, DockState.FAILED_SAFE):
            raise RuntimeError(f"cannot begin_dock from {self.state}")
        self.generation = next(self._gen_counter)
        self.state = DockState.DOCKING
        return self.generation

    def complete_dock(self, output_id: int) -> DisplayLease:
        """docking -> docked; the secondary grants the primary a display lease."""
        if self.state is not DockState.DOCKING:
            raise RuntimeError(f"cannot complete_dock from {self.state}")
        now = self.clock()
        self.lease = DisplayLease(
            output_id=output_id, generation=self.generation, ttl=self.lease_ttl,
            granted_at=now, last_heartbeat=now)
        self.state = DockState.DOCKED
        return self.lease

    def heartbeat(self) -> bool:
        """Renew the lease. Returns False (and does not renew) if expired/stale."""
        if self.state is not DockState.DOCKED or self.lease is None:
            return False
        now = self.clock()
        if not self.lease.is_valid(now):
            self._fail_safe("lease expired before heartbeat")
            return False
        return self.lease.renew(now, self.generation)

    def tick(self) -> None:
        """Check lease validity; if it lapsed, fail safe (independent timeout)."""
        if self.state is DockState.DOCKED and self.lease is not None:
            if not self.lease.is_valid(self.clock()):
                self._fail_safe("lease heartbeat timeout")

    def begin_undock(self) -> None:
        if self.state not in (DockState.DOCKED, DockState.DOCKING):
            raise RuntimeError(f"cannot begin_undock from {self.state}")
        self.state = DockState.UNDOCKING

    def complete_undock(self) -> None:
        if self.lease is not None:
            self.lease.revoke()
        self.lease = None
        self.state = DockState.STANDALONE

    def peer_lost(self) -> None:
        """Heartbeat says peer gone: revoke lease, return to fail-safe local."""
        self._fail_safe("peer lost")

    def _fail_safe(self, reason: str) -> None:
        if self.lease is not None:
            self.lease.revoke()
        self.lease = None
        self.state = DockState.FAILED_SAFE
        self.rejected.append((self.generation, "_failsafe", reason))

    # ---- generation guard (productized D3) ------------------------------
    def recv(self, gen: int, kind: str, payload: object = None) -> bool:
        """Apply a message iff it belongs to the current dock generation AND
        the session is in a state that accepts that kind. Stale input is never
        applied; stale pixels/control are rejected.
        """
        # only docked sessions accept link traffic; anything else is unsafe.
        if self.state is not DockState.DOCKED:
            self.rejected.append((gen, kind, f"state={self.state.value}"))
            return False
        if gen != self.generation:
            self.rejected.append((gen, kind, "stale-generation"))
            return False
        # input additionally requires a currently-valid lease (no input onto a
        # panel we no longer own) — stale input is never allowed.
        if kind in INPUT_KINDS:
            if self.lease is None or not self.lease.is_valid(self.clock()):
                self.rejected.append((gen, kind, "no-valid-lease"))
                return False
        self.applied.append((gen, kind))
        return True
