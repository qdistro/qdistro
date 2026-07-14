"""Trusted deployment boundary and panel-owner action tests for R9."""
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

from multimachine.display_panel_endpoint import SystemdPanelActions
from multimachine.mm_display_authority import issue_display_grant
from multimachine.mm_display_panel_launcher import (
    build_verified_panel_config,
    exec_panel,
    parse_panel_config,
)
from multimachine.mm_pairing_authority import public_key_bytes


SECRET = b"panel-generation-secret-value-32"[:32]
AUTHORITY = Ed25519PrivateKey.generate()


def _certificate(common_name: str) -> tuple[bytes, bytes, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (x509.CertificateBuilder()
                   .subject_name(name)
                   .issuer_name(name)
                   .public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=1))
                   .add_extension(
                       x509.BasicConstraints(ca=True, path_length=0), True)
                   .add_extension(x509.ExtendedKeyUsage([
                       ExtendedKeyUsageOID.SERVER_AUTH,
                       ExtendedKeyUsageOID.CLIENT_AUTH,
                   ]), False)
                   .sign(key, hashes.SHA256()))
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    pin = hashlib.sha256(
        certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_pem, key_pem, pin


PRIMARY_CERT, PRIMARY_KEY, PRIMARY_PIN = _certificate("panel-primary")
PEER_CERT, PEER_KEY, PEER_PIN = _certificate("panel-peer")


def _receipt(now: int) -> dict:
    return issue_display_grant(
        primary_machine="vm-primary", peer_machine="vm-peer",
        trust_domain_id="owner-machines", generation=112,
        session_id="display-session-112", slot_name="rdp-0",
        logical_x=1280, logical_y=0, width=1280, height=800, scale=1,
        allow_input=True, carrier_secret=SECRET,
        primary_tls_cert_sha256=PRIMARY_PIN,
        peer_tls_cert_sha256=PEER_PIN,
        issued_at=now, handoff_expires_at=now + 120,
        lease_expires_at=now + 3600, heartbeat_ms=1000,
        private_key=AUTHORITY)


def _listener() -> socket.socket:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


def _build(role: str, *, now: int, listener: socket.socket | None = None,
           receipt: dict | None = None, secret: bytes = SECRET) -> dict:
    common = {
        "receipt": receipt or _receipt(now),
        "secret": secret,
        "authority_public_key": public_key_bytes(AUTHORITY.public_key()),
        "role": role,
        "local_machine_id": "vm-primary" if role == "primary" else "vm-peer",
        "session_id": "display-session-112",
        "now": lambda: now,
    }
    if role == "primary":
        assert listener is not None
        return build_verified_panel_config(
            local_certificate_pem=PRIMARY_CERT,
            local_private_key_pem=PRIMARY_KEY,
            peer_certificate_pem=PEER_CERT,
            listener_fd=listener.fileno(),
            control_unix_path="/run/qdistro/multimachine/panel-rdp-0.sock",
            control_uid=1000, control_gid=1000, **common)
    return build_verified_panel_config(
        local_certificate_pem=PEER_CERT,
        local_private_key_pem=PEER_KEY,
        peer_certificate_pem=PRIMARY_CERT,
        primary_host="primary.internal", primary_port=3388,
        local_unit="qdistro-local-desktop.service",
        remote_unit="qdistro-remote-panel@rdp-0.service", **common)


def test_verified_primary_and_peer_panel_configs_round_trip() -> None:
    now = int(time.time())
    listener = _listener()
    try:
        primary = parse_panel_config(
            _build("primary", now=now, listener=listener), now=lambda: now)
        peer = parse_panel_config(_build("peer", now=now), now=lambda: now)
    finally:
        listener.close()
    assert primary.identity.role == "primary"
    assert primary.lease.generation == 112
    assert primary.target["control_uid"] == 1000
    assert peer.identity.role == "peer"
    assert peer.listener_fd is None
    assert peer.target == {
        "primary_host": "primary.internal",
        "primary_port": 3388,
        "local_unit": "qdistro-local-desktop.service",
        "remote_unit": "qdistro-remote-panel@rdp-0.service",
    }


def test_signature_machine_and_secret_remain_load_bearing() -> None:
    now = int(time.time())
    listener = _listener()
    try:
        tampered = _receipt(now)
        tampered["payload"]["generation"] += 1
        with pytest.raises(ValueError, match="signature"):
            _build("primary", now=now, listener=listener, receipt=tampered)
        with pytest.raises(ValueError, match="secret digest"):
            _build("primary", now=now, listener=listener, secret=b"x" * 32)
        with pytest.raises(ValueError, match="different local machine"):
            build_verified_panel_config(
                _receipt(now), SECRET,
                public_key_bytes(AUTHORITY.public_key()), role="peer",
                local_machine_id="attacker", session_id="display-session-112",
                local_certificate_pem=PEER_CERT,
                local_private_key_pem=PEER_KEY,
                peer_certificate_pem=PRIMARY_CERT,
                primary_host="primary.internal", primary_port=3388,
                local_unit="qdistro-local-desktop.service",
                remote_unit="qdistro-remote-panel@rdp-0.service",
                now=lambda: now)
    finally:
        listener.close()


def test_role_targets_and_unit_names_fail_closed() -> None:
    now = int(time.time())
    listener = _listener()
    try:
        config = _build("primary", now=now, listener=listener)
        config["listener_fd"] = None
        with pytest.raises(ValueError, match="listener fd"):
            parse_panel_config(config, now=lambda: now)
        config = _build("peer", now=now)
        config["target"]["local_unit"] = "../../attacker"
        with pytest.raises(ValueError, match="local.*unit"):
            parse_panel_config(config, now=lambda: now)
        config = _build("peer", now=now)
        config["target"]["remote_unit"] = config["target"]["local_unit"]
        with pytest.raises(ValueError, match="must differ"):
            parse_panel_config(config, now=lambda: now)
    finally:
        listener.close()


def test_primary_exec_inherits_only_listener_and_sealed_config(monkeypatch) -> None:
    now = int(time.time())
    listener = _listener()
    config = _build("primary", now=now, listener=listener)
    observed: list[bool] = []

    def fake_exec(program: str, argv: list[str]) -> None:
        assert program == "/panel"
        assert argv[:2] == ["/panel", "--config-fd"]
        assert len(argv) == 3
        assert int(argv[2]) != listener.fileno()
        observed.append(os.get_inheritable(listener.fileno()))
        raise FileNotFoundError("test exec")

    original = os.get_inheritable(listener.fileno())
    monkeypatch.setattr(os, "execvp", fake_exec)
    try:
        with pytest.raises(FileNotFoundError, match="test exec"):
            exec_panel(config, "/panel")
        assert observed == [True]
        assert os.get_inheritable(listener.fileno()) is original
    finally:
        listener.close()


def test_systemd_panel_actions_switch_and_restore_exact_owners() -> None:
    active = {"local.service": True, "remote.service": False}
    calls: list[tuple[str, str]] = []

    def run(action: str, unit: str) -> int:
        calls.append((action, unit))
        if action == "is-active":
            return 0 if active[unit] else 3
        if action == "stop":
            active[unit] = False
            return 0
        if action == "start":
            active[unit] = True
            return 0
        raise AssertionError(action)

    actions = SystemdPanelActions(
        slot_name="rdp-0", local_unit="local.service",
        remote_unit="remote.service", run=run)
    actions.blank("rdp-0")
    assert active == {"local.service": False, "remote.service": False}
    actions.restore("rdp-0")
    assert active == {"local.service": True, "remote.service": False}
    assert actions.safe("rdp-0")
    assert ("stop", "remote.service") in calls


def test_panel_reservation_refuses_to_invent_local_ownership() -> None:
    def run(action: str, _unit: str) -> int:
        return 3 if action == "is-active" else 0

    actions = SystemdPanelActions(
        slot_name="rdp-0", local_unit="local.service",
        remote_unit="remote.service", run=run)
    with pytest.raises(RuntimeError, match="local panel owner is not active"):
        actions.blank("rdp-0")


def test_partial_reservation_failure_rolls_back_to_local_owner() -> None:
    active = {"local.service": True, "remote.service": False}
    failed_once = [False]

    def run(action: str, unit: str) -> int:
        if action == "is-active":
            return 0 if active[unit] else 3
        if action == "stop":
            active[unit] = False
            if unit == "local.service" and not failed_once[0]:
                failed_once[0] = True
                return 1
            return 0
        if action == "start":
            active[unit] = True
            return 0
        raise AssertionError(action)

    actions = SystemdPanelActions(
        slot_name="rdp-0", local_unit="local.service",
        remote_unit="remote.service", run=run)
    with pytest.raises(RuntimeError, match="cannot stop local"):
        actions.blank("rdp-0")
    assert active == {"local.service": True, "remote.service": False}
