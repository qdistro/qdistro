"""Host-side JSON-lines control side-channel server for the live two-VM gate.

codex impl-9 (Q1): for the FIRST live managed-toplevel gate, the control channel
is **host-served using source-derived message content** — a tiny TCP server bound
to host loopback (``127.0.0.1:CONTROL_RELAY_PORT``, default 5556) that VM-B's
``mm-viewer-launch`` connects to over its OWN SLIRP NAT (``10.0.2.2:5556``). The
messages it emits are exactly what the source-side bridge produces
(``bridge_approved(...)`` → :class:`~..sidechannel.Announce`, then
``Closed``/``Disconnect``), so the viewer consumes a **real forwarded TCP byte
stream** — not in-process simulation. This keeps the honesty fence (the viewer is
not fed host-internal state) while giving the orchestrator deterministic control
over WHEN lifecycle messages are sent (steps 8–10).

Deferred to product (impl-9 explicit deferral): a VM-A-hosted ``mm-control`` unit
that serves its own control channel and a source-side watcher that autonomously
emits ``Closed``. For this gate the host is the actor that kills the marker, so it
can authoritatively emit the matching ``Closed``.

Bidirectional: the server also READS newline-delimited JSON the viewer writes back
(viewer-originated ``CloseRequest``), decoded into :attr:`ControlServer.received`.

Pure-stdlib (socket/threading); unit-tested against a real loopback client (no VM).
"""
from __future__ import annotations

import socket
import threading

from ..sidechannel import ControlMessage, decode, encode


class ControlServer:
    """A one-connection JSON-lines control server.

    Binds + listens on construction (so the port is reserved before the viewer is
    told to connect). :meth:`accept` blocks for the single VM-B client; :meth:`send`
    writes one encoded message + newline; inbound viewer lines are decoded onto
    :attr:`received`. ``host="127.0.0.1"`` is the loopback the SLIRP hostfwd
    targets; ``host="0.0.0.0"`` if the guest must reach it on a routable address.
    """

    def __init__(self, port: int = 5556, host: str = "127.0.0.1",
                 backlog: int = 1):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(backlog)
        self.host, self.port = self._srv.getsockname()
        self._conn: socket.socket | None = None
        self._rfile = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self.received: list[ControlMessage] = []
        self._closed = False

    # ---- lifecycle -------------------------------------------------------
    def accept(self, timeout: float | None = 30.0) -> None:
        """Block for the single VM-B viewer connection, then start reading its
        upstream (viewer→source) lines in the background."""
        self._srv.settimeout(timeout)
        self._conn, _ = self._srv.accept()
        self._conn.settimeout(None)
        self._rfile = self._conn.makefile("r")
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            for line in self._rfile:               # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = decode(line)
                except Exception:                  # noqa: BLE001 — ignore junk
                    continue
                with self._lock:
                    self.received.append(msg)
        except OSError:
            pass

    # ---- outbound (source -> viewer) -------------------------------------
    def send(self, msg: ControlMessage) -> None:
        """Write one encoded control message + newline to the viewer. Raises if no
        client has connected yet."""
        if self._conn is None:
            raise RuntimeError("control server has no client (call accept first)")
        self._conn.sendall((encode(msg) + "\n").encode())

    # ---- inbound (viewer -> source) --------------------------------------
    def upstream(self) -> list[ControlMessage]:
        """A snapshot of the viewer-originated messages received so far."""
        with self._lock:
            return list(self.received)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # shutdown wakes the reader thread blocked in recv (a bare close does not
        # reliably unblock a recv in another thread).
        if self._conn is not None:
            try:
                self._conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for s in (self._conn, self._srv):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass
        if self._reader is not None:
            self._reader.join(timeout=2)

    def __enter__(self) -> "ControlServer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
