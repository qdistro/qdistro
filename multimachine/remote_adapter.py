"""Authenticated wire and lifecycle core for nested-over-network R6.

This is the transport-independent boundary between a future encoder/decoder
pair.  It deliberately does not forward Wayland objects, PipeWire node names,
file descriptors, or QDNI Unix paths.  Every message is instead authenticated
against one paired-machine session and one source-minted stream id.

The caller supplies an expiring 32-byte session secret through a sealed
launcher boundary.  The network endpoints MUST use TLS with authority-vouched
peer-certificate pins; the HMAC envelope supplies end-to-end identity, channel
separation, and replay protection inside that confidential carrier.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable


SCHEMA = "qdistro-mm-remote-v1"
SOURCE_TO_VIEWER = "source-to-viewer"
VIEWER_TO_SOURCE = "viewer-to-source"
DIRECTIONS = frozenset({SOURCE_TO_VIEWER, VIEWER_TO_SOURCE})
CHANNELS = frozenset({"control", "media", "input"})
MAX_WIRE_BYTES = 4 * 1024 * 1024
MAX_SESSION_KEY_LIFETIME_SECONDS = 12 * 60 * 60
CHANNEL_PAYLOAD_LIMITS = {
    "control": 64 * 1024,
    "input": 4 * 1024,
    "media": 3 * 1024 * 1024,
}
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_STREAM_ID = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_SHA256_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_WIRE_FIELDS = {
    "schema", "binding", "direction", "channel", "kind", "seq", "ack",
    "payload", "tag",
}
_UNSIGNED_FIELDS = _WIRE_FIELDS - {"tag"}


class RemoteAdapterError(ValueError):
    """An R6 remote-adapter message or transition failed closed."""


class AuthenticationError(RemoteAdapterError):
    pass


class ReplayError(RemoteAdapterError):
    pass


class FlowControlError(RemoteAdapterError):
    pass


class RemoteSessionKey:
    """An authority-issued adapter key with a bounded, revocable lifetime.

    A pairing receipt is only a short-lived handoff authorization.  This
    separate key remains valid for at most one dock session.  Reconnect epochs
    reuse it until expiry; a new dock generation or renewal receives a new key
    id and secret.  ``revoke`` both disables and forgets the local secret.
    """

    def __init__(self, *, key_id: str, secret: bytes, issued_at: int,
                 expires_at: int) -> None:
        if (not isinstance(key_id, str)
                or _IDENTITY.fullmatch(key_id) is None):
            raise RemoteAdapterError("invalid remote session key id")
        if not isinstance(secret, bytes) or len(secret) != 32:
            raise RemoteAdapterError("remote session key must be 32 bytes")
        for name, value in (("issued_at", issued_at),
                            ("expires_at", expires_at)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise RemoteAdapterError(f"{name} must be an integer")
        lifetime = expires_at - issued_at
        if (lifetime <= 0
                or lifetime > MAX_SESSION_KEY_LIFETIME_SECONDS):
            raise RemoteAdapterError(
                "remote session key lifetime is out of bounds")
        self.key_id = key_id
        self.issued_at = issued_at
        self.expires_at = expires_at
        self._secret = secret
        self._revoked = False

    @property
    def revoked(self) -> bool:
        return self._revoked

    def require_active(self, *, now: int) -> bytes:
        if not isinstance(now, int) or isinstance(now, bool):
            raise RemoteAdapterError("current time must be an integer")
        if self._revoked:
            raise AuthenticationError("remote session key has been revoked")
        if now < self.issued_at:
            raise AuthenticationError("remote session key is not valid yet")
        if now >= self.expires_at:
            raise AuthenticationError("remote session key has expired")
        return self._secret

    def revoke(self) -> None:
        self._revoked = True
        self._secret = b""


@dataclass(frozen=True)
class RemoteSessionBinding:
    source_machine: str
    viewer_machine: str
    trust_domain_id: str
    generation: int
    session_id: str
    stream_id: str
    epoch: int
    key_id: str
    key_expires_at: int
    source_tls_cert_sha256: str
    viewer_tls_cert_sha256: str

    def validate(self) -> None:
        for name in ("source_machine", "viewer_machine", "trust_domain_id",
                     "session_id", "key_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
                raise RemoteAdapterError(f"invalid {name} {value!r}")
        if (not isinstance(self.stream_id, str)
                or _STREAM_ID.fullmatch(self.stream_id) is None):
            raise RemoteAdapterError(f"invalid stream_id {self.stream_id!r}")
        for name in ("source_tls_cert_sha256", "viewer_tls_cert_sha256"):
            value = getattr(self, name)
            if (not isinstance(value, str)
                    or _SHA256_FINGERPRINT.fullmatch(value) is None):
                raise RemoteAdapterError(f"invalid {name}")
        for name in ("generation", "epoch", "key_expires_at"):
            value = getattr(self, name)
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value <= 0):
                raise RemoteAdapterError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class AuthenticatedMessage:
    direction: str
    channel: str
    kind: str
    seq: int
    ack: int
    payload: bytes


def verify_tls_peer_certificate(certificate_der: bytes,
                                expected_sha256: str) -> None:
    """Fail unless a completed TLS handshake returned the authority pin."""
    if (not isinstance(certificate_der, bytes) or not certificate_der
            or len(certificate_der) > 64 * 1024):
        raise RemoteAdapterError("TLS peer certificate DER is invalid")
    if (not isinstance(expected_sha256, str)
            or _SHA256_FINGERPRINT.fullmatch(expected_sha256) is None):
        raise RemoteAdapterError("TLS certificate pin must be lowercase SHA-256")
    actual = hashlib.sha256(certificate_der).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        raise AuthenticationError("TLS peer certificate pin mismatch")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: object, *, name: str) -> bytes:
    if not isinstance(value, str) or "=" in value:
        raise RemoteAdapterError(f"{name} must be unpadded base64url")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4),
                                altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RemoteAdapterError(
            f"{name} must be unpadded base64url") from exc


class AuthenticatedEnvelopeCodec:
    """Seal/open strict JSON envelopes using channel-separated HMAC keys."""

    def __init__(self, binding: RemoteSessionBinding,
                 session_key: RemoteSessionKey, *,
                 now: Callable[[], float] = time.time):
        binding.validate()
        if not isinstance(session_key, RemoteSessionKey):
            raise RemoteAdapterError(
                "session_key must be a RemoteSessionKey")
        if (binding.key_id != session_key.key_id
                or binding.key_expires_at != session_key.expires_at):
            raise AuthenticationError(
                "remote binding does not match session key grant")
        if not callable(now):
            raise RemoteAdapterError("now must be callable")
        self.binding = binding
        self.session_key = session_key
        self._now = now
        self._binding_doc = asdict(binding)

    def _active_key(self) -> bytes:
        return self.session_key.require_active(now=int(self._now()))

    def _key(self, direction: str, channel: str) -> bytes:
        context = _canonical({
            "schema": SCHEMA,
            "binding": self._binding_doc,
            "direction": direction,
            "channel": channel,
        })
        return hmac.new(self._active_key(), b"envelope-key\0" + context,
                        hashlib.sha256).digest()

    @staticmethod
    def _validate_header(*, direction: object, channel: object, kind: object,
                         seq: object, ack: object) -> None:
        if direction not in DIRECTIONS:
            raise RemoteAdapterError("invalid remote message direction")
        if channel not in CHANNELS:
            raise RemoteAdapterError("invalid remote message channel")
        if (not isinstance(kind, str) or not kind
                or _IDENTITY.fullmatch(kind) is None):
            raise RemoteAdapterError("invalid remote message kind")
        for name, value in (("seq", seq), ("ack", ack)):
            minimum = 1 if name == "seq" else 0
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value < minimum):
                raise RemoteAdapterError(
                    f"{name} must be an integer >= {minimum}")

    def seal(self, *, direction: str, channel: str, kind: str, seq: int,
             payload: bytes, ack: int = 0) -> bytes:
        self._validate_header(direction=direction, channel=channel, kind=kind,
                              seq=seq, ack=ack)
        if not isinstance(payload, bytes):
            raise RemoteAdapterError("payload must be bytes")
        if len(payload) > CHANNEL_PAYLOAD_LIMITS[channel]:
            raise RemoteAdapterError(f"{channel} payload exceeds limit")
        unsigned = {
            "schema": SCHEMA,
            "binding": self._binding_doc,
            "direction": direction,
            "channel": channel,
            "kind": kind,
            "seq": seq,
            "ack": ack,
            "payload": _b64encode(payload),
        }
        tag = hmac.new(self._key(direction, channel), _canonical(unsigned),
                       hashlib.sha256).digest()
        return _canonical({**unsigned, "tag": _b64encode(tag)})

    def open(self, wire: bytes, *, expected_direction: str) -> AuthenticatedMessage:
        if expected_direction not in DIRECTIONS:
            raise RemoteAdapterError("invalid expected direction")
        if not isinstance(wire, bytes) or len(wire) > MAX_WIRE_BYTES:
            raise RemoteAdapterError("remote envelope exceeds wire limit")
        try:
            doc = json.loads(wire)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteAdapterError("remote envelope is not JSON") from exc
        if not isinstance(doc, dict) or set(doc) != _WIRE_FIELDS:
            raise RemoteAdapterError("remote envelope fields do not match schema")
        if doc["schema"] != SCHEMA:
            raise RemoteAdapterError("unsupported remote envelope schema")
        binding_doc = doc["binding"]
        if (not isinstance(binding_doc, dict)
                or set(binding_doc) != set(self._binding_doc)):
            raise RemoteAdapterError(
                "remote envelope binding fields do not match schema")
        try:
            claimed_binding = RemoteSessionBinding(**binding_doc)
        except TypeError as exc:
            raise RemoteAdapterError(
                "remote envelope binding fields do not match schema") from exc
        claimed_binding.validate()
        if claimed_binding != self.binding:
            raise AuthenticationError("remote envelope binding mismatch")
        self._validate_header(direction=doc["direction"], channel=doc["channel"],
                              kind=doc["kind"], seq=doc["seq"], ack=doc["ack"])
        if doc["direction"] != expected_direction:
            raise AuthenticationError("remote envelope direction mismatch")
        tag = _b64decode(doc["tag"], name="tag")
        if len(tag) != hashlib.sha256().digest_size:
            raise AuthenticationError("remote envelope tag length is invalid")
        unsigned = {key: doc[key] for key in _UNSIGNED_FIELDS}
        expected = hmac.new(
            self._key(doc["direction"], doc["channel"]),
            _canonical(unsigned), hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise AuthenticationError("remote envelope authentication failed")
        payload = _b64decode(doc["payload"], name="payload")
        if len(payload) > CHANNEL_PAYLOAD_LIMITS[doc["channel"]]:
            raise RemoteAdapterError(f"{doc['channel']} payload exceeds limit")
        return AuthenticatedMessage(
            direction=doc["direction"], channel=doc["channel"],
            kind=doc["kind"], seq=doc["seq"], ack=doc["ack"],
            payload=payload)


class AuthenticatedReceiver:
    """Authenticate, capability-check, then enforce strict per-channel order."""

    def __init__(self, codec: AuthenticatedEnvelopeCodec, *, direction: str,
                 allow_input: bool):
        if direction not in DIRECTIONS:
            raise RemoteAdapterError("invalid receiver direction")
        if not isinstance(allow_input, bool):
            raise RemoteAdapterError("allow_input must be boolean")
        self.codec = codec
        self.direction = direction
        self.allow_input = allow_input
        self._last_seq = {channel: 0 for channel in CHANNELS}

    def receive(self, wire: bytes) -> AuthenticatedMessage:
        message = self.codec.open(wire, expected_direction=self.direction)
        if message.channel == "input" and not self.allow_input:
            raise AuthenticationError("paired session lacks input capability")
        expected = self._last_seq[message.channel] + 1
        if message.seq != expected:
            raise ReplayError(
                f"{message.channel} sequence {message.seq} != expected {expected}")
        self._last_seq[message.channel] = message.seq
        return message


class MediaFlowController:
    """Bound sender memory and require cumulative decoder acknowledgements."""

    def __init__(self, *, max_inflight: int = 2,
                 max_frame_bytes: int = CHANNEL_PAYLOAD_LIMITS["media"]):
        if (not isinstance(max_inflight, int) or isinstance(max_inflight, bool)
                or max_inflight <= 0 or max_inflight > 16):
            raise FlowControlError("max_inflight must be in 1..16")
        if (not isinstance(max_frame_bytes, int)
                or isinstance(max_frame_bytes, bool)
                or max_frame_bytes <= 0
                or max_frame_bytes > CHANNEL_PAYLOAD_LIMITS["media"]):
            raise FlowControlError("max_frame_bytes is out of bounds")
        self.max_inflight = max_inflight
        self.max_frame_bytes = max_frame_bytes
        self.next_seq = 1
        self.acked_seq = 0
        self._inflight: dict[int, int] = {}

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    def offer(self, frame: bytes) -> int:
        if not isinstance(frame, bytes) or not frame:
            raise FlowControlError("frame must be non-empty bytes")
        if len(frame) > self.max_frame_bytes:
            raise FlowControlError("frame exceeds negotiated size")
        if self.inflight >= self.max_inflight:
            raise FlowControlError("media window is full")
        seq = self.next_seq
        self.next_seq += 1
        self._inflight[seq] = len(frame)
        return seq

    def acknowledge(self, seq: int) -> None:
        if (not isinstance(seq, int) or isinstance(seq, bool)
                or seq < self.acked_seq or seq >= self.next_seq):
            raise FlowControlError("media acknowledgement is stale or unsent")
        self.acked_seq = seq
        self._inflight = {n: size for n, size in self._inflight.items()
                          if n > seq}


class ProxyAttachmentState(str, Enum):
    ABSENT = "absent"
    ATTACHED = "attached"
    DETACHED = "detached"
    CLOSED = "closed"


@dataclass(frozen=True, order=True)
class InputRelease:
    """A synthetic release the endpoint must inject before seat handoff."""

    kind: str
    code: int


class RemoteInputState:
    """Confine held input state to one attached connection epoch."""

    def __init__(self) -> None:
        self.epoch = 0
        self.attached = False
        self._held_keys: set[int] = set()
        self._held_buttons: set[int] = set()

    @property
    def held_keys(self) -> frozenset[int]:
        return frozenset(self._held_keys)

    @property
    def held_buttons(self) -> frozenset[int]:
        return frozenset(self._held_buttons)

    def attach(self, *, epoch: int) -> tuple[InputRelease, ...]:
        if (not isinstance(epoch, int) or isinstance(epoch, bool)
                or epoch <= self.epoch):
            raise ReplayError("input epoch is stale")
        releases = self.detach()
        self.epoch = epoch
        self.attached = True
        return releases

    def record(self, *, epoch: int, kind: str, code: int,
               pressed: bool) -> None:
        if not isinstance(epoch, int) or isinstance(epoch, bool):
            raise RemoteAdapterError("input epoch must be an integer")
        if epoch != self.epoch:
            if epoch < self.epoch:
                raise ReplayError("input epoch is stale")
            raise AuthenticationError("input epoch is not attached")
        if not self.attached:
            raise AuthenticationError("input is disabled while detached")
        if kind not in {"key", "button"}:
            raise RemoteAdapterError("input kind must be key or button")
        if (not isinstance(code, int) or isinstance(code, bool) or code <= 0):
            raise RemoteAdapterError("input code must be a positive integer")
        if not isinstance(pressed, bool):
            raise RemoteAdapterError("pressed must be boolean")
        held = self._held_keys if kind == "key" else self._held_buttons
        if pressed:
            held.add(code)
        else:
            held.discard(code)

    def detach(self) -> tuple[InputRelease, ...]:
        releases = tuple(
            [InputRelease("button", code)
             for code in sorted(self._held_buttons)]
            + [InputRelease("key", code)
               for code in sorted(self._held_keys)]
        )
        self._held_buttons.clear()
        self._held_keys.clear()
        self.attached = False
        return releases


class RemoteProxyLifecycle:
    """Keep source lifetime distinct from attachment/transport lifetime."""

    def __init__(self) -> None:
        self.state = ProxyAttachmentState.ABSENT
        self.source_alive = False
        self.source_revision = 0
        self.epoch = 0
        self.input = RemoteInputState()

    def announce(self, *, epoch: int,
                 source_revision: int) -> tuple[InputRelease, ...]:
        self._advance(epoch=epoch, source_revision=source_revision)
        releases = self.input.attach(epoch=epoch)
        self.source_alive = True
        self.state = ProxyAttachmentState.ATTACHED
        return releases

    def transport_lost(self) -> tuple[InputRelease, ...]:
        releases = self.input.detach()
        if self.state is ProxyAttachmentState.ATTACHED:
            self.state = ProxyAttachmentState.DETACHED
        # Deliberately retain source_alive: link loss is not app death.
        return releases

    def reconcile(self, *, epoch: int, source_revision: int,
                  source_alive: bool) -> tuple[InputRelease, ...]:
        if not isinstance(source_alive, bool):
            raise RemoteAdapterError("source_alive must be boolean")
        self._advance(epoch=epoch, source_revision=source_revision)
        releases = (self.input.attach(epoch=epoch) if source_alive
                    else self.input.detach())
        self.source_alive = source_alive
        self.state = (ProxyAttachmentState.ATTACHED if source_alive
                      else ProxyAttachmentState.CLOSED)
        return releases

    def source_closed(self, *,
                      source_revision: int) -> tuple[InputRelease, ...]:
        if (not isinstance(source_revision, int)
                or isinstance(source_revision, bool)
                or source_revision <= self.source_revision):
            raise ReplayError("source close revision is stale")
        self.source_revision = source_revision
        releases = self.input.detach()
        self.source_alive = False
        self.state = ProxyAttachmentState.CLOSED
        return releases

    def record_input(self, *, epoch: int, kind: str, code: int,
                     pressed: bool) -> None:
        self.input.record(epoch=epoch, kind=kind, code=code, pressed=pressed)

    def _advance(self, *, epoch: int, source_revision: int) -> None:
        if (not isinstance(epoch, int) or isinstance(epoch, bool)
                or epoch <= self.epoch):
            raise ReplayError("connection epoch is stale")
        if (not isinstance(source_revision, int)
                or isinstance(source_revision, bool)
                or source_revision < self.source_revision):
            raise ReplayError("source revision is stale")
        self.epoch = epoch
        self.source_revision = source_revision
