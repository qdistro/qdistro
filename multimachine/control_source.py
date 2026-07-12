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
  only after an optional inherited-fd capability handshake (built IN-GUEST;
  ``stream_id`` minted in-guest — it is an opaque correlation token, not a
  credential, and is never disclosed to an unauthenticated connector), then
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

import hmac
import json
from dataclasses import dataclass
from typing import Callable

from .bridge import SourceWindowInfo, mint_stream_id
from .sidechannel import (
    Announce, CloseRequest, Closed, ControlMessage, RemoteWindowMeta,
    ShellOperation, ShellRequest, decode,
)

# What a single viewer-poll observed (one watch iteration):
VIEWER_ALIVE = "alive"   # idle (poll timed out) — connection healthy, no data
VIEWER_DATA = "data"     # the viewer sent upstream bytes (e.g. CloseRequest)
VIEWER_EOF = "eof"       # the viewer socket closed (detach) — NOT source death


def authenticate_viewer(raw: str, expected_capability: str) -> bool:
    """Validate the broker's first control-channel message.

    No ``Announce`` (and thus no source-minted stream_id) is disclosed until
    this capability matches. Malformed and non-object messages fail closed.
    """
    if not isinstance(expected_capability, str) or not expected_capability:
        return False
    try:
        request = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(request, dict):
        return False
    presented = request.get("capability")
    return (request.get("v") == 1
            and request.get("type") == "authenticate"
            and isinstance(presented, str)
            and hmac.compare_digest(presented, expected_capability))


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


def viewer_close_requested(chunk: str, stream_id: str) -> bool:
    """Pure: does the viewer's upstream byte ``chunk`` (JSON-lines control
    messages) contain a ``CloseRequest`` for ``stream_id``? (codex impl-32 Q4 —
    source-mediated close: the viewer asks, the SOURCE decides). Unparseable /
    non-CloseRequest / mismatched-stream lines are ignored — only an exact
    stream_id match is a close. Robust to partial reads delivering several
    newline-delimited messages at once."""
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = decode(line)
        except (ValueError, KeyError):
            continue
        if isinstance(msg, CloseRequest) and msg.stream_id == stream_id:
            return True
    return False


def viewer_shell_requests(chunk: str, *, stream_id: str, generation: int,
                          window_id: int) -> list[ShellRequest]:
    """Return only shell requests bound to this exact live export.

    The control socket is authenticated at connection time, but every request is
    still correlated to generation + source window + source-minted stream id so
    delayed bytes from an earlier export cannot mutate a replacement window.
    Coordinates are deliberately bounded before they reach the source shell.
    """
    accepted: list[ShellRequest] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = decode(line)
        except (TypeError, ValueError, KeyError):
            continue
        if not isinstance(msg, ShellRequest):
            continue
        if (msg.stream_id != stream_id or msg.generation != generation
                or msg.window_id != window_id):
            continue
        if (isinstance(msg.x, bool) or not isinstance(msg.x, int)
                or isinstance(msg.y, bool) or not isinstance(msg.y, int)):
            continue
        if msg.operation is ShellOperation.MOVE:
            if not (-32768 <= msg.x <= 32767 and -32768 <= msg.y <= 32767):
                continue
        elif msg.x != 0 or msg.y != 0:
            continue
        accepted.append(msg)
    return accepted


def shell_request_commands(msg: ShellRequest) -> tuple[str, ...]:
    """Translate a validated request to qdwin-bystander FIFO commands."""
    handle = msg.window_id
    if msg.operation is ShellOperation.MINIMIZE:
        return (f"min {handle}",)
    if msg.operation is ShellOperation.MAXIMIZE:
        return (f"max {handle}",)
    if msg.operation is ShellOperation.RESTORE:
        # Clear all source shell-owned special states, then unminimize. These
        # commands are idempotent, making one remote restore state-independent.
        return (f"fullscreen {handle} 0", f"restore {handle}",
                f"raise {handle}")
    if msg.operation is ShellOperation.MOVE:
        return (f"move {handle} {msg.x} {msg.y}",)
    raise ValueError(f"unsupported shell operation {msg.operation!r}")


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
          on_viewer_data: Callable[[], str | None] | None = None,
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

    ``on_viewer_data`` (codex impl-32 Q4): called when the viewer sends upstream
    bytes; it parses them and, on a matching ``CloseRequest(stream_id)``, closes
    the SOURCE toplevel (stops the marker unit) and returns the teardown reason
    (e.g. ``"viewer-close"``) — else None. This is the source-mediated close:
    the viewer ASKS, the source closes its own toplevel, and the existing
    liveness path then emits the authoritative ``Closed`` with that reason (NOT
    the forbidden viewer-side peer kill). If None, upstream bytes are drained as
    before.

    Race note: killing the source toplevel also tears down its RDP pixel stream, so
    the viewer can disconnect (control-socket EOF) at nearly the same instant the
    liveness poll would fire. EOF alone is therefore ambiguous — so on EOF we
    RE-CHECK source liveness: a viewer disconnect whose root cause is source death
    is still a SOURCE-driven ``Closed`` (not a detach). Only a viewer EOF while the
    source is still alive is a genuine detach (no ``Closed``). This makes the
    source-death teardown deterministic regardless of which signal lands first.
    """
    send(source.announce())
    # The reason carried by the eventual Closed. A viewer CloseRequest upgrades
    # it to "viewer-close" so the artefact distinguishes a source-mediated close
    # from a spontaneous source death — both still flow through the SAME
    # liveness→Closed path (the close is real source death, never a viewer kill).
    exit_reason = "source-exit"
    for _ in range(max_polls):
        if not is_source_alive():
            send(source.closed(exit_reason))
            return exit_reason
        ev = poll_viewer()
        if ev == VIEWER_EOF:
            if not is_source_alive():      # disconnect caused BY source death
                send(source.closed(exit_reason))
                return exit_reason
            return "viewer-eof"            # genuine detach — source alive, NO Closed
        if ev == VIEWER_DATA and on_viewer_data is not None:
            r = on_viewer_data()           # may close the source + return a reason
            if r:
                exit_reason = r
        # VIEWER_ALIVE (idle) or VIEWER_DATA (drained) → keep watching until the
        # liveness poll observes the (now-closing) source die.
    return "limit"


# ---- live wiring (the thin, untested-by-unit-tests CLI shell) ------------
def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live shell
    """``mm-control``: bind a port in VM-A, accept the viewer, emit the
    source-derived ``Announce``, then emit ``Closed`` when the marker unit dies.
    The real socket + ``systemctl --user is-active`` liveness probe live here;
    :class:`ControlSource` + :func:`watch` (unit-tested) hold the logic."""
    import argparse
    import os
    import select
    import socket
    import subprocess
    import time

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
    ap.add_argument(
        "--command-fifo",
        default=os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
                             "qdwin-cmd.fifo"),
                    help="source qdwin-bystander command FIFO used for R3 "
                         "source-owned shell operations")
    ap.add_argument("--poll-interval", type=float, default=0.25)
    ap.add_argument("--accept-timeout", type=float, default=30.0)
    ap.add_argument("--auth-fd", type=int, default=-1,
                    help="fd containing the control capability; when set, no "
                         "Announce is sent before authentication")
    ap.add_argument("--emit-log", default="",
                    help="append each control message SENT here (evidence: what "
                         "VM-A produced, incl. the in-guest-minted stream_id)")
    ap.add_argument("--status-file", default="",
                    help="atomically write {stream_id, state, reason} here — the "
                         "FILE-BASED handshake the harness reads (codex impl-13: no "
                         "scraping a --collect transient unit's journal)")
    args = ap.parse_args(argv)

    control_capability = ""
    if args.auth_fd >= 0:
        with os.fdopen(os.dup(args.auth_fd), "r", encoding="utf-8") as authf:
            control_capability = authf.readline().strip()
        if not control_capability:
            ap.error("--auth-fd supplied an empty control capability")

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
    accept_deadline = time.monotonic() + args.accept_timeout
    while True:
        srv.settimeout(max(0.01, accept_deadline - time.monotonic()))
        conn, peer = srv.accept()
        if not control_capability:
            break
        conn.settimeout(max(0.01, accept_deadline - time.monotonic()))
        auth = b""
        while b"\n" not in auth and len(auth) <= 4096:
            chunk = conn.recv(4096)
            if not chunk:
                break
            auth += chunk
        if (b"\n" in auth and len(auth) <= 4096
                and authenticate_viewer(
                    auth.split(b"\n", 1)[0].decode("utf-8", "replace"),
                    control_capability)):
            break
        print(f"MM_CONTROL_AUTH_REJECT peer={peer}", flush=True)
        conn.close()
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

    # Persistent receive buffer: a stream socket may split a JSON-lines message
    # across recv()s OR deliver several at once. We only ever parse COMPLETE
    # newline-terminated lines and keep the trailing partial in the buffer, so a
    # CloseRequest split across two reads is never lost (codex impl-33 HIGH).
    rx = {"buf": "", "ready": ""}

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
        rx["buf"] += chunk.decode("utf-8", "replace")
        if "\n" not in rx["buf"]:
            rx["ready"] = ""               # no complete line yet — keep buffering
            return VIEWER_ALIVE            # treat as idle until a full line lands
        *lines, rest = rx["buf"].split("\n")
        rx["buf"] = rest                   # leave the trailing partial line buffered
        rx["ready"] = "\n".join(lines)
        return VIEWER_DATA                 # ≥1 complete upstream message

    def on_viewer_data() -> str | None:
        # Source-mediated close (codex impl-32 Q4): a CloseRequest for OUR stream
        # closes OUR source toplevel by stopping its marker unit. The existing
        # liveness watcher then emits the authoritative Closed("viewer-close").
        if viewer_close_requested(rx["ready"], source.meta.stream_id):
            print(f"MM_CONTROL_CLOSEREQ stream_id={source.meta.stream_id} "
                  f"-> stop {args.marker_unit}", flush=True)
            subprocess.run(["systemctl", "--user", "stop", args.marker_unit],
                           capture_output=True, text=True)
            return "viewer-close"
        for request in viewer_shell_requests(
                rx["ready"], stream_id=source.meta.stream_id,
                generation=source.generation,
                window_id=source.meta.window_id):
            commands = shell_request_commands(request)
            try:
                fd = os.open(args.command_fifo, os.O_WRONLY | os.O_NONBLOCK)
                try:
                    os.write(fd, ("\n".join(commands) + "\n").encode())
                finally:
                    os.close(fd)
            except OSError as exc:
                print(f"MM_CONTROL_SHELLREQ_REJECT operation="
                      f"{request.operation.value} error={exc}", flush=True)
                continue
            print(f"MM_CONTROL_SHELLREQ operation={request.operation.value} "
                  f"window_id={request.window_id} x={request.x} y={request.y}",
                  flush=True)
        return None

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
                   poll_viewer=poll_viewer, send=send,
                   on_viewer_data=on_viewer_data)
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
