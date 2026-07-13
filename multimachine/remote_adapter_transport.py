"""Pinned mutual-TLS carrier and local control seam for the R6 adapter."""
from __future__ import annotations

import base64
import binascii
import fcntl
import json
import os
import select
import socket
import ssl
import struct
import time
from dataclasses import dataclass
from typing import Mapping

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .remote_adapter import (
    CHANNEL_PAYLOAD_LIMITS,
    MAX_WIRE_BYTES,
    SOURCE_TO_VIEWER,
    VIEWER_TO_SOURCE,
    AuthenticatedEnvelopeCodec,
    AuthenticatedReceiver,
    FlowControlError,
    MediaFlowController,
    RemoteAdapterError,
    RemoteProxyLifecycle,
    RemoteSessionBinding,
    RemoteSessionKey,
    verify_tls_peer_certificate,
)


MAX_LOCAL_FRAME_BYTES = MAX_WIRE_BYTES + 4096
MAX_TLS_PEM_BYTES = 256 * 1024
IO_TIMEOUT_SECONDS = 5.0
_LOCAL_SEND_FIELDS = frozenset({
    "op", "channel", "kind", "payload", "ack",
})


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: object, *, label: str, maximum: int) -> bytes:
    if not isinstance(value, str) or "=" in value:
        raise RemoteAdapterError(f"{label} must be unpadded base64url")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4),
                               altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RemoteAdapterError(
            f"{label} must be unpadded base64url") from exc
    if len(raw) > maximum:
        raise RemoteAdapterError(f"{label} exceeds size limit")
    return raw


def recv_frame(sock: socket.socket, *, maximum: int) -> bytes | None:
    """Read one non-empty uint32-length-prefixed frame; EOF before header is clean."""
    header = _recv_exact(sock, 4, allow_initial_eof=True)
    if header is None:
        return None
    length = struct.unpack("!I", header)[0]
    if length == 0 or length > maximum:
        raise RemoteAdapterError("framed message length is out of bounds")
    payload = _recv_exact(sock, length, allow_initial_eof=False)
    assert payload is not None
    return payload


def _recv_exact(sock: socket.socket, count: int, *,
                allow_initial_eof: bool) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            if not chunks and allow_initial_eof:
                return None
            raise RemoteAdapterError("framed message ended early")
        chunks.extend(chunk)
    return bytes(chunks)


def send_frame(sock: socket.socket, payload: bytes, *, maximum: int) -> None:
    if not isinstance(payload, bytes) or not payload or len(payload) > maximum:
        raise RemoteAdapterError("framed message payload is out of bounds")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def _certificate_der(pem: bytes, *, label: str) -> bytes:
    if (not isinstance(pem, bytes) or not pem
            or len(pem) > MAX_TLS_PEM_BYTES):
        raise RemoteAdapterError(f"{label} PEM is invalid")
    try:
        cert = x509.load_pem_x509_certificate(pem)
    except ValueError as exc:
        raise RemoteAdapterError(f"{label} PEM is invalid") from exc
    return cert.public_bytes(serialization.Encoding.DER)


def validate_tls_material(*, role: str, binding: RemoteSessionBinding,
                          local_certificate_pem: bytes,
                          local_private_key_pem: bytes,
                          peer_certificate_pem: bytes) -> None:
    """Validate pins and prove that the local private key matches its cert."""
    if role not in {"source", "viewer"}:
        raise RemoteAdapterError("TLS endpoint role must be source or viewer")
    local_pin = (binding.source_tls_cert_sha256 if role == "source"
                 else binding.viewer_tls_cert_sha256)
    peer_pin = (binding.viewer_tls_cert_sha256 if role == "source"
                else binding.source_tls_cert_sha256)
    local_der = _certificate_der(
        local_certificate_pem, label="local TLS certificate")
    peer_der = _certificate_der(
        peer_certificate_pem, label="peer TLS certificate")
    verify_tls_peer_certificate(local_der, local_pin)
    verify_tls_peer_certificate(peer_der, peer_pin)
    if (not isinstance(local_private_key_pem, bytes)
            or not local_private_key_pem
            or len(local_private_key_pem) > MAX_TLS_PEM_BYTES):
        raise RemoteAdapterError("local TLS private key PEM is invalid")
    try:
        private_key = serialization.load_pem_private_key(
            local_private_key_pem, password=None)
    except (TypeError, ValueError) as exc:
        raise RemoteAdapterError(
            "local TLS private key PEM is invalid") from exc
    cert = x509.load_der_x509_certificate(local_der)
    cert_public = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    key_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    if cert_public != key_public:
        raise RemoteAdapterError(
            "local TLS private key does not match certificate")


def _sealed_pem_fd(name: str, payload: bytes) -> int:
    flags = os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    fd = os.memfd_create(name, flags)
    try:
        os.write(fd, payload)
        os.lseek(fd, 0, os.SEEK_SET)
        seals = (fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK
                 | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
        return fd
    except Exception:
        os.close(fd)
        raise


def build_tls_context(*, role: str, binding: RemoteSessionBinding,
                      local_certificate_pem: bytes,
                      local_private_key_pem: bytes,
                      peer_certificate_pem: bytes) -> ssl.SSLContext:
    validate_tls_material(
        role=role, binding=binding,
        local_certificate_pem=local_certificate_pem,
        local_private_key_pem=local_private_key_pem,
        peer_certificate_pem=peer_certificate_pem)
    purpose = (ssl.Purpose.CLIENT_AUTH if role == "source"
               else ssl.Purpose.SERVER_AUTH)
    context = ssl.create_default_context(purpose)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_verify_locations(cadata=peer_certificate_pem.decode("ascii"))
    cert_fd = _sealed_pem_fd("qdistro-mm-tls-cert", local_certificate_pem)
    key_fd = _sealed_pem_fd("qdistro-mm-tls-key", local_private_key_pem)
    try:
        context.load_cert_chain(
            certfile=f"/proc/self/fd/{cert_fd}",
            keyfile=f"/proc/self/fd/{key_fd}")
    finally:
        os.close(cert_fd)
        os.close(key_fd)
    return context


@dataclass(frozen=True)
class EndpointRuntime:
    role: str
    allow_input: bool
    binding: RemoteSessionBinding
    session_key: RemoteSessionKey
    local_certificate_pem: bytes
    local_private_key_pem: bytes
    peer_certificate_pem: bytes
    host: str
    port: int
    local_fd: int


class RemoteAdapterEndpoint:
    """Translate a trusted local framed socket to authenticated TLS messages."""

    def __init__(self, runtime: EndpointRuntime):
        self.runtime = runtime
        self.codec = AuthenticatedEnvelopeCodec(
            runtime.binding, runtime.session_key)
        incoming = (VIEWER_TO_SOURCE if runtime.role == "source"
                    else SOURCE_TO_VIEWER)
        self.receiver = AuthenticatedReceiver(
            self.codec, direction=incoming, allow_input=runtime.allow_input)
        self.outgoing = (SOURCE_TO_VIEWER if runtime.role == "source"
                         else VIEWER_TO_SOURCE)
        self._next_seq = {"control": 1, "media": 1, "input": 1}
        self._flow = MediaFlowController() if runtime.role == "source" else None
        self._last_media_received = 0
        self._life = RemoteProxyLifecycle()
        self._life.announce(epoch=runtime.binding.epoch, source_revision=1)

    def run(self) -> None:
        local = socket.socket(fileno=self.runtime.local_fd)
        local.settimeout(IO_TIMEOUT_SECONDS)
        tls = None
        listener = None
        try:
            tls, listener = self._connect_tls()
            peer_pin = (self.runtime.binding.viewer_tls_cert_sha256
                        if self.runtime.role == "source"
                        else self.runtime.binding.source_tls_cert_sha256)
            self._send_local_event(local, {
                "op": "connected", "tls_version": tls.version(),
                "peer_cert_sha256": peer_pin,
            })
            self._event_loop(local, tls)
        except Exception as exc:
            self._send_local_event(local, {
                "op": "error", "message": str(exc),
            }, ignore_errors=True)
            raise
        finally:
            releases = self._life.transport_lost()
            self._send_local_event(local, {
                "op": "detached",
                "releases": [
                    {"kind": item.kind, "code": item.code}
                    for item in releases
                ],
            }, ignore_errors=True)
            if tls is not None:
                tls.close()
            if listener is not None:
                listener.close()
            local.close()

    def _connect_tls(self) -> tuple[ssl.SSLSocket, socket.socket | None]:
        context = build_tls_context(
            role=self.runtime.role, binding=self.runtime.binding,
            local_certificate_pem=self.runtime.local_certificate_pem,
            local_private_key_pem=self.runtime.local_private_key_pem,
            peer_certificate_pem=self.runtime.peer_certificate_pem)
        listener = None
        if self.runtime.role == "source":
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.settimeout(IO_TIMEOUT_SECONDS)
            listener.bind((self.runtime.host, self.runtime.port))
            listener.listen(1)
            raw, _address = listener.accept()
            raw.settimeout(IO_TIMEOUT_SECONDS)
            tls = context.wrap_socket(raw, server_side=True)
            expected = self.runtime.binding.viewer_tls_cert_sha256
        else:
            deadline = time.monotonic() + IO_TIMEOUT_SECONDS
            while True:
                try:
                    raw = socket.create_connection(
                        (self.runtime.host, self.runtime.port), timeout=1.0)
                    break
                except ConnectionRefusedError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
            tls = context.wrap_socket(raw, server_hostname=None)
            expected = self.runtime.binding.source_tls_cert_sha256
        peer_der = tls.getpeercert(binary_form=True)
        verify_tls_peer_certificate(peer_der, expected)
        return tls, listener

    def _event_loop(self, local: socket.socket, tls: ssl.SSLSocket) -> None:
        while True:
            if tls.pending():
                readable = [tls]
            else:
                readable, _, _ = select.select(
                    [local, tls], [], [], IO_TIMEOUT_SECONDS)
            if not readable:
                raise TimeoutError("adapter peer made no progress")
            if local in readable:
                payload = recv_frame(local, maximum=MAX_LOCAL_FRAME_BYTES)
                if payload is None:
                    return
                self._handle_local(local, tls, payload)
            if tls in readable:
                wire = recv_frame(tls, maximum=MAX_WIRE_BYTES)
                if wire is None:
                    return
                self._handle_remote(local, wire)

    def _handle_local(self, local: socket.socket, tls: ssl.SSLSocket,
                      payload: bytes) -> None:
        try:
            doc = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteAdapterError("local adapter request is not JSON") from exc
        if not isinstance(doc, Mapping) or set(doc) != _LOCAL_SEND_FIELDS:
            raise RemoteAdapterError(
                "local adapter request fields do not match schema")
        if doc["op"] != "send":
            raise RemoteAdapterError("unsupported local adapter operation")
        channel = doc["channel"]
        kind = doc["kind"]
        ack = doc["ack"]
        if channel not in self._next_seq:
            raise RemoteAdapterError("invalid local adapter channel")
        if self.runtime.role == "source" and channel not in {"control", "media"}:
            raise RemoteAdapterError("source endpoint cannot send this channel")
        if self.runtime.role == "viewer" and channel not in {"control", "input"}:
            raise RemoteAdapterError("viewer endpoint cannot send this channel")
        if (self.runtime.role == "viewer" and channel == "input"
                and not self.runtime.allow_input):
            raise RemoteAdapterError("viewer endpoint lacks input capability")
        body = _b64decode(
            doc["payload"], label="local adapter payload",
            maximum=CHANNEL_PAYLOAD_LIMITS.get(channel, 0))
        if self.runtime.role == "viewer" and kind == "media_ack":
            if channel != "control" or ack > self._last_media_received:
                raise FlowControlError("media acknowledgement is unsent")
        seq = self._next_seq[channel]
        if channel == "media":
            assert self._flow is not None
            flow_seq = self._flow.offer(body)
            if flow_seq != seq:
                raise FlowControlError("media sequence state diverged")
        wire = self.codec.seal(
            direction=self.outgoing, channel=channel, kind=kind,
            seq=seq, payload=body, ack=ack)
        send_frame(tls, wire, maximum=MAX_WIRE_BYTES)
        self._next_seq[channel] += 1
        self._send_local_event(local, {
            "op": "sent", "channel": channel, "seq": seq,
        })

    def _handle_remote(self, local: socket.socket, wire: bytes) -> None:
        message = self.receiver.receive(wire)
        if (self.runtime.role == "source" and message.channel == "control"
                and message.kind == "media_ack"):
            assert self._flow is not None
            self._flow.acknowledge(message.ack)
        if message.channel == "media":
            self._last_media_received = message.seq
        if self.runtime.role == "source" and message.channel == "input":
            try:
                event = json.loads(message.payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RemoteAdapterError("input payload is not JSON") from exc
            if not isinstance(event, Mapping) or set(event) != {"code", "pressed"}:
                raise RemoteAdapterError("input payload fields do not match schema")
            self._life.record_input(
                epoch=self.runtime.binding.epoch, kind=message.kind,
                code=event["code"], pressed=event["pressed"])
        self._send_local_event(local, {
            "op": "received", "channel": message.channel,
            "kind": message.kind, "seq": message.seq, "ack": message.ack,
            "payload": _b64encode(message.payload),
        })

    @staticmethod
    def _send_local_event(local: socket.socket, event: Mapping, *,
                          ignore_errors: bool = False) -> None:
        payload = json.dumps(event, sort_keys=True,
                             separators=(",", ":")).encode("ascii")
        try:
            send_frame(local, payload, maximum=MAX_LOCAL_FRAME_BYTES)
        except (OSError, RemoteAdapterError):
            if not ignore_errors:
                raise


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - subprocess
    import argparse

    from .mm_remote_session_launcher import read_endpoint_runtime
    from .mm_session_launcher import _read_owned_json_fd

    ap = argparse.ArgumentParser(prog="qdistro-mm-remote-adapter")
    ap.add_argument("--config-fd", type=int, required=True)
    args = ap.parse_args(argv)
    config = _read_owned_json_fd(args.config_fd, "adapter endpoint config")
    runtime = read_endpoint_runtime(config)
    RemoteAdapterEndpoint(runtime).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
