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


class TestRecheckCallerIdentity:
    @pytest.mark.cheat_aware(
        protects="qsu recheck fails closed when caller identity is unanchored",
        severity="critical",
        cheats=[
            "delete or weaken the pytest.raises(CallerIdentityChanged) guard",
            "monkeypatch _peer_exe/_peer_start_time to a readable value so the "
            "anchor is no longer absent",
            "convert the expected exception into a skip/xfail",
        ],
        consequence="a pid-reused process inherits another caller's root "
                    "approval and executes as that identity",
    )
    def test_no_anchor_fails_closed(self, monkeypatch):
        """When neither the exe nor starttime was readable at accept
        time, the recheck has nothing to anchor on and MUST fail closed
        rather than silently approving a possibly pid-reused process.
        """
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 99999)
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: "/usr/bin/whatever")
        with pytest.raises(Q.CallerIdentityChanged):
            Q._recheck_caller_identity(1234, "?", 0)

    def test_matching_identity_passes(self, monkeypatch):
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 555)
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: "/usr/bin/qsu")
        # Should not raise.
        Q._recheck_caller_identity(1234, "/usr/bin/qsu", 555)

    def test_starttime_only_anchor_detects_pid_reuse(self, monkeypatch):
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 777)
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: "?")
        with pytest.raises(Q.CallerIdentityChanged):
            Q._recheck_caller_identity(1234, "?", 555)


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

    def test_rejects_exec_during_broker_setup_window(self, pair, monkeypatch):
        """The caller passes the handle_one recheck (exe stable through
        the first reads) but execs a different binary in the window
        between that recheck and the actual RequestPermissionAs call.
        The final _ask_broker recheck must catch it and fail closed
        without ever reaching the broker.
        """
        sent: list[dict] = []

        monkeypatch.setattr(Q, "_peer_cred", lambda sock: (4242, 2000, 2000))
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 12345)
        # exe reads: accept-time, handle_one recheck, then the
        # _ask_broker pre-request recheck sees the swapped binary.
        exe_reads = iter([
            "/usr/bin/original",   # exe_at_accept
            "/usr/bin/original",   # handle_one Step-6 recheck (passes)
            "/usr/bin/changed",    # _recheck_caller_identity (fails)
        ])
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: next(exe_reads))
        monkeypatch.setattr(Q, "_resolve_target",
                            lambda target: (0, 0, "/root", "/bin/sh"))
        monkeypatch.setattr(Q, "_resolve_argv", lambda argv: ["/usr/bin/id"])
        monkeypatch.setattr(Q, "_send",
                            lambda sock, obj: sent.append(dict(obj)))

        # Stub dbus so _ask_broker reaches its pre-request recheck.
        # RequestPermissionAs must never fire — if it does, the stale
        # identity leaked to the broker.
        class _Iface:
            def RequestPermissionAs(self, *a, **k):
                pytest.fail("stale executable reached broker "
                            "RequestPermissionAs")

            def WaitForDecision(self, *a, **k):
                pytest.fail("WaitForDecision reached with stale identity")

        class _Bus:
            def get_object(self, *a, **k):
                return object()

        monkeypatch.setattr(Q.dbus, "SystemBus", lambda: _Bus())
        monkeypatch.setattr(Q.dbus, "Interface", lambda obj, name: _Iface())

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

    def test_rejects_exec_during_pending_approval(self, pair, monkeypatch):
        """Approval succeeds, but the caller execs a different binary
        while WaitForDecision is pending. The final recheck inside
        _spawn_and_stream (immediately before Popen) must catch it and
        refuse to spawn — the admin's click was about the original
        identity. Popen must never be reached.
        """
        sent: list[dict] = []
        popened: list = []

        monkeypatch.setattr(Q, "_peer_cred", lambda sock: (4242, 2000, 2000))
        monkeypatch.setattr(Q, "_peer_start_time", lambda pid: 12345)
        # _ask_broker is stubbed below, so its internal pre-request
        # recheck does not read _peer_exe. The reads are: accept-time,
        # handle_one Step-6 recheck (both original), then the final
        # pre-Popen recheck inside _spawn_and_stream sees the swap.
        exe_reads = iter([
            "/usr/bin/original",   # exe_at_accept
            "/usr/bin/original",   # handle_one Step-6 recheck
            "/usr/bin/changed",    # pre-Popen recheck (fails)
        ])
        monkeypatch.setattr(Q, "_peer_exe", lambda pid: next(exe_reads))
        monkeypatch.setattr(Q, "_resolve_target",
                            lambda target: (0, 0, "/root", "/bin/sh"))
        monkeypatch.setattr(Q, "_resolve_argv", lambda argv: ["/usr/bin/id"])
        monkeypatch.setattr(Q, "_send",
                            lambda sock, obj: sent.append(dict(obj)))
        # Broker approves the original identity. Let the REAL
        # _spawn_and_stream run so the pre-Popen recheck fires; stub
        # Popen so a slip-through would be loudly visible.
        monkeypatch.setattr(Q, "_ask_broker", lambda *a, **k: True)
        monkeypatch.setattr(Q.os, "getgrouplist", lambda u, g: [g])
        monkeypatch.setattr(Q.subprocess, "Popen",
                            lambda *a, **k:
                            popened.append(True) or pytest.fail(
                                "exec'd-away caller reached Popen"))

        _send(pair.a, {"target_user": "root", "argv": ["id"]})
        Q.handle_one(pair.b)

        assert popened == [], "exec'd-away caller reached Popen"
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
