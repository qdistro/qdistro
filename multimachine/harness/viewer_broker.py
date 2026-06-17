"""Phase-2 rung-1 VIEWER BROKER (codex impl-32 Q3/Q4).

The thin, host-side viewer authority for the rung-1 gate. It is the deliberate
rung-1 stand-in for the durable `qdistro-mm-rdp-client-wrapper` / qdshell
`RemoteMachineWindows` service (deferred to rung-1-fold/rung-2): it owns the
backend-swappable `remote_managed_toplevel` records and the source-mediated
close path WITHOUT becoming the lifecycle authority itself.

For each announced source stream it holds a :class:`RemotePeer` record keyed by a
human label, learns the in-guest-minted `stream_id` from the source's `Announce`
over the per-stream control socket, and — crucially — routes a viewer close
through the SOURCE: `request_source_close(label)` sends `CloseRequest(stream_id)`
upstream; the source closes its own toplevel and emits `Closed(stream_id)`; only
THEN does the caller tear down that one peer's FreeRDP backend. The broker never
kills the FreeRDP client tree to "close" a window (the forbidden tier-4 path) and
never infers source lifecycle from viewer-process death.

The control plane is VM-A-served (`multimachine.control_source`); the broker
reaches each stream's `mm-control-<label>` at ``<control_host>:<control_port>``
— on the host that is the QEMU hostfwd loopback for VM-A's port (the same bridge
VM-B would use via 10.0.2.2). A reader thread per connection captures the
`Announce` then any source-driven `Closed`, so close ordering is observable.

Handle↔stream attribution (impl-30 Q6: secctx, never title) is the caller's job
via the bystander's `toplevel_security_context` app_id; the caller records it
with :meth:`bind_handle`. The broker only needs the map to resolve a viewer
handle back to its `stream_id` for the close request.
"""
from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field

from ..sidechannel import Announce, CloseRequest, Closed, decode, encode


@dataclass
class RemotePeer:
    """One backend-swappable remote managed toplevel record (rung-1 RDP backend).

    Mirrors the durable `remote_managed_toplevel` shape (impl-30 Q2) the qdshell
    service will own later: origin + stream identity, the viewer qdwin handle, the
    pixel-backend process identity (here a systemd RDP unit), and close state.
    """

    label: str
    origin: str
    app_id: str
    rdp_unit: str
    relay_port: int
    control_port: int
    marker_unit: str
    window_id: int = 0
    allow_input: int = 1
    expect_generation: int | None = None    # verified against the Announce
    # learned at runtime:
    stream_id: str = ""
    handle: int | None = None
    close_state: str = "open"            # open | close_requested | closed
    announce: Announce | None = None
    closed: Closed | None = None
    generation: int = 0
    _conn: socket.socket | None = field(default=None, repr=False)
    _reader: "threading.Thread | None" = field(default=None, repr=False)
    _buf: str = field(default="", repr=False)


class ViewerBroker:
    """Owns the per-stream control sockets + the handle↔stream map (rung-1)."""

    def __init__(self, control_host: str = "127.0.0.1") -> None:
        self.control_host = control_host
        self.peers: dict[str, RemotePeer] = {}
        self._lock = threading.Lock()

    # ---- registration -------------------------------------------------
    def add_stream(self, label: str, *, origin: str, app_id: str, rdp_unit: str,
                   relay_port: int, control_port: int, marker_unit: str,
                   window_id: int = 0, allow_input: int = 1,
                   expect_generation: int | None = None) -> RemotePeer:
        peer = RemotePeer(
            label=label, origin=origin, app_id=app_id, rdp_unit=rdp_unit,
            relay_port=relay_port, control_port=control_port,
            marker_unit=marker_unit, window_id=window_id, allow_input=allow_input,
            expect_generation=expect_generation)
        self.peers[label] = peer
        return peer

    def bind_handle(self, label: str, handle: int) -> None:
        """Record the viewer qdwin handle the caller resolved for this stream via
        the bystander's secctx app_id (impl-30 Q6 attribution)."""
        self.peers[label].handle = handle

    def peer_for_handle(self, handle: int) -> RemotePeer | None:
        for p in self.peers.values():
            if p.handle == handle:
                return p
        return None

    # ---- control-socket lifecycle ------------------------------------
    def connect(self, label: str, timeout: float = 30.0) -> str:
        """Connect to this stream's mm-control, read its `Announce`, and learn the
        in-guest-minted `stream_id`. Starts a reader thread that captures any
        later source-driven `Closed`. Returns the stream_id."""
        peer = self.peers[label]
        s = socket.create_connection((self.control_host, peer.control_port),
                                     timeout=timeout)
        s.settimeout(None)
        peer._conn = s
        # read the Announce synchronously so the stream_id is known on return.
        ann = self._read_message(peer, timeout=timeout)
        if not isinstance(ann, Announce):
            raise RuntimeError(
                f"{label}: first control message was {ann!r}, expected Announce")
        # FAIL CLOSED on a mismatched Announce (codex impl-33 MEDIUM): a stale
        # hostfwd / leftover control unit could route this port to the wrong
        # (still syntactically valid) source. The app_id, the window_id, and the
        # generation must match what we asked the source to export, else the
        # stream_id we'd learn is for the wrong stream.
        if ann.meta.app_id != peer.app_id:
            raise RuntimeError(f"{label}: Announce app_id={ann.meta.app_id!r} "
                               f"!= expected {peer.app_id!r} (stale control?)")
        if peer.window_id and ann.meta.window_id != peer.window_id:
            raise RuntimeError(f"{label}: Announce window_id={ann.meta.window_id} "
                               f"!= expected {peer.window_id}")
        if peer.expect_generation is not None and ann.generation != peer.expect_generation:
            raise RuntimeError(f"{label}: Announce generation={ann.generation} "
                               f"!= expected {peer.expect_generation} (stale control?)")
        with self._lock:
            peer.announce = ann
            peer.stream_id = ann.meta.stream_id
            peer.generation = ann.generation
            if not peer.window_id:
                peer.window_id = ann.meta.window_id
        t = threading.Thread(target=self._reader_loop, args=(peer,), daemon=True)
        peer._reader = t
        t.start()
        return peer.stream_id

    def _read_message(self, peer: RemotePeer, timeout: float | None = None):
        """Read ONE decoded control message off this peer's socket (line-framed).
        Single-reader invariant: ONLY called for the initial synchronous Announce,
        BEFORE the reader thread starts — so `_buf`/`_conn` have exactly one
        consumer at all times (codex impl-33 LOW). The reader thread owns them
        afterwards."""
        assert peer._reader is None, "_read_message after reader thread started"
        s = peer._conn
        assert s is not None
        s.settimeout(timeout)
        while "\n" not in peer._buf:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError(f"{peer.label}: control EOF before a message")
            peer._buf += chunk.decode("utf-8", "replace")
        line, peer._buf = peer._buf.split("\n", 1)
        s.settimeout(None)
        return decode(line.strip())

    def _reader_loop(self, peer: RemotePeer) -> None:
        s = peer._conn
        if s is None:
            return
        try:
            while True:
                # drain any already-buffered complete lines first.
                while "\n" in peer._buf:
                    line, peer._buf = peer._buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = decode(line)
                    except (ValueError, KeyError):
                        continue
                    if isinstance(msg, Closed) and msg.stream_id == peer.stream_id:
                        with self._lock:
                            peer.closed = msg
                            peer.close_state = "closed"
                        return
                chunk = s.recv(4096)
                if not chunk:
                    return
                peer._buf += chunk.decode("utf-8", "replace")
        except OSError:
            return

    # ---- source-mediated close ---------------------------------------
    def request_source_close(self, label: str) -> None:
        """The viewer close action: resolve label→stream_id and send
        `CloseRequest(stream_id)` UPSTREAM. The peer stays visible + its FreeRDP
        backend stays ALIVE (close-pending) — teardown waits for source `Closed`.
        This is NOT bystander `close <handle>` (that xdg-closes FreeRDP = the
        forbidden client-tree kill)."""
        peer = self.peers[label]
        if peer._conn is None or not peer.stream_id:
            raise RuntimeError(f"{label}: not connected / no stream_id")
        msg = CloseRequest("close_request", peer.generation, peer.window_id,
                           peer.stream_id)
        peer._conn.sendall((encode(msg) + "\n").encode())
        with self._lock:
            if peer.close_state == "open":
                peer.close_state = "close_requested"

    def request_source_close_by_handle(self, handle: int) -> str:
        """Decoration-close arriving on a viewer qdwin handle: resolve it to the
        stream and route upstream. Returns the label closed."""
        peer = self.peer_for_handle(handle)
        if peer is None:
            raise RuntimeError(f"no peer bound to handle {handle}")
        self.request_source_close(peer.label)
        return peer.label

    def wait_closed(self, label: str, timeout: float = 20.0) -> Closed | None:
        """Block until the source emits `Closed(stream_id)` for this peer (the
        authoritative source-driven teardown signal), or timeout. The caller then
        — and only then — tears down that one peer's FreeRDP backend."""
        import time
        peer = self.peers[label]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if peer.closed is not None:
                    return peer.closed
            time.sleep(0.1)
        with self._lock:
            return peer.closed

    def status(self, label: str) -> dict:
        peer = self.peers[label]
        with self._lock:
            return {
                "label": peer.label, "origin": peer.origin,
                "app_id": peer.app_id, "stream_id": peer.stream_id,
                "handle": peer.handle, "rdp_unit": peer.rdp_unit,
                "relay_port": peer.relay_port, "control_port": peer.control_port,
                "marker_unit": peer.marker_unit, "window_id": peer.window_id,
                "allow_input": peer.allow_input, "close_state": peer.close_state,
                "closed_reason": peer.closed.reason if peer.closed else "",
            }

    def close(self) -> None:
        for peer in self.peers.values():
            if peer._conn is not None:
                try:
                    peer._conn.close()
                except OSError:
                    pass
                peer._conn = None
