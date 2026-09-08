"""Trusted-FD configuration boundary for the R9 display carrier."""
from __future__ import annotations

import hashlib
import os
import socket
import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from multimachine.mm_display_authority import issue_display_grant
from multimachine.mm_display_carrier_launcher import (
    build_verified_carrier_config,
    exec_carrier,
    parse_carrier_config,
)
from multimachine.mm_pairing_authority import public_key_bytes


SECRET = b"r9-display-carrier-secret-value!"[:32]
AUTHORITY = Ed25519PrivateKey.generate()


def _certificate(common_name: str) -> tuple[bytes, bytes, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .add_extension(x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH,
                ExtendedKeyUsageOID.CLIENT_AUTH,
            ]), False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    pin = hashlib.sha256(
        cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_pem, key_pem, pin


PRIMARY_CERT, PRIMARY_KEY, PRIMARY_PIN = _certificate("primary")
PEER_CERT, PEER_KEY, PEER_PIN = _certificate("peer")


def _receipt(now: int) -> dict:
    return issue_display_grant(
        primary_machine="vm-primary", peer_machine="vm-peer",
        trust_domain_id="owner-machines", generation=101,
        session_id="display-session-101", slot_name="rdp-0",
        logical_x=1280, logical_y=0, width=1280, height=800, scale=1,
        allow_input=True, carrier_secret=SECRET,
        primary_tls_cert_sha256=PRIMARY_PIN,
        peer_tls_cert_sha256=PEER_PIN,
        issued_at=now, handoff_expires_at=now + 120,
        lease_expires_at=now + 3600, heartbeat_ms=1000,
        private_key=AUTHORITY)


def _listener(host: str = "127.0.0.1") -> socket.socket:
    listener = socket.socket()
    listener.bind((host, 0))
    listener.listen(1)
    return listener


def _build(role: str, listener: socket.socket, *, now: int,
           receipt: dict | None = None, secret: bytes = SECRET) -> dict:
    common = {
        "receipt": receipt or _receipt(now),
        "secret": secret,
        "authority_public_key": public_key_bytes(AUTHORITY.public_key()),
        "role": role,
        "local_machine_id": "vm-primary" if role == "primary" else "vm-peer",
        "session_id": "display-session-101",
        "listener_fd": listener.fileno(),
        "now": lambda: now,
    }
    if role == "primary":
        return build_verified_carrier_config(
            local_certificate_pem=PRIMARY_CERT,
            local_private_key_pem=PRIMARY_KEY,
            peer_certificate_pem=PEER_CERT,
            backend_unix_path="/run/qdistro/rdp-0.sock",
            control_unix_path="/run/qdistro/mm-display-carrier-supervisor.sock",
            control_uid=1000, control_gid=1000, **common)
    return build_verified_carrier_config(
        local_certificate_pem=PEER_CERT,
        local_private_key_pem=PEER_KEY,
        peer_certificate_pem=PRIMARY_CERT,
        primary_host="primary.internal", primary_port=3389,
        client_unit="qdistro-remote-panel@rdp-0.service", **common)


def test_verified_primary_and_peer_configs_round_trip() -> None:
    now = int(time.time())
    primary_listener = _listener()
    peer_listener = _listener()
    try:
        primary = parse_carrier_config(
            _build("primary", primary_listener, now=now), now=lambda: now)
        peer = parse_carrier_config(
            _build("peer", peer_listener, now=now), now=lambda: now)
    finally:
        primary_listener.close()
        peer_listener.close()
    assert primary.identity.role == "primary"
    assert primary.identity.binding.generation == 101
    assert primary.target == {
        "backend_unix_path": "/run/qdistro/rdp-0.sock",
        "control_unix_path": "/run/qdistro/mm-display-carrier-supervisor.sock",
        "control_uid": 1000,
        "control_gid": 1000,
    }
    assert peer.identity.role == "peer"
    assert peer.target == {
        "primary_host": "primary.internal", "primary_port": 3389,
        "client_unit": "qdistro-remote-panel@rdp-0.service",
    }


def test_authority_signature_machine_and_secret_are_load_bearing() -> None:
    now = int(time.time())
    listener = _listener()
    try:
        tampered = _receipt(now)
        tampered["payload"]["generation"] += 1
        with pytest.raises(ValueError, match="signature"):
            _build("primary", listener, now=now, receipt=tampered)
        with pytest.raises(ValueError, match="different local machine"):
            build_verified_carrier_config(
                _receipt(now), SECRET,
                public_key_bytes(AUTHORITY.public_key()), role="primary",
                local_machine_id="attacker", session_id="display-session-101",
                local_certificate_pem=PRIMARY_CERT,
                local_private_key_pem=PRIMARY_KEY,
                peer_certificate_pem=PEER_CERT,
                listener_fd=listener.fileno(),
                backend_unix_path="/run/qdistro/rdp-0.sock",
                control_unix_path="/run/qdistro/mm-display-carrier-supervisor.sock",
                control_uid=1000, control_gid=1000, now=lambda: now)
        with pytest.raises(ValueError, match="secret digest"):
            _build("primary", listener, now=now, secret=b"x" * 32)
    finally:
        listener.close()


def test_peer_raw_rdp_listener_must_be_loopback_only() -> None:
    now = int(time.time())
    listener = _listener("0.0.0.0")
    try:
        with pytest.raises(ValueError, match="must be bound to loopback"):
            _build("peer", listener, now=now)
    finally:
        listener.close()


def test_config_rejects_expired_lease_and_unknown_fields() -> None:
    now = int(time.time())
    listener = _listener()
    try:
        config = _build("primary", listener, now=now)
        config["binding"]["lease_expires_at"] = now
        with pytest.raises(ValueError, match="lease has expired"):
            parse_carrier_config(config, now=lambda: now)
        config = _build("primary", listener, now=now)
        config["unexpected"] = True
        with pytest.raises(ValueError, match="fields do not match"):
            parse_carrier_config(config, now=lambda: now)
    finally:
        listener.close()


def test_exec_passes_only_sealed_config_and_inherited_listener(
        monkeypatch) -> None:
    now = int(time.time())
    listener = _listener()
    config = _build("primary", listener, now=now)
    inherited_during_exec: list[bool] = []

    def fake_exec(program: str, argv: list[str]) -> None:
        assert program == "/carrier"
        assert argv[:2] == ["/carrier", "--config-fd"]
        assert len(argv) == 3
        assert int(argv[2]) not in {listener.fileno()}
        inherited_during_exec.append(os.get_inheritable(listener.fileno()))
        raise FileNotFoundError("test exec boundary")

    original = os.get_inheritable(listener.fileno())
    monkeypatch.setattr(os, "execvp", fake_exec)
    try:
        with pytest.raises(FileNotFoundError, match="test exec boundary"):
            exec_carrier(config, "/carrier")
        assert inherited_during_exec == [True]
        assert os.get_inheritable(listener.fileno()) is original
    finally:
        listener.close()
