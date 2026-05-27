"""Tests for the sandboxed Python hook executor.

Covers:
- HookLoader: loading modules, calling hooks, hot-reload, error handling
- Protocol: length-prefixed JSON frames over AF_UNIX
- Server: SO_PEERCRED verification, connection handling, timeout
- End-to-end: executor process serving requests
"""
from __future__ import annotations

import json
import os
import socket
import struct
import textwrap
import threading
import time
from pathlib import Path

import pytest

# Make broker modules importable.
import sys
_BROKER = Path(__file__).resolve().parents[1] / "broker"
if str(_BROKER) not in sys.path:
    sys.path.insert(0, str(_BROKER))

from qdistro_hook_executor import (  # noqa: E402
    HookLoader,
    _handle_connection,
    _send_frame,
    _recv_frame,
    _peer_uid,
    serve,
)


# ---------------------------------------------------------------------------
# HookLoader tests
# ---------------------------------------------------------------------------

class TestHookLoader:
    def test_load_valid_module(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "myhook.py").write_text(textwrap.dedent("""\
            def on_clipboard_send(event):
                return {"action": "allow"}
        """))
        loader = HookLoader(str(hooks))
        errs = loader.reload_all()
        assert errs == []
        assert loader.module_count == 1

    def test_call_hook_returns_allow(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "allow_all.py").write_text(textwrap.dedent("""\
            def on_test_action(event):
                return {"action": "allow"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        result = loader.call_hooks("test_action", {"foo": "bar"})
        assert result == {"action": "allow"}

    def test_call_hook_returns_deny(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "deny_big.py").write_text(textwrap.dedent("""\
            def on_clipboard_send(event):
                if event.get("payload_size", 0) > 100:
                    return {"action": "deny", "reason": "too big"}
                return None
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        result = loader.call_hooks("clipboard_send",
                                    {"payload_size": 200})
        assert result is not None
        assert result["action"] == "deny"
        assert "too big" in result["reason"]

    def test_call_hook_returns_transform(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "truncate.py").write_text(textwrap.dedent("""\
            def on_clipboard_send(event):
                return {"action": "transform", "new_payload_size": 500,
                        "transform_desc": "truncated"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        result = loader.call_hooks("clipboard_send", {})
        assert result is not None
        assert result["action"] == "transform"
        assert result["new_payload_size"] == 500

    def test_call_hook_returns_none_fallthrough(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "passthrough.py").write_text(textwrap.dedent("""\
            def on_clipboard_send(event):
                return None
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        result = loader.call_hooks("clipboard_send", {})
        assert result is None

    def test_no_handler_for_action(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "other.py").write_text(textwrap.dedent("""\
            def on_file_open(event):
                return {"action": "allow"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        # Action is clipboard_send but hook only handles file_open.
        result = loader.call_hooks("clipboard_send", {})
        assert result is None

    def test_hook_exception_falls_through(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "crasher.py").write_text(textwrap.dedent("""\
            def on_clipboard_send(event):
                raise ValueError("intentional crash")
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        # Exception is caught; result is None (fall through).
        result = loader.call_hooks("clipboard_send", {})
        assert result is None

    def test_malformed_hook_file_does_not_crash(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "bad_syntax.py").write_text("def broken(\n")
        loader = HookLoader(str(hooks))
        errs = loader.reload_all()
        assert len(errs) == 1
        assert "bad_syntax.py" in errs[0]
        assert loader.module_count == 0

    def test_malformed_return_value_skipped(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "bad_return.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return "not a dict"
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        result = loader.call_hooks("test", {})
        assert result is None

    def test_missing_action_key_in_return(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "no_action.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return {"verdict": "allow"}  # missing 'action' key
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        result = loader.call_hooks("test", {})
        assert result is None

    def test_hot_reload_picks_up_new_file(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        loader = HookLoader(str(hooks))
        loader.reload_all()
        assert loader.module_count == 0

        (hooks / "new_hook.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return {"action": "deny", "reason": "new hook"}
        """))
        loader.reload_all()
        assert loader.module_count == 1
        result = loader.call_hooks("test", {})
        assert result is not None
        assert result["action"] == "deny"

    def test_hot_reload_removes_deleted_file(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        f = hooks / "temp_hook.py"
        f.write_text(textwrap.dedent("""\
            def on_test(event):
                return {"action": "allow"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        assert loader.module_count == 1

        f.unlink()
        loader.reload_all()
        assert loader.module_count == 0

    def test_hot_reload_updates_changed_file(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        f = hooks / "mutable.py"
        f.write_text(textwrap.dedent("""\
            def on_test(event):
                return {"action": "allow"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        result = loader.call_hooks("test", {})
        assert result["action"] == "allow"

        # Ensure mtime changes (some filesystems have 1s resolution).
        time.sleep(0.05)
        f.write_text(textwrap.dedent("""\
            def on_test(event):
                return {"action": "deny", "reason": "updated"}
        """))
        # Force mtime difference for fast filesystems.
        os.utime(str(f), (time.time() + 1, time.time() + 1))
        loader.reload_all()
        result = loader.call_hooks("test", {})
        assert result["action"] == "deny"

    def test_multiple_hooks_first_wins(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "aaa_first.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return {"action": "deny", "reason": "first hook"}
        """))
        (hooks / "bbb_second.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return {"action": "allow"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        result = loader.call_hooks("test", {})
        # Sorted by filename: aaa_first runs first and returns deny.
        assert result["action"] == "deny"

    def test_first_hook_none_second_decides(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "aaa_pass.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return None
        """))
        (hooks / "bbb_decide.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return {"action": "allow"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()
        result = loader.call_hooks("test", {})
        assert result["action"] == "allow"

    def test_nonexistent_directory(self):
        loader = HookLoader("/nonexistent/hooks/path")
        errs = loader.reload_all()
        assert len(errs) == 1
        assert "does not exist" in errs[0]

    def test_empty_directory(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        loader = HookLoader(str(hooks))
        errs = loader.reload_all()
        assert errs == []
        assert loader.module_count == 0


# ---------------------------------------------------------------------------
# Protocol frame tests (using socketpair)
# ---------------------------------------------------------------------------

class TestProtocolFrames:
    def test_send_recv_frame(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            payload = b'{"action":"test","event":{}}'
            assert _send_frame(a, payload)
            received = _recv_frame(b)
            assert received == payload
        finally:
            a.close()
            b.close()

    def test_recv_frame_eof(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        a.close()
        result = _recv_frame(b)
        assert result is None
        b.close()

    def test_oversized_frame_rejected(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Send a length header claiming 10 MiB.
            a.sendall(struct.pack("!I", 10 * 1024 * 1024))
            result = _recv_frame(b)
            assert result is None
        finally:
            a.close()
            b.close()


# ---------------------------------------------------------------------------
# Connection handler test (in-process, no server loop)
# ---------------------------------------------------------------------------

class TestConnectionHandler:
    def test_handle_allow_hook(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "h.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return {"action": "allow"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()

        client, server = socket.socketpair(socket.AF_UNIX,
                                            socket.SOCK_STREAM)
        t = threading.Thread(target=_handle_connection,
                             args=(server, loader), daemon=True)
        t.start()

        req = json.dumps({"action": "test", "event": {"x": 1}})
        _send_frame(client, req.encode("utf-8"))
        raw = _recv_frame(client)
        assert raw is not None
        resp = json.loads(raw)
        assert resp["verdict"] == "allow"

        client.close()
        t.join(timeout=2)

    def test_handle_deny_hook(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "h.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return {"action": "deny", "reason": "nope"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()

        client, server = socket.socketpair(socket.AF_UNIX,
                                            socket.SOCK_STREAM)
        t = threading.Thread(target=_handle_connection,
                             args=(server, loader), daemon=True)
        t.start()

        req = json.dumps({"action": "test", "event": {}})
        _send_frame(client, req.encode("utf-8"))
        raw = _recv_frame(client)
        resp = json.loads(raw)
        assert resp["verdict"] == "deny"
        assert resp["reason"] == "nope"

        client.close()
        t.join(timeout=2)

    def test_handle_transform_hook(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "h.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return {"action": "transform",
                        "new_payload_size": 42,
                        "transform_desc": "shrunk"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()

        client, server = socket.socketpair(socket.AF_UNIX,
                                            socket.SOCK_STREAM)
        t = threading.Thread(target=_handle_connection,
                             args=(server, loader), daemon=True)
        t.start()

        req = json.dumps({"action": "test", "event": {}})
        _send_frame(client, req.encode("utf-8"))
        raw = _recv_frame(client)
        resp = json.loads(raw)
        assert resp["verdict"] == "transform"
        assert resp["new_payload_size"] == 42

        client.close()
        t.join(timeout=2)

    def test_handle_null_verdict_fallthrough(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "h.py").write_text(textwrap.dedent("""\
            def on_test(event):
                return None
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()

        client, server = socket.socketpair(socket.AF_UNIX,
                                            socket.SOCK_STREAM)
        t = threading.Thread(target=_handle_connection,
                             args=(server, loader), daemon=True)
        t.start()

        req = json.dumps({"action": "test", "event": {}})
        _send_frame(client, req.encode("utf-8"))
        raw = _recv_frame(client)
        resp = json.loads(raw)
        assert resp["verdict"] is None

        client.close()
        t.join(timeout=2)

    def test_handle_exception_in_hook(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "h.py").write_text(textwrap.dedent("""\
            def on_test(event):
                raise RuntimeError("boom")
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()

        client, server = socket.socketpair(socket.AF_UNIX,
                                            socket.SOCK_STREAM)
        t = threading.Thread(target=_handle_connection,
                             args=(server, loader), daemon=True)
        t.start()

        req = json.dumps({"action": "test", "event": {}})
        _send_frame(client, req.encode("utf-8"))
        raw = _recv_frame(client)
        resp = json.loads(raw)
        # Exception falls through -> null verdict.
        assert resp["verdict"] is None

        client.close()
        t.join(timeout=2)

    def test_handle_malformed_json(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        loader = HookLoader(str(hooks))
        loader.reload_all()

        client, server = socket.socketpair(socket.AF_UNIX,
                                            socket.SOCK_STREAM)
        t = threading.Thread(target=_handle_connection,
                             args=(server, loader), daemon=True)
        t.start()

        _send_frame(client, b"not json at all")
        raw = _recv_frame(client)
        resp = json.loads(raw)
        assert resp["verdict"] is None
        assert "malformed" in resp.get("error", "").lower()

        client.close()
        t.join(timeout=2)

    def test_multiple_requests_on_one_connection(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "h.py").write_text(textwrap.dedent("""\
            def on_a(event):
                return {"action": "allow"}
            def on_b(event):
                return {"action": "deny", "reason": "no"}
        """))
        loader = HookLoader(str(hooks))
        loader.reload_all()

        client, server = socket.socketpair(socket.AF_UNIX,
                                            socket.SOCK_STREAM)
        t = threading.Thread(target=_handle_connection,
                             args=(server, loader), daemon=True)
        t.start()

        # First request: action a -> allow.
        req1 = json.dumps({"action": "a", "event": {}})
        _send_frame(client, req1.encode("utf-8"))
        resp1 = json.loads(_recv_frame(client))
        assert resp1["verdict"] == "allow"

        # Second request: action b -> deny.
        req2 = json.dumps({"action": "b", "event": {}})
        _send_frame(client, req2.encode("utf-8"))
        resp2 = json.loads(_recv_frame(client))
        assert resp2["verdict"] == "deny"

        client.close()
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# Server integration test (full serve() with real AF_UNIX)
# ---------------------------------------------------------------------------

class TestServerIntegration:
    def test_serve_and_query(self, tmp_path):
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "h.py").write_text(textwrap.dedent("""\
            def on_test(event):
                if event.get("block"):
                    return {"action": "deny", "reason": "blocked"}
                return None
        """))
        sock_path = str(tmp_path / "test.sock")
        stop = threading.Event()

        # Start server in background.
        t = threading.Thread(
            target=serve,
            kwargs={
                "hook_dir": str(hooks),
                "socket_path": sock_path,
                "broker_uid": -1,  # disable peer-uid check for test
                "stop_event": stop,
            },
            daemon=True,
        )
        t.start()

        # Wait for socket to appear.
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.05)
        assert os.path.exists(sock_path)

        # Connect and send a request that falls through.
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(5)
        conn.connect(sock_path)
        req = json.dumps({"action": "test", "event": {"block": False}})
        _send_frame(conn, req.encode("utf-8"))
        raw = _recv_frame(conn)
        resp = json.loads(raw)
        assert resp["verdict"] is None
        conn.close()

        # Connect and send a request that gets denied.
        conn2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn2.settimeout(5)
        conn2.connect(sock_path)
        req2 = json.dumps({"action": "test", "event": {"block": True}})
        _send_frame(conn2, req2.encode("utf-8"))
        raw2 = _recv_frame(conn2)
        resp2 = json.loads(raw2)
        assert resp2["verdict"] == "deny"
        assert resp2["reason"] == "blocked"
        conn2.close()

        # Stop server.
        stop.set()
        t.join(timeout=5)

    def test_server_hot_reload(self, tmp_path):
        """Verify that adding a hook file while the server is running
        makes the new hook available on the next query."""
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        sock_path = str(tmp_path / "reload.sock")
        stop = threading.Event()

        t = threading.Thread(
            target=serve,
            kwargs={
                "hook_dir": str(hooks),
                "socket_path": sock_path,
                "broker_uid": -1,
                "stop_event": stop,
            },
            daemon=True,
        )
        t.start()
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.05)

        # Query with no hooks loaded -> null verdict.
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(5)
        conn.connect(sock_path)
        _send_frame(conn, json.dumps(
            {"action": "new_action", "event": {}}).encode("utf-8"))
        resp = json.loads(_recv_frame(conn))
        assert resp["verdict"] is None
        conn.close()

        # Drop a new hook file.
        (hooks / "dynamic.py").write_text(textwrap.dedent("""\
            def on_new_action(event):
                return {"action": "allow"}
        """))
        # Wait for the polling watcher to pick it up (poll interval
        # is 5s in production; in tests the poll thread or inotify
        # thread will reload within a few seconds).
        time.sleep(7)

        conn2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn2.settimeout(5)
        conn2.connect(sock_path)
        _send_frame(conn2, json.dumps(
            {"action": "new_action", "event": {}}).encode("utf-8"))
        resp2 = json.loads(_recv_frame(conn2))
        assert resp2["verdict"] == "allow"
        conn2.close()

        stop.set()
        t.join(timeout=5)


# ---------------------------------------------------------------------------
# SO_PEERCRED test (best-effort: works on Linux only)
# ---------------------------------------------------------------------------

class TestPeerCredentials:
    def test_peer_uid_returns_own_uid(self):
        """On Linux, SO_PEERCRED on a socketpair returns the current uid."""
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            uid = _peer_uid(a)
            if uid is not None:  # Non-Linux returns None.
                assert uid == os.getuid()
        finally:
            a.close()
            b.close()
