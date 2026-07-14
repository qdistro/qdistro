"""One-shot controller-to-qdshell output-layout transaction mailbox.

The display controller cannot mutate qdwin directly: once qdshell is bound,
qdwin correctly authorizes only that shell client (or another connection from
the exact shell pid).  This mailbox exposes only the two slot-layout actions
qdshell must perform.  A D-Bus service authenticates the claiming sender as the
configured qdshell pid before calling :meth:`claim`; same-uid IPC is not enough.

Requests contain no carrier secret, private key, certificate, or general layout
array.  They are exact, one-shot, generation-bound slot deltas.  qdshell builds
the full atomic layout from its live head snapshot and acknowledges the exact
request after qdwin reports succeeded/failed/cancelled.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Callable, Mapping

from .remote_display_slot import ActionKind, SlotAction


SCHEMA = "qdistro-mm-shell-layout-v1"
RESULTS = frozenset({"applied", "failed", "cancelled"})
MAX_CONSUMED_REQUEST_IDS = 4096


class DisplayShellError(RuntimeError):
    """A shell layout request was invalid, stale, replayed, or unsuccessful."""


@dataclass(frozen=True)
class ShellLayoutRequest:
    schema: str
    request_id: str
    generation: int
    session_id: str
    slot_name: str
    enabled: bool
    logical_x: int
    logical_y: int
    width: int
    height: int
    scale: int
    expires_at: int

    def validate(self, *, now: float) -> None:
        if self.schema != SCHEMA:
            raise DisplayShellError("unsupported shell layout request schema")
        if (not isinstance(self.request_id, str) or len(self.request_id) != 32
                or any(c not in "0123456789abcdef" for c in self.request_id)):
            raise DisplayShellError("shell layout request id is invalid")
        for name in ("session_id", "slot_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise DisplayShellError(f"shell layout {name} is invalid")
        if (not isinstance(self.generation, int)
                or isinstance(self.generation, bool) or self.generation <= 0):
            raise DisplayShellError("shell layout generation is invalid")
        if not isinstance(self.enabled, bool):
            raise DisplayShellError("shell layout enabled state is invalid")
        for name in ("logical_x", "logical_y"):
            value = getattr(self, name)
            if (not isinstance(value, int) or isinstance(value, bool)
                    or abs(value) > 65_535):
                raise DisplayShellError(f"shell layout {name} is invalid")
        for name, maximum in (("width", 16_384), ("height", 16_384), ("scale", 4)):
            value = getattr(self, name)
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value <= 0 or value > maximum):
                raise DisplayShellError(f"shell layout {name} is invalid")
        if self.width * self.height > 67_108_864:
            raise DisplayShellError("shell layout pixel count is out of bounds")
        if (not isinstance(self.expires_at, int)
                or isinstance(self.expires_at, bool) or now >= self.expires_at):
            raise DisplayShellError("shell layout request has expired")


@dataclass
class _Pending:
    request: ShellLayoutRequest
    claimed: bool = False
    result: str | None = None


class DisplayShellMailbox:
    """Serialize one outstanding qdshell mutation and its exact acknowledgement."""

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 request_timeout: float = 10.0,
                 request_id: Callable[[], str] | None = None,
                 on_pending: Callable[[], None] | None = None):
        if request_timeout <= 0 or request_timeout > 30:
            raise ValueError("shell layout request timeout is out of bounds")
        self.clock = clock
        self.request_timeout = request_timeout
        self.request_id = request_id or (lambda: secrets.token_hex(16))
        self.on_pending = on_pending or (lambda: None)
        self._condition = threading.Condition()
        self._pending: _Pending | None = None
        self._last_generation = 0
        self._consumed_order: deque[str] = deque()
        self._consumed_ids: set[str] = set()

    def _consume(self, request_id: str) -> None:
        if request_id in self._consumed_ids:
            return
        self._consumed_ids.add(request_id)
        self._consumed_order.append(request_id)
        while len(self._consumed_order) > MAX_CONSUMED_REQUEST_IDS:
            self._consumed_ids.remove(self._consumed_order.popleft())

    @staticmethod
    def _request_from(action: SlotAction, grant: Mapping, *,
                      request_id: str, expires_at: int) -> ShellLayoutRequest:
        if action.kind is ActionKind.PRIMARY_ENABLE_OUTPUT:
            enabled = True
        elif action.kind is ActionKind.PRIMARY_DISABLE_OUTPUT:
            enabled = False
        else:
            raise DisplayShellError(
                "shell mailbox accepts only primary output layout actions")
        try:
            request = ShellLayoutRequest(
                schema=SCHEMA, request_id=request_id,
                generation=grant["generation"],
                session_id=grant["session_id"],
                slot_name=grant["slot_name"], enabled=enabled,
                logical_x=grant["logical_x"], logical_y=grant["logical_y"],
                width=grant["width"], height=grant["height"],
                scale=grant["scale"], expires_at=expires_at)
        except (KeyError, TypeError) as exc:
            raise DisplayShellError(
                "display grant lacks shell layout fields") from exc
        return request

    def perform(self, action: SlotAction, grant: Mapping) -> None:
        """Publish one action and wait for qdshell's exact async apply verdict."""
        deadline = self.clock() + self.request_timeout
        expiry = min(int(deadline + 0.999), int(grant.get("lease_expires_at", 0)))
        request = self._request_from(
            action, grant, request_id=self.request_id(), expires_at=expiry)
        request.validate(now=self.clock())
        with self._condition:
            if self._pending is not None:
                raise DisplayShellError("another shell layout request is pending")
            if (request.generation < self._last_generation
                    or (request.enabled
                        and request.generation <= self._last_generation)):
                raise DisplayShellError("shell layout generation is stale")
            if request.request_id in self._consumed_ids:
                raise DisplayShellError("shell layout request id was replayed")
            self._pending = _Pending(request)
            self.on_pending()
            while self._pending is not None and self._pending.result is None:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    self._consume(request.request_id)
                    self._pending = None
                    self._condition.notify_all()
                    raise DisplayShellError("qdshell layout acknowledgement timed out")
                self._condition.wait(timeout=remaining)
            pending = self._pending
            if pending is None:
                raise DisplayShellError("shell layout request was cancelled")
            result = pending.result
            self._consume(request.request_id)
            self._pending = None
            self._condition.notify_all()
            if result != "applied":
                raise DisplayShellError(f"qdshell layout result was {result}")
            if request.enabled:
                self._last_generation = request.generation

    def claim(self) -> dict | None:
        """Return the outstanding request once; caller authentication is external."""
        with self._condition:
            if self._pending is None or self._pending.claimed:
                return None
            if self.clock() >= self._pending.request.expires_at:
                self._consume(self._pending.request.request_id)
                self._pending = None
                self._condition.notify_all()
                return None
            self._pending.claimed = True
            return deepcopy(asdict(self._pending.request))

    def acknowledge(self, *, request_id: str, generation: int,
                    result: str) -> None:
        with self._condition:
            if self._pending is None or not self._pending.claimed:
                raise DisplayShellError("no claimed shell layout request exists")
            request = self._pending.request
            if request_id != request.request_id:
                raise DisplayShellError("shell layout acknowledgement id mismatch")
            if generation != request.generation:
                raise DisplayShellError(
                    "shell layout acknowledgement generation mismatch")
            if result not in RESULTS:
                raise DisplayShellError("shell layout acknowledgement result is invalid")
            if self._pending.result is not None:
                raise DisplayShellError("shell layout request was already acknowledged")
            self._pending.result = result
            self._condition.notify_all()

    def cancel(self) -> None:
        """Fail an outstanding waiter during controller shutdown."""
        with self._condition:
            if self._pending is not None:
                self._consume(self._pending.request.request_id)
                self._pending = None
                self._condition.notify_all()

    def safe_state_confirmed(self, _slot_name: str) -> bool:
        with self._condition:
            return self._pending is None
