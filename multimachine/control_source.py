"""Source-side (VM-A) control producer — the `mm-control` unit (codex impl-12).

Product shape: the JSON-lines control side-channel must ORIGINATE on the source
machine (VM-A), not the host. Session-4's managed gate was honest-but-not-product:
a host Python `ControlServer` produced the `Announce`/`Closed` bytes (from
source-derived content) and the host orchestrator drove teardown by killing the
marker itself. This module closes that gap — it is the VM-A `systemd --user` unit
that:

- binds a TCP port inside VM-A (reached by VM-B over the same host-loopback bridge
  the RDP pixels use: a VM-A QEMU ``hostfwd_add`` exposes it on host loopback, VM-B
  connects via ``10.0.2.2:<port>`` over its own NAT — symmetric with the relay),
- accepts the single viewer client and emits the source-derived ``Announce``
  (built IN-GUEST; ``stream_id`` minted in-guest — it is an opaque correlation
  token, not a credential, and the shipped qdwin approval carries none), then
- runs a **watcher** that emits ``Closed`` when the SOURCE toplevel dies, driven by
  the source itself (the exported marker's own unit going inactive), NOT by a host
  inject. A viewer detach (socket EOF) is NOT source death → it emits NO ``Closed``
  (the source window is still alive; the viewer simply went away).

Honesty fence (impl-12): this proves "VM-A-served control using source-derived
metadata" + "VM-A owns the source lifecycle". The marker-unit liveness is a PROXY
for source-toplevel lifetime — NOT the internal qdwin view-stream ``torn_down``,
which has no external signal until qdwin exports one. Stale-generation rejection
stays covered by :class:`~.sidechannel.RemoteViewerState` unit tests + the prior
session-4 live evidence; we deliberately do NOT reintroduce a host-side control
inject hook just to re-prove it here (it would blur the ownership story).

The message construction (:class:`ControlSource`) and the watch decision
(:func:`watch`) are pure + unit-tested headless; :func:`main` wires the real
socket, a ``select``-based viewer poll, and a ``systemctl --user is-active``
liveness probe for the live two-VM gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .bridge import SourceWindowInfo, mint_stream_id
from .sidechannel import (
    Announce, Closed, ControlMessage, RemoteWindowMeta,
)

# What a single viewer-poll observed (one watch iteration):
VIEWER_ALIVE = "alive"   # idle (poll timed out) — connection healthy, no data
VIEWER_DATA = "data"     # the viewer sent upstream bytes (e.g. CloseRequest)
VIEWER_EOF = "eof"       # the viewer socket closed (detach) — NOT source death


@dataclass
class ControlSource:
    """The control messages this source serves for ONE exported stream.

    ``meta`` carries the in-guest-minted ``stream_id`` (the correlation join key);
    :meth:`announce` / :meth:`closed` mint the wire messages the viewer applies via
    the shipped :class:`~.sidechannel.RemoteViewerState` contract.
    """

    generation: int
    meta: RemoteWindowMeta

    @classmethod
    def from_source(cls, src: SourceWindowInfo, generation: int,
                    stream_id: str | None = None,
                    nonce: Callable[[], str] | None = None) -> "ControlSource":
        """Build from the source toplevel's metadata, minting a fresh in-guest
        ``stream_id`` unless one is supplied (``nonce`` injectable for tests)."""
        sid = stream_id or (mint_stream_id(src.window_id, nonce) if nonce
                            else mint_stream_id(src.window_id))
        meta = RemoteWindowMeta(
            window_id=src.window_id, source_machine=src.source_machine,
            stream_id=sid, title=src.title, app_id=src.app_id,
            security_label=src.security_label, req_w=src.req_w, req_h=src.req_h,
            sensitivity=src.sensitivity, pointer_policy=src.pointer_policy)
        return cls(generation, meta)

    def announce(self) -> Announce:
        return Announce("announce", self.generation, self.meta)

    def closed(self, reason: str = "source-exit") -> Closed:
        """``Closed`` for the exported toplevel — the source window is gone, so the
        viewer removes THIS proxy (impl-12: source death is a ``Closed``, never a
        transport ``Disconnect``)."""
        return Closed("closed", self.generation, self.meta.window_id, reason,
                      self.meta.stream_id)


def active_state_alive(show_output: str) -> bool:
    """Classify a ``systemctl --user show -p ActiveState -p SubState`` dump for the
    marker unit: the source is DEAD only on a terminal ``inactive``/``failed``
    ``ActiveState`` (a not-found unit also shows ``inactive``). Transient
    ``activating``/``deactivating``/``reloading`` — or an unparseable/empty dump (a
    momentary systemctl/DBus probe hiccup) — count as ALIVE, so a transient state
    or probe error never fabricates an authoritative source-death ``Closed`` (codex
    impl-13: literal ``is-active == active`` was too aggressive)."""
    state: dict[str, str] = {}
    for line in show_output.splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            state[k] = v.strip()
    return state.get("ActiveState", "") not in ("inactive", "failed")


def watch(source: ControlSource, *, is_source_alive: Callable[[], bool],
          poll_viewer: Callable[[], str], send: Callable[[ControlMessage], None],
          max_polls: int = 10_000_000) -> str:
    """Drive one exported stream's control lifecycle, source-owned.

    Sends the ``Announce`` once, then watches until the stream ends, returning the
    reason:

    - ``"source-exit"`` — the source toplevel died (``is_source_alive`` went
      False); a ``Closed`` was sent so the viewer removes the proxy.
    - ``"viewer-eof"`` — the viewer detached (socket EOF) while the source was
      still alive; NO ``Closed`` was sent (detach, not death — the slot frees on
      the source side and a fresh resubscribe can reclaim it).
    - ``"limit"`` — the poll budget was exhausted (only reachable in tests).

    ``poll_viewer()`` blocks up to one poll interval and returns one of
    ``VIEWER_*``; this is the wait that paces the watcher, so source death is
    detected within ~one interval. ``is_source_alive`` is the marker-unit liveness
    proxy. Both injected so the loop is pure + unit-tested headless.

    Race note: killing the source toplevel also tears down its RDP pixel stream, so
    the viewer can disconnect (control-socket EOF) at nearly the same instant the
    liveness poll would fire. EOF alone is therefore ambiguous — so on EOF we
    RE-CHECK source liveness: a viewer disconnect whose root cause is source death
    is still a SOURCE-driven ``Closed`` (not a detach). Only a viewer EOF while the
    source is still alive is a genuine detach (no ``Closed``). This makes the
    source-death teardown deterministic regardless of which signal lands first.
    """
    send(source.announce())
    for _ in range(max_polls):
        if not is_source_alive():
            send(source.closed("source-exit"))
            return "source-exit"
        ev = poll_viewer()
        if ev == VIEWER_EOF:
            if not is_source_alive():      # disconnect caused BY source death
                send(source.closed("source-exit"))
                return "source-exit"
            return "viewer-eof"            # genuine detach — source alive, NO Closed
        # VIEWER_ALIVE (idle) or VIEWER_DATA (upstream drained) → keep watching.
    return "limit"


# ---- live wiring (the thin, untested-by-unit-tests CLI shell) ------------
def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live shell
    """``mm-control``: bind a port in VM-A, accept the viewer, emit the
    source-derived ``Announce``, then emit ``Closed`` when the marker unit dies.
    The real socket + ``systemctl --user is-active`` liveness probe live here;
    :class:`ControlSource` + :func:`watch` (unit-tested) hold the logic."""
    import argparse
    import json
    import os
    import select
    import socket
    import subprocess

    from .sidechannel import encode

    ap = argparse.ArgumentParser(prog="mm-control")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind addr; 0.0.0.0 so the VM-A QEMU hostfwd (which "
                         "targets the guest NAT IP, not loopback) reaches it")
    ap.add_argument("--generation", type=int, required=True)
    ap.add_argument("--window-id", type=int, required=True)
    ap.add_argument("--source-machine", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--app-id", default="")
    ap.add_argument("--req-w", type=int, default=0)
    ap.add_argument("--req-h", type=int, default=0)
    ap.add_argument("--stream-id", default="",
                    help="optional fixed stream_id; default mints a fresh one")
    ap.add_argument("--marker-unit", default="mm-marker",
                    help="the systemd --user unit whose liveness is the source "
                         "toplevel lifetime proxy (impl-12)")
    ap.add_argument("--poll-interval", type=float, default=0.25)
    ap.add_argument("--accept-timeout", type=float, default=30.0)
    ap.add_argument("--emit-log", default="",
                    help="append each control message SENT here (evidence: what "
                         "VM-A produced, incl. the in-guest-minted stream_id)")
    ap.add_argument("--status-file", default="",
                    help="atomically write {stream_id, state, reason} here — the "
                         "FILE-BASED handshake the harness reads (codex impl-13: no "
                         "scraping a --collect transient unit's journal)")
    args = ap.parse_args(argv)

    src = SourceWindowInfo(
        window_id=args.window_id, source_machine=args.source_machine,
        title=args.title, app_id=args.app_id, req_w=args.req_w, req_h=args.req_h)
    source = ControlSource.from_source(
        src, args.generation, stream_id=args.stream_id or None)

    def write_status(state: str, reason: str = "") -> None:
        if not args.status_file:
            return
        doc = {"stream_id": source.meta.stream_id, "port": args.port,
               "state": state, "reason": reason}
        tmp = args.status_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, args.status_file)   # atomic: the harness never reads a torn file

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))   # bind failure is fatal + visible (impl-12)
    srv.listen(1)
    write_status("listening")          # publishes the in-guest-minted stream_id
    print(f"MM_CONTROL_LISTEN host={args.host} port={args.port} "
          f"stream_id={source.meta.stream_id}", flush=True)
    srv.settimeout(args.accept_timeout)
    conn, peer = srv.accept()
    conn.settimeout(None)
    print(f"MM_CONTROL_VIEWER_CONNECTED peer={peer}", flush=True)

    logf = open(args.emit_log, "a") if args.emit_log else None

    def send(msg: ControlMessage) -> None:
        wire = encode(msg)
        conn.sendall((wire + "\n").encode())
        if logf:
            logf.write(wire + "\n")
            logf.flush()
        print(f"MM_CONTROL_SENT {msg.type}", flush=True)

    def poll_viewer() -> str:
        r, _, _ = select.select([conn], [], [], args.poll_interval)
        if not r:
            return VIEWER_ALIVE
        try:
            chunk = conn.recv(4096)
        except OSError:
            return VIEWER_EOF
        if not chunk:
            return VIEWER_EOF              # clean EOF — viewer detached
        return VIEWER_DATA                 # upstream bytes (e.g. CloseRequest)

    def is_source_alive() -> bool:
        # the marker's OWN systemd --user unit going terminally inactive/failed is
        # the source-death signal — observed by the source, not injected by the
        # host. `show` (not `is-active`) so transient/probe states don't fabricate a
        # premature source-death Closed (codex impl-13; see active_state_alive).
        out = subprocess.run(
            ["systemctl", "--user", "show", args.marker_unit,
             "-p", "ActiveState", "-p", "SubState"],
            capture_output=True, text=True)
        return active_state_alive(out.stdout)

    reason = watch(source, is_source_alive=is_source_alive,
                   poll_viewer=poll_viewer, send=send)
    write_status("done", reason)        # file-based terminal reason for the harness
    print(f"MM_CONTROL_DONE reason={reason}", flush=True)
    if logf:
        logf.close()
    try:
        conn.close()
        srv.close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover — `python3 -m multimachine.control_source`
    raise SystemExit(main())
