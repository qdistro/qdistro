"""Security and byte-integrity tests for the R9 outer RDP carrier."""
from __future__ import annotations

import hashlib
import socket
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from multimachine.display_carrier import (
    DisplayCarrierBinding,
    DisplayCarrierError,
    DisplayCarrierIdentity,
    authenticate_peer,
    authenticate_primary,
    relay_streams,
    serve_peer_once,
    serve_primary_once,
    validate_tls_material,
)


SECRET = bytes(range(32))


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


PRIMARY_CERT, PRIMARY_KEY, PRIMARY_PIN = _certificate("r9-primary")
PEER_CERT, PEER_KEY, PEER_PIN = _certificate("r9-peer")


def _binding(**changes) -> DisplayCarrierBinding:
    values = {
        "authority_key_id": "authority-r9",
        "primary_machine": "vm-primary",
        "peer_machine": "vm-peer",
        "trust_domain_id": "owner-machines",
        "generation": 91,
        "session_id": "display-session-r9",
        "slot_name": "rdp-0",
        "carrier_secret_sha256": hashlib.sha256(SECRET).hexdigest(),
        "primary_tls_cert_sha256": PRIMARY_PIN,
        "peer_tls_cert_sha256": PEER_PIN,
        "lease_expires_at": int(time.time()) + 300,
    }
    values.update(changes)
    return DisplayCarrierBinding(**values)


def _identity(role: str, **changes) -> DisplayCarrierIdentity:
    if role == "primary":
        local_cert, local_key, peer_cert = PRIMARY_CERT, PRIMARY_KEY, PEER_CERT
    else:
        local_cert, local_key, peer_cert = PEER_CERT, PEER_KEY, PRIMARY_CERT
    values = {
        "role": role,
        "binding": _binding(),
        "secret": SECRET,
        "local_certificate_pem": local_cert,
        "local_private_key_pem": local_key,
        "peer_certificate_pem": peer_cert,
    }
    values.update(changes)
    return DisplayCarrierIdentity(**values)


def _run(target, errors: list[BaseException], results: list[object]) -> None:
    try:
        results.append(target())
    except BaseException as exc:  # expose a worker failure to the assertion
        errors.append(exc)


def test_binding_is_canonical_and_generation_scoped() -> None:
    first = _binding()
    assert first.canonical() == _binding().canonical()
    assert first.canonical() != _binding(generation=92).canonical()


def test_tls_material_requires_exact_signed_pins_and_matching_key() -> None:
    validate_tls_material(
        role="primary", binding=_binding(),
        local_certificate_pem=PRIMARY_CERT,
        local_private_key_pem=PRIMARY_KEY,
        peer_certificate_pem=PEER_CERT)
    with pytest.raises(DisplayCarrierError, match="local TLS certificate pin"):
        validate_tls_material(
            role="primary", binding=_binding(primary_tls_cert_sha256="0" * 64),
            local_certificate_pem=PRIMARY_CERT,
            local_private_key_pem=PRIMARY_KEY,
            peer_certificate_pem=PEER_CERT)
    with pytest.raises(DisplayCarrierError, match="does not match certificate"):
        validate_tls_material(
            role="primary", binding=_binding(),
            local_certificate_pem=PRIMARY_CERT,
            local_private_key_pem=PEER_KEY,
            peer_certificate_pem=PEER_CERT)


def test_identity_requires_separately_delivered_grant_secret() -> None:
    with pytest.raises(DisplayCarrierError, match="secret digest mismatch"):
        _identity("primary", secret=b"x" * 32).validate()


def test_handoff_proof_is_mutual_and_replay_is_rejected() -> None:
    used: set[bytes] = set()
    fixed_peer_nonce = b"p" * 32

    def exchange(expect_success: bool) -> tuple[list[BaseException], list[object]]:
        primary, peer = socket.socketpair()
        errors: list[BaseException] = []
        results: list[object] = []

        def server_exchange() -> None:
            try:
                authenticate_primary(
                    primary, binding=_binding(), secret=SECRET,
                    used_peer_nonces=used,
                    nonce_factory=lambda _size: b"s" * 32)
            finally:
                primary.close()

        server = threading.Thread(
            target=_run, args=(server_exchange, errors, results))
        server.start()
        try:
            authenticate_peer(
                peer, binding=_binding(), secret=SECRET,
                nonce_factory=lambda _size: fixed_peer_nonce)
            results.append("peer-ok")
        except BaseException as exc:
            errors.append(exc)
        finally:
            peer.close()
        server.join(timeout=2)
        assert not server.is_alive()
        if expect_success:
            assert errors == []
        return errors, results

    errors, results = exchange(True)
    assert sorted(results, key=lambda value: value is not None) == [None, "peer-ok"]
    errors, _results = exchange(False)
    assert any("replayed" in str(exc) for exc in errors)


def test_handoff_proof_rejects_another_generation() -> None:
    primary, peer = socket.socketpair()
    errors: list[BaseException] = []
    results: list[object] = []

    def server_exchange() -> None:
        try:
            authenticate_primary(
                primary, binding=_binding(), secret=SECRET,
                used_peer_nonces=set())
        finally:
            primary.close()

    server = threading.Thread(
        target=_run, args=(server_exchange, errors, results))
    server.start()
    with pytest.raises(DisplayCarrierError):
        authenticate_peer(
            peer, binding=_binding(generation=92), secret=SECRET)
    peer.close()
    server.join(timeout=2)
    assert not server.is_alive()
    assert any("another grant" in str(exc) for exc in errors)


def test_relay_is_byte_exact_bounded_and_ends_on_disconnect() -> None:
    left_client, left_relay = socket.socketpair()
    right_relay, right_client = socket.socketpair()
    errors: list[BaseException] = []
    results: list[object] = []
    worker = threading.Thread(target=_run, args=(lambda: relay_streams(
        left_relay, right_relay, lease_expires_at=int(time.time()) + 60),
        errors, results))
    worker.start()
    left_payload = bytes(range(256)) * 400
    right_payload = b"rdp-input" * 7000
    left_client.sendall(left_payload)
    assert _receive_exact(right_client, len(left_payload)) == left_payload
    right_client.sendall(right_payload)
    assert _receive_exact(left_client, len(right_payload)) == right_payload
    left_client.close()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []
    result = results[0]
    assert result.closed_by == "left"
    assert result.left_to_right == len(left_payload)
    assert result.right_to_left == len(right_payload)
    right_client.close()


class _DisconnectingSocket:
    def __init__(self, sock: socket.socket, *, operation: str):
        self.sock = sock
        self.operation = operation

    def fileno(self) -> int:
        return self.sock.fileno()

    def recv(self, size: int) -> bytes:
        if self.operation == "recv":
            raise ConnectionResetError("test reset")
        return self.sock.recv(size)

    def sendall(self, payload: bytes) -> None:
        if self.operation == "send":
            raise BrokenPipeError("test broken pipe")
        self.sock.sendall(payload)


@pytest.mark.parametrize(
    ("operation", "expected_closed_by"),
    (("recv", "left"), ("send", "right")),
)
def test_relay_treats_abrupt_endpoint_reset_as_disconnect(
        operation: str, expected_closed_by: str) -> None:
    left_client, left_socket = socket.socketpair()
    right_socket, right_client = socket.socketpair()
    left = _DisconnectingSocket(left_socket, operation=operation)
    right = _DisconnectingSocket(right_socket, operation=operation)
    left_client.sendall(b"make-left-readable")

    result = relay_streams(
        left, right, lease_expires_at=int(time.time()) + 60)

    assert result.closed_by == expected_closed_by
    assert result.left_to_right == 0
    assert result.right_to_left == 0
    left_client.close()
    left_socket.close()
    right_socket.close()
    right_client.close()


def _receive_exact(sock: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        assert chunk
        result.extend(chunk)
    return bytes(result)


@pytest.mark.integration
def test_full_outer_mtls_carrier_bridges_only_to_private_unix_listener(
        tmp_path) -> None:
    backend_path = str(tmp_path / "qdwin-rdp.sock")
    backend_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    backend_listener.bind(backend_path)
    backend_listener.listen(1)

    primary_listener = socket.socket()
    primary_listener.bind(("127.0.0.1", 0))
    primary_listener.listen(1)
    primary_port = primary_listener.getsockname()[1]
    peer_listener = socket.socket()
    peer_listener.bind(("127.0.0.1", 0))
    peer_listener.listen(1)
    peer_port = peer_listener.getsockname()[1]

    errors: list[BaseException] = []
    results: list[object] = []
    primary = threading.Thread(target=_run, args=(lambda: serve_primary_once(
        _identity("primary"), listener=primary_listener,
        backend_unix_path=backend_path), errors, results))
    peer = threading.Thread(target=_run, args=(lambda: serve_peer_once(
        _identity("peer"), local_listener=peer_listener,
        primary_host="127.0.0.1", primary_port=primary_port), errors, results))
    primary.start()
    peer.start()

    local_rdp = socket.create_connection(("127.0.0.1", peer_port), timeout=2)
    qdwin_rdp, _address = backend_listener.accept()
    local_rdp.sendall(b"inner-rdp-tls-client-hello")
    assert qdwin_rdp.recv(128) == b"inner-rdp-tls-client-hello"
    qdwin_rdp.sendall(b"inner-rdp-tls-server-hello")
    assert local_rdp.recv(128) == b"inner-rdp-tls-server-hello"
    local_rdp.close()

    primary.join(timeout=3)
    peer.join(timeout=3)
    assert not primary.is_alive()
    assert not peer.is_alive()
    assert errors == []
    assert len(results) == 2
    qdwin_rdp.close()
    backend_listener.close()
    primary_listener.close()
    peer_listener.close()
