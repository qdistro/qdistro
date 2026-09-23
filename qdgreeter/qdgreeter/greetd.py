"""greetd JSON IPC client.

greetd exposes a length-prefixed JSON protocol over a UNIX socket at
$GREETD_SOCK. The protocol is documented in `greetd-ipc(7)`:

  → {"type":"create_session","username":"admin"}
  ← {"type":"success"}                                   # no auth needed
  ← {"type":"auth_message","auth_message_type":"secret","auth_message":"Password:"}
  → {"type":"post_auth_message_response","response":"hunter2"}
  ← {"type":"success"}
  → {"type":"start_session","cmd":["qdwin-session.target"],"env":[]}
  ← {"type":"success"}
  ← {"type":"error","error_type":"error|auth_error","description":"..."}

Length prefix is a 4-byte native-endian unsigned int (greetd's choice,
confirmed against greetd 0.10 source `greetd-ipc/src/codec.rs`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
from typing import Any

log = logging.getLogger("qdgreeter.greetd")

_HEADER_FMT = "=I"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)

# Upper bound on a single greetd frame body, in bytes.
#
# Real greetd replies are tiny JSON objects: the largest is an
# auth_message carrying a prompt string, or a start_session frame whose
# `cmd`/`env` lists we ourselves control — all well under a kilobyte. A
# 1 MiB cap is therefore enormously generous for any legitimate frame
# while still bounding the worst case.
#
# The length prefix is an attacker- (or bug-) controlled uint32, so it
# can advertise up to 4 GiB. Without a cap, `readexactly(length)` would
# buffer that much before we ever see a byte of the (never-arriving)
# body: an unbounded allocation / memory-exhaustion DoS, or an
# indefinite hang. We fail closed — reject the advertised length BEFORE
# allocating or awaiting the body — rather than trust the peer.
MAX_FRAME_SIZE = 1 << 20  # 1 MiB

# Bound every individual IPC write/read await. A valid-size frame whose
# peer stalls mid-header or mid-body must fail closed instead of pinning
# the auth worker forever.
IPC_TIMEOUT_S = 5.0


def encode_frame(payload: dict[str, Any]) -> bytes:
    """Serialize a greetd JSON payload as a length-prefixed frame."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(_HEADER_FMT, len(body)) + body


def _check_frame_length(length: int) -> None:
    """Validate an advertised frame-body length, fail-closed.

    Rejects a length that is negative (defensive — the wire format is
    unsigned, but never trust the caller), zero (greetd always sends a
    non-empty JSON object), or larger than ``MAX_FRAME_SIZE``. Raising
    here — before any slice or ``readexactly`` — is what stops a hostile
    or buggy peer from driving an oversized allocation.
    """
    if length <= 0:
        raise ValueError(f"frame body length non-positive: {length}")
    if length > MAX_FRAME_SIZE:
        raise ValueError(
            f"frame body length {length} exceeds maximum {MAX_FRAME_SIZE}"
        )


def _validate_reply(reply: Any) -> dict[str, Any]:
    """Validate a decoded greetd reply and return it as a typed dict.

    greetd replies are always JSON objects carrying a string ``type``
    discriminator. Callers downstream do ``reply.get("type")`` and switch
    on the value, so a reply that is a JSON array / string / number /
    null — or an object with a non-string ``type`` — would otherwise blow
    up with an ``AttributeError`` deep in the auth flow. Fail closed here
    with a ``ValueError`` instead. Unknown but well-formed string ``type``
    values are accepted (forward-compat with newer greetd).
    """
    if not isinstance(reply, dict) or not isinstance(reply.get("type"), str):
        raise ValueError("greetd reply not a JSON object with string 'type'")
    return reply


def decode_frame(data: bytes) -> dict[str, Any]:
    """Inverse of encode_frame. Raises ValueError if the prefix lies."""
    if len(data) < _HEADER_SIZE:
        raise ValueError("frame shorter than header")
    (length,) = struct.unpack(_HEADER_FMT, data[:_HEADER_SIZE])
    # Bound-check the advertised length BEFORE slicing the body, so an
    # oversized prefix is rejected without materializing a giant slice.
    _check_frame_length(length)
    body = data[_HEADER_SIZE : _HEADER_SIZE + length]
    if len(body) != length:
        raise ValueError(f"frame body length mismatch: header={length} actual={len(body)}")
    return _validate_reply(json.loads(body))


class GreetdError(RuntimeError):
    """Raised when greetd returns an error reply.

    `error_type` distinguishes `auth_error` (wrong password — recoverable
    by cancel_session + retry) from `error` (protocol or backend failure).
    """

    def __init__(self, error_type: str, description: str) -> None:
        super().__init__(f"{error_type}: {description}")
        self.error_type = error_type
        self.description = description


class GreetdClient:
    def __init__(self, sock_path: str | None = None) -> None:
        self._path = sock_path or os.environ.get("GREETD_SOCK", "")
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def connect(self) -> None:
        if not self._path:
            raise RuntimeError("GREETD_SOCK not set — qdgreeter must run under greetd")
        if self._writer is not None:
            return
        self._reader, self._writer = await asyncio.open_unix_connection(self._path)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._reader = None
        self._writer = None

    async def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._writer is None or self._reader is None:
            raise RuntimeError("greetd client not connected; call connect() first")
        try:
            frame = encode_frame(payload)
            self._writer.write(frame)
            await asyncio.wait_for(self._writer.drain(), timeout=IPC_TIMEOUT_S)
            header = await asyncio.wait_for(
                self._reader.readexactly(_HEADER_SIZE),
                timeout=IPC_TIMEOUT_S,
            )
            (length,) = struct.unpack(_HEADER_FMT, header)
            # Reject an oversized / nonsense advertised length BEFORE awaiting
            # the body. A hostile or buggy greetd advertising a huge length
            # would otherwise make readexactly() buffer up to 4 GiB while
            # waiting on a body that never arrives — a memory-exhaustion DoS
            # or indefinite hang. Fail closed.
            _check_frame_length(length)
            body = await asyncio.wait_for(
                self._reader.readexactly(length),
                timeout=IPC_TIMEOUT_S,
            )
            reply = _validate_reply(json.loads(body))
        except TimeoutError:
            await self.close()
            raise
        # Never log the payload itself — `post_auth_message_response`
        # carries the plaintext password. Log the type for tracing,
        # nothing more.
        log.debug("greetd: sent=%s recv=%s", payload.get("type"), reply.get("type"))
        return reply

    async def create_session(self, username: str) -> dict[str, Any]:
        return await self._send({"type": "create_session", "username": username})

    async def post_auth(self, response: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": "post_auth_message_response"}
        # Per spec, response is optional (omitted for info / error
        # auth_message_types). null is the wire encoding; we map None → null.
        payload["response"] = response
        return await self._send(payload)

    async def start_session(
        self, cmd: list[str], env: list[str] | None = None
    ) -> dict[str, Any]:
        return await self._send(
            {"type": "start_session", "cmd": cmd, "env": env or []}
        )

    async def cancel_session(self) -> dict[str, Any]:
        return await self._send({"type": "cancel_session"})
