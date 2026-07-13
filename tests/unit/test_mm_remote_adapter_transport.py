"""Real-process pinned-TLS carrier tests for the R6 remote adapter."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from multimachine.mm_pairing_authority import public_key_bytes
from multimachine.mm_remote_session_authority import issue_remote_session_grant
from multimachine.mm_remote_session_launcher import build_verified_endpoint_config
from multimachine.mm_session_launcher import sealed_config_fd
from multimachine.remote_adapter import MAX_WIRE_BYTES, RemoteAdapterError
from multimachine.remote_adapter_transport import (
    MAX_LOCAL_FRAME_BYTES,
    recv_frame,
    send_frame,
)


REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "multimachine/qdistro-mm-remote-adapter"
SESSION = "viewer-session-7f5c"
STREAM = "stream_0123456789abcdef"
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


SOURCE_CERT, SOURCE_KEY, SOURCE_PIN = _certificate("vm-source")
VIEWER_CERT, VIEWER_KEY, VIEWER_PIN = _certificate("vm-viewer")


def _port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _configs(source_fd: int, viewer_fd: int, port: int) -> tuple[dict, dict]:
    now = int(time.time())
    authority = Ed25519PrivateKey.generate()
    receipt = issue_remote_session_grant(
        source_machine="vm-source", viewer_machine="vm-viewer",
        trust_domain_id="owner-machines", generation=51,
        session_id=SESSION, stream_id=STREAM, key_id="adapter-key-7f5c",
        session_secret=SECRET, issued_at=now, handoff_expires_at=now + 120,
        key_expires_at=now + 8 * 60 * 60, allow_input=True,
        source_tls_cert_sha256=SOURCE_PIN,
        viewer_tls_cert_sha256=VIEWER_PIN, private_key=authority)
    public_key = public_key_bytes(authority.public_key())
    common = {
        "receipt": receipt,
        "secret": SECRET,
        "authority_public_key": public_key,
        "session_id": SESSION,
        "stream_id": STREAM,
        "host": "127.0.0.1",
        "port": port,
        "now": lambda: now,
    }
    source = build_verified_endpoint_config(
        role="source", local_machine_id="vm-source",
        local_certificate_pem=SOURCE_CERT,
        local_private_key_pem=SOURCE_KEY,
        peer_certificate_pem=VIEWER_CERT, local_fd=source_fd, **common)
    viewer = build_verified_endpoint_config(
        role="viewer", local_machine_id="vm-viewer",
        local_certificate_pem=VIEWER_CERT,
        local_private_key_pem=VIEWER_KEY,
        peer_certificate_pem=SOURCE_CERT, local_fd=viewer_fd, **common)
    return source, viewer


def _local_send(sock: socket.socket, *, channel: str, kind: str,
                payload: bytes = b"", ack: int = 0) -> None:
    doc = {
        "op": "send", "channel": channel, "kind": kind, "ack": ack,
        "payload": base64.urlsafe_b64encode(payload).rstrip(b"=").decode(),
    }
    send_frame(
        sock, json.dumps(doc, separators=(",", ":")).encode(),
        maximum=MAX_LOCAL_FRAME_BYTES)


def _event(sock: socket.socket) -> dict:
    payload = recv_frame(sock, maximum=MAX_LOCAL_FRAME_BYTES)
    assert payload is not None
    return json.loads(payload)


def _drop_transport(sock: socket.socket) -> None:
    send_frame(
        sock, b'{"op":"drop_transport"}', maximum=MAX_LOCAL_FRAME_BYTES)


def _wait(process: subprocess.Popen, label: str) -> None:
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        pytest.fail(f"{label} did not exit; stdout={stdout!r} stderr={stderr!r}")
    assert process.returncode == 0, (
        f"{label} failed: stdout={stdout!r} stderr={stderr!r}")


@pytest.mark.integration
def test_two_real_processes_exchange_control_bounded_media_input_and_cleanup() -> None:
    source_endpoint, source_control = socket.socketpair()
    viewer_endpoint, viewer_control = socket.socketpair()
    source_fd = source_endpoint.detach()
    viewer_fd = viewer_endpoint.detach()
    source_config, viewer_config = _configs(source_fd, viewer_fd, _port())
    source_config_fd = sealed_config_fd(source_config)
    viewer_config_fd = sealed_config_fd(viewer_config)
    processes = []
    try:
        # Deliberately start the connector first. It must retry the complete
        # TCP+TLS establishment rather than treating a pre-listener refusal/EOF
        # as an authenticated session failure.
        viewer = subprocess.Popen(
            [str(ENTRYPOINT), "--config-fd", str(viewer_config_fd)],
            pass_fds=(viewer_config_fd, viewer_fd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(viewer)
        source = subprocess.Popen(
            [str(ENTRYPOINT), "--config-fd", str(source_config_fd)],
            pass_fds=(source_config_fd, source_fd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(source)
        for fd in (source_config_fd, viewer_config_fd, source_fd, viewer_fd):
            os.close(fd)
        source_config_fd = viewer_config_fd = source_fd = viewer_fd = -1
        source_control.settimeout(10)
        viewer_control.settimeout(10)
        source_connected = _event(source_control)
        viewer_connected = _event(viewer_control)
        assert source_connected == {
            "op": "connected", "peer_cert_sha256": VIEWER_PIN,
            "tls_version": "TLSv1.3", "epoch": 1,
        }
        assert viewer_connected == {
            "op": "connected", "peer_cert_sha256": SOURCE_PIN,
            "tls_version": "TLSv1.3", "epoch": 1,
        }

        _local_send(
            source_control, channel="control", kind="announce",
            payload=b'{"source_revision":1}')
        assert _event(source_control) == {
            "channel": "control", "op": "sent", "seq": 1}
        announced = _event(viewer_control)
        assert (announced["op"], announced["kind"], announced["seq"]) == (
            "received", "announce", 1)

        for frame in (b"frame-one", b"frame-two"):
            _local_send(source_control, channel="media", kind="frame",
                        payload=frame)
            assert _event(source_control)["op"] == "sent"
        first_frame = _event(viewer_control)
        second_frame = _event(viewer_control)
        assert (first_frame["seq"], second_frame["seq"]) == (1, 2)

        _local_send(viewer_control, channel="control", kind="media_ack", ack=1)
        assert _event(viewer_control)["op"] == "sent"
        acknowledged = _event(source_control)
        assert (acknowledged["kind"], acknowledged["ack"]) == ("media_ack", 1)
        _local_send(source_control, channel="media", kind="frame",
                    payload=b"frame-three")
        assert _event(source_control)["seq"] == 3
        assert _event(viewer_control)["seq"] == 3

        _local_send(
            viewer_control, channel="input", kind="key",
            payload=b'{"code":42,"pressed":true}')
        assert _event(viewer_control)["op"] == "sent"
        landed = _event(source_control)
        assert (landed["channel"], landed["kind"]) == ("input", "key")

        _drop_transport(viewer_control)
        assert _event(viewer_control) == {
            "op": "transport_drop_requested", "epoch": 1}
        assert _event(viewer_control) == {
            "op": "detached", "epoch": 1, "releases": []}
        assert _event(source_control) == {
            "op": "detached", "epoch": 1,
            "releases": [{"code": 42, "kind": "key"}],
        }
        source_reconnected = _event(source_control)
        viewer_reconnected = _event(viewer_control)
        assert source_reconnected == {
            "op": "connected", "peer_cert_sha256": VIEWER_PIN,
            "tls_version": "TLSv1.3", "epoch": 2,
        }
        assert viewer_reconnected == {
            "op": "connected", "peer_cert_sha256": SOURCE_PIN,
            "tls_version": "TLSv1.3", "epoch": 2,
        }

        # A fresh authenticated epoch resets channel sequence state while the
        # source process and both local controller sockets remain alive.
        _local_send(
            source_control, channel="control", kind="reconcile",
            payload=b'{"source_revision":1}')
        assert _event(source_control) == {
            "channel": "control", "op": "sent", "seq": 1}
        reconciled = _event(viewer_control)
        assert (reconciled["kind"], reconciled["seq"]) == ("reconcile", 1)
        for pressed in (True, False):
            _local_send(
                viewer_control, channel="input", kind="key",
                payload=json.dumps({"code": 30, "pressed": pressed}).encode())
            assert _event(viewer_control)["op"] == "sent"
            resumed = _event(source_control)
            assert (resumed["kind"], resumed["seq"]) == (
                "key", 1 if pressed else 2)

        viewer_control.close()
        detached = _event(source_control)
        assert detached == {
            "op": "detached", "epoch": 2, "releases": []}
        source_control.close()
        _wait(viewer, "viewer endpoint")
        _wait(source, "source endpoint")
    finally:
        for sock in (source_control, viewer_control):
            try:
                sock.close()
            except OSError:
                pass
        for fd in (source_config_fd, viewer_config_fd, source_fd, viewer_fd):
            if fd >= 0:
                os.close(fd)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_framing_rejects_zero_oversize_and_truncated_messages() -> None:
    left, right = socket.socketpair()
    try:
        left.sendall(struct.pack("!I", 0))
        with pytest.raises(RemoteAdapterError, match="length"):
            recv_frame(right, maximum=MAX_WIRE_BYTES)
        left.sendall(struct.pack("!I", MAX_WIRE_BYTES + 1))
        with pytest.raises(RemoteAdapterError, match="length"):
            recv_frame(right, maximum=MAX_WIRE_BYTES)
        left.sendall(struct.pack("!I", 4) + b"xx")
        left.shutdown(socket.SHUT_WR)
        with pytest.raises(RemoteAdapterError, match="ended early"):
            recv_frame(right, maximum=MAX_WIRE_BYTES)
    finally:
        left.close()
        right.close()
