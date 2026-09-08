"""External authority contract for one R6 adapter session."""
from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import multimachine.mm_remote_session_authority as authority
from multimachine.mm_pairing_authority import public_key_bytes
from multimachine.mm_remote_session_authority import (
    RemoteSessionGrantError,
    RemoteSessionGrantVerifier,
    issue_remote_session_grant,
)


NOW = 1_800_000_000
SESSION = "viewer-session-7f5c"
STREAM = "stream_0123456789abcdef"
SECRET = bytes(range(32))
SOURCE_PIN = hashlib.sha256(b"source cert").hexdigest()
VIEWER_PIN = hashlib.sha256(b"viewer cert").hexdigest()


def _grant(key: Ed25519PrivateKey, **changes) -> dict:
    values = {
        "source_machine": "vm-source",
        "viewer_machine": "vm-viewer",
        "trust_domain_id": "owner-machines",
        "generation": 51,
        "session_id": SESSION,
        "stream_id": STREAM,
        "key_id": "adapter-key-7f5c",
        "session_secret": SECRET,
        "issued_at": NOW,
        "handoff_expires_at": NOW + 120,
        "key_expires_at": NOW + 8 * 60 * 60,
        "allow_input": True,
        "source_tls_cert_sha256": SOURCE_PIN,
        "viewer_tls_cert_sha256": VIEWER_PIN,
        "private_key": key,
    }
    values.update(changes)
    return issue_remote_session_grant(**values)


def _verifier(key: Ed25519PrivateKey, *, role="viewer",
              machine="vm-viewer", now=NOW) -> RemoteSessionGrantVerifier:
    return RemoteSessionGrantVerifier.from_public_bytes(
        public_key_bytes(key.public_key()), role=role,
        local_machine_id=machine, now=lambda: now)


@pytest.mark.parametrize(("role", "machine"), [
    ("source", "vm-source"),
    ("viewer", "vm-viewer"),
])
def test_same_signed_grant_authorizes_exactly_the_two_named_endpoints(
        role, machine) -> None:
    key = Ed25519PrivateKey.generate()
    payload = _verifier(key, role=role, machine=machine).verify(
        _grant(key), session_id=SESSION, stream_id=STREAM)
    assert payload["key_sha256"] == hashlib.sha256(SECRET).hexdigest()
    assert payload["allow_input"] is True


def test_tamper_wrong_signer_and_unknown_signed_fields_fail() -> None:
    key = Ed25519PrivateKey.generate()
    receipt = deepcopy(_grant(key))
    receipt["payload"]["allow_input"] = False
    with pytest.raises(RemoteSessionGrantError, match="signature is invalid"):
        _verifier(key).verify(receipt, session_id=SESSION, stream_id=STREAM)

    other = Ed25519PrivateKey.generate()
    with pytest.raises(RemoteSessionGrantError, match="signature is invalid"):
        _verifier(other).verify(
            _grant(key), session_id=SESSION, stream_id=STREAM)

    receipt = deepcopy(_grant(key))
    receipt["payload"]["display_name"] = "smuggled"
    receipt["signature"] = authority._encode_signature(
        key.sign(authority._canonical(receipt["payload"])))
    with pytest.raises(RemoteSessionGrantError, match="payload fields"):
        _verifier(key).verify(receipt, session_id=SESSION, stream_id=STREAM)


@pytest.mark.parametrize(("role", "machine", "session", "stream", "message"), [
    ("viewer", "other-viewer", SESSION, STREAM, "different local machine"),
    ("source", "vm-viewer", SESSION, STREAM, "different local machine"),
    ("viewer", "vm-viewer", "other-session", STREAM, "different session"),
    ("viewer", "vm-viewer", SESSION, "other_0123456789abcdef",
     "different stream"),
])
def test_grant_cannot_cross_role_machine_session_or_stream(
        role, machine, session, stream, message) -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(RemoteSessionGrantError, match=message):
        _verifier(key, role=role, machine=machine).verify(
            _grant(key), session_id=session, stream_id=stream)


@pytest.mark.parametrize(("changes", "now", "message"), [
    ({"issued_at": NOW + 1}, NOW, "not valid yet"),
    ({}, NOW + 120, "handoff has expired"),
    ({"handoff_expires_at": NOW + 301}, NOW, "handoff lifetime"),
    ({"key_expires_at": NOW + 12 * 60 * 60 + 1}, NOW, "key lifetime"),
    ({"key_expires_at": NOW + 60}, NOW, "key lifetime"),
])
def test_handoff_and_key_lifetimes_fail_closed(changes, now, message) -> None:
    key = Ed25519PrivateKey.generate()
    if message == "not valid yet":
        with pytest.raises(RemoteSessionGrantError, match=message):
            _verifier(key, now=now).verify(
                _grant(key, **changes), session_id=SESSION, stream_id=STREAM)
    elif changes:
        with pytest.raises(RemoteSessionGrantError, match=message):
            _grant(key, **changes)
    else:
        with pytest.raises(RemoteSessionGrantError, match=message):
            _verifier(key, now=now).verify(
                _grant(key), session_id=SESSION, stream_id=STREAM)


@pytest.mark.parametrize(("changes", "message"), [
    ({"allow_input": 1}, "allow_input"),
    ({"generation": True}, "binding"),
    ({"source_machine": "vm-viewer"}, "different machines"),
    ({"source_tls_cert_sha256": "A" * 64}, "binding"),
    ({"session_secret": b"short"}, "exactly 32 bytes"),
])
def test_authority_rejects_malformed_policy_and_identity(changes, message) -> None:
    with pytest.raises(RemoteSessionGrantError, match=message):
        _grant(Ed25519PrivateKey.generate(), **changes)
