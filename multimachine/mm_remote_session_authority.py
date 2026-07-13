"""Signed authority grant for one R6 remote-adapter session.

The signed grant contains identities, policy, TLS certificate pins, and key
lifetime metadata, but never the symmetric key itself.  The external authority
delivers the same 32-byte key to each approved endpoint on a separate inherited
file descriptor.  A five-minute handoff window limits receipt replay while the
key may remain usable for one bounded dock session.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
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
from .remote_adapter import (
    MAX_SESSION_KEY_LIFETIME_SECONDS,
    RemoteAdapterError,
    RemoteSessionBinding,
)


SCHEMA = "qdistro-mm-adapter-grant-v1"
MAX_HANDOFF_LIFETIME_SECONDS = 300
ROLES = frozenset({"source", "viewer"})
_RECEIPT_FIELDS = frozenset({"payload", "signature"})
_PAYLOAD_FIELDS = frozenset({
    "schema", "authority_key_id", "source_machine", "viewer_machine",
    "trust_domain_id", "generation", "session_id", "stream_id", "key_id",
    "key_sha256", "issued_at", "handoff_expires_at", "key_expires_at",
    "allow_input",
    "source_tls_cert_sha256", "viewer_tls_cert_sha256",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class RemoteSessionGrantError(ValueError):
    """An adapter-session grant is malformed, stale, or unauthentic."""


def _canonical(payload: Mapping) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _encode_signature(signature: bytes) -> str:
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def _decode_signature(value: object) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise RemoteSessionGrantError(
            "adapter grant signature must be unpadded base64url")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4),
                               altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RemoteSessionGrantError(
            "adapter grant signature must be unpadded base64url") from exc
    if len(raw) != 64:
        raise RemoteSessionGrantError(
            "adapter grant Ed25519 signature must be 64 bytes")
    return raw


def _validate_payload(payload: object, *, session_id: str, stream_id: str,
                      now: int) -> dict:
    if not isinstance(payload, Mapping) or set(payload) != _PAYLOAD_FIELDS:
        raise RemoteSessionGrantError(
            "adapter grant payload fields do not match schema")
    if payload["schema"] != SCHEMA:
        raise RemoteSessionGrantError("unsupported adapter grant schema")
    if payload["session_id"] != session_id:
        raise RemoteSessionGrantError(
            "adapter grant targets a different session")
    if payload["stream_id"] != stream_id:
        raise RemoteSessionGrantError(
            "adapter grant targets a different stream")
    if not isinstance(payload["allow_input"], bool):
        raise RemoteSessionGrantError("allow_input must be boolean")
    if (not isinstance(payload["key_sha256"], str)
            or _SHA256.fullmatch(payload["key_sha256"]) is None):
        raise RemoteSessionGrantError(
            "adapter key digest must be lowercase SHA-256")
    if payload["source_machine"] == payload["viewer_machine"]:
        raise RemoteSessionGrantError(
            "adapter endpoints must be different machines")

    for name in ("issued_at", "handoff_expires_at", "key_expires_at"):
        value = payload[name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise RemoteSessionGrantError(
                "adapter grant timestamps must be integers")
    issued_at = payload["issued_at"]
    handoff_expires_at = payload["handoff_expires_at"]
    key_expires_at = payload["key_expires_at"]
    if issued_at > now:
        raise RemoteSessionGrantError("adapter grant is not valid yet")
    if handoff_expires_at <= now:
        raise RemoteSessionGrantError("adapter grant handoff has expired")
    handoff_lifetime = handoff_expires_at - issued_at
    if (handoff_lifetime <= 0
            or handoff_lifetime > MAX_HANDOFF_LIFETIME_SECONDS):
        raise RemoteSessionGrantError(
            "adapter grant handoff lifetime is out of bounds")
    key_lifetime = key_expires_at - issued_at
    if (key_lifetime <= 0
            or key_lifetime > MAX_SESSION_KEY_LIFETIME_SECONDS
            or key_expires_at < handoff_expires_at):
        raise RemoteSessionGrantError(
            "adapter session key lifetime is out of bounds")

    try:
        RemoteSessionBinding(
            source_machine=payload["source_machine"],
            viewer_machine=payload["viewer_machine"],
            trust_domain_id=payload["trust_domain_id"],
            generation=payload["generation"],
            session_id=payload["session_id"],
            stream_id=payload["stream_id"],
            epoch=1,
            key_id=payload["key_id"],
            key_expires_at=key_expires_at,
            source_tls_cert_sha256=payload["source_tls_cert_sha256"],
            viewer_tls_cert_sha256=payload["viewer_tls_cert_sha256"],
        ).validate()
    except RemoteAdapterError as exc:
        raise RemoteSessionGrantError(
            f"invalid adapter session binding: {exc}") from exc
    authority_id = payload["authority_key_id"]
    if not isinstance(authority_id, str) or not authority_id:
        raise RemoteSessionGrantError(
            "authority_key_id must be a non-empty string")
    return deepcopy(dict(payload))


def issue_remote_session_grant(*, source_machine: str, viewer_machine: str,
                               trust_domain_id: str, generation: int,
                               session_id: str, stream_id: str, key_id: str,
                               session_secret: bytes,
                               issued_at: int, handoff_expires_at: int,
                               key_expires_at: int, allow_input: bool,
                               source_tls_cert_sha256: str,
                               viewer_tls_cert_sha256: str,
                               private_key: Ed25519PrivateKey) -> dict:
    """Sign metadata after the external authority approves both endpoints."""
    if not isinstance(session_secret, bytes) or len(session_secret) != 32:
        raise RemoteSessionGrantError(
            "adapter session secret must be exactly 32 bytes")
    payload = {
        "schema": SCHEMA,
        "authority_key_id": authority_key_id(private_key.public_key()),
        "source_machine": source_machine,
        "viewer_machine": viewer_machine,
        "trust_domain_id": trust_domain_id,
        "generation": generation,
        "session_id": session_id,
        "stream_id": stream_id,
        "key_id": key_id,
        "key_sha256": hashlib.sha256(session_secret).hexdigest(),
        "issued_at": issued_at,
        "handoff_expires_at": handoff_expires_at,
        "key_expires_at": key_expires_at,
        "allow_input": allow_input,
        "source_tls_cert_sha256": source_tls_cert_sha256,
        "viewer_tls_cert_sha256": viewer_tls_cert_sha256,
    }
    _validate_payload(payload, session_id=session_id, stream_id=stream_id,
                      now=issued_at)
    return {
        "payload": payload,
        "signature": _encode_signature(
            private_key.sign(_canonical(payload))),
    }


@dataclass(frozen=True)
class RemoteSessionGrantVerifier:
    public_key: Ed25519PublicKey
    role: str
    local_machine_id: str
    now: Callable[[], float] = time.time

    @classmethod
    def from_public_bytes(cls, raw: bytes, *, role: str,
                          local_machine_id: str,
                          now: Callable[[], float] = time.time,
                          ) -> "RemoteSessionGrantVerifier":
        try:
            key = Ed25519PublicKey.from_public_bytes(raw)
        except ValueError as exc:
            raise RemoteSessionGrantError(
                "authority public key must be 32 bytes") from exc
        return cls(key, role, local_machine_id, now)

    def verify(self, receipt: object, *, session_id: str,
               stream_id: str) -> dict:
        if self.role not in ROLES:
            raise RemoteSessionGrantError("adapter role must be source or viewer")
        if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS:
            raise RemoteSessionGrantError(
                "adapter grant receipt fields do not match schema")
        payload = receipt["payload"]
        if not isinstance(payload, Mapping):
            raise RemoteSessionGrantError(
                "adapter grant payload must be an object")
        signature = _decode_signature(receipt["signature"])
        try:
            self.public_key.verify(signature, _canonical(payload))
        except InvalidSignature as exc:
            raise RemoteSessionGrantError(
                "adapter grant signature is invalid") from exc
        if payload.get("authority_key_id") != authority_key_id(self.public_key):
            raise RemoteSessionGrantError(
                "adapter grant authority key id mismatch")
        verified = _validate_payload(
            payload, session_id=session_id, stream_id=stream_id,
            now=int(self.now()))
        machine_field = f"{self.role}_machine"
        if verified[machine_field] != self.local_machine_id:
            raise RemoteSessionGrantError(
                "adapter grant targets a different local machine")
        return verified
