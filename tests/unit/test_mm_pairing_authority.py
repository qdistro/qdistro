"""Signed external pairing-authority receipt contract."""
from __future__ import annotations

from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from multimachine.mm_pairing_authority import (
    PairingReceiptError,
    PairingReceiptVerifier,
    issue_pairing_receipt,
    public_key_bytes,
)


NOW = 1_800_000_000
SESSION = "viewer-session-7f5c"


def _origins() -> list[dict]:
    return [{
        "machine_id": "vm-a",
        "trust_domain_id": "owner-machines",
        "generation": 51,
        "capabilities": ["attach_ui", "receive_input"],
    }]


def _receipt(key: Ed25519PrivateKey, **overrides) -> dict:
    values = {
        "origins": _origins(),
        "viewer_machine_id": "vm-viewer",
        "session_id": SESSION,
        "issued_at": NOW,
        "expires_at": NOW + 120,
        "private_key": key,
    }
    values.update(overrides)
    return issue_pairing_receipt(**values)


def _verifier(key: Ed25519PrivateKey, now=NOW) -> PairingReceiptVerifier:
    return PairingReceiptVerifier.from_public_bytes(
        public_key_bytes(key.public_key()), viewer_machine_id="vm-viewer",
        now=lambda: now,
    )


def test_valid_receipt_returns_only_broker_pairing_snapshot() -> None:
    key = Ed25519PrivateKey.generate()
    receipt = _receipt(key)
    pairing = _verifier(key).verify(receipt, session_id=SESSION)
    assert pairing == {"origins": _origins()}
    assert pairing["origins"] is not receipt["payload"]["origins"]


def test_tampered_grant_is_rejected_before_authorization() -> None:
    key = Ed25519PrivateKey.generate()
    receipt = deepcopy(_receipt(key))
    receipt["payload"]["origins"][0]["capabilities"] = ["attach_ui"]
    with pytest.raises(PairingReceiptError, match="signature is invalid"):
        _verifier(key).verify(receipt, session_id=SESSION)


def test_wrong_authority_key_is_rejected() -> None:
    signer = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    with pytest.raises(PairingReceiptError, match="signature is invalid"):
        _verifier(other).verify(_receipt(signer), session_id=SESSION)


@pytest.mark.parametrize(("viewer", "session", "message"), [
    ("other-viewer", SESSION, "different viewer"),
    ("vm-viewer", "other-session", "different session"),
])
def test_receipt_is_bound_to_viewer_and_stream_session(
        viewer, session, message) -> None:
    key = Ed25519PrivateKey.generate()
    verifier = PairingReceiptVerifier.from_public_bytes(
        public_key_bytes(key.public_key()), viewer_machine_id=viewer,
        now=lambda: NOW,
    )
    with pytest.raises(PairingReceiptError, match=message):
        verifier.verify(_receipt(key), session_id=session)


@pytest.mark.parametrize(("issued", "expires", "now", "message"), [
    (NOW, NOW + 120, NOW + 120, "expired"),
    (NOW + 1, NOW + 120, NOW, "not valid yet"),
])
def test_receipt_time_window_fails_closed(issued, expires, now, message) -> None:
    key = Ed25519PrivateKey.generate()
    receipt = _receipt(key, issued_at=issued, expires_at=expires)
    with pytest.raises(PairingReceiptError, match=message):
        _verifier(key, now=now).verify(receipt, session_id=SESSION)


def test_authority_cannot_issue_overlong_receipt() -> None:
    key = Ed25519PrivateKey.generate()
    with pytest.raises(PairingReceiptError, match="lifetime"):
        _receipt(key, expires_at=NOW + 301)


def test_unknown_signed_grant_field_is_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    origins = _origins()
    origins[0]["display_name"] = "untrusted extra"
    with pytest.raises(PairingReceiptError, match="grant schema"):
        _receipt(key, origins=origins)
