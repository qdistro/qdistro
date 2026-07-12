"""Cryptographic receipt boundary for the external pairing authority.

The pairing service owns owner approval, peer-certificate policy, and its
Ed25519 signing key.  The viewer-session launcher owns only verification: a
short-lived receipt must target this viewer and the one stream-session nonce
before its grants can reach the broker.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .origin_authority import StaticOriginAuthority


SCHEMA = "qdistro-mm-pairing-v1"
MAX_RECEIPT_LIFETIME_SECONDS = 300
_PAYLOAD_FIELDS = {
    "schema", "authority_key_id", "viewer_machine_id", "session_id",
    "issued_at", "expires_at", "origins",
}


class PairingReceiptError(ValueError):
    """A signed pairing receipt is malformed, stale, or unauthentic."""


def public_key_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def authority_key_id(key: Ed25519PublicKey) -> str:
    digest = hashlib.sha256(public_key_bytes(key)).hexdigest()
    return f"ed25519-sha256:{digest}"


def _canonical(payload: Mapping) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _encoded_signature(signature: bytes) -> str:
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def _decoded_signature(value: object) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise PairingReceiptError("signature must be unpadded base64url")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4),
                               altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PairingReceiptError("signature must be unpadded base64url") from exc
    if len(raw) != 64:
        raise PairingReceiptError("Ed25519 signature must be 64 bytes")
    return raw


def issue_pairing_receipt(*, origins: list[dict], viewer_machine_id: str,
                          session_id: str, issued_at: int, expires_at: int,
                          private_key: Ed25519PrivateKey) -> dict:
    """Create the receipt an already-authorized external service may hand off."""
    public_key = private_key.public_key()
    payload = {
        "schema": SCHEMA,
        "authority_key_id": authority_key_id(public_key),
        "viewer_machine_id": viewer_machine_id,
        "session_id": session_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "origins": deepcopy(origins),
    }
    _validate_payload(payload, viewer_machine_id=viewer_machine_id,
                      session_id=session_id, now=issued_at)
    return {
        "payload": payload,
        "signature": _encoded_signature(private_key.sign(_canonical(payload))),
    }


def _validate_payload(payload: object, *, viewer_machine_id: str,
                      session_id: str, now: int) -> list[dict]:
    if not isinstance(payload, Mapping) or set(payload) != _PAYLOAD_FIELDS:
        raise PairingReceiptError("pairing payload fields do not match schema")
    if payload["schema"] != SCHEMA:
        raise PairingReceiptError("unsupported pairing receipt schema")
    for name in ("authority_key_id", "viewer_machine_id", "session_id"):
        if not isinstance(payload[name], str) or not payload[name]:
            raise PairingReceiptError(f"{name} must be a non-empty string")
    if payload["viewer_machine_id"] != viewer_machine_id:
        raise PairingReceiptError("pairing receipt targets a different viewer")
    if payload["session_id"] != session_id:
        raise PairingReceiptError("pairing receipt targets a different session")
    issued_at = payload["issued_at"]
    expires_at = payload["expires_at"]
    if (not isinstance(issued_at, int) or isinstance(issued_at, bool)
            or not isinstance(expires_at, int) or isinstance(expires_at, bool)):
        raise PairingReceiptError("receipt timestamps must be integers")
    if issued_at > now:
        raise PairingReceiptError("pairing receipt is not valid yet")
    if expires_at <= now:
        raise PairingReceiptError("pairing receipt has expired")
    lifetime = expires_at - issued_at
    if lifetime <= 0 or lifetime > MAX_RECEIPT_LIFETIME_SECONDS:
        raise PairingReceiptError("pairing receipt lifetime is out of bounds")
    origins = payload["origins"]
    try:
        StaticOriginAuthority.from_config(origins)
    except ValueError as exc:
        raise PairingReceiptError(f"invalid origin grants: {exc}") from exc
    return deepcopy(origins)


@dataclass(frozen=True)
class PairingReceiptVerifier:
    public_key: Ed25519PublicKey
    viewer_machine_id: str
    now: Callable[[], float] = time.time

    @classmethod
    def from_public_bytes(cls, raw: bytes, *, viewer_machine_id: str,
                          now: Callable[[], float] = time.time,
                          ) -> "PairingReceiptVerifier":
        try:
            key = Ed25519PublicKey.from_public_bytes(raw)
        except ValueError as exc:
            raise PairingReceiptError("authority public key must be 32 bytes") from exc
        return cls(key, viewer_machine_id, now)

    def verify(self, receipt: object, *, session_id: str) -> dict:
        if not isinstance(receipt, Mapping) or set(receipt) != {
                "payload", "signature"}:
            raise PairingReceiptError("pairing receipt fields do not match schema")
        payload = receipt["payload"]
        if not isinstance(payload, Mapping):
            raise PairingReceiptError("pairing receipt payload must be an object")
        signature = _decoded_signature(receipt["signature"])
        try:
            self.public_key.verify(signature, _canonical(payload))
        except InvalidSignature as exc:
            raise PairingReceiptError("pairing receipt signature is invalid") from exc
        expected_key_id = authority_key_id(self.public_key)
        if payload.get("authority_key_id") != expected_key_id:
            raise PairingReceiptError("pairing receipt authority key id mismatch")
        origins = _validate_payload(
            payload, viewer_machine_id=self.viewer_machine_id,
            session_id=session_id, now=int(self.now()),
        )
        return {"origins": origins}
