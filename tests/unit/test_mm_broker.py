"""Unit tests for the durable in-VM broker (codex impl-34 Q1 / Q7 step 2).

`MultiMachineSession` is driven with a FAKE wrapper backend + the REAL
:class:`ViewerBroker` + REAL per-stream :func:`control_source.watch` loops over
loopback — so the registry, fail-closed Announce match, secctx handle attribution,
source-mediated close ORDERING (CloseRequest → source Closed → token-gated
teardown of ONLY that peer), and `pixel_backend_lost` are exercised end-to-end in
memory. Only the wrapper subprocess + the D-Bus server are faked/omitted.
"""
from __future__ import annotations

import json
import socket
import threading

from multimachine.bridge import SourceWindowInfo
from multimachine.control_source import (
    VIEWER_ALIVE, VIEWER_DATA, VIEWER_EOF, ControlSource, viewer_close_requested,
    watch,
)
from multimachine.mm_broker import Event, MultiMachineSession
from multimachine.rdp_client_wrapper import StreamSpec
from multimachine.sidechannel import ControlMessage, encode


class _StreamControl:
    """One stream's mm-control: real ControlSource + watch loop over loopback,
    with source-mediated-close wiring (CloseRequest stops the marker)."""

    def __init__(self, *, app_id, generation, window_id, stream_id,
                 marker_dies=True):
        src = SourceWindowInfo(window_id=window_id, source_machine="vm-a",
                               title="t", app_id=app_id, req_w=640, req_h=400)
        self.source = ControlSource.from_source(src, generation,
                                                stream_id=stream_id)
        self._marker_dies = marker_dies
        self.marker_alive = True
        self.sent: list[dict] = []
        self.reason = ""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.port = srv.getsockname()[1]
        self._srv = srv
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self._srv.settimeout(10)
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        conn.settimeout(None)
        rx = {"buf": "", "ready": ""}

        def send(msg: ControlMessage):
            conn.sendall((encode(msg) + "\n").encode())
            self.sent.append(json.loads(encode(msg)))

        def poll_viewer():
            import select
            r, _, _ = select.select([conn], [], [], 0.1)
            if not r:
                return VIEWER_ALIVE
            try:
                chunk = conn.recv(4096)
            except OSError:
                return VIEWER_EOF
            if not chunk:
                return VIEWER_EOF
            rx["buf"] += chunk.decode("utf-8", "replace")
            if "\n" not in rx["buf"]:
                return VIEWER_ALIVE
            *lines, rest = rx["buf"].split("\n")
            rx["buf"] = rest
            rx["ready"] = "\n".join(lines)
            return VIEWER_DATA

        def on_viewer_data():
            if viewer_close_requested(rx["ready"], self.source.meta.stream_id):
                if self._marker_dies:
                    self.marker_alive = False
                return "viewer-close"
            return None

        self.reason = watch(self.source, is_source_alive=lambda: self.marker_alive,
                            poll_viewer=poll_viewer, send=send,
                            on_viewer_data=on_viewer_data)
        try:
            conn.close()
        except OSError:
            pass

    def stop(self):
        try:
            self._srv.close()
        except OSError:
            pass


class FakeWrapper:
    """Stand-in for the qdistro-mm-rdp-client-wrapper process (the session talks to
    it over a socket in production)."""

    def __init__(self, spec: StreamSpec, token: str, otp: str):
        self.spec = spec
        self.token = token
        self.otp = otp
        self._alive = True
        self.teardown_calls: list[tuple] = []

    def alive(self):
        return self._alive

    def process_truth(self):
        return {"stream_id": self.spec.stream_id, "alive": self._alive,
                "exit_status": None if self._alive else 1}

    def teardown(self, stream_id, generation, token):
        self.teardown_calls.append((stream_id, generation, token))
        if (stream_id == self.spec.stream_id
                and generation == self.spec.generation
                and token == self.token):
            self._alive = False
            return True
        return False

    def crash(self):
        self._alive = False     # FreeRDP exits before any source Closed


def _spec(label, stream_suffix, gen=51):
    return StreamSpec(
        origin="vm-a", stream_id=f"expect-{label}", generation=gen,
        app_id=f"qdistro.mm.vm-a.{stream_suffix}",
        instance_id=f"vm-a-{stream_suffix}-1", rdp_host="10.0.2.2",
        rdp_port=5555 if label == "a" else 5560, width=640, height=400)


def _session():
    events: list[Event] = []
    wrappers: list[FakeWrapper] = []

    def spawn(spec, *, teardown_token, otp):
        w = FakeWrapper(spec, teardown_token, otp)
        wrappers.append(w)
        return w

    n = {"i": 0}

    def gen_token():
        n["i"] += 1
        return f"token-{n['i']}"

    s = MultiMachineSession(spawn_wrapper=spawn, on_event=events.append,
                            gen_token=gen_token)
    return s, events, wrappers


def _setup_two(s, *, b_marker_dies=True):
    ca = _StreamControl(app_id="qdistro.mm.vm-a.streamA", generation=51,
                        window_id=1, stream_id="sid-A")
    cb = _StreamControl(app_id="qdistro.mm.vm-a.streamB", generation=51,
                        window_id=2, stream_id="sid-B", marker_dies=b_marker_dies)
    s.register_stream("a", spec=_spec("a", "streamA"), rdp_unit="mm-rdp-a",
                      control_port=ca.port, marker_unit="mm-marker")
    s.register_stream("b", spec=_spec("b", "streamB"), rdp_unit="mm-rdp-b",
                      control_port=cb.port, marker_unit="mm-marker2")
    s.connect("a"); s.connect("b")
    s.launch_backend("a", "otpA"); s.launch_backend("b", "otpB")
    return ca, cb


class TestMultiMachineSession:
    def test_connect_adopts_source_stream_id_and_announces(self):
        s, events, _ = _session()
        _setup_two(s)
        try:
            # the EXPECTED stream_id in the spec is replaced by the source-minted one.
            assert s.regs["a"].spec.stream_id == "sid-A"
            assert s.broker.peers["a"].stream_id == "sid-A"
            announced = [e for e in events if e.kind == "announced"]
            assert {e.label for e in announced} == {"a", "b"}
        finally:
            s.close()

    def test_bind_handle_by_secctx_failclosed(self):
        s, events, _ = _session()
        _setup_two(s)
        try:
            assert s.bind_handle("qdistro.mm.vm-a.streamA", 101) is True
            assert s.bind_handle("qdistro.mm.vm-a.streamB", 102) is True
            # unknown app_id → reject (fail closed).
            assert s.bind_handle("qdistro.mm.vm-a.streamZ", 103) is False
            # already-bound stream with a DIFFERENT handle → reject.
            assert s.bind_handle("qdistro.mm.vm-a.streamA", 999) is False
            # idempotent rebind to the SAME handle → accept (no duplicate event).
            assert s.bind_handle("qdistro.mm.vm-a.streamA", 101) is True
            bound = [e for e in events if e.kind == "bound"]
            assert {e.label for e in bound} == {"a", "b"}
            assert len(bound) == 2          # idempotent rebind did not re-emit
        finally:
            s.close()

    def test_bind_handle_validates_redundant_identity(self):
        # the D-Bus BindHandle(origin, stream, generation, secctx, handle) passes
        # the full tuple; redundant fields are validated against the resolved peer
        # (codex impl-36 HIGH) — a disagreement is rejected.
        s, _events, _ = _session()
        _setup_two(s)
        try:
            app = "qdistro.mm.vm-a.streamA"
            # mismatched origin / stream / generation → reject (no bind).
            assert s.bind_handle(app, 101, origin="vm-Z") is False
            assert s.bind_handle(app, 101, stream_id="wrong") is False
            assert s.bind_handle(app, 101, generation="999") is False
            assert s.bind_handle(app, 101, generation="not-an-int") is False
            assert s.broker.peers["a"].handle is None    # nothing bound yet
            # matching full tuple → accept.
            assert s.bind_handle(app, 101, origin="vm-a", stream_id="sid-A",
                                 generation="51") is True
            assert s.broker.peers["a"].handle == 101
        finally:
            s.close()

    def test_finalize_is_idempotent_and_retires_handle(self):
        s, events, _ = _session()
        _setup_two(s)
        try:
            s.bind_handle("qdistro.mm.vm-a.streamA", 101)
            s.request_close(101)
            assert s.finalize_close("a", timeout=10) is True
            wa = s.regs["a"].wrapper
            # handle retired so a reused qdwin handle can't resolve to the dead peer.
            assert s.broker.peers["a"].handle is None
            assert s.broker.peer_for_handle(101) is None
            n_closed = len([e for e in events if e.kind == "closed"])
            n_teardown = len(wa.teardown_calls)
            # second finalize is a no-op: no second teardown, no second event.
            assert s.finalize_close("a", timeout=1) is True
            assert len(wa.teardown_calls) == n_teardown
            assert len([e for e in events if e.kind == "closed"]) == n_closed
        finally:
            s.close()

    def test_source_mediated_close_ordering(self):
        s, events, wrappers = _session()
        _setup_two(s)
        try:
            s.bind_handle("qdistro.mm.vm-a.streamA", 101)
            s.bind_handle("qdistro.mm.vm-a.streamB", 102)
            wa = s.regs["a"].wrapper
            wb = s.regs["b"].wrapper
            # request close on A's handle: CloseRequest upstream, backend NOT torn.
            assert s.request_close(101) == "a"
            assert wa.alive() and not wa.teardown_calls   # not torn before Closed
            assert any(e.kind == "close_pending" and e.label == "a"
                       for e in events)
            # finalize: blocks for source Closed, THEN token-gated teardown of A.
            assert s.finalize_close("a", timeout=10) is True
            assert wa.teardown_calls == [("sid-A", 51, s.regs["a"].teardown_token)]
            assert not wa.alive()
            assert any(e.kind == "closed" and e.label == "a" for e in events)
            # B untouched: record open, backend alive, never torn down.
            assert s.broker.peers["b"].close_state == "open"
            assert wb.alive() and not wb.teardown_calls
        finally:
            s.close()

    def test_finalize_without_source_close_does_not_teardown(self):
        # marker does NOT die on CloseRequest → no source Closed → finalize must
        # NOT tear down the backend (the close is source-mediated, never assumed).
        s, events, _ = _session()
        _setup_two(s, b_marker_dies=False)
        try:
            s.bind_handle("qdistro.mm.vm-a.streamB", 102)
            s.request_close(102)
            wb = s.regs["b"].wrapper
            assert s.finalize_close("b", timeout=2) is False
            assert wb.alive() and not wb.teardown_calls
            assert not any(e.kind == "closed" for e in events)
        finally:
            s.close()

    def test_pixel_backend_lost_keeps_record(self):
        s, events, _ = _session()
        _setup_two(s)
        try:
            s.bind_handle("qdistro.mm.vm-a.streamA", 101)
            # A's FreeRDP crashes before any source Closed.
            s.regs["a"].wrapper.crash()
            s.poll_backends()
            peer = s.broker.peers["a"]
            assert peer.close_state == "pixel_backend_lost"
            assert peer.closed is None and not peer.backend_alive
            assert "a" in s.broker.peers           # record KEPT
            lost = [e for e in events if e.kind == "pixel_backend_lost"]
            assert len(lost) == 1 and lost[0].label == "a"
            # idempotent: polling again does not re-emit.
            s.poll_backends()
            assert len([e for e in events if e.kind == "pixel_backend_lost"]) == 1
        finally:
            s.close()

    def test_request_close_unknown_handle(self):
        s, _events, _ = _session()
        _setup_two(s)
        try:
            assert s.request_close(777) is None
        finally:
            s.close()
