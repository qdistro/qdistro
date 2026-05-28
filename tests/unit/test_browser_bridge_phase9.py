"""Phase-9 unit tests for qdistro-browser-bridge.

Spec: ``todo/browser/01-bridge-phase9.md`` (sections 9a–9e).

Test layers:

* 9a — pwd.fill / pwd.save: handler happy path, intent-token gate,
  parent_not_allowed, D-Bus body shape, identity stripped from reply.
* 9b — request_id correlation: in-order, out-of-order, unknown id,
  timeout; heartbeat round-trip + miss-driven exit.
* 9c — page.extract: forwards to broker via D-Bus mock.
* 9d — intent tokens: handshake exchanges secret; valid / expired /
  replay / wrong-op / future / bad-hmac rejection.
* 9e — MPRIS / downloads / notifications / screenlock_inhibit/release.

Every test mocks the D-Bus client by assigning a fake to
``bb._dbus_client``. No session bus is required.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import queue
import struct
import sys
import threading
import time
from pathlib import Path

import pytest


class _FrameStream:
    """Thread-safe write-only stream that records each ``write`` call.

    The bridge calls ``write(prefix)`` then ``write(body)`` then
    ``flush()``. We buffer per-write and assemble frames lazily so a
    reader thread can pop one frame at a time without racing the
    position pointer.
    """
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._frames: queue.Queue = queue.Queue()
        self._pending_len: int | None = None

    def write(self, data: bytes) -> int:
        with self._lock:
            self._buf.extend(data)
            self._drain()
        return len(data)

    def flush(self) -> None:
        pass

    def _drain(self) -> None:
        while True:
            if self._pending_len is None:
                if len(self._buf) < 4:
                    return
                self._pending_len = struct.unpack(
                    "<I", bytes(self._buf[:4]))[0]
                del self._buf[:4]
            if len(self._buf) < self._pending_len:
                return
            frame = bytes(self._buf[:self._pending_len])
            del self._buf[:self._pending_len]
            self._pending_len = None
            self._frames.put(json.loads(frame.decode()))

    def pop_frame(self, timeout: float = 2.0) -> dict:
        return self._frames.get(timeout=timeout)

_MOD = (Path(__file__).resolve().parent.parent.parent
        / "browser_bridge" / "qdistro_browser_bridge.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge", _MOD)
bb = importlib.util.module_from_spec(spec)
sys.modules["qdistro_browser_bridge"] = bb
spec.loader.exec_module(bb)


# ---- shared helpers ----

ALLOWED = {
    "ppid": 100,
    "parent_exe": "/usr/lib64/firefox/firefox",
    "parent_selinux": "user_u:user_r:user_t:s0",
    "extension_id": "qdistro@qdistro.local",
    "allowed": True,
}
DENIED = {**ALLOWED, "allowed": False}


class FakeDBus(bb._BaseDBusClient):
    """Records every call; returns a canned reply or one per service."""

    def __init__(self, reply=None, by_method=None):
        self.calls = []
        self._reply = reply or {"ok": True}
        self._by_method = by_method or {}

    def call(self, bus, service, object_path, interface, method,
             signature, body):
        self.calls.append({
            "bus": bus, "service": service,
            "object_path": object_path,
            "interface": interface, "method": method,
            "signature": signature, "body": body,
        })
        if method in self._by_method:
            return dict(self._by_method[method])
        return dict(self._reply)


@pytest.fixture(autouse=True)
def _isolate_state():
    """Each test gets a fresh session secret + empty pending/heartbeat."""
    bb.reset_session_secret()
    bb._pending.clear()
    bb._heartbeat.__init__()  # type: ignore[misc]
    yield
    bb._pending.clear()
    bb._dbus_client = None


def _mint_token(op, request_id="ti-1", at=None):
    ts = time.time() if at is None else at
    mac = bb._compute_token_hmac(request_id, ts, op)
    return {"request_id": request_id, "ts": ts, "op": op, "hmac": mac}


# =====================================================================
# 9a — pwd.fill / pwd.save
# =====================================================================

class TestPwdFill:
    def test_happy_path_forwards_to_dbus(self):
        bb._dbus_client = FakeDBus(reply={
            "ok": True, "credentials": [
                {"username": "alice", "password": "redacted"}]})
        resp = bb.dispatch(
            {"op": "pwd.fill",
             "url": "https://example.com/login",
             "intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        assert resp["ok"] is True
        assert resp["op"] == "pwd.fill"
        assert resp["credentials"][0]["username"] == "alice"
        # D-Bus body must NOT include parent_selinux / ppid leaks.
        # SYSTEM bus + canonical org.qdistro.Pwd1 name (P04 fix-pass H3).
        call = bb._dbus_client.calls[0]
        assert call["bus"] == "SYSTEM"
        assert call["service"] == "org.qdistro.Pwd1"
        assert call["method"] == "Fill"
        body = json.loads(call["body"][0])
        assert body["url"] == "https://example.com/login"
        assert body["extension_id"] == "qdistro@qdistro.local"
        assert body["parent_exe"] == "/usr/lib64/firefox/firefox"
        assert "parent_selinux" not in body

    def test_parent_not_allowed(self):
        bb._dbus_client = FakeDBus()
        resp = bb.dispatch(
            {"op": "pwd.fill", "url": "https://x.com",
             "intent_token": _mint_token("pwd.fill")},
            DENIED)
        assert resp["ok"] is False
        assert resp["error"] == "parent_not_allowed"
        # Must not call the daemon when identity check fails.
        assert bb._dbus_client.calls == []

    def test_missing_intent_token_rejected(self):
        bb._dbus_client = FakeDBus()
        resp = bb.dispatch(
            {"op": "pwd.fill", "url": "https://x.com"},
            ALLOWED)
        assert resp["ok"] is False
        assert resp["error"] == "missing_intent_token"
        assert bb._dbus_client.calls == []

    def test_missing_url(self):
        bb._dbus_client = FakeDBus()
        resp = bb.dispatch(
            {"op": "pwd.fill",
             "intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        assert resp["ok"] is False
        assert resp["error"] == "missing_url"

    def test_identity_stripped_from_reply(self):
        # If the daemon (mock) accidentally returns identity-shaped
        # fields, the handler must scrub them so the extension never
        # sees the kernel-attested chain.
        bb._dbus_client = FakeDBus(reply={
            "ok": True, "ppid": 9999, "parent_exe": "/leak",
            "parent_selinux": "leak", "extension_id": "leak",
            "credentials": []})
        resp = bb.dispatch(
            {"op": "pwd.fill", "url": "https://x.com",
             "intent_token": _mint_token("pwd.fill")},
            ALLOWED)
        for leak in ("ppid", "parent_exe", "parent_selinux",
                     "extension_id"):
            assert leak not in resp, f"{leak} leaked into reply"


class TestPwdSave:
    def test_happy_path(self):
        bb._dbus_client = FakeDBus(reply={"ok": True})
        resp = bb.dispatch(
            {"op": "pwd.save",
             "url": "https://example.com",
             "username": "alice", "password": "s3cr3t",
             "intent_token": _mint_token("pwd.save")},
            ALLOWED)
        assert resp["ok"] is True
        call = bb._dbus_client.calls[0]
        body = json.loads(call["body"][0])
        assert body["username"] == "alice"
        assert body["password"] == "s3cr3t"
        assert call["method"] == "Save"

    def test_missing_credentials(self):
        bb._dbus_client = FakeDBus()
        resp = bb.dispatch(
            {"op": "pwd.save", "url": "https://x.com",
             "intent_token": _mint_token("pwd.save")},
            ALLOWED)
        assert resp["error"] == "missing_credentials"
        assert bb._dbus_client.calls == []


# =====================================================================
# 9b — request_id correlation + heartbeat
# =====================================================================

class TestRequestCorrelation:
    def test_in_order_reply(self):
        out = _FrameStream()
        result: dict = {}

        def daemon():
            result["reply"] = bb.enqueue_inbound_request(
                "tabs.list", {}, out, timeout_s=2.0)

        t = threading.Thread(target=daemon)
        t.start()
        sent = out.pop_frame()
        assert sent["op"] == "tabs.list"
        bb.deliver_reply({
            "op": "tabs.list.reply", "request_id": sent["request_id"],
            "tabs": [{"id": 1, "url": "https://x.com"}]})
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert result["reply"]["tabs"][0]["id"] == 1

    def test_out_of_order_replies(self):
        out = _FrameStream()
        results: list = [None, None]

        def daemon(i, op):
            results[i] = bb.enqueue_inbound_request(
                op, {}, out, timeout_s=2.0)

        t1 = threading.Thread(target=daemon, args=(0, "tabs.list"))
        t2 = threading.Thread(target=daemon, args=(1, "tabs.open"))
        t1.start(); t2.start()
        a = out.pop_frame()
        b = out.pop_frame()
        # Reply to second-sent first.
        bb.deliver_reply({
            "op": f"{b['op']}.reply",
            "request_id": b["request_id"], "tab_id": 42})
        bb.deliver_reply({
            "op": f"{a['op']}.reply",
            "request_id": a["request_id"], "tabs": []})
        t1.join(timeout=2.0); t2.join(timeout=2.0)
        # Map results back by op so the test isn't fragile to
        # thread-start ordering.
        by_op = {
            "tabs.list": results[0] if a["op"] == "tabs.list" else results[1],
            "tabs.open": results[0] if a["op"] == "tabs.open" else results[1],
        }
        assert by_op["tabs.list"]["tabs"] == []
        assert by_op["tabs.open"]["tab_id"] == 42

    def test_unknown_request_id_no_match(self):
        assert bb.deliver_reply(
            {"op": "tabs.list.reply",
             "request_id": "ghost"}) is False

    def test_timeout_returns_error(self):
        out = _FrameStream()
        reply = bb.enqueue_inbound_request(
            "tabs.list", {}, out, timeout_s=0.1)
        assert reply["ok"] is False
        assert reply["error"] == "request_timeout"
        assert all(p.op != "tabs.list" for p in bb._pending.values())


class TestStringRequestId:
    """Explicit coverage for string request_ids (e.g. "r1-abc123")
    generated by _next_request_id(). The bridge always generates string
    request_ids; the extension dispatchers must accept both string and
    integer request_ids (they check `!= null`, not typeof)."""

    def test_next_request_id_returns_string(self):
        rid = bb._next_request_id()
        assert isinstance(rid, str)
        assert rid.startswith("r")
        # Format: r<seq>-<hex>
        parts = rid.split("-", 1)
        assert len(parts) == 2
        assert parts[0][1:].isdigit()
        assert len(parts[1]) == 8  # token_hex(4) = 8 chars

    def test_deliver_reply_matches_string_request_id(self):
        """deliver_reply must match when request_id is a string like
        'r5-deadbeef'."""
        rid = "r5-deadbeef"
        pending = bb._PendingRequest("tabs.list")
        with bb._pending_lock:
            bb._pending[rid] = pending
        matched = bb.deliver_reply({
            "op": "tabs.list.reply",
            "request_id": rid,
            "tabs": [{"id": 1}],
        })
        assert matched is True
        assert pending.reply is not None
        assert pending.reply["tabs"][0]["id"] == 1

    def test_deliver_reply_rejects_integer_request_id(self):
        """deliver_reply requires string request_id; integer keys are
        never inserted into _pending, so integer lookups must not match."""
        assert bb.deliver_reply({
            "op": "tabs.list.reply",
            "request_id": 42,
        }) is False

    def test_deliver_reply_rejects_empty_string(self):
        assert bb.deliver_reply({
            "op": "tabs.list.reply",
            "request_id": "",
        }) is False

    def test_deliver_reply_rejects_none(self):
        assert bb.deliver_reply({
            "op": "tabs.list.reply",
            "request_id": None,
        }) is False

    def test_enqueue_and_deliver_round_trip_uses_string_ids(self):
        """Full round-trip: enqueue generates a string request_id, the
        extension echoes it back, deliver_reply matches."""
        out = _FrameStream()
        result: dict = {}

        def daemon():
            result["reply"] = bb.enqueue_inbound_request(
                "tabs.list", {"window_id": 1}, out, timeout_s=2.0)

        t = threading.Thread(target=daemon)
        t.start()
        sent = out.pop_frame()
        # request_id on the wire must be a string, not an integer
        assert isinstance(sent["request_id"], str)
        assert sent["request_id"].startswith("r")
        bb.deliver_reply({
            "op": "tabs.list.reply",
            "request_id": sent["request_id"],
            "ok": True,
            "tabs": [{"id": 10, "url": "https://test.com"}],
        })
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert result["reply"]["ok"] is True
        assert result["reply"]["tabs"][0]["id"] == 10


class TestPageExtractRequest:
    """Bridge-initiated `page.extract.request` — the read-side
    counterpart to user-initiated `page.extract`. Daemons (compositor,
    agent-control, recall) call the bridge's D-Bus surface with
    op="page.extract.request" + JSON args; the bridge enqueues the
    inbound op down the stdio pipe and blocks on the extension's
    reply. Wire shape is symmetric with the tabs.* bridge → ext
    family — no intent token (the bridge's identity gate is the
    security boundary; the extension trusts whatever the bridge
    relays)."""

    def test_round_trip(self):
        out = _FrameStream()
        result: dict = {}

        def daemon():
            result["reply"] = bb.enqueue_inbound_request(
                "page.extract.request",
                {"tab_id": 5, "mode": "visible_text"},
                out, timeout_s=2.0)

        t = threading.Thread(target=daemon)
        t.start()
        sent = out.pop_frame()
        assert sent["op"] == "page.extract.request"
        assert sent["tab_id"] == 5
        assert sent["mode"] == "visible_text"
        bb.deliver_reply({
            "op": "page.extract.request.reply",
            "request_id": sent["request_id"],
            "ok": True,
            "mode": "visible_text",
            "url": "https://example.com/",
            "title": "Example",
            "content": "Hello world",
            "truncated": False,
        })
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert result["reply"]["ok"] is True
        assert result["reply"]["content"] == "Hello world"
        assert result["reply"]["url"] == "https://example.com/"

    def test_by_selector_round_trip(self):
        out = _FrameStream()
        result: dict = {}

        def daemon():
            result["reply"] = bb.enqueue_inbound_request(
                "page.extract.request",
                {"tab_id": 7, "mode": "by_selector", "selector": "h1"},
                out, timeout_s=2.0)

        t = threading.Thread(target=daemon)
        t.start()
        sent = out.pop_frame()
        assert sent["op"] == "page.extract.request"
        assert sent["mode"] == "by_selector"
        assert sent["selector"] == "h1"
        bb.deliver_reply({
            "op": "page.extract.request.reply",
            "request_id": sent["request_id"],
            "ok": True, "mode": "by_selector",
            "content": "Headline", "matched": True,
            "url": "https://example.com/", "title": "Example",
        })
        t.join(timeout=2.0)
        assert result["reply"]["matched"] is True
        assert result["reply"]["content"] == "Headline"

    def test_extension_error_surfaces_through(self):
        """If the extension returns ok:false (e.g. tab closed mid-flight),
        the bridge returns it as-is to the D-Bus caller — no
        bridge-side massaging beyond the wire-correlation."""
        out = _FrameStream()
        result: dict = {}

        def daemon():
            result["reply"] = bb.enqueue_inbound_request(
                "page.extract.request",
                {"tab_id": 999, "mode": "visible_text"},
                out, timeout_s=2.0)

        t = threading.Thread(target=daemon)
        t.start()
        sent = out.pop_frame()
        bb.deliver_reply({
            "op": "page.extract.request.reply",
            "request_id": sent["request_id"],
            "ok": False,
            "error": "executeScript_failed",
            "detail": "Error: No tab with id 999",
        })
        t.join(timeout=2.0)
        assert result["reply"]["ok"] is False
        assert result["reply"]["error"] == "executeScript_failed"

    def test_timeout_returns_request_timeout(self):
        out = _FrameStream()
        reply = bb.enqueue_inbound_request(
            "page.extract.request",
            {"tab_id": 1, "mode": "visible_text"},
            out, timeout_s=0.1)
        assert reply["ok"] is False
        assert reply["error"] == "request_timeout"


class TestContainersRequest:
    """Bridge-initiated `containers.list / .create / .remove` —
    Firefox-only contextual identities. Daemons (admin panel, recall)
    call the bridge's D-Bus surface; the bridge enqueues down stdio
    and blocks on the extension's reply. Wire shape symmetric with
    tabs.* and page.extract.request. No intent token — the bridge's
    identity gate is the security boundary; cross-uid routing for
    *other* uids' bridges is an open question (see
    doc/firefox-containers.md). This class only covers own-uid Option-A."""

    def test_list_round_trip(self):
        out = _FrameStream()
        result: dict = {}

        def daemon():
            result["reply"] = bb.enqueue_inbound_request(
                "containers.list", {}, out, timeout_s=2.0)

        t = threading.Thread(target=daemon)
        t.start()
        sent = out.pop_frame()
        assert sent["op"] == "containers.list"
        bb.deliver_reply({
            "op": "containers.list.reply",
            "request_id": sent["request_id"],
            "ok": True,
            "containers": [
                {
                    "cookie_store_id": "firefox-container-1",
                    "name": "Personal", "color": "blue",
                    "color_code": "#37adff", "icon": "fingerprint",
                    "icon_url": "resource://usercontext-content/fingerprint.svg",
                },
                {
                    "cookie_store_id": "firefox-container-2",
                    "name": "Work", "color": "red",
                    "color_code": "#ff613d", "icon": "briefcase",
                    "icon_url": "resource://usercontext-content/briefcase.svg",
                },
            ],
        })
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert result["reply"]["ok"] is True
        assert len(result["reply"]["containers"]) == 2
        assert result["reply"]["containers"][0]["name"] == "Personal"

    def test_create_round_trip(self):
        out = _FrameStream()
        result: dict = {}

        def daemon():
            result["reply"] = bb.enqueue_inbound_request(
                "containers.create",
                {"name": "Banking", "color": "red", "icon": "dollar"},
                out, timeout_s=2.0)

        t = threading.Thread(target=daemon)
        t.start()
        sent = out.pop_frame()
        assert sent["op"] == "containers.create"
        assert sent["name"] == "Banking"
        assert sent["color"] == "red"
        assert sent["icon"] == "dollar"
        bb.deliver_reply({
            "op": "containers.create.reply",
            "request_id": sent["request_id"],
            "ok": True,
            "container": {
                "cookie_store_id": "firefox-container-9",
                "name": "Banking", "color": "red",
                "color_code": "#ff613d", "icon": "dollar",
                "icon_url": "resource://usercontext-content/dollar.svg",
            },
        })
        t.join(timeout=2.0)
        assert result["reply"]["container"]["cookie_store_id"] == "firefox-container-9"

    def test_remove_round_trip(self):
        out = _FrameStream()
        result: dict = {}

        def daemon():
            result["reply"] = bb.enqueue_inbound_request(
                "containers.remove",
                {"cookie_store_id": "firefox-container-9"},
                out, timeout_s=2.0)

        t = threading.Thread(target=daemon)
        t.start()
        sent = out.pop_frame()
        assert sent["op"] == "containers.remove"
        assert sent["cookie_store_id"] == "firefox-container-9"
        bb.deliver_reply({
            "op": "containers.remove.reply",
            "request_id": sent["request_id"],
            "ok": True,
            "container": {
                "cookie_store_id": "firefox-container-9",
                "name": "Banking", "color": "red",
                "color_code": "#ff613d", "icon": "dollar",
                "icon_url": "resource://usercontext-content/dollar.svg",
            },
        })
        t.join(timeout=2.0)
        assert result["reply"]["ok"] is True

    def test_chromium_replies_unavailable(self):
        """qdchrome-extension has no containers module — the extension
        replies unknown_op, which the bridge surfaces verbatim."""
        out = _FrameStream()
        result: dict = {}

        def daemon():
            result["reply"] = bb.enqueue_inbound_request(
                "containers.list", {}, out, timeout_s=2.0)

        t = threading.Thread(target=daemon)
        t.start()
        sent = out.pop_frame()
        bb.deliver_reply({
            "op": "containers.list.reply",
            "request_id": sent["request_id"],
            "ok": False,
            "error": "contextualIdentities_unavailable",
            "containers": [],
        })
        t.join(timeout=2.0)
        assert result["reply"]["ok"] is False
        assert result["reply"]["error"] == "contextualIdentities_unavailable"

    def test_remove_missing_id_extension_error_surfaces(self):
        """If the daemon forgets cookie_store_id, the extension replies
        ok:false / missing_cookie_store_id; the bridge passes it
        through unchanged."""
        out = _FrameStream()
        result: dict = {}

        def daemon():
            result["reply"] = bb.enqueue_inbound_request(
                "containers.remove", {}, out, timeout_s=2.0)

        t = threading.Thread(target=daemon)
        t.start()
        sent = out.pop_frame()
        bb.deliver_reply({
            "op": "containers.remove.reply",
            "request_id": sent["request_id"],
            "ok": False, "error": "missing_cookie_store_id",
        })
        t.join(timeout=2.0)
        assert result["reply"]["error"] == "missing_cookie_store_id"

    def test_timeout_returns_request_timeout(self):
        out = _FrameStream()
        reply = bb.enqueue_inbound_request(
            "containers.list", {}, out, timeout_s=0.1)
        assert reply["ok"] is False
        assert reply["error"] == "request_timeout"


class TestHeartbeat:
    def test_send_and_ack_resets_misses(self):
        """heartbeat_loop sends a frame, ack clears miss counter."""
        # BytesIO is not thread-safe (single cursor shared between
        # writer thread and reader test). Use a real pipe so each side
        # has its own fd and read() blocks until the writer flushes.
        r_fd, w_fd = os.pipe()
        out = os.fdopen(w_fd, "wb")
        in_ = os.fdopen(r_fd, "rb", buffering=0)
        stop = threading.Event()
        clock = [1000.0]

        def now():
            return clock[0]

        def fake_exit(rc):
            stop.set()
            raise SystemExit(rc)

        def driver():
            try:
                bb.heartbeat_loop(
                    out, stop, interval_s=0.05, deadline_s=10.0,
                    max_misses=3, on_exit=fake_exit, now_fn=now)
            except SystemExit:
                pass

        t = threading.Thread(target=driver, daemon=True)
        t.start()
        try:
            acked = 0
            for _ in range(3):
                head = in_.read(4)
                assert len(head) == 4, "no heartbeat frame"
                n = struct.unpack("<I", head)[0]
                body = in_.read(n)
                hb = json.loads(body.decode())
                assert hb["op"] == "qdistro.heartbeat"
                bb._heartbeat.acked(hb["request_id"])
                acked += 1
            assert acked == 3
        finally:
            stop.set()
            t.join(timeout=2.0)
            try:
                out.close()
            except Exception:
                pass
            try:
                in_.close()
            except Exception:
                pass

    def test_three_misses_exits(self):
        """If the extension never acks, the bridge tears down."""
        out = io.BytesIO()
        stop = threading.Event()
        clock = [1000.0]

        def now():
            return clock[0]

        exited = []

        def fake_exit(rc):
            exited.append(rc)
            stop.set()

        # interval tiny so we tick fast; deadline 0 so each send
        # immediately counts as a miss on the next sweep.
        def driver():
            bb.heartbeat_loop(
                out, stop, interval_s=0.01, deadline_s=0.0,
                max_misses=3, on_exit=fake_exit, now_fn=now)
            # Advance the clock between ticks so the sweep sees the
            # send-time as ancient.
        t = threading.Thread(target=driver, daemon=True)
        t.start()
        # Advance the wall clock so sweeps mark each tick missed.
        for _ in range(50):
            clock[0] += 1.0
            if exited:
                break
            time.sleep(0.02)
        stop.set()
        t.join(timeout=2.0)
        assert exited and exited[0] == bb.EXIT_HEARTBEAT_LOST

    def test_heartbeat_ack_handler(self):
        bb._heartbeat.sent("hb-1", time.time())
        resp = bb.dispatch(
            {"op": "qdistro.heartbeat.ack", "request_id": "hb-1"},
            ALLOWED)
        assert resp["ok"] is True
        assert resp["matched"] is True


# =====================================================================
# 9c — page.extract
# =====================================================================

class TestPageExtract:
    def test_forwards_to_broker(self):
        bb._dbus_client = FakeDBus(reply={"ok": True, "delivery_id": "x"})
        resp = bb.dispatch(
            {"op": "page.extract",
             "url": "https://example.com/article",
             "title": "Hello", "selected_text": "lorem",
             "dest_uid": "dev-user",
             "intent_token": _mint_token("page.extract")},
            ALLOWED)
        assert resp["ok"] is True
        call = bb._dbus_client.calls[0]
        # Broker lives on the SYSTEM bus as ``org.qdistro.AdminBroker1``
        # (P04 fix-pass H3).
        assert call["bus"] == "SYSTEM"
        assert call["service"] == "org.qdistro.AdminBroker1"
        assert call["method"] == "PageExtract"
        body = json.loads(call["body"][0])
        assert body["dest_uid"] == "dev-user"
        assert body["title"] == "Hello"

    def test_requires_intent_token(self):
        bb._dbus_client = FakeDBus()
        resp = bb.dispatch(
            {"op": "page.extract", "url": "https://x.com"},
            ALLOWED)
        assert resp["error"] == "missing_intent_token"
        assert bb._dbus_client.calls == []

    def test_parent_not_allowed(self):
        bb._dbus_client = FakeDBus()
        resp = bb.dispatch(
            {"op": "page.extract", "url": "https://x.com",
             "intent_token": _mint_token("page.extract")},
            DENIED)
        assert resp["error"] == "parent_not_allowed"


# =====================================================================
# 9d — handshake + intent tokens + cookies.export
# =====================================================================

class TestHandshake:
    def test_returns_session_secret(self):
        resp = bb.dispatch({"op": "qdistro.handshake"}, ALLOWED)
        assert resp["ok"] is True
        assert resp["hmac_algo"] == "sha256"
        assert resp["token_ttl_s"] == bb.INTENT_TOKEN_TTL_S
        secret_hex = resp["session_secret_hex"]
        assert len(secret_hex) == 64
        # P04 fix-pass H2: handshake returns a per-extension derived
        # secret rather than the bare master. The reply is bound to
        # the caller's argv-attested extension_id.
        expected = bb.derive_extension_session_secret(
            bb._SESSION_SECRET, ALLOWED["extension_id"])
        assert bytes.fromhex(secret_hex) == expected

    def test_rotation_on_reset(self):
        a = bb.dispatch({"op": "qdistro.handshake"}, ALLOWED)
        bb.reset_session_secret()
        b = bb.dispatch({"op": "qdistro.handshake"}, ALLOWED)
        assert a["session_secret_hex"] != b["session_secret_hex"]

    def test_denied_parent(self):
        resp = bb.dispatch({"op": "qdistro.handshake"}, DENIED)
        assert resp["error"] == "parent_not_allowed"


class TestIntentToken:
    def test_valid(self):
        tok = _mint_token("pwd.fill", request_id="t-valid")
        ok, err = bb.verify_intent_token(tok, "pwd.fill")
        assert ok is True
        assert err == ""

    def test_expired(self):
        tok = _mint_token("pwd.fill", request_id="t-expired",
                          at=time.time() - 10.0)
        ok, err = bb.verify_intent_token(tok, "pwd.fill")
        assert ok is False
        assert err == "intent_token_expired"

    def test_future_dated_rejected(self):
        tok = _mint_token("pwd.fill", request_id="t-future",
                          at=time.time() + 60.0)
        ok, err = bb.verify_intent_token(tok, "pwd.fill")
        assert ok is False
        assert err == "intent_token_future"

    def test_wrong_op(self):
        tok = _mint_token("pwd.fill", request_id="t-op")
        ok, err = bb.verify_intent_token(tok, "pwd.save")
        assert ok is False
        assert err == "intent_token_op_mismatch"

    def test_bad_hmac(self):
        tok = _mint_token("pwd.fill", request_id="t-bad")
        tok["hmac"] = "deadbeef" * 8
        ok, err = bb.verify_intent_token(tok, "pwd.fill")
        assert ok is False
        assert err == "intent_token_bad_hmac"

    def test_replay_rejected(self):
        tok = _mint_token("pwd.fill", request_id="t-replay")
        ok, err = bb.verify_intent_token(tok, "pwd.fill")
        assert ok is True
        # Second use of the same request_id must fail.
        ok2, err2 = bb.verify_intent_token(dict(tok), "pwd.fill")
        assert ok2 is False
        assert err2 == "intent_token_replay"

    def test_missing(self):
        ok, err = bb.verify_intent_token(None, "pwd.fill")
        assert ok is False
        assert err == "missing_intent_token"
        ok, err = bb.verify_intent_token({}, "pwd.fill")
        assert ok is False
        assert err == "missing_intent_token"


class TestCookiesExport:
    def test_happy_path(self):
        bb._dbus_client = FakeDBus(reply={"ok": True, "exported": 3})
        resp = bb.dispatch(
            {"op": "cookies.export",
             "domain": "example.com",
             "cookie_store_id": "firefox-container-1",
             "cookies": [{"name": "sid", "value": "abc"}],
             "intent_token": _mint_token("cookies.export")},
            ALLOWED)
        assert resp["ok"] is True
        call = bb._dbus_client.calls[0]
        assert call["method"] == "ExportCookies"
        body = json.loads(call["body"][0])
        assert body["domain"] == "example.com"
        assert body["cookie_store_id"] == "firefox-container-1"
        assert body["cookies"][0]["name"] == "sid"

    def test_replayed_token_rejected(self):
        bb._dbus_client = FakeDBus(reply={"ok": True})
        tok = _mint_token("cookies.export", request_id="rep-1")
        first = bb.dispatch(
            {"op": "cookies.export", "domain": "x.com",
             "cookies": [], "intent_token": tok},
            ALLOWED)
        assert first["ok"] is True
        second = bb.dispatch(
            {"op": "cookies.export", "domain": "x.com",
             "cookies": [], "intent_token": dict(tok)},
            ALLOWED)
        assert second["error"] == "intent_token_replay"
        # Only one D-Bus call — the second was short-circuited.
        assert len(bb._dbus_client.calls) == 1


# =====================================================================
# 9e — MPRIS / downloads / notifications / screenlock
# =====================================================================

class TestDesktopIntegrations:
    @pytest.mark.parametrize("op,service,method,fields", [
        ("mpris.publish", "org.qdistro.Mpris", "Publish",
         {"title": "T", "artist": "A"}),
        ("downloads.notify", "org.qdistro.Downloads", "Notify",
         {"download_id": "d1", "filename": "f.iso", "state": "done"}),
        ("notifications.show", "org.qdistro.Notifications", "Show",
         {"title": "hi", "body": "world", "origin": "https://x"}),
        ("screenlock.inhibit", "org.qdistro.Compositor",
         "ScreenlockInhibit",
         {"reason": "fullscreen-video", "tab_id": 7,
          "url": "https://video"}),
        ("screenlock.release", "org.qdistro.Compositor",
         "ScreenlockRelease", {"tab_id": 7, "url": "https://video"}),
    ])
    def test_forwards_to_daemon(self, op, service, method, fields):
        bb._dbus_client = FakeDBus(reply={"ok": True})
        resp = bb.dispatch({"op": op, **fields}, ALLOWED)
        assert resp["ok"] is True
        call = bb._dbus_client.calls[0]
        assert call["service"] == service
        assert call["method"] == method
        body = json.loads(call["body"][0])
        for k, v in fields.items():
            assert body[k] == v
        assert body["extension_id"] == "qdistro@qdistro.local"

    def test_parent_not_allowed(self):
        bb._dbus_client = FakeDBus()
        for op in ("mpris.publish", "downloads.notify",
                   "notifications.show", "screenlock.inhibit",
                   "screenlock.release"):
            resp = bb.dispatch({"op": op}, DENIED)
            assert resp["error"] == "parent_not_allowed"
        assert bb._dbus_client.calls == []


# =====================================================================
# main loop: handshake starts heartbeat
# =====================================================================

class TestMainLoopHandshake:
    def test_handshake_does_not_break_phase8_shape(self, monkeypatch):
        """Bare ping-only clients still get a single-frame reply."""
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1,
                     "parent_exe": "/usr/lib64/firefox/firefox",
                     "parent_selinux": "", "allowed": True,
                     "extension_id": "ext"})
        stdin = io.BytesIO()
        body = json.dumps({"op": "qdistro.ping"}).encode()
        stdin.write(struct.pack("<I", len(body)) + body)
        stdin.seek(0)
        stdout = io.BytesIO()
        rc = bb.main(stdin=stdin, stdout=stdout,
                     enable_heartbeat=False, enable_dbus=False)
        assert rc == bb.EXIT_OK
        stdout.seek(0)
        msgs = []
        while True:
            m = bb.read_message(stdout)
            if m is None:
                break
            msgs.append(m)
        assert len(msgs) == 1
        assert msgs[0]["op"] == "qdistro.ping"

    def test_inbound_reply_does_not_echo(self, monkeypatch):
        """Extension replies to a daemon-initiated request must NOT
        be looped back as a fresh dispatch frame; deliver_reply
        consumes them silently."""
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1,
                     "parent_exe": "/usr/lib64/firefox/firefox",
                     "parent_selinux": "", "allowed": True,
                     "extension_id": "ext"})
        # Pre-register a pending request so deliver_reply matches.
        bb._pending["abc"] = bb._PendingRequest("tabs.list")
        stdin = io.BytesIO()
        body = json.dumps({"op": "tabs.list.reply",
                           "request_id": "abc",
                           "tabs": []}).encode()
        stdin.write(struct.pack("<I", len(body)) + body)
        stdin.seek(0)
        stdout = io.BytesIO()
        rc = bb.main(stdin=stdin, stdout=stdout,
                     enable_heartbeat=False, enable_dbus=False)
        assert rc == bb.EXIT_OK
        stdout.seek(0)
        # No outbound frame: the bridge swallowed the reply.
        assert stdout.read() == b""
