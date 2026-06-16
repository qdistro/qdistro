"""Peer-side (VM-B) remote whole-window viewer — Phase 1, slice b (codex impl-8).

impl-8's verdict: the peer viewer toplevel simply IS the ``sdl-freerdp`` Wayland
client window — an ordinary ``xdg_toplevel`` qdwin already manages, focuses,
closes, and composites. So Phase 1 needs **no new compositor protocol**. The
peer-side increment is this Python supervisor that:

- consumes the JSON-lines control side-channel (``sidechannel.encode``/``decode``
  over a bidirectional newline-delimited TCP stream, forwarded next to the RDP
  relay),
- applies each message through :class:`~.sidechannel.RemoteViewerState` (so
  stale-generation / stream-id-mismatch / post-disconnect control is rejected by
  the shipped contract, not re-implemented here),
- on ``Announce`` launches ``sdl-freerdp`` (via :func:`bridge.rdp_client_argv` +
  :func:`bridge.rdp_password`, OTP on stdin, no client scaling) as the managed
  viewer toplevel, and tears it down on ``Closed`` / ``Disconnect`` / decoder
  exit,
- emits a viewer-originated ``CloseRequest`` upstream when the user closes the
  window, and
- exposes a machine-readable :meth:`RemoteViewer.status` the harness asserts on
  (connected / disconnected / capacity-exceeded + generation + stream_ids).

The process launch is injected (``launcher``) so the whole state machine is
unit-testable with NO ``sdl-freerdp`` and NO VM (the dry-run tests). The thin
:func:`main` wires a real socket + ``subprocess`` for the live two-VM gate.

Honesty (impl-8): this proves the cross-machine window/control/lifecycle
semantics — managed toplevel, input confinement, teardown, stale rejection — NOT
"feels native"/A5. Capacity/admission stays a SOURCE-side condition surfaced into
status; no new peer-side capacity policy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from .sidechannel import (
    Announce, Closed, CloseRequest, ControlMessage, Disconnect, Focus,
    RemoteViewerState, decode, encode,
)


class ViewerStatus(str, Enum):
    IDLE = "idle"                       # no stream yet
    CONNECTED = "connected"            # a viewer toplevel is live
    DISCONNECTED = "disconnected"      # link/dock ended; proxies blanked
    CAPACITY_EXCEEDED = "capacity-exceeded"  # source could not grant a stream


# A Disconnect whose reason names a source-side admission failure is surfaced as
# CAPACITY_EXCEEDED rather than a plain link drop (impl-8: "no free pipewire
# output" / failed subscribe). Kept to the shipped Disconnect message + reason
# field — no new control message invented.
_CAPACITY_HINTS = ("capacity", "no free", "no-free", "admission", "full",
                   "exhausted")


class ViewerProcess(Protocol):
    """The launched decoder (``sdl-freerdp``). Injectable for tests."""

    def poll(self) -> int | None: ...   # None while alive, exit code once dead

    def terminate(self) -> None: ...


@dataclass
class RemoteViewer:
    """Supervises the peer-side viewer toplevels for one dock generation.

    ``launcher(announce) -> ViewerProcess`` starts the decoder for an approved
    export; it is the ONLY side-effecting dependency, so the state machine runs
    headless in tests. ``upstream`` collects viewer→source messages (CloseRequest)
    the transport layer must forward.
    """

    generation: int
    launcher: Callable[[Announce], ViewerProcess]
    source_machine: str = ""
    state: RemoteViewerState = field(init=False)
    procs: dict[int, ViewerProcess] = field(default_factory=dict)  # window_id -> proc
    status_value: ViewerStatus = ViewerStatus.IDLE
    upstream: list[ControlMessage] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.state = RemoteViewerState(self.generation)

    # ---- inbound control (source -> viewer) ------------------------------
    def on_message(self, msg: ControlMessage) -> bool:
        """Apply one control message; returns whether it was accepted. Rejected
        messages are recorded in ``self.state.rejected`` by the contract."""
        accepted = self.state.apply(msg)
        if not accepted:
            return False
        if isinstance(msg, Announce):
            self.procs[msg.meta.window_id] = self.launcher(msg)
            self.status_value = ViewerStatus.CONNECTED
        elif isinstance(msg, Closed):
            self._kill(msg.window_id)
            if not self.procs:
                # the source toplevel is gone and nothing else is live; the
                # link is still up (detach vs death is the source's call) — stay
                # CONNECTED only if other proxies remain, else go IDLE.
                self.status_value = ViewerStatus.IDLE
        elif isinstance(msg, Disconnect):
            self._kill_all()
            self.status_value = (
                ViewerStatus.CAPACITY_EXCEEDED
                if _is_capacity(msg.reason) else ViewerStatus.DISCONNECTED)
        return True

    # ---- outbound (viewer -> source) -------------------------------------
    def request_close(self, window_id: int) -> bool:
        """User closed the viewer window: emit a viewer-originated CloseRequest
        upstream (the source removes the proxy + sends the authoritative
        ``Closed``). Returns False if there is no such live proxy."""
        proxy = self.state.windows.get(window_id)
        if not proxy:
            return False
        self.upstream.append(CloseRequest(
            "close_request", self.state.generation, window_id, proxy.stream_id))
        return True

    def on_decoder_exit(self, window_id: int) -> None:
        """The ``sdl-freerdp`` process died (link/transport ended, or the user
        closed it). Map it to a side-channel Disconnect for this generation so
        all proxies blank and stale control is rejected — failing safe."""
        self.on_message(Disconnect(
            "disconnect", self.state.generation, "viewer decoder exited"))

    # ---- process supervision --------------------------------------------
    def reap(self) -> list[int]:
        """Poll launched decoders; for any that exited, fail safe via
        :meth:`on_decoder_exit`. Returns the window_ids that exited."""
        dead = [wid for wid, p in self.procs.items() if p.poll() is not None]
        for wid in dead:
            self.on_decoder_exit(wid)
        return dead

    def _kill(self, window_id: int) -> None:
        p = self.procs.pop(window_id, None)
        if p is not None:
            _safe_terminate(p)

    def _kill_all(self) -> None:
        for p in self.procs.values():
            _safe_terminate(p)
        self.procs.clear()

    # ---- evidence --------------------------------------------------------
    def status(self) -> dict:
        """Machine-readable status the harness asserts on / writes to a file."""
        return {
            "status": self.status_value.value,
            "generation": self.state.generation,
            "connected": self.state.connected,
            "source_machine": self.source_machine,
            "windows": [
                {"window_id": wid, "stream_id": w.stream_id, "title": w.title,
                 "app_id": w.meta.app_id, "source_machine": w.meta.source_machine,
                 "focused": w.focused, "w": w.w, "h": w.h,
                 "security_label": w.meta.security_label, "remote": True}
                for wid, w in self.state.windows.items()
            ],
            "rejected": [
                {"generation": g, "type": t, "reason": r}
                for (g, t, r) in self.state.rejected
            ],
        }


def _is_capacity(reason: str) -> bool:
    r = (reason or "").lower()
    return any(h in r for h in _CAPACITY_HINTS)


def _safe_terminate(p: ViewerProcess) -> None:
    try:
        if p.poll() is None:
            p.terminate()
    except Exception:  # noqa: BLE001 — teardown must not raise
        pass


# ---- live wiring (the thin, untested-by-unit-tests CLI shell) ------------
def main(argv: list[str] | None = None) -> int:  # pragma: no cover - live shell
    """``mm-viewer-launch``: connect to the control side-channel, launch
    ``sdl-freerdp`` per Announce, emit status. The real socket + subprocess live
    here; :class:`RemoteViewer` (unit-tested) holds the logic."""
    import argparse
    import socket
    import subprocess
    import sys
    import threading

    from .bridge import ViewStreamApproved, rdp_client_argv, rdp_password

    ap = argparse.ArgumentParser(prog="mm-viewer-launch")
    ap.add_argument("--control-host", required=True)
    ap.add_argument("--control-port", type=int, required=True)
    ap.add_argument("--generation", type=int, required=True)
    ap.add_argument("--rdp-host", default="127.0.0.1")
    ap.add_argument("--rdp-port", type=int, required=True)
    ap.add_argument("--rdp-cert", default="")
    ap.add_argument("--otp", required=True, help="single-use stream password")
    ap.add_argument("--size", default="", help="WxH; empty = source size")
    ap.add_argument("--status-file", default="")
    ap.add_argument("--fullscreen", action="store_true",
                    help="decode fullscreen at the kiosk output origin (the "
                         "live decoded-remote capture geometry, impl-9 Q2)")
    ap.add_argument("--rdp-user", default="",
                    help="sdl-freerdp /u:<name> (proven decoder used 'mm'); the "
                         "OTP on stdin authenticates, so this is optional")
    ap.add_argument("--decoder-log", default="",
                    help="redirect the launched sdl-freerdp stdout/stderr here "
                         "(default: discard) — for live bring-up diagnosis")
    args = ap.parse_args(argv)

    w = h = 0
    if args.size and "x" in args.size:
        w, h = (int(v) for v in args.size.split("x", 1))

    def launcher(ann: Announce) -> ViewerProcess:
        approved = ViewStreamApproved(
            pipewire_node_name="", rdp_port=args.rdp_port,
            rdp_cert_path=args.rdp_cert, rdp_password=args.otp)
        argvv = rdp_client_argv(approved, host=args.rdp_host,
                                width=w or ann.meta.req_w, height=h or ann.meta.req_h,
                                fullscreen=args.fullscreen,
                                username=args.rdp_user or None)
        logf = open(args.decoder_log, "wb") if args.decoder_log \
            else subprocess.DEVNULL
        proc = subprocess.Popen(argvv, stdin=subprocess.PIPE,
                                stdout=logf, stderr=subprocess.STDOUT)
        if proc.stdin:
            proc.stdin.write((rdp_password(approved) + "\n").encode())
            proc.stdin.flush()
        return proc

    viewer = RemoteViewer(args.generation, launcher)

    def write_status() -> None:
        if args.status_file:
            with open(args.status_file, "w") as f:
                json.dump(viewer.status(), f)

    sock = socket.create_connection((args.control_host, args.control_port))
    sock_file = sock.makefile("r")

    def reaper() -> None:
        import time
        while viewer.status_value not in (ViewerStatus.DISCONNECTED,
                                          ViewerStatus.CAPACITY_EXCEEDED):
            if viewer.reap():
                write_status()
            time.sleep(0.2)
    threading.Thread(target=reaper, daemon=True).start()

    write_status()
    for line in sock_file:
        line = line.strip()
        if not line:
            continue
        viewer.on_message(decode(line))
        write_status()
        # forward any viewer-originated upstream messages back on the same socket
        while viewer.upstream:
            sock.sendall((encode(viewer.upstream.pop(0)) + "\n").encode())
        if viewer.status_value in (ViewerStatus.DISCONNECTED,
                                   ViewerStatus.CAPACITY_EXCEEDED):
            break
    write_status()
    return 0


if __name__ == "__main__":  # pragma: no cover — `python3 -m multimachine.viewer`
    raise SystemExit(main())
