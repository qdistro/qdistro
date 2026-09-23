"""Wire-format tests for the greetd JSON-IPC client.

Pins the on-the-wire behavior against greetd 0.10 spec
(greetd-ipc(7) — https://man.sr.ht/~kennylevinsen/greetd/greetd-ipc.7.scd).

These tests run against an in-process fake greetd server so they
don't require a real $GREETD_SOCK or the full Qt stack.
"""

from __future__ import annotations

import asyncio
import json
import struct
import tempfile
from pathlib import Path

import pytest
import qdgreeter.greetd as greetd_mod
from qdgreeter.greetd import (
    MAX_FRAME_SIZE,
    GreetdClient,
    decode_frame,
    encode_frame,
)

# ---------------------------------------------------------------------------
# Frame codec (pure, no I/O).
# ---------------------------------------------------------------------------


def test_encode_frame_prefixes_native_uint32_length():
    frame = encode_frame({"type": "create_session", "username": "admin"})
    (length,) = struct.unpack("=I", frame[:4])
    assert length == len(frame) - 4
    assert json.loads(frame[4:]) == {
        "type": "create_session",
        "username": "admin",
    }


def test_encode_frame_compact_json_no_extra_whitespace():
    """greetd parses the JSON irrespective of whitespace, but compact
    output keeps frames small and avoids accidental dependence on
    Python's default ', ' separator. The test pins compact form so a
    future encoder change is loud."""
    frame = encode_frame({"a": 1, "b": 2})
    assert b'", "' not in frame
    assert b'": ' not in frame


def test_create_session_frame_shape():
    frame = encode_frame({"type": "create_session", "username": "admin"})
    payload = decode_frame(frame)
    assert payload == {"type": "create_session", "username": "admin"}


def test_post_auth_message_response_frame_carries_response_string():
    frame = encode_frame(
        {"type": "post_auth_message_response", "response": "hunter2"}
    )
    payload = decode_frame(frame)
    assert payload["type"] == "post_auth_message_response"
    assert payload["response"] == "hunter2"


def test_post_auth_message_response_null_response_encodes_as_json_null():
    frame = encode_frame(
        {"type": "post_auth_message_response", "response": None}
    )
    # Spec: response is optional; `null` is the wire encoding for "no
    # response" (used for info / error auth_message_types).
    assert b'"response":null' in frame
    payload = decode_frame(frame)
    assert payload["response"] is None


def test_start_session_frame_shape():
    cmd = ["systemctl", "--user", "start", "qdwin-session.target"]
    frame = encode_frame({"type": "start_session", "cmd": cmd, "env": []})
    payload = decode_frame(frame)
    assert payload == {"type": "start_session", "cmd": cmd, "env": []}


def test_start_session_with_env_vars():
    frame = encode_frame(
        {
            "type": "start_session",
            "cmd": ["qdwin-session.target"],
            "env": ["XDG_SESSION_TYPE=wayland", "QT_QPA_PLATFORM=wayland"],
        }
    )
    payload = decode_frame(frame)
    assert payload["env"] == [
        "XDG_SESSION_TYPE=wayland",
        "QT_QPA_PLATFORM=wayland",
    ]


def test_cancel_session_frame_shape():
    frame = encode_frame({"type": "cancel_session"})
    payload = decode_frame(frame)
    assert payload == {"type": "cancel_session"}


def test_decode_frame_rejects_truncated_body():
    good = encode_frame({"type": "success"})
    truncated = good[: -2]
    with pytest.raises(ValueError):
        decode_frame(truncated)


# ---------------------------------------------------------------------------
# Reply-shape validation (fail-closed on malformed JSON bodies).
#
# greetd replies are always JSON objects with a string `type` discriminator;
# callers downstream do `reply.get("type")`. A JSON array / null / number, or
# an object whose `type` isn't a string, must be rejected with ValueError here
# rather than blowing up later with AttributeError. Unknown but well-formed
# string types stay accepted (forward-compat with newer greetd).
# ---------------------------------------------------------------------------


def _frame_from_json(text: str) -> bytes:
    """Build a length-prefixed frame from an arbitrary JSON body (which
    encode_frame, being dict-only, can't produce)."""
    body = text.encode("utf-8")
    return struct.pack("=I", len(body)) + body


@pytest.mark.parametrize("body", ["[]", "null", '{"type": 1}'])
def test_decode_frame_rejects_non_object_or_non_string_type(body):
    """A JSON array, null, or object with non-string `type` must raise
    ValueError (not AttributeError) so the failure is contained in the
    codec instead of surfacing deep in the auth flow."""
    with pytest.raises(ValueError):
        decode_frame(_frame_from_json(body))


def test_decode_frame_accepts_unknown_string_type():
    """Forward-compat: a well-formed reply with an unknown but string
    `type` must decode fine — we never reject unknown string types."""
    frame = encode_frame({"type": "new_future_reply"})
    payload = decode_frame(frame)
    assert payload == {"type": "new_future_reply"}


# ---------------------------------------------------------------------------
# Frame-length upper bound (memory-exhaustion / malformed-frame hardening).
#
# The length prefix is an attacker-/bug-controlled uint32. Without a cap a
# hostile or buggy greetd advertising a huge length drives an unbounded
# allocation (decode_frame slice) or an unbounded readexactly() await (the
# async read loop). These tests pin the fail-closed behavior: the length is
# rejected BEFORE any body is materialized or awaited.
# ---------------------------------------------------------------------------


def test_max_frame_size_is_generous_but_bounded():
    """Sanity-pin the cap: big enough for any real greetd reply, small
    enough to bound the worst case. If someone bumps it to absurdity the
    DoS protection is gone."""
    assert MAX_FRAME_SIZE == 1 << 20  # 1 MiB


def test_decode_frame_at_max_length_is_accepted():
    """Boundary: a body of exactly MAX_FRAME_SIZE bytes must decode.

    We don't build a 1 MiB JSON document; instead we craft a header that
    advertises exactly MAX_FRAME_SIZE and supply a real, parseable JSON
    body padded to that length. This proves the cap is inclusive (rejecting
    at == MAX would be an off-by-one that breaks legitimate large frames).
    """
    payload = {"type": "auth_message", "auth_message": ""}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    # Pad the prompt string with spaces until the body is exactly MAX bytes.
    pad = MAX_FRAME_SIZE - len(body)
    assert pad >= 0
    payload["auth_message"] = " " * pad
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    assert len(body) == MAX_FRAME_SIZE
    frame = struct.pack("=I", MAX_FRAME_SIZE) + body
    decoded = decode_frame(frame)
    assert decoded["type"] == "auth_message"
    assert len(decoded["auth_message"]) == pad


def test_decode_frame_rejects_length_over_max_without_reading_body():
    """MAX+1 must be rejected, and rejected from the HEADER alone — we
    pass a 4-byte header with NO body, so a decoder that bound-checks only
    after slicing would instead raise the 'mismatch' error (or, worse,
    attempt the slice). Assert the rejection cites the maximum."""
    header_only = struct.pack("=I", MAX_FRAME_SIZE + 1)
    with pytest.raises(ValueError, match="maximum"):
        decode_frame(header_only)


def test_decode_frame_rejects_huge_uint32_length_without_allocating():
    """A near-uint32-max length (≈4 GiB) must be rejected outright. If the
    bound check were missing, the body slice would be attempted; here we
    feed only the header, so any pre-check failure proves we never tried to
    materialize ~4 GiB."""
    header_only = struct.pack("=I", 0xFFFFFFFF)
    with pytest.raises(ValueError, match="maximum"):
        decode_frame(header_only)


def test_decode_frame_rejects_zero_length():
    """greetd always sends a non-empty JSON object; a zero-length frame is
    nonsense and must be rejected rather than fed to json.loads("")."""
    with pytest.raises(ValueError):
        decode_frame(struct.pack("=I", 0))


def test_read_loop_rejects_oversized_header_before_reading_body():
    """The async read loop must reject an oversized advertised length BEFORE
    awaiting readexactly(length).

    We drive _send with a StreamReader fed ONLY a crafted header (no body
    bytes). If the loop bound-checks the length first, it raises promptly.
    If it instead called readexactly(length) it would block forever waiting
    on a body that never comes — so we wrap the call in a short timeout and
    assert it raises ValueError, NOT TimeoutError. A TimeoutError would mean
    the loop tried to read the body (the bug we are guarding against)."""

    async def go():
        reader = asyncio.StreamReader()
        # Advertise a body far larger than MAX, then provide NO body bytes
        # and DO NOT feed EOF — this mirrors the real DoS: a peer that sends
        # a huge length and then stalls. A loop that awaited the body here
        # would hang until the timeout fires (-> TimeoutError); the guard
        # must instead raise ValueError immediately off the header alone.
        reader.feed_data(struct.pack("=I", MAX_FRAME_SIZE + 1))

        class _NullWriter:
            def write(self, _data):
                pass

            async def drain(self):
                pass

        client = GreetdClient(sock_path="/unused")
        client._reader = reader
        client._writer = _NullWriter()

        # If the loop wrongly awaits the (missing) body, this times out.
        await asyncio.wait_for(
            client._send({"type": "create_session", "username": "admin"}),
            timeout=2.0,
        )

    with pytest.raises(ValueError, match="maximum"):
        asyncio.run(go())


def test_read_loop_rejects_malformed_reply_with_value_error():
    """_send must raise ValueError (not AttributeError) when greetd
    returns a well-framed but malformed reply — here a JSON array, which
    would otherwise reach `reply.get("type")` and crash.

    We feed a complete frame (header + body) so the read succeeds; the
    rejection must come from reply-shape validation, and it must happen
    BEFORE the debug log line that itself does reply.get("type")."""

    async def go():
        reader = asyncio.StreamReader()
        body = b"[]"
        reader.feed_data(struct.pack("=I", len(body)) + body)
        reader.feed_eof()

        class _NullWriter:
            def write(self, _data):
                pass

            async def drain(self):
                pass

        client = GreetdClient(sock_path="/unused")
        client._reader = reader
        client._writer = _NullWriter()
        await client._send({"type": "create_session", "username": "admin"})

    with pytest.raises(ValueError):
        asyncio.run(go())


def test_read_loop_times_out_when_header_never_arrives(monkeypatch):
    monkeypatch.setattr(greetd_mod, "IPC_TIMEOUT_S", 0.05)

    async def go():
        reader = asyncio.StreamReader()

        class _NullWriter:
            closed = False

            def write(self, _data):
                pass

            async def drain(self):
                pass

            def close(self):
                self.closed = True

            async def wait_closed(self):
                pass

        writer = _NullWriter()
        client = GreetdClient(sock_path="/unused")
        client._reader = reader
        client._writer = writer

        with pytest.raises(asyncio.TimeoutError):
            await client._send({"type": "create_session", "username": "admin"})
        assert writer.closed is True
        assert client.connected is False

    asyncio.run(go())


def test_read_loop_times_out_when_valid_length_body_stalls(monkeypatch):
    monkeypatch.setattr(greetd_mod, "IPC_TIMEOUT_S", 0.05)

    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("=I", 16))

        class _NullWriter:
            closed = False

            def write(self, _data):
                pass

            async def drain(self):
                pass

            def close(self):
                self.closed = True

            async def wait_closed(self):
                pass

        writer = _NullWriter()
        client = GreetdClient(sock_path="/unused")
        client._reader = reader
        client._writer = writer

        with pytest.raises(asyncio.TimeoutError):
            await client._send({"type": "create_session", "username": "admin"})
        assert writer.closed is True
        assert client.connected is False

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Connection / env-var contract.
# ---------------------------------------------------------------------------


def test_connect_raises_when_GREETD_SOCK_unset(monkeypatch):
    monkeypatch.delenv("GREETD_SOCK", raising=False)
    client = GreetdClient()
    with pytest.raises(RuntimeError, match="GREETD_SOCK"):
        asyncio.run(client.connect())


def test_explicit_sock_path_overrides_env(monkeypatch):
    monkeypatch.setenv("GREETD_SOCK", "/from/env.sock")
    c = GreetdClient(sock_path="/explicit/path.sock")
    assert c._path == "/explicit/path.sock"


# ---------------------------------------------------------------------------
# Round-trip against a fake greetd over a real UNIX socket.
# ---------------------------------------------------------------------------


class _FakeGreetd:
    """In-process greetd that records frames and replies on a script."""

    def __init__(self, replies: list[dict]) -> None:
        self._replies = list(replies)
        self.received: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self.sock_path: str = ""

    async def start(self, sock_path: str) -> None:
        self.sock_path = sock_path
        self._server = await asyncio.start_unix_server(self._handle, sock_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                header = await reader.readexactly(4)
                (length,) = struct.unpack("=I", header)
                body = await reader.readexactly(length)
                self.received.append(json.loads(body))
                if not self._replies:
                    return
                reply = self._replies.pop(0)
                writer.write(encode_frame(reply))
                await writer.drain()
        except asyncio.IncompleteReadError:
            return
        finally:
            writer.close()


def _run(coro):
    return asyncio.run(coro)


def test_round_trip_create_session_success():
    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            sock = str(Path(tmp) / "greetd.sock")
            fake = _FakeGreetd([{"type": "success"}])
            await fake.start(sock)
            try:
                c = GreetdClient(sock_path=sock)
                await c.connect()
                reply = await c.create_session("admin")
                await c.close()
                assert reply == {"type": "success"}
                assert fake.received == [
                    {"type": "create_session", "username": "admin"}
                ]
            finally:
                await fake.stop()

    _run(go())


@pytest.mark.cheat_aware(
    protects="start_session is only reached AFTER create_session + a "
    "successful post_auth exchange — auth gates session start",
    severity="critical",
    cheats=[
        "reorder/relax the received-frame sequence assertion",
        "stop asserting the password actually traveled in post_auth",
        "let start_session fire before the success reply",
    ],
    consequence="a session could be launched without the password ever being "
    "verified by greetd/PAM",
)
def test_round_trip_full_auth_flow_to_start_session():
    """create_session → auth_message(secret) → post_auth → success → start_session → success."""

    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            sock = str(Path(tmp) / "greetd.sock")
            fake = _FakeGreetd(
                [
                    {
                        "type": "auth_message",
                        "auth_message_type": "secret",
                        "auth_message": "Password:",
                    },
                    {"type": "success"},
                    {"type": "success"},
                ]
            )
            await fake.start(sock)
            try:
                c = GreetdClient(sock_path=sock)
                await c.connect()
                r1 = await c.create_session("admin")
                assert r1["type"] == "auth_message"
                r2 = await c.post_auth("hunter2")
                assert r2 == {"type": "success"}
                r3 = await c.start_session(["qdwin-session.target"])
                assert r3 == {"type": "success"}
                await c.close()
                assert [m["type"] for m in fake.received] == [
                    "create_session",
                    "post_auth_message_response",
                    "start_session",
                ]
                # Confirm password actually traveled in the post_auth frame.
                assert fake.received[1]["response"] == "hunter2"
                assert fake.received[2]["cmd"] == ["qdwin-session.target"]
                assert fake.received[2]["env"] == []
            finally:
                await fake.stop()

    _run(go())


def test_round_trip_cancel_session_after_auth_error():
    """auth_error reply is the canonical "wrong password" — driver
    cancels and the client can be reused for another attempt."""

    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            sock = str(Path(tmp) / "greetd.sock")
            fake = _FakeGreetd(
                [
                    {
                        "type": "auth_message",
                        "auth_message_type": "secret",
                        "auth_message": "Password:",
                    },
                    {
                        "type": "error",
                        "error_type": "auth_error",
                        "description": "incorrect password",
                    },
                    {"type": "success"},  # reply to cancel_session
                ]
            )
            await fake.start(sock)
            try:
                c = GreetdClient(sock_path=sock)
                await c.connect()
                await c.create_session("admin")
                err = await c.post_auth("wrong")
                assert err == {
                    "type": "error",
                    "error_type": "auth_error",
                    "description": "incorrect password",
                }
                cancel = await c.cancel_session()
                assert cancel == {"type": "success"}
                await c.close()
                assert fake.received[-1] == {"type": "cancel_session"}
            finally:
                await fake.stop()

    _run(go())


@pytest.mark.cheat_aware(
    protects="the password is never written to the greetd debug log — only "
    "the frame `type` may be logged",
    severity="critical",
    cheats=[
        "lower the caplog level so DEBUG records are not captured",
        "narrow the substring search away from the real secret",
        "skip the assertion when caplog is empty",
    ],
    consequence="passwords land in journald/log files in cleartext, readable "
    "by anyone with log access",
)
def test_password_is_not_serialized_into_log(monkeypatch, caplog):
    """Regression guard for §security review: greetd debug logs must
    never carry the payload — only its `type`. If a future contributor
    re-adds `log.debug("...send=%r", payload)` this test fails."""

    import logging as _logging

    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            sock = str(Path(tmp) / "greetd.sock")
            fake = _FakeGreetd([{"type": "success"}])
            await fake.start(sock)
            try:
                with caplog.at_level(_logging.DEBUG, logger="qdgreeter.greetd"):
                    c = GreetdClient(sock_path=sock)
                    await c.connect()
                    await c.post_auth("super-secret-password-42")
                    await c.close()
            finally:
                await fake.stop()

    _run(go())
    blob = " ".join(rec.getMessage() for rec in caplog.records)
    assert "super-secret-password-42" not in blob
