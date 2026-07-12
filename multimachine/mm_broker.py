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
- consumes paired-machine grants from the inherited configuration fd and
  authorizes the exact dock generation, UI attachment, and input capability
  before registry mutation or wrapper launch;
- resolves viewer qdwin handles to streams by secctx ``app_id`` (impl-30 Q6),
  while ``BindHandleIdentity`` returns the paired trust-domain facts only after
  that correlation succeeds;
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

import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .harness.viewer_broker import RemotePeer, ViewerBroker
from .origin_authority import OriginGrant, StaticOriginAuthority
from .rdp_client_wrapper import StreamSpec


class WrapperHandle(Protocol):
    """What the session needs from a per-stream pixel-backend wrapper. In
    production this is a socket client to the `qdistro-mm-rdp-client-wrapper`
    process; in tests a fake. Duck-typed so the session never imports a transport."""

    def alive(self) -> bool: ...
    def process_truth(self) -> dict: ...
    def teardown(self, stream_id: str, generation: int, token: str) -> bool: ...


class SocketWrapperHandle:
    """Live broker-side handle for one wrapper subprocess.

    OTP and teardown token are delivered through inherited pipes.  Neither
    secret is placed in argv or the environment.  All subsequent supervision
    uses the wrapper's mode-0600 Unix socket.
    """

    def __init__(self, spec: StreamSpec, *, teardown_token: str, otp: str,
                 socket_path: str, wrapper_program: str,
                 rdp_client: str = "sdl-freerdp",
                 secctx_exec: str = "qdistro-secctx-exec",
                 ready_timeout: float = 15.0) -> None:
        self.spec = spec
        self.socket_path = socket_path
        self._proc: subprocess.Popen | None = None

        otp_r, otp_w = os.pipe()
        token_r, token_w = os.pipe()
        argv = [
            wrapper_program,
            "--origin", spec.origin,
            "--stream-id", spec.stream_id,
            "--generation", str(spec.generation),
            "--app-id", spec.app_id,
            "--instance-id", spec.instance_id,
            "--rdp-host", spec.rdp_host,
            "--rdp-port", str(spec.rdp_port),
            "--width", str(spec.width),
            "--height", str(spec.height),
            "--rdp-user", spec.rdp_user,
            "--allow-input", str(spec.allow_input),
            "--rdp-client", rdp_client,
            "--secctx-exec", secctx_exec,
            "--control-socket", socket_path,
            "--otp-fd", str(otp_r),
            "--teardown-token-fd", str(token_r),
        ]
        try:
            self._proc = subprocess.Popen(argv, pass_fds=(otp_r, token_r),
                                          start_new_session=True)
        finally:
            os.close(otp_r)
            os.close(token_r)
        try:
            os.write(otp_w, (otp + "\n").encode())
            os.write(token_w, (teardown_token + "\n").encode())
        finally:
            os.close(otp_w)
            os.close(token_w)

        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if os.path.exists(socket_path):
                return
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"wrapper exited before its socket was ready: "
                    f"status={self._proc.returncode}")
            time.sleep(0.05)
        raise TimeoutError(f"wrapper socket was not ready: {socket_path}")

    def _request(self, request: dict) -> dict:
        data = (json.dumps(request, separators=(",", ":")) + "\n").encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(self.socket_path)
            client.sendall(data)
            buf = b""
            while b"\n" not in buf and len(buf) <= 65536:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buf += chunk
        if b"\n" not in buf:
            raise RuntimeError("wrapper returned no complete response")
        response = json.loads(buf.split(b"\n", 1)[0])
        if not isinstance(response, dict):
            raise RuntimeError("wrapper returned a non-object response")
        return response

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def process_truth(self) -> dict:
        try:
            return self._request({"cmd": "status"})
        except (OSError, ValueError, RuntimeError):
            status = None if self._proc is None else self._proc.poll()
            return {
                "stream_id": self.spec.stream_id,
                "generation": self.spec.generation,
                "app_id": self.spec.app_id,
                "pid": None if self._proc is None else self._proc.pid,
                "alive": status is None and self._proc is not None,
                "exit_status": status,
            }

    def teardown(self, stream_id: str, generation: int, token: str) -> bool:
        try:
            response = self._request({
                "cmd": "teardown", "stream_id": stream_id,
                "generation": generation, "token": token,
            })
        except (OSError, ValueError, RuntimeError):
            return False
        return response.get("accepted") is True


@dataclass
class _StreamReg:
    label: str
    spec: StreamSpec
    teardown_token: str
    origin_grant: OriginGrant
    wrapper: WrapperHandle | None = None
    backend_lost_emitted: bool = False
    finalized: bool = False


@dataclass(frozen=True)
class ConfiguredStream:
    """One fully parsed stream from the inherited session description."""

    label: str
    spec: StreamSpec
    control_port: int
    rdp_unit: str
    marker_unit: str
    otp: str
    control_capability: str
    connect_timeout: float


@dataclass(frozen=True)
class SessionConfig:
    """Side-effect-free, authority-checked broker startup configuration."""

    control_host: str
    origin_authority: StaticOriginAuthority
    streams: tuple[ConfiguredStream, ...]


def parse_session_config(raw: object) -> SessionConfig:
    """Validate the entire inherited config before any live resource is opened.

    In particular, a malformed or unauthorized later stream must not leave an
    earlier control listener, upstream connection, or pixel wrapper running.
    Pairing facts and secret-bearing stream material share the inherited-fd
    trust boundary, but remain separate objects in the parsed result.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("broker config must be an object")
    control_host = raw.get("control_host", "127.0.0.1")
    if not isinstance(control_host, str) or not control_host:
        raise ValueError("control_host must be a non-empty string")
    authority = StaticOriginAuthority.from_config(raw.get("origins"))
    stream_items = raw.get("streams")
    if not isinstance(stream_items, list) or not stream_items:
        raise ValueError("broker config must contain a non-empty streams array")

    streams: list[ConfiguredStream] = []
    labels: set[str] = set()
    app_ids: set[str] = set()
    control_ports: set[int] = set()
    rdp_endpoints: set[tuple[str, int]] = set()
    for index, item in enumerate(stream_items):
        prefix = f"streams[{index}]"
        if not isinstance(item, Mapping) or not isinstance(item.get("spec"), Mapping):
            raise ValueError(f"{prefix} must be an object with a spec object")
        label = item.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError(f"{prefix}.label must be a non-empty string")
        if label in labels:
            raise ValueError(f"duplicate stream label {label!r}")
        try:
            spec = StreamSpec(**item["spec"])
            spec.validate()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{prefix}.spec is invalid: {exc}") from exc
        authority.authorize(spec)

        control_port = item.get("control_port")
        if (not isinstance(control_port, int) or isinstance(control_port, bool)
                or control_port <= 0 or control_port > 65535):
            raise ValueError(f"{prefix}.control_port must be an integer 1..65535")
        if control_port in control_ports:
            raise ValueError(f"duplicate control_port {control_port}")
        if spec.app_id in app_ids:
            raise ValueError(f"duplicate secctx app_id {spec.app_id!r}")
        endpoint = (spec.rdp_host, spec.rdp_port)
        if endpoint in rdp_endpoints:
            raise ValueError(f"duplicate RDP endpoint {endpoint!r}")

        strings: dict[str, str] = {}
        for name in ("rdp_unit", "marker_unit", "otp", "control_capability"):
            value = item.get(name)
            if not isinstance(value, str):
                raise ValueError(f"{prefix}.{name} must be a string")
            if name in ("otp", "control_capability") and not value:
                raise ValueError(f"{prefix}.{name} must be non-empty")
            strings[name] = value
        connect_timeout = item.get("connect_timeout", 30.0)
        if (not isinstance(connect_timeout, (int, float))
                or isinstance(connect_timeout, bool) or connect_timeout <= 0):
            raise ValueError(f"{prefix}.connect_timeout must be positive")

        streams.append(ConfiguredStream(
            label=label, spec=spec, control_port=control_port,
            rdp_unit=strings["rdp_unit"], marker_unit=strings["marker_unit"],
            otp=strings["otp"],
            control_capability=strings["control_capability"],
            connect_timeout=float(connect_timeout),
        ))
        labels.add(label)
        app_ids.add(spec.app_id)
        control_ports.add(control_port)
        rdp_endpoints.add(endpoint)
    return SessionConfig(control_host=control_host, origin_authority=authority,
                         streams=tuple(streams))


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
                 origin_authority: StaticOriginAuthority,
                 on_event: Callable[[Event], None] | None = None,
                 gen_token: Callable[[], str] | None = None) -> None:
        self.broker = ViewerBroker(control_host=control_host)
        self._spawn_wrapper = spawn_wrapper
        self._origin_authority = origin_authority
        self._on_event = on_event or (lambda e: None)
        self._gen_token = gen_token or (lambda: secrets.token_hex(16))
        self.regs: dict[str, _StreamReg] = {}

    def _emit(self, kind: str, label: str, **detail) -> None:
        self._on_event(Event(kind, label, detail))

    # ---- registration + connect (fail-closed Announce match) ----------
    def register_stream(self, label: str, *, spec: StreamSpec, rdp_unit: str,
                        control_port: int, marker_unit: str,
                        control_capability: str = "") -> _StreamReg:
        """Register a stream the source announced. ``spec`` is the wrapper launch
        spec (origin/stream_id/generation/app_id/...) — it is validated.

        Registration is fail-closed and atomic: duplicate human labels, secctx
        attribution keys, or transport endpoints are rejected by ``ViewerBroker``
        before either registry is mutated.  This matters once two origins may
        independently choose the same per-machine stream label (rung 2)."""
        spec.validate()
        origin_grant = self._origin_authority.authorize(spec)
        for existing in self.regs.values():
            if (existing.spec.rdp_host, existing.spec.rdp_port) == \
                    (spec.rdp_host, spec.rdp_port):
                raise ValueError(
                    f"duplicate RDP endpoint {spec.rdp_host}:{spec.rdp_port} "
                    f"for {existing.label!r} and {label!r}")
        teardown_token = self._gen_token()
        self.broker.add_stream(
            label, origin=spec.origin, app_id=spec.app_id, rdp_unit=rdp_unit,
            relay_port=spec.rdp_port, control_port=control_port,
            marker_unit=marker_unit, window_id=0, allow_input=spec.allow_input,
            expect_generation=spec.generation,
            control_capability=control_capability)
        reg = _StreamReg(label=label, spec=spec,
                         teardown_token=teardown_token,
                         origin_grant=origin_grant)
        self.regs[label] = reg
        return reg

    def connect(self, label: str, timeout: float = 30.0) -> str:
        """Connect to the stream's mm-control (FAIL-CLOSED on a mismatched
        Announce) and learn the in-guest-minted stream_id. Emits ``announced``."""
        sid = self.broker.connect(label, timeout=timeout)
        # the source-minted stream_id is authoritative — adopt it for the wrapper
        # spec + teardown identity (the spec's stream_id was the EXPECTED one).
        # NOTE the secctx app_id (qdistro.mm.<origin>.<stream_label>) encodes the
        # stream LABEL, a SEPARATE namespace from the minted stream_id; adopting
        # sid therefore does NOT make app_id inconsistent (handle attribution
        # matches by app_id, teardown by stream_id). `ViewerBroker.connect` already
        # fail-closed on the app_id/window_id/generation match; we re-validate the
        # adopted spec so a bad sid can't yield an unlaunchable spec (impl-36 MED).
        reg = self.regs[label]
        if sid and sid != reg.spec.stream_id:
            reg.spec = replace(reg.spec, stream_id=sid)
            reg.spec.validate()
        peer = self.broker.peers[label]
        self._emit("announced", label, stream_id=sid, app_id=peer.app_id,
                   allow_input=peer.allow_input, generation=peer.generation,
                   origin=peer.origin,
                   trust_domain_id=reg.origin_grant.trust_domain_id)
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
    def bind_handle(self, secctx_app_id: str, handle: int, *,
                    origin: str = "", stream_id: str = "",
                    generation: str | int = "") -> bool:
        """Resolve a viewer qdwin handle to its stream by secctx ``app_id`` (NEVER
        title/pixels) and bind it. FAIL CLOSED: reject if no registered stream
        matches the app_id, or if it (or this handle) is already bound.

        The D-Bus method ``BindHandle(origin, stream_id, generation,
        secctx_app_id, handle)`` (impl-34 Q2) passes the full identity tuple; the
        redundant ``origin``/``stream_id``/``generation`` are VALIDATED against the
        peer resolved from ``secctx_app_id`` so qdshell and the broker can't
        disagree about what was bound (codex impl-36 HIGH) — empty fields skip that
        check (a 2-arg core caller). Emits ``bound`` on success."""
        match = [lbl for lbl, p in self.broker.peers.items()
                 if p.app_id == secctx_app_id]
        if len(match) != 1:
            return False
        label = match[0]
        peer = self.broker.peers[label]
        reg = self.regs[label]
        # validate the redundant identity fields when the caller supplied them.
        if origin and origin != peer.origin:
            return False
        if stream_id and stream_id != peer.stream_id:
            return False
        if generation != "" and generation is not None:
            try:
                if int(generation) != peer.generation:
                    return False
            except (TypeError, ValueError):
                return False
        if peer.handle is not None:
            return peer.handle == handle      # idempotent rebind to the same handle
        if self.broker.peer_for_handle(handle) is not None:
            return False                      # handle already attributed elsewhere
        self.broker.bind_handle(label, handle)
        self._emit("bound", label, handle=handle, stream_id=peer.stream_id,
                   origin=peer.origin,
                   trust_domain_id=reg.origin_grant.trust_domain_id)
        return True

    def bound_identity(self, handle: int) -> dict | None:
        """Return the broker-vouched presentation identity for a bound handle.

        qdshell may use this only after :meth:`bind_handle` succeeds.  The
        secctx-derived origin is not trust chrome by itself; this result joins
        it to the paired grant and exact source-minted stream record."""
        peer = self.broker.peer_for_handle(handle)
        if peer is None:
            return None
        reg = self.regs.get(peer.label)
        if reg is None:
            return None
        return {
            "handle": handle,
            "origin": peer.origin,
            "stream_id": peer.stream_id,
            "generation": peer.generation,
            "trust_domain_id": reg.origin_grant.trust_domain_id,
            "allow_input": peer.allow_input,
        }

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
        the close ORDERING the gate proves. Idempotent (codex impl-36 LOW): a
        second call after a successful finalize is a no-op. Emits ``closed`` once."""
        reg = self.regs[label]
        if reg.finalized:
            return True
        closed = self.broker.wait_closed(label, timeout=timeout)
        if closed is None:
            return False                      # no source Closed yet — do NOT tear down
        if reg.wrapper is not None:
            ok = reg.wrapper.teardown(reg.spec.stream_id, reg.spec.generation,
                                      reg.teardown_token)
            if not ok:
                return False
        # Retire the handle association so a REUSED qdwin handle can't later
        # resolve to this now-dead peer (codex impl-36 MED). The record stays
        # (close_state 'closed') for status, but it owns no handle.
        peer = self.broker.peers.get(label)
        if peer is not None:
            peer.handle = None
        reg.finalized = True
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
        st["trust_domain_id"] = (
            reg.origin_grant.trust_domain_id if reg else "")
        return st

    def close(self) -> None:
        self.broker.close()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live shell
    """``qdistro-mm-broker``: stand up a :class:`MultiMachineSession`, spawn a real
    `qdistro-mm-rdp-client-wrapper` per stream, and serve the D-Bus interface
    ``org.qdistro.MultiMachine1`` for qdshell. The orchestration core is
    unit-tested; only the D-Bus server + real wrapper subprocess/socket plumbing
    live here. (Wired in the live-gate step — see the session-11 RESUME HERE.)"""
    import argparse
    import signal

    ap = argparse.ArgumentParser(prog="qdistro-mm-broker")
    ap.add_argument("--config-fd", type=int, default=0,
                    help="fd containing the one-shot JSON session description")
    ap.add_argument("--wrapper-program", default="qdistro-mm-rdp-client-wrapper")
    ap.add_argument("--rdp-client", default="sdl-freerdp")
    ap.add_argument("--secctx-exec", default="qdistro-secctx-exec")
    ap.add_argument("--runtime-dir", default="")
    args = ap.parse_args(argv)

    # The session description includes single-use RDP OTPs, so it is read from
    # an inherited fd, never argv or the environment.  Expected shape:
    # {"control_host":"...", "origins":[{"machine_id":"vm-a",
    #   "trust_domain_id":"owner-machines", "generation":51,
    #   "capabilities":["attach_ui","receive_input"]}],
    #   "streams":[{"label":"a", "spec":{...},
    #   "control_port":5571, "rdp_unit":"...", "marker_unit":"...",
    #   "otp":"...", "control_capability":"..."}]}
    with os.fdopen(os.dup(args.config_fd), "r", encoding="utf-8") as stream:
        config = parse_session_config(json.load(stream))

    runtime_parent = args.runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    socket_dir = Path(tempfile.mkdtemp(prefix="qdistro-mm-", dir=runtime_parent))
    socket_dir.chmod(0o700)
    wrapper_index = {"value": 0}

    def spawn_wrapper(spec: StreamSpec, *, teardown_token: str,
                      otp: str) -> SocketWrapperHandle:
        wrapper_index["value"] += 1
        path = socket_dir / f"wrapper-{wrapper_index['value']}.sock"
        return SocketWrapperHandle(
            spec, teardown_token=teardown_token, otp=otp,
            socket_path=str(path), wrapper_program=args.wrapper_program,
            rdp_client=args.rdp_client, secctx_exec=args.secctx_exec)

    pending_events: list[Event] = []
    session = MultiMachineSession(
        control_host=config.control_host,
        spawn_wrapper=spawn_wrapper, origin_authority=config.origin_authority,
        on_event=pending_events.append)

    # dbus-python matches the rest of qdistro's session daemons and is present in
    # the image.  Claim the name BEFORE launching FreeRDP: its toplevel can map
    # immediately, and qdshell's one-shot BindHandle must never race a missing
    # bus name. Calls queue until the GLib loop starts after initialization.
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBUS_NAME = "org.qdistro.MultiMachine1"
    DBUS_PATH = "/org/qdistro/MultiMachine1"
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    name = dbus.service.BusName(DBUS_NAME, bus=bus, do_not_queue=True)

    class MultiMachineDBus(dbus.service.Object):
        def __init__(self) -> None:
            super().__init__(name, DBUS_PATH)

        @dbus.service.method(DBUS_NAME, in_signature="sssst", out_signature="b")
        def BindHandle(self, origin, stream_id, generation, secctx_app_id,
                       handle):
            return session.bind_handle(
                str(secctx_app_id), int(handle), origin=str(origin),
                stream_id=str(stream_id), generation=str(generation))

        @dbus.service.method(DBUS_NAME, in_signature="sssst", out_signature="s")
        def BindHandleIdentity(self, origin, stream_id, generation,
                               secctx_app_id, handle):
            try:
                accepted = session.bind_handle(
                    str(secctx_app_id), int(handle), origin=str(origin),
                    stream_id=str(stream_id), generation=str(generation))
                identity = session.bound_identity(int(handle)) if accepted else None
                return (json.dumps(identity, sort_keys=True,
                                   separators=(",", ":"))
                        if identity is not None else "")
            except (KeyError, TypeError, ValueError):
                return ""

        @dbus.service.method(DBUS_NAME, in_signature="t", out_signature="b")
        def RequestClose(self, handle):
            try:
                return session.request_close(int(handle)) is not None
            except (KeyError, OSError, RuntimeError):
                return False

        @dbus.service.signal(DBUS_NAME, signature="sss")
        def BrokerEvent(self, kind, label, detail_json):
            return None

    service = MultiMachineDBus()

    def emit(event: Event) -> None:
        service.BrokerEvent(event.kind, event.label,
                            json.dumps(event.detail, sort_keys=True))

    session._on_event = emit

    # Register every already-validated stream before opening any upstream
    # connection or launching any backend. Duplicate identities/endpoints can
    # therefore never produce a partially live session.
    for configured in config.streams:
        session.register_stream(
            configured.label, spec=configured.spec,
            rdp_unit=configured.rdp_unit,
            control_port=configured.control_port,
            marker_unit=configured.marker_unit,
            control_capability=configured.control_capability)
    # Connect all source control channels before launching the first pixel
    # backend. A missing/unauthenticated source cannot leave a partial UI.
    for configured in config.streams:
        session.connect(configured.label, timeout=configured.connect_timeout)
    for configured in config.streams:
        session.launch_backend(configured.label, configured.otp)

    for event in pending_events:
        emit(event)
    pending_events.clear()

    def supervise() -> bool:
        session.poll_backends()
        # Source-driven Closed may arrive without a viewer RequestClose.  It is
        # authoritative in either case and must trigger ordered finalization.
        for label, reg in tuple(session.regs.items()):
            peer = session.broker.peers.get(label)
            if peer is not None and peer.closed is not None and not reg.finalized:
                session.finalize_close(label, timeout=0)
        return True

    GLib.timeout_add(100, supervise)
    loop = GLib.MainLoop()
    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    signal.signal(signal.SIGINT, lambda *_: loop.quit())
    try:
        loop.run()
    finally:
        session.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
