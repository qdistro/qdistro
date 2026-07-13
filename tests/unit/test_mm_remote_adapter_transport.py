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
from multimachine.remote_nested_protocol import encode_input_event
from multimachine.remote_nested_protocol import (
    LocalBoundaryMessage,
    RawFrame,
    StreamAnnouncement,
)
from multimachine.remote_nested_service import MAX_BOUNDARY_FRAME_BYTES


REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "multimachine/qdistro-mm-remote-adapter"
CONTROLLER_ENTRYPOINT = (
    REPO / "multimachine/qdistro-mm-remote-nested-controller")
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


def _boundary_send(sock: socket.socket, message: LocalBoundaryMessage) -> None:
    send_frame(sock, message.encode(), maximum=MAX_BOUNDARY_FRAME_BYTES)


def _boundary_event(sock: socket.socket) -> LocalBoundaryMessage:
    payload = recv_frame(sock, maximum=MAX_BOUNDARY_FRAME_BYTES)
    assert payload is not None
    return LocalBoundaryMessage.decode(payload)


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
        for seq, (kind, event) in enumerate((
            ("motion", {"x_fixed": 256, "y_fixed": -128}),
            ("axis", {"axis": 0, "value_fixed": 120}),
            ("focus", {"focused": True}),
            ("button", {"code": 272, "pressed": True}),
            ("button", {"code": 272, "pressed": False}),
        ), start=3):
            _local_send(
                viewer_control, channel="input", kind=kind,
                payload=encode_input_event(kind, event))
            assert _event(viewer_control)["op"] == "sent"
            resumed = _event(source_control)
            assert (resumed["kind"], resumed["seq"]) == (kind, seq)

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


@pytest.mark.integration
def test_real_endpoints_and_nested_controllers_reconnect_and_resume() -> None:
    source_endpoint, source_controller_endpoint = socket.socketpair()
    source_controller_helper, source_helper = socket.socketpair()
    viewer_endpoint, viewer_controller_endpoint = socket.socketpair()
    viewer_controller_helper, viewer_helper = socket.socketpair()
    source_fd = source_endpoint.detach()
    source_controller_endpoint_fd = source_controller_endpoint.detach()
    source_controller_helper_fd = source_controller_helper.detach()
    viewer_fd = viewer_endpoint.detach()
    viewer_controller_endpoint_fd = viewer_controller_endpoint.detach()
    viewer_controller_helper_fd = viewer_controller_helper.detach()
    source_config, viewer_config = _configs(source_fd, viewer_fd, _port())
    source_config_fd = sealed_config_fd(source_config)
    viewer_config_fd = sealed_config_fd(viewer_config)
    inherited = [
        source_fd, source_controller_endpoint_fd, source_controller_helper_fd,
        viewer_fd, viewer_controller_endpoint_fd, viewer_controller_helper_fd,
        source_config_fd, viewer_config_fd,
    ]
    processes = []
    try:
        viewer = subprocess.Popen(
            [str(ENTRYPOINT), "--config-fd", str(viewer_config_fd)],
            pass_fds=(viewer_config_fd, viewer_fd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(viewer)
        viewer_controller = subprocess.Popen(
            [str(CONTROLLER_ENTRYPOINT), "--role", "viewer",
             "--endpoint-fd", str(viewer_controller_endpoint_fd),
             "--helper-fd", str(viewer_controller_helper_fd)],
            pass_fds=(viewer_controller_endpoint_fd,
                      viewer_controller_helper_fd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(viewer_controller)
        source = subprocess.Popen(
            [str(ENTRYPOINT), "--config-fd", str(source_config_fd)],
            pass_fds=(source_config_fd, source_fd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(source)
        source_controller = subprocess.Popen(
            [str(CONTROLLER_ENTRYPOINT), "--role", "source",
             "--endpoint-fd", str(source_controller_endpoint_fd),
             "--helper-fd", str(source_controller_helper_fd)],
            pass_fds=(source_controller_endpoint_fd,
                      source_controller_helper_fd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(source_controller)
        for fd in inherited:
            os.close(fd)
        inherited = []
        source_helper.settimeout(10)
        viewer_helper.settimeout(10)
        assert _boundary_event(source_helper) == LocalBoundaryMessage(
            "connected", seq=1)
        assert _boundary_event(viewer_helper) == LocalBoundaryMessage(
            "connected", seq=1)

        announcement = StreamAnnouncement(
            source_revision=7, width=4, height=3, stride=16,
            app_id="org.example.Editor", title="Remote editor")
        _boundary_send(source_helper, LocalBoundaryMessage(
            "announce", payload=announcement.encode()))
        assert _boundary_event(viewer_helper) == LocalBoundaryMessage(
            "announce", seq=7, payload=announcement.encode())

        frames = [
            RawFrame(4, 3, 16, bytes([value]) * 48)
            for value in (1, 2, 3)
        ]
        for frame in frames:
            _boundary_send(source_helper, LocalBoundaryMessage(
                "frame", payload=frame.encode()))
        delivered_1 = _boundary_event(viewer_helper)
        delivered_2 = _boundary_event(viewer_helper)
        assert [delivered_1.seq, delivered_2.seq] == [1, 2]
        assert RawFrame.decode(delivered_1.payload) == frames[0]
        assert RawFrame.decode(delivered_2.payload) == frames[1]

        _boundary_send(
            viewer_helper, LocalBoundaryMessage("decoder_ack", seq=1))
        assert _boundary_event(source_helper) == LocalBoundaryMessage(
            "media_ack", seq=1)
        delivered_3 = _boundary_event(viewer_helper)
        assert delivered_3.seq == 3
        assert RawFrame.decode(delivered_3.payload) == frames[2]
        _boundary_send(
            viewer_helper, LocalBoundaryMessage("decoder_ack", seq=3))
        assert _boundary_event(source_helper) == LocalBoundaryMessage(
            "media_ack", seq=3)

        key_down = encode_input_event(
            "key", {"code": 42, "pressed": True})
        _boundary_send(viewer_helper, LocalBoundaryMessage(
            "key", payload=key_down))
        assert _boundary_event(source_helper) == LocalBoundaryMessage(
            "key", payload=key_down)

        _boundary_send(viewer_helper, LocalBoundaryMessage("drop_transport"))
        released = _boundary_event(source_helper)
        assert released.kind == "key"
        assert json.loads(released.payload) == {"code": 42, "pressed": False}
        assert _boundary_event(source_helper) == LocalBoundaryMessage(
            "detached", seq=1)
        assert _boundary_event(viewer_helper) == LocalBoundaryMessage(
            "detached", seq=1)
        assert _boundary_event(source_helper) == LocalBoundaryMessage(
            "connected", seq=2)
        assert _boundary_event(viewer_helper) == LocalBoundaryMessage(
            "connected", seq=2)
        assert _boundary_event(viewer_helper) == LocalBoundaryMessage(
            "announce", seq=7, payload=announcement.encode())

        resumed = RawFrame(4, 3, 16, b"r" * 48)
        _boundary_send(source_helper, LocalBoundaryMessage(
            "frame", payload=resumed.encode()))
        delivered = _boundary_event(viewer_helper)
        assert delivered.seq == 1
        assert RawFrame.decode(delivered.payload) == resumed
        _boundary_send(
            viewer_helper, LocalBoundaryMessage("decoder_ack", seq=1))
        assert _boundary_event(source_helper) == LocalBoundaryMessage(
            "media_ack", seq=1)

        _boundary_send(
            source_helper, LocalBoundaryMessage("source_closed", seq=8))
        assert _boundary_event(viewer_helper) == LocalBoundaryMessage(
            "source_closed", seq=8)
        _wait(source_controller, "source nested controller")
        assert _boundary_event(viewer_helper) == LocalBoundaryMessage(
            "detached", seq=2)
        _boundary_send(viewer_helper, LocalBoundaryMessage("shutdown"))
        _wait(viewer_controller, "viewer nested controller")
        _wait(source, "source endpoint")
        _wait(viewer, "viewer endpoint")
    finally:
        for helper in (source_helper, viewer_helper):
            try:
                helper.close()
            except OSError:
                pass
        for fd in inherited:
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
