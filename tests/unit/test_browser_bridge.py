"""Tests for browser_bridge — task(113)/spec/14 Phase-8 MVP.

The browser-bridge module is pure-python so every branch is
drivable in-process. Tests inject:
- BytesIO for stdin/stdout to drive the length-prefix codec.
- Stubs for the four parent-chain probes.
- A custom dispatch table for op-routing edge cases.
"""
from __future__ import annotations

import importlib.util
import io
import json
import struct
import sys
from pathlib import Path

_MOD = (Path(__file__).resolve().parent.parent
        / "browser_bridge" / "qdistro_browser_bridge.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge", _MOD)
bb = importlib.util.module_from_spec(spec)
sys.modules["qdistro_browser_bridge"] = bb
spec.loader.exec_module(bb)


# ---- length-prefix framing ----

class TestFraming:
    def test_roundtrip(self):
        buf = io.BytesIO()
        bb.write_message(buf, {"op": "qdistro.ping", "n": 42})
        buf.seek(0)
        msg = bb.read_message(buf)
        assert msg == {"op": "qdistro.ping", "n": 42}

    def test_eof_returns_none(self):
        assert bb.read_message(io.BytesIO()) is None

    def test_short_length_prefix_raises(self):
        try:
            bb.read_message(io.BytesIO(b"\x01\x02"))
        except ValueError as e:
            assert "short length prefix" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_too_large_length_raises(self):
        # 5 MiB request — over the 4 MiB cap.
        big = struct.pack("<I", 5 * 1024 * 1024)
        try:
            bb.read_message(io.BytesIO(big))
        except ValueError as e:
            assert "too large" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_short_body_raises(self):
        # Length says 100, only 5 bytes of body present.
        prefix = struct.pack("<I", 100)
        try:
            bb.read_message(io.BytesIO(prefix + b"hello"))
        except ValueError as e:
            assert "short body" in str(e)
        else:
            raise AssertionError("expected ValueError")


# ---- parent-chain identity ----

class TestVerifyParent:
    def test_allowed_firefox(self):
        ident = bb.verify_parent(
            ppid_fn=lambda: 4242,
            exe_reader=lambda _p: "/usr/lib64/firefox/firefox",
            selinux_reader=lambda _p: "user_u:user_r:user_t:s0",
        )
        assert ident["allowed"] is True
        assert ident["ppid"] == 4242
        assert ident["parent_exe"] == "/usr/lib64/firefox/firefox"
        assert ident["parent_selinux"].startswith("user_u")

    def test_allowed_chromium(self):
        ident = bb.verify_parent(
            ppid_fn=lambda: 1,
            exe_reader=lambda _p: "/usr/bin/chromium",
            selinux_reader=lambda _p: "",
        )
        assert ident["allowed"] is True

    def test_denied_unknown_parent(self):
        ident = bb.verify_parent(
            ppid_fn=lambda: 999,
            exe_reader=lambda _p: "/usr/local/bin/curl",
            selinux_reader=lambda _p: "",
        )
        assert ident["allowed"] is False

    def test_denied_snap_firefox(self):
        # The Snap Firefox case from spec/14 §"Supported-browser
        # matrix" — parent ends up being xdg-desktop-portal, not
        # firefox itself.
        ident = bb.verify_parent(
            ppid_fn=lambda: 1234,
            exe_reader=lambda _p: "/usr/libexec/xdg-desktop-portal",
            selinux_reader=lambda _p: "",
        )
        assert ident["allowed"] is False

    def test_denied_empty_exe(self):
        ident = bb.verify_parent(
            ppid_fn=lambda: 1,
            exe_reader=lambda _p: "",
            selinux_reader=lambda _p: "",
        )
        assert ident["allowed"] is False

    def test_explicit_allowlist_override(self):
        ident = bb.verify_parent(
            ppid_fn=lambda: 1,
            exe_reader=lambda _p: "/opt/qdistro/test-firefox",
            selinux_reader=lambda _p: "",
            allowlist=("/opt/qdistro/test-firefox",),
        )
        assert ident["allowed"] is True


# ---- dispatch ----

class TestDispatch:
    _ALLOWED = {
        "ppid": 100, "parent_exe": "/usr/lib64/firefox/firefox",
        "parent_selinux": "user_u:user_r:user_t:s0", "allowed": True}
    _DENIED = {**_ALLOWED, "allowed": False}

    def test_ping_handler(self):
        resp = bb.dispatch(
            {"op": "qdistro.ping", "echo": "hello",
             "extension_id": "qdistro@qdistro.local"},
            self._ALLOWED)
        assert resp["ok"] is True
        assert resp["op"] == "qdistro.ping"
        assert resp["pong"] is True
        assert resp["echo"] == "hello"
        assert resp["parent_exe"] == "/usr/lib64/firefox/firefox"
        assert resp["extension_id"] == "qdistro@qdistro.local"

    def test_denied_parent_short_circuits(self):
        resp = bb.dispatch(
            {"op": "qdistro.ping"}, self._DENIED)
        assert resp["ok"] is False
        assert resp["error"] == "parent_not_allowed"

    def test_unknown_op(self):
        resp = bb.dispatch({"op": "qdistro.fake"}, self._ALLOWED)
        assert resp["ok"] is False
        assert resp["error"] == "unknown_op"
        assert resp["op"] == "qdistro.fake"

    def test_missing_op(self):
        resp = bb.dispatch({}, self._ALLOWED)
        assert resp["ok"] is False
        assert resp["error"] == "missing_op"

    def test_handler_raises_caught(self):
        def boom(_msg, _id):
            raise RuntimeError("nope")
        resp = bb.dispatch(
            {"op": "x"}, self._ALLOWED,
            handlers={"x": boom})
        assert resp["ok"] is False
        assert resp["error"] == "handler_raised"
        assert resp["op"] == "x"
        assert "nope" in resp["detail"]

    def test_custom_handler(self):
        def echo(msg, _id):
            return {"got": msg.get("payload")}
        resp = bb.dispatch(
            {"op": "myop", "payload": [1, 2, 3]},
            self._ALLOWED,
            handlers={"myop": echo})
        assert resp["ok"] is True
        assert resp["got"] == [1, 2, 3]
        assert resp["op"] == "myop"


# ---- main loop ----

class TestMainLoop:
    def _stdio_with(self, msgs):
        """Build a BytesIO stdin with N length-prefixed messages.
        Returns (stdin, stdout)."""
        stdin = io.BytesIO()
        for m in msgs:
            body = json.dumps(m).encode()
            stdin.write(struct.pack("<I", len(body)))
            stdin.write(body)
        stdin.seek(0)
        return stdin, io.BytesIO()

    def _read_responses(self, stdout):
        stdout.seek(0)
        out = []
        while True:
            msg = bb.read_message(stdout)
            if msg is None:
                break
            out.append(msg)
        return out

    def test_roundtrip_one_message(self, monkeypatch):
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1,
                     "parent_exe": "/usr/lib64/firefox/firefox",
                     "parent_selinux": "", "allowed": True})
        stdin, stdout = self._stdio_with(
            [{"op": "qdistro.ping", "echo": "ok"}])
        rc = bb.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        responses = self._read_responses(stdout)
        assert len(responses) == 1
        assert responses[0]["pong"] is True
        assert responses[0]["echo"] == "ok"

    def test_two_messages(self, monkeypatch):
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1,
                     "parent_exe": "/usr/bin/chromium",
                     "parent_selinux": "", "allowed": True})
        stdin, stdout = self._stdio_with([
            {"op": "qdistro.ping", "echo": "first"},
            {"op": "qdistro.ping", "echo": "second"},
        ])
        rc = bb.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        responses = self._read_responses(stdout)
        assert len(responses) == 2
        assert [r["echo"] for r in responses] == ["first", "second"]

    def test_denied_parent_main(self, monkeypatch):
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1, "parent_exe": "/bin/sh",
                     "parent_selinux": "", "allowed": False})
        stdin, stdout = self._stdio_with(
            [{"op": "qdistro.ping"}])
        rc = bb.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        responses = self._read_responses(stdout)
        assert responses[0]["error"] == "parent_not_allowed"

    def test_frame_error_returns_2(self, monkeypatch):
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1,
                     "parent_exe": "/usr/lib64/firefox/firefox",
                     "parent_selinux": "", "allowed": True})
        # Truncated frame: claims 100-byte body, supplies 3.
        stdin = io.BytesIO(struct.pack("<I", 100) + b"abc")
        stdout = io.BytesIO()
        rc = bb.main(stdin=stdin, stdout=stdout)
        assert rc == 2
