"""Phase-2 rung-1 durable in-VM broker — ``qdistro-mm-broker`` (codex impl-34 Q1/Q2).

ONE per viewer session. The durable replacement for the host-side rung-1
:class:`~multimachine.harness.viewer_broker.ViewerBroker` stand-in, moved INSIDE
VM-B. It is the authority codex impl-34 Q1 placed centrally:

- owns the authoritative ``remote_managed_toplevel`` registry (a `ViewerBroker`:
  per-stream upstream control sockets, fail-closed `Announce` matching, the
  source-mediated close path);
- supervises ONE `qdistro-mm-rdp-client-wrapper` per stream (process truth +
  token-gated teardown — the wrapper is a SEPARATE process the session talks to
  over the wrapper's broker-private socket; injected here so the orchestration is
  unit-tested with a fake);
- resolves viewer qdwin handles to streams by secctx ``app_id`` (impl-30 Q6) —
  the ``BindHandle`` D-Bus method;
- routes a viewer close UPSTREAM (``RequestClose``) and tears down the pixel
  backend ONLY after the authoritative source ``Closed`` (close ORDERING), with
  the broker-issued teardown token;
- detects ``pixel_backend_lost`` (a wrapper that exits before a source ``Closed``)
  WITHOUT synthesizing a source close.

qdshell (``RemoteMachineWindows``) is a thin D-Bus client of this session
(``org.qdistro.MultiMachine1``); the registry lives HERE, not in QML, because the
upstream control plane crosses the VM-A trust boundary (impl-34 Q1).

The pure orchestration (:class:`MultiMachineSession`) is unit-tested with a fake
wrapper backend + the REAL `ViewerBroker` + REAL `control_source.watch` loops;
the D-Bus server + the real wrapper subprocess/socket live in :func:`main` (the
thin, not-unit-tested shell).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .harness.viewer_broker import RemotePeer, ViewerBroker
from .rdp_client_wrapper import StreamSpec


class WrapperHandle(Protocol):
    """What the session needs from a per-stream pixel-backend wrapper. In
    production this is a socket client to the `qdistro-mm-rdp-client-wrapper`
    process; in tests a fake. Duck-typed so the session never imports a transport."""

    def alive(self) -> bool: ...
    def process_truth(self) -> dict: ...
    def teardown(self, stream_id: str, generation: int, token: str) -> bool: ...


@dataclass
class _StreamReg:
    label: str
    spec: StreamSpec
    teardown_token: str
    wrapper: WrapperHandle | None = None
    backend_lost_emitted: bool = False


# D-Bus-shaped events the session emits; the live D-Bus server maps these to
# org.qdistro.MultiMachine1 signals, qdshell mirrors them.
@dataclass
class Event:
    kind: str                 # announced | bound | close_pending | closed | pixel_backend_lost
    label: str
    detail: dict = field(default_factory=dict)


class MultiMachineSession:
    """The pure broker-daemon orchestration (codex impl-34 Q1).

    ``spawn_wrapper(spec, teardown_token, otp) -> WrapperHandle`` launches the
    per-stream pixel backend and returns the handle the session supervises
    (injected for tests). ``on_event`` receives :class:`Event`\\ s (the D-Bus
    signals). ``gen_token`` mints the per-stream teardown token (injectable so
    tests are deterministic; defaults to a CSPRNG)."""

    def __init__(self, *, control_host: str = "127.0.0.1",
                 spawn_wrapper: Callable[..., WrapperHandle],
                 on_event: Callable[[Event], None] | None = None,
                 gen_token: Callable[[], str] | None = None) -> None:
        self.broker = ViewerBroker(control_host=control_host)
        self._spawn_wrapper = spawn_wrapper
        self._on_event = on_event or (lambda e: None)
        self._gen_token = gen_token or (lambda: secrets.token_hex(16))
        self.regs: dict[str, _StreamReg] = {}

    def _emit(self, kind: str, label: str, **detail) -> None:
        self._on_event(Event(kind, label, detail))

    # ---- registration + connect (fail-closed Announce match) ----------
    def register_stream(self, label: str, *, spec: StreamSpec, rdp_unit: str,
                        control_port: int, marker_unit: str) -> _StreamReg:
        """Register a stream the source announced. ``spec`` is the wrapper launch
        spec (origin/stream_id/generation/app_id/...) — it is validated."""
        spec.validate()
        self.broker.add_stream(
            label, origin=spec.origin, app_id=spec.app_id, rdp_unit=rdp_unit,
            relay_port=spec.rdp_port, control_port=control_port,
            marker_unit=marker_unit, window_id=0, allow_input=spec.allow_input,
            expect_generation=spec.generation)
        reg = _StreamReg(label=label, spec=spec, teardown_token=self._gen_token())
        self.regs[label] = reg
        return reg

    def connect(self, label: str, timeout: float = 30.0) -> str:
        """Connect to the stream's mm-control (FAIL-CLOSED on a mismatched
        Announce) and learn the in-guest-minted stream_id. Emits ``announced``."""
        sid = self.broker.connect(label, timeout=timeout)
        # the source-minted stream_id is authoritative — adopt it for the wrapper
        # spec + teardown identity (the spec's stream_id was the expected one).
        reg = self.regs[label]
        if sid and sid != reg.spec.stream_id:
            from dataclasses import replace
            reg.spec = replace(reg.spec, stream_id=sid)
        peer = self.broker.peers[label]
        self._emit("announced", label, stream_id=sid, app_id=peer.app_id,
                   allow_input=peer.allow_input, generation=peer.generation)
        return sid

    # ---- pixel backend (wrapper) supervision --------------------------
    def launch_backend(self, label: str, otp: str) -> WrapperHandle:
        """Spawn the windowed-FreeRDP wrapper for this stream under its
        broker-issued teardown token (the OTP + token never ride the wrapper's
        own argv — the wrapper reads them off fds; impl-34 Q5)."""
        reg = self.regs[label]
        reg.wrapper = self._spawn_wrapper(
            reg.spec, teardown_token=reg.teardown_token, otp=otp)
        return reg.wrapper

    # ---- handle attribution (BindHandle, impl-30 Q6) ------------------
    def bind_handle(self, secctx_app_id: str, handle: int) -> bool:
        """Resolve a viewer qdwin handle to its stream by secctx ``app_id`` (NEVER
        title/pixels) and bind it. FAIL CLOSED: reject if no registered stream
        matches the app_id, or if it (or this handle) is already bound. Emits
        ``bound`` on success."""
        match = [lbl for lbl, p in self.broker.peers.items()
                 if p.app_id == secctx_app_id]
        if len(match) != 1:
            return False
        label = match[0]
        peer = self.broker.peers[label]
        if peer.handle is not None:
            return peer.handle == handle      # idempotent rebind to the same handle
        if self.broker.peer_for_handle(handle) is not None:
            return False                      # handle already attributed elsewhere
        self.broker.bind_handle(label, handle)
        self._emit("bound", label, handle=handle, stream_id=peer.stream_id)
        return True

    # ---- close (source-mediated, ordered) -----------------------------
    def request_close(self, handle: int) -> str | None:
        """Viewer close on a qdwin handle → resolve to the stream and send
        ``CloseRequest`` UPSTREAM (does NOT touch the pixel backend). Emits
        ``close_pending``. Returns the label, or None if the handle is unknown."""
        peer = self.broker.peer_for_handle(handle)
        if peer is None:
            return None
        self.broker.request_source_close(peer.label)
        self._emit("close_pending", peer.label, handle=handle,
                   stream_id=peer.stream_id)
        return peer.label

    def finalize_close(self, label: str, timeout: float = 20.0) -> bool:
        """Block until the authoritative source ``Closed`` arrives, THEN tear down
        ONLY this peer's pixel backend with the broker token. Returns True once the
        backend is torn down. The teardown is token-gated AND only after Closed —
        the close ORDERING the gate proves. Emits ``closed``."""
        closed = self.broker.wait_closed(label, timeout=timeout)
        if closed is None:
            return False                      # no source Closed yet — do NOT tear down
        reg = self.regs[label]
        if reg.wrapper is not None:
            ok = reg.wrapper.teardown(reg.spec.stream_id, reg.spec.generation,
                                      reg.teardown_token)
            if not ok:
                return False
        self._emit("closed", label, stream_id=reg.spec.stream_id,
                   reason=closed.reason)
        return True

    # ---- pixel-backend-lost (impl-34 Q3) ------------------------------
    def poll_backends(self) -> None:
        """Detect any wrapper that exited BEFORE a source ``Closed`` → mark the
        record ``pixel_backend_lost`` (keep it; do NOT synthesize a source close)
        and emit ``pixel_backend_lost`` ONCE. A later real ``Closed`` still wins."""
        for label, reg in self.regs.items():
            peer = self.broker.peers.get(label)
            if peer is None or reg.wrapper is None:
                continue
            if (not reg.wrapper.alive() and peer.closed is None
                    and not reg.backend_lost_emitted):
                self.broker.note_backend_exit(label)
                reg.backend_lost_emitted = True
                self._emit("pixel_backend_lost", label,
                           stream_id=peer.stream_id,
                           exit_status=reg.wrapper.process_truth().get("exit_status"))

    def status(self, label: str) -> dict:
        st = self.broker.status(label)
        reg = self.regs.get(label)
        st["backend"] = (reg.wrapper.process_truth()
                         if reg and reg.wrapper is not None else None)
        return st

    def close(self) -> None:
        self.broker.close()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live shell
    """``qdistro-mm-broker``: stand up a :class:`MultiMachineSession`, spawn a real
    `qdistro-mm-rdp-client-wrapper` per stream, and serve the D-Bus interface
    ``org.qdistro.MultiMachine1`` for qdshell. The orchestration core is
    unit-tested; only the D-Bus server + real wrapper subprocess/socket plumbing
    live here. (Wired in the live-gate step — see the session-11 RESUME HERE.)"""
    raise NotImplementedError(
        "qdistro-mm-broker D-Bus shell is wired in the live-gate integration step")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
