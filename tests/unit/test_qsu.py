"""Tests for qdistro_root_exec — parse/validate the JSON wire format.

These are plain unit tests around the framing and request-validation
helpers. End-to-end exec is VM-smoke only (see ).
"""
from __future__ import annotations

import io
import json
import socket

import pytest

import qdistro_root_exec as Q
from qdistro_root_exec import _recv_request, _send, _resolve_target


class _SockPair:
    """Two connected AF_UNIX sockets for wire-format tests."""
    def __init__(self):
        self.a, self.b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    def close(self):
        self.a.close(); self.b.close()


@pytest.fixture
def pair():
    p = _SockPair()
    yield p
    p.close()


class TestFraming:
    def test_send_and_recv_roundtrip(self, pair):
        req = {"target_user": "root", "argv": ["id"]}
        _send(pair.a, req)
        got = _recv_request(pair.b)
        assert got == req

    def test_recv_empty_returns_none(self, pair):
        pair.a.close()
        assert _recv_request(pair.b) is None

    def test_recv_rejects_oversized_payload(self, pair):
        # sendall on a 1MB blob would block — the socketpair buffer is
        # ~200KB and the recv side is single-threaded. Push the send
        # off to a daemon thread so the main thread can run _recv_request
        # and hit the size limit while the send is still in flight.
        import threading
        big = b"A" * (1_500_000)

        def _push():
            try:
                pair.a.sendall(big)
            except OSError:
                pass

        t = threading.Thread(target=_push, daemon=True); t.start()
        with pytest.raises(ValueError):
            _recv_request(pair.b)

    def test_recv_accepts_multi_chunk_json(self, pair):
        frame = json.dumps({"target_user": "root", "argv": ["id"]}).encode()
        # Send in two halves to prove the recv loop assembles correctly.
        mid = len(frame) // 2
        pair.a.sendall(frame[:mid])
        pair.a.sendall(frame[mid:] + b"\n")
        got = _recv_request(pair.b)
        assert got["argv"] == ["id"]


class TestResolveTarget:
    def test_root_is_known(self):
        uid, gid, home, shell = _resolve_target("root")
        assert uid == 0
        assert isinstance(home, str) and home.startswith("/")

    def test_unknown_user_raises(self):
        with pytest.raises(ValueError):
            _resolve_target("definitely-not-a-real-user-" + "x" * 32)


class TestHandleOneIdentity:
    def test_rejects_same_pid_exec_between_connect_and_broker(self, pair,
                                                               monkeypatch):
        sent: list[dict] = []

        monkeypatch.setattr(Q, "_peer_cred", lambda sock: (4242, 2000, 2000))
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 12345)
        exe_reads = iter(["/usr/bin/original", "/usr/bin/changed"])
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: next(exe_reads))
        monkeypatch.setattr(Q, "_resolve_target",
                            lambda target: (0, 0, "/root", "/bin/sh"))
        monkeypatch.setattr(Q, "_resolve_argv", lambda argv: ["/usr/bin/id"])
        monkeypatch.setattr(Q, "_send",
                            lambda sock, obj: sent.append(dict(obj)))
        monkeypatch.setattr(Q, "_ask_broker",
                            lambda *args, **kwargs:
                            pytest.fail("stale executable reached broker"))

        _send(pair.a, {"target_user": "root", "argv": ["id"]})
        Q.handle_one(pair.b)

        assert sent == [
            {
                "type": "error",
                "message": "caller executable changed between connect "
                           "and request; refusing",
            },
            {"type": "exit", "code": 1},
        ]

    def test_passes_accept_start_time_to_broker(self, pair, monkeypatch):
        sent: list[dict] = []
        broker_kwargs: dict = {}

        monkeypatch.setattr(Q, "_peer_cred", lambda sock: (4242, 2000, 2000))
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 12345)
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: "/usr/bin/qsu")
        monkeypatch.setattr(Q, "_resolve_target",
                            lambda target: (0, 0, "/root", "/bin/sh"))
        monkeypatch.setattr(Q, "_resolve_argv", lambda argv: ["/usr/bin/id"])
        monkeypatch.setattr(Q, "_send",
                            lambda sock, obj: sent.append(dict(obj)))

        def fake_ask_broker(*args, **kwargs):
            broker_kwargs.update(kwargs)
            return False

        monkeypatch.setattr(Q, "_ask_broker", fake_ask_broker)

        _send(pair.a, {"target_user": "root", "argv": ["id"]})
        Q.handle_one(pair.b)

        assert broker_kwargs["caller_start_time"] == 12345
        assert sent[-1] == {"type": "exit", "code": 1}
