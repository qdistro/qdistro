"""External-authority grant for one leased RDP display-output slot.

The receipt authorizes display capture and console-equivalent RDP input.  It
contains no carrier secret: the authority hands the same random 32-byte secret
to the two endpoints on separate sealed descriptors, and the signed digest
prevents substitution.  RDP is exposed only behind the separately authenticated
carrier named by the endpoint certificate pins; a general Weston listener is
test apparatus, not an authorized product boundary.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .mm_pairing_authority import authority_key_id


SCHEMA = "qdistro-mm-display-grant-v1"
MAX_HANDOFF_LIFETIME_SECONDS = 300
MAX_DISPLAY_LEASE_SECONDS = 12 * 60 * 60
MIN_HEARTBEAT_MILLISECONDS = 250
MAX_HEARTBEAT_MILLISECONDS = 10_000
ROLES = frozenset({"primary", "peer"})
_RECEIPT_FIELDS = frozenset({"payload", "signature"})
_PAYLOAD_FIELDS = frozenset({
    "schema", "authority_key_id", "primary_machine", "peer_machine",
    "trust_domain_id", "generation", "session_id", "slot_name",
    "logical_x", "logical_y", "width", "height", "scale",
    "allow_input", "carrier_secret_sha256",
    "primary_tls_cert_sha256", "peer_tls_cert_sha256",
    "issued_at", "handoff_expires_at", "lease_expires_at",
    "heartbeat_ms",
})
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SLOT = re.compile(r"rdp-[0-9]{1,3}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class DisplayGrantError(ValueError):
    """A display grant is malformed, stale, unauthentic, or mis-targeted."""


def _canonical(payload: Mapping) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _encode_signature(signature: bytes) -> str:
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def _decode_signature(value: object) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise DisplayGrantError(
            "display grant signature must be unpadded base64url")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DisplayGrantError(
            "display grant signature must be unpadded base64url") from exc
    if len(raw) != 64:
        raise DisplayGrantError(
            "display grant Ed25519 signature must be 64 bytes")
    return raw


def _positive_int(payload: Mapping, name: str) -> int:
    value = payload[name]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DisplayGrantError(f"display grant {name} must be positive")
    return value


def _validate_payload(payload: object, *, session_id: str,
                      now: int) -> dict:
    if not isinstance(payload, Mapping) or set(payload) != _PAYLOAD_FIELDS:
        raise DisplayGrantError(
            "display grant payload fields do not match schema")
    if payload["schema"] != SCHEMA:
        raise DisplayGrantError("unsupported display grant schema")
    if payload["session_id"] != session_id:
        raise DisplayGrantError("display grant targets a different session")
    for name in (
        "authority_key_id", "primary_machine", "peer_machine",
        "trust_domain_id", "session_id",
    ):
        value = payload[name]
        if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
            raise DisplayGrantError(f"display grant {name} is invalid")
    if payload["primary_machine"] == payload["peer_machine"]:
        raise DisplayGrantError("display endpoints must be different machines")
    if (not isinstance(payload["slot_name"], str)
            or _SLOT.fullmatch(payload["slot_name"]) is None):
        raise DisplayGrantError("display slot name is invalid")

    _positive_int(payload, "generation")
    width = _positive_int(payload, "width")
    height = _positive_int(payload, "height")
    scale = _positive_int(payload, "scale")
    heartbeat_ms = _positive_int(payload, "heartbeat_ms")
    if width > 16_384 or height > 16_384 or width * height > 67_108_864:
        raise DisplayGrantError("display mode is out of bounds")
    if scale > 4:
        raise DisplayGrantError("display scale is out of bounds")
    for name in ("logical_x", "logical_y"):
        value = payload[name]
        if (not isinstance(value, int) or isinstance(value, bool)
                or abs(value) > 65_535):
            raise DisplayGrantError(
                f"display grant {name} is out of bounds")
    if not isinstance(payload["allow_input"], bool):
        raise DisplayGrantError("display allow_input must be boolean")
    if not MIN_HEARTBEAT_MILLISECONDS <= heartbeat_ms <= MAX_HEARTBEAT_MILLISECONDS:
        raise DisplayGrantError("display heartbeat interval is out of bounds")
    for name in (
        "carrier_secret_sha256", "primary_tls_cert_sha256",
        "peer_tls_cert_sha256",
    ):
        value = payload[name]
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise DisplayGrantError(f"display grant {name} must be SHA-256")

    for name in ("issued_at", "handoff_expires_at", "lease_expires_at"):
        value = payload[name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise DisplayGrantError(
                "display grant timestamps must be integers")
    issued_at = payload["issued_at"]
    handoff_expires_at = payload["handoff_expires_at"]
    lease_expires_at = payload["lease_expires_at"]
    if issued_at > now:
        raise DisplayGrantError("display grant is not valid yet")
    if handoff_expires_at <= now:
        raise DisplayGrantError("display grant handoff has expired")
    handoff_lifetime = handoff_expires_at - issued_at
    if (handoff_lifetime <= 0
            or handoff_lifetime > MAX_HANDOFF_LIFETIME_SECONDS):
        raise DisplayGrantError(
            "display grant handoff lifetime is out of bounds")
    lease_lifetime = lease_expires_at - issued_at
    if (lease_lifetime <= 0 or lease_lifetime > MAX_DISPLAY_LEASE_SECONDS
            or lease_expires_at < handoff_expires_at):
        raise DisplayGrantError("display lease lifetime is out of bounds")
    return deepcopy(dict(payload))


def issue_display_grant(*, primary_machine: str, peer_machine: str,
                        trust_domain_id: str, generation: int,
                        session_id: str, slot_name: str,
                        logical_x: int, logical_y: int,
                        width: int, height: int, scale: int,
                        allow_input: bool, carrier_secret: bytes,
                        primary_tls_cert_sha256: str,
                        peer_tls_cert_sha256: str,
                        issued_at: int, handoff_expires_at: int,
                        lease_expires_at: int, heartbeat_ms: int,
                        private_key: Ed25519PrivateKey) -> dict:
    if not isinstance(carrier_secret, bytes) or len(carrier_secret) != 32:
        raise DisplayGrantError(
            "display carrier secret must be exactly 32 bytes")
    payload = {
        "schema": SCHEMA,
        "authority_key_id": authority_key_id(private_key.public_key()),
        "primary_machine": primary_machine,
        "peer_machine": peer_machine,
        "trust_domain_id": trust_domain_id,
        "generation": generation,
        "session_id": session_id,
        "slot_name": slot_name,
        "logical_x": logical_x,
        "logical_y": logical_y,
        "width": width,
        "height": height,
        "scale": scale,
        "allow_input": allow_input,
        "carrier_secret_sha256": hashlib.sha256(carrier_secret).hexdigest(),
        "primary_tls_cert_sha256": primary_tls_cert_sha256,
        "peer_tls_cert_sha256": peer_tls_cert_sha256,
        "issued_at": issued_at,
        "handoff_expires_at": handoff_expires_at,
        "lease_expires_at": lease_expires_at,
        "heartbeat_ms": heartbeat_ms,
    }
    _validate_payload(payload, session_id=session_id, now=issued_at)
    return {
        "payload": payload,
        "signature": _encode_signature(
            private_key.sign(_canonical(payload))),
    }


@dataclass(frozen=True)
class DisplayGrantVerifier:
    public_key: Ed25519PublicKey
    role: str
    local_machine_id: str
    now: Callable[[], float] = time.time

    @classmethod
    def from_public_bytes(cls, raw: bytes, *, role: str,
                          local_machine_id: str,
                          now: Callable[[], float] = time.time,
                          ) -> "DisplayGrantVerifier":
        try:
            key = Ed25519PublicKey.from_public_bytes(raw)
        except ValueError as exc:
            raise DisplayGrantError(
                "authority public key must be 32 bytes") from exc
        return cls(key, role, local_machine_id, now)

    def verify(self, receipt: object, *, session_id: str,
               carrier_secret: bytes) -> dict:
        if self.role not in ROLES:
            raise DisplayGrantError(
                "display role must be primary or peer")
        if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS:
            raise DisplayGrantError(
                "display grant receipt fields do not match schema")
        payload = receipt["payload"]
        if not isinstance(payload, Mapping):
            raise DisplayGrantError("display grant payload must be an object")
        try:
            self.public_key.verify(
                _decode_signature(receipt["signature"]), _canonical(payload))
        except InvalidSignature as exc:
            raise DisplayGrantError(
                "display grant signature is invalid") from exc
        if payload.get("authority_key_id") != authority_key_id(self.public_key):
            raise DisplayGrantError(
                "display grant authority key id mismatch")
        verified = _validate_payload(
            payload, session_id=session_id, now=int(self.now()))
        machine_field = f"{self.role}_machine"
        if verified[machine_field] != self.local_machine_id:
            raise DisplayGrantError(
                "display grant targets a different local machine")
        if not isinstance(carrier_secret, bytes) or len(carrier_secret) != 32:
            raise DisplayGrantError(
                "display carrier secret must be exactly 32 bytes")
        if not hmac.compare_digest(
                hashlib.sha256(carrier_secret).hexdigest(),
                verified["carrier_secret_sha256"]):
            raise DisplayGrantError("display carrier secret digest mismatch")
        return verified
