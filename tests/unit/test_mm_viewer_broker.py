"""Unit tests for the rung-1 viewer broker (codex impl-32 Q3/Q4).

Drives a real loopback control socket (an in-process stand-in for VM-A's
mm-control) so the broker's connect/Announce-learn, the source-mediated close
(CloseRequest upstream → wait for source Closed), and the handle↔stream map are
pinned without a VM. The honesty property under test: the broker asks the source
to close and waits for the source-driven Closed — it never fabricates the Closed.
"""
from __future__ import annotations

import socket
import threading

from multimachine.bridge import SourceWindowInfo
from multimachine.control_source import (
    ControlSource, authenticate_viewer, viewer_close_requested,
)
from multimachine.harness.viewer_broker import ViewerBroker
from multimachine.sidechannel import Closed, encode


class _FakeControl:
    """A minimal VM-A mm-control: send Announce on connect, and when the viewer
    sends a CloseRequest for our stream, reply with the source-driven Closed."""

    def __init__(self, stream_id="sid", window_id=1, generation=7,
                 close_reason="viewer-close", capability="",
                 source_machine="vm-a"):
        self.stream_id = stream_id
        self.window_id = window_id
        self.generation = generation
        self.close_reason = close_reason
        self.capability = capability
        self.source_machine = source_machine
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.got_close_request = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        src = ControlSource.from_source(
            SourceWindowInfo(window_id=self.window_id,
                             source_machine=self.source_machine,
                             title="marker", app_id="qdistro.mm.vm-a.streamA",
                             req_w=640, req_h=400),
            self.generation, stream_id=self.stream_id)
        conn, _ = self.srv.accept()
        if self.capability:
            auth = b""
            while b"\n" not in auth:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return
                auth += chunk
            if not authenticate_viewer(auth.split(b"\n", 1)[0].decode(),
                                       self.capability):
                conn.close()
                return
        conn.sendall((encode(src.announce()) + "\n").encode())
        buf = ""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk.decode()
            if viewer_close_requested(buf, self.stream_id):
                self.got_close_request.set()
                closed = Closed("closed", self.generation, self.window_id,
                                self.close_reason, self.stream_id)
                conn.sendall((encode(closed) + "\n").encode())
                break
        conn.close()


def _broker_with(fake) -> tuple[ViewerBroker, str]:
    b = ViewerBroker(control_host="127.0.0.1")
    b.add_stream("a", origin="vm-a", app_id="qdistro.mm.vm-a.streamA",
                 rdp_unit="mm-rdp-a", relay_port=5555, control_port=fake.port,
                 marker_unit="mm-marker", window_id=1)
    sid = b.connect("a", timeout=5.0)
    return b, sid


class TestBrokerConnectAndAnnounce:
    def test_connect_learns_stream_id_from_announce(self):
        fake = _FakeControl(stream_id="vs-1-cafe")
        b, sid = _broker_with(fake)
        assert sid == "vs-1-cafe"
        assert b.peers["a"].stream_id == "vs-1-cafe"
        assert b.peers["a"].window_id == 1
        b.close()

    def test_bind_handle_and_resolve(self):
        fake = _FakeControl()
        b, _ = _broker_with(fake)
        b.bind_handle("a", 42)
        assert b.peer_for_handle(42).label == "a"
        assert b.peer_for_handle(99) is None
        b.close()

    def test_authenticated_control_capability_precedes_announce(self):
        fake = _FakeControl(capability="control-secret")
        b = ViewerBroker(control_host="127.0.0.1")
        b.add_stream("a", origin="vm-a", app_id="qdistro.mm.vm-a.streamA",
                     rdp_unit="mm-rdp-a", relay_port=5555,
                     control_port=fake.port, marker_unit="mm-marker",
                     window_id=1, control_capability="control-secret")
        assert b.connect("a", timeout=5.0) == "sid"
        b.close()

    def test_wrong_control_capability_discloses_no_announce(self):
        fake = _FakeControl(capability="right-secret")
        b = ViewerBroker(control_host="127.0.0.1")
        b.add_stream("a", origin="vm-a", app_id="qdistro.mm.vm-a.streamA",
                     rdp_unit="mm-rdp-a", relay_port=5555,
                     control_port=fake.port, marker_unit="mm-marker",
                     window_id=1, control_capability="wrong-secret")
        try:
            b.connect("a", timeout=5.0)
            assert False, "wrong capability unexpectedly received Announce"
        except RuntimeError as e:
            assert "EOF before a message" in str(e)
        b.close()

    def test_mismatched_announce_app_id_fails_closed(self):
        # a stale control source announcing the WRONG app_id must be rejected at
        # connect (codex impl-33 MEDIUM) — never learn a stale stream_id.
        fake = _FakeControl()             # announces qdistro.mm.vm-a.streamA
        b = ViewerBroker(control_host="127.0.0.1")
        b.add_stream("a", origin="vm-a", app_id="qdistro.mm.vm-a.streamWRONG",
                     rdp_unit="mm-rdp-a", relay_port=5555, control_port=fake.port,
                     marker_unit="mm-marker", window_id=1)
        try:
            b.connect("a", timeout=5.0)
            assert False, "expected a mismatch RuntimeError"
        except RuntimeError as e:
            assert "app_id" in str(e)
        b.close()

    def test_mismatched_generation_fails_closed(self):
        fake = _FakeControl(generation=7)
        b = ViewerBroker(control_host="127.0.0.1")
        b.add_stream("a", origin="vm-a", app_id="qdistro.mm.vm-a.streamA",
                     rdp_unit="mm-rdp-a", relay_port=5555, control_port=fake.port,
                     marker_unit="mm-marker", window_id=1, expect_generation=99)
        try:
            b.connect("a", timeout=5.0)
            assert False, "expected a generation-mismatch RuntimeError"
        except RuntimeError as e:
            assert "generation" in str(e)
        b.close()

    def test_mismatched_source_machine_fails_closed(self):
        # R2: app_id alone cannot let a control endpoint registered for origin A
        # claim that its lifecycle authority is origin B.
        fake = _FakeControl(source_machine="vm-b")
        b = ViewerBroker(control_host="127.0.0.1")
        b.add_stream("a", origin="vm-a", app_id="qdistro.mm.vm-a.streamA",
                     rdp_unit="mm-rdp-a", relay_port=5555,
                     control_port=fake.port, marker_unit="mm-marker",
                     window_id=1)
        try:
            b.connect("a", timeout=5.0)
            assert False, "expected a source-machine mismatch RuntimeError"
        except RuntimeError as e:
            assert "source_machine" in str(e) and "origin" in str(e)
        b.close()


class TestSourceMediatedClose:
    def test_request_close_then_source_closed(self):
        fake = _FakeControl(stream_id="sid", close_reason="viewer-close")
        b, _ = _broker_with(fake)
        assert b.peers["a"].close_state == "open"
        b.request_source_close("a")
        assert b.peers["a"].close_state == "close_requested"
        # the fake source received the CloseRequest...
        assert fake.got_close_request.wait(5.0) is True
        # ...and the broker observes the SOURCE-driven Closed (not fabricated).
        closed = b.wait_closed("a", timeout=5.0)
        assert isinstance(closed, Closed)
        assert closed.stream_id == "sid" and closed.reason == "viewer-close"
        assert b.peers["a"].close_state == "closed"
        b.close()

    def test_close_by_handle_routes_to_stream(self):
        fake = _FakeControl(stream_id="sid")
        b, _ = _broker_with(fake)
        b.bind_handle("a", 7)
        label = b.request_source_close_by_handle(7)
        assert label == "a"
        assert fake.got_close_request.wait(5.0) is True
        assert b.wait_closed("a", timeout=5.0).stream_id == "sid"
        b.close()

    def test_wait_closed_times_out_when_source_keeps_window(self):
        # a source that never closes: wait_closed returns None (peer stays open).
        class _Mute(_FakeControl):
            def _serve(self):
                src = ControlSource.from_source(
                    SourceWindowInfo(window_id=1, source_machine="vm-a",
                                     title="m", app_id="qdistro.mm.vm-a.streamA",
                                     req_w=640, req_h=400),
                    7, stream_id=self.stream_id)
                conn, _ = self.srv.accept()
                conn.sendall((encode(src.announce()) + "\n").encode())
                # never reply to CloseRequest; just hold the socket open.
                while conn.recv(4096):
                    pass
                conn.close()

        fake = _Mute(stream_id="sid")
        b, _ = _broker_with(fake)
        b.request_source_close("a")
        assert b.wait_closed("a", timeout=0.5) is None
        assert b.peers["a"].close_state == "close_requested"
        b.close()

    def test_closed_must_match_generation_and_window(self):
        class _WrongClosed(_FakeControl):
            def _serve(self):
                src = ControlSource.from_source(
                    SourceWindowInfo(window_id=self.window_id,
                                     source_machine="vm-a", title="m",
                                     app_id="qdistro.mm.vm-a.streamA",
                                     req_w=640, req_h=400),
                    self.generation, stream_id=self.stream_id)
                conn, _ = self.srv.accept()
                conn.sendall((encode(src.announce()) + "\n").encode())
                if conn.recv(4096):
                    wrong = Closed("closed", self.generation + 1,
                                   self.window_id + 1, "stale", self.stream_id)
                    conn.sendall((encode(wrong) + "\n").encode())
                conn.close()

        fake = _WrongClosed(stream_id="sid")
        b, _ = _broker_with(fake)
        b.request_source_close("a")
        assert b.wait_closed("a", timeout=0.5) is None
        assert b.peers["a"].close_state == "close_requested"
        b.close()


class TestStatus:
    def test_status_reports_record(self):
        fake = _FakeControl(stream_id="sid")
        b, _ = _broker_with(fake)
        b.bind_handle("a", 5)
        st = b.status("a")
        assert st["stream_id"] == "sid" and st["handle"] == 5
        assert st["rdp_unit"] == "mm-rdp-a" and st["control_port"] == fake.port
        assert st["close_state"] == "open"
        b.close()
