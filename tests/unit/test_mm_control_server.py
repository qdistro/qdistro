"""Unit tests for the host-side JSON-lines control server (impl-9 Q1).

Exercises the real :class:`ControlServer` against a real loopback client socket
(no VM): a server-emitted Announce/Disconnect arrives as the exact encoded JSON
lines, and a viewer-originated CloseRequest written back is decoded onto
``received``. This is the same byte path the live VM-B viewer uses (only the
transport hop differs — SLIRP vs loopback).
"""
from __future__ import annotations

import socket
import threading

from multimachine.harness.control_server import ControlServer
from multimachine.sidechannel import (
    Announce, CloseRequest, Disconnect, RemoteWindowMeta, decode, encode,
)


def _announce(gen=7, wid=1, sid="vs-1-a"):
    meta = RemoteWindowMeta(window_id=wid, source_machine="server",
                            stream_id=sid, title="Build", app_id="org.x.term",
                            req_w=800, req_h=600)
    return Announce("announce", gen, meta)


def _client_connect(port):
    return socket.create_connection(("127.0.0.1", port), timeout=5)


class TestControlServer:
    def test_send_announce_reaches_client_as_jsonlines(self):
        srv = ControlServer(port=0)
        cli_box: list[socket.socket] = []

        def connect():
            cli_box.append(_client_connect(srv.port))
        t = threading.Thread(target=connect)
        t.start()
        srv.accept(timeout=5)
        t.join()
        cli = cli_box[0]

        ann = _announce()
        srv.send(ann)
        srv.send(Disconnect("disconnect", 7, "admin revoked"))
        rf = cli.makefile("r")
        line1 = decode(rf.readline().strip())
        line2 = decode(rf.readline().strip())
        assert isinstance(line1, Announce) and line1.meta.stream_id == "vs-1-a"
        assert isinstance(line2, Disconnect) and line2.reason == "admin revoked"
        cli.close()
        srv.close()

    def test_viewer_close_request_is_received_upstream(self):
        srv = ControlServer(port=0)
        cli_box: list[socket.socket] = []
        t = threading.Thread(target=lambda: cli_box.append(_client_connect(srv.port)))
        t.start()
        srv.accept(timeout=5)
        t.join()
        cli = cli_box[0]

        cli.sendall((encode(CloseRequest("close_request", 7, 1, "vs-1-a")) + "\n").encode())
        # the reader thread decodes it onto received; poll briefly.
        import time
        for _ in range(50):
            if srv.upstream():
                break
            time.sleep(0.02)
        up = srv.upstream()
        assert len(up) == 1
        assert isinstance(up[0], CloseRequest)
        assert up[0].window_id == 1 and up[0].stream_id == "vs-1-a"
        cli.close()
        srv.close()

    def test_send_before_accept_raises(self):
        srv = ControlServer(port=0)
        try:
            import pytest
            with pytest.raises(RuntimeError):
                srv.send(_announce())
        finally:
            srv.close()

    def test_context_manager_closes(self):
        with ControlServer(port=0) as srv:
            port = srv.port
            assert port > 0
        # after close, binding the same port again should succeed (it was freed).
        s2 = ControlServer(port=port)
        s2.close()
