"""Signed external-authority contract for one RDP display lease."""
from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import multimachine.mm_display_authority as authority
from multimachine.mm_display_authority import (
    DisplayGrantError,
    DisplayGrantVerifier,
    issue_display_grant,
)
from multimachine.mm_pairing_authority import public_key_bytes


NOW = 1_800_000_000
SESSION = "dock-session-r9"
SECRET = bytes(range(32))
PRIMARY_PIN = hashlib.sha256(b"primary cert").hexdigest()
PEER_PIN = hashlib.sha256(b"peer cert").hexdigest()


def grant(key: Ed25519PrivateKey, **changes) -> dict:
    values = {
        "primary_machine": "laptop",
        "peer_machine": "workstation",
        "trust_domain_id": "owner-machines",
        "generation": 90,
        "session_id": SESSION,
        "slot_name": "rdp-0",
        "logical_x": 1920,
        "logical_y": 0,
        "width": 2560,
        "height": 1440,
        "scale": 1,
        "allow_input": True,
        "carrier_secret": SECRET,
        "primary_tls_cert_sha256": PRIMARY_PIN,
        "peer_tls_cert_sha256": PEER_PIN,
        "issued_at": NOW,
        "handoff_expires_at": NOW + 120,
        "lease_expires_at": NOW + 8 * 60 * 60,
        "heartbeat_ms": 2000,
        "private_key": key,
    }
    values.update(changes)
    return issue_display_grant(**values)


def verifier(key: Ed25519PrivateKey, *, role="primary", machine="laptop",
             now=NOW) -> DisplayGrantVerifier:
    return DisplayGrantVerifier.from_public_bytes(
        public_key_bytes(key.public_key()), role=role,
        local_machine_id=machine, now=lambda: now)


@pytest.mark.parametrize(("role", "machine"), [
    ("primary", "laptop"),
    ("peer", "workstation"),
])
def test_same_receipt_authorizes_only_the_named_endpoints(role, machine):
    key = Ed25519PrivateKey.generate()
    payload = verifier(key, role=role, machine=machine).verify(
        grant(key), session_id=SESSION, carrier_secret=SECRET)
    assert payload["slot_name"] == "rdp-0"
    assert payload["carrier_secret_sha256"] == hashlib.sha256(SECRET).hexdigest()


def test_tamper_signer_unknown_field_and_secret_substitution_fail():
    key = Ed25519PrivateKey.generate()
    receipt = deepcopy(grant(key))
    receipt["payload"]["logical_x"] = 0
    with pytest.raises(DisplayGrantError, match="signature is invalid"):
        verifier(key).verify(receipt, session_id=SESSION, carrier_secret=SECRET)

    with pytest.raises(DisplayGrantError, match="signature is invalid"):
        verifier(Ed25519PrivateKey.generate()).verify(
            grant(key), session_id=SESSION, carrier_secret=SECRET)

    receipt = deepcopy(grant(key))
    receipt["payload"]["rdp_password"] = "smuggled"
    receipt["signature"] = authority._encode_signature(
        key.sign(authority._canonical(receipt["payload"])))
    with pytest.raises(DisplayGrantError, match="payload fields"):
        verifier(key).verify(receipt, session_id=SESSION, carrier_secret=SECRET)

    with pytest.raises(DisplayGrantError, match="secret digest mismatch"):
        verifier(key).verify(
            grant(key), session_id=SESSION, carrier_secret=b"x" * 32)


@pytest.mark.parametrize(("role", "machine", "session", "message"), [
    ("primary", "other", SESSION, "different local machine"),
    ("peer", "laptop", SESSION, "different local machine"),
    ("primary", "laptop", "other-session", "different session"),
])
def test_grant_cannot_cross_role_machine_or_session(
        role, machine, session, message):
    key = Ed25519PrivateKey.generate()
    with pytest.raises(DisplayGrantError, match=message):
        verifier(key, role=role, machine=machine).verify(
            grant(key), session_id=session, carrier_secret=SECRET)


@pytest.mark.parametrize(("changes", "now", "message"), [
    ({"issued_at": NOW + 1}, NOW, "not valid yet"),
    ({}, NOW + 120, "handoff has expired"),
    ({"handoff_expires_at": NOW + 301}, NOW, "handoff lifetime"),
    ({"lease_expires_at": NOW + 12 * 60 * 60 + 1}, NOW, "lease lifetime"),
    ({"lease_expires_at": NOW + 60}, NOW, "lease lifetime"),
])
def test_handoff_and_lease_lifetimes_fail_closed(changes, now, message):
    key = Ed25519PrivateKey.generate()
    if changes:
        with pytest.raises(DisplayGrantError, match=message):
            receipt = grant(key, **changes)
            verifier(key, now=now).verify(
                receipt, session_id=SESSION, carrier_secret=SECRET)
    else:
        with pytest.raises(DisplayGrantError, match=message):
            verifier(key, now=now).verify(
                grant(key), session_id=SESSION, carrier_secret=SECRET)


@pytest.mark.parametrize(("changes", "message"), [
    ({"generation": True}, "generation"),
    ({"slot_name": "headless"}, "slot name"),
    ({"width": 20_000}, "mode"),
    ({"scale": 5}, "scale"),
    ({"heartbeat_ms": 100}, "heartbeat"),
    ({"logical_x": 100_000}, "logical_x"),
    ({"allow_input": 1}, "allow_input"),
    ({"primary_tls_cert_sha256": "A" * 64}, "SHA-256"),
    ({"carrier_secret": b"short"}, "exactly 32 bytes"),
])
def test_authority_rejects_malformed_policy_geometry_and_identity(
        changes, message):
    with pytest.raises(DisplayGrantError, match=message):
        grant(Ed25519PrivateKey.generate(), **changes)
