"""Generation-bound mutual-TLS carrier for a leased RDP output slot.

RDP remains end-to-end TLS protected by qdwin and FreeRDP.  This outer
carrier is an admission boundary: both machines present the exact
certificates pinned by a signed display grant and prove possession of the
separately delivered carrier secret before any RDP byte reaches qdwin's
private Unix listener.

The carrier is intentionally a one-connection, one-generation byte relay.
Control heartbeats live on the slot controller channel, never inside this
opaque stream.  Closing either side therefore fails closed and gives the
controller an unambiguous detach signal.
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import os
import select
import socket
import ssl
import struct
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from cryptography import x509
from cryptography.hazmat.primitives import serialization


SCHEMA = "qdistro-mm-display-carrier-v1"
MAX_HANDSHAKE_BYTES = 4096
MAX_TLS_PEM_BYTES = 256 * 1024
COPY_CHUNK_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 15.0
_CLIENT_FIELDS = frozenset({
    "schema", "session_id", "generation", "slot_name", "nonce", "proof",
})
_SERVER_FIELDS = frozenset({"schema", "nonce", "proof"})


class DisplayCarrierError(RuntimeError):
    """Carrier TLS, handoff authentication, or relay invariants failed."""


def _canonical(value: Mapping) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: object, *, label: str, size: int) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise DisplayCarrierError(f"{label} must be unpadded base64url")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4),
                               altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DisplayCarrierError(
            f"{label} must be unpadded base64url") from exc
    if len(raw) != size:
        raise DisplayCarrierError(f"{label} must decode to {size} bytes")
    return raw


def _recv_exact(sock: socket.socket, size: int, *, clean_eof: bool) -> bytes | None:
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            if clean_eof and not result:
                return None
            raise DisplayCarrierError("carrier frame ended early")
        result.extend(chunk)
    return bytes(result)


def _recv_frame(sock: socket.socket) -> bytes:
    header = _recv_exact(sock, 4, clean_eof=False)
    assert header is not None
    size = struct.unpack("!I", header)[0]
    if size == 0 or size > MAX_HANDSHAKE_BYTES:
        raise DisplayCarrierError("carrier handshake length is out of bounds")
    payload = _recv_exact(sock, size, clean_eof=False)
    assert payload is not None
    return payload


def _send_frame(sock: socket.socket, payload: bytes) -> None:
    if not payload or len(payload) > MAX_HANDSHAKE_BYTES:
        raise DisplayCarrierError("carrier handshake payload is out of bounds")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def _parse_json(payload: bytes, *, label: str) -> Mapping:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DisplayCarrierError(f"{label} is not JSON") from exc
    if not isinstance(value, Mapping):
        raise DisplayCarrierError(f"{label} must be an object")
    return value


@dataclass(frozen=True)
class DisplayCarrierBinding:
    authority_key_id: str
    primary_machine: str
    peer_machine: str
    trust_domain_id: str
    generation: int
    session_id: str
    slot_name: str
    carrier_secret_sha256: str
    primary_tls_cert_sha256: str
    peer_tls_cert_sha256: str
    lease_expires_at: int

    @classmethod
    def from_verified_payload(cls, payload: Mapping) -> "DisplayCarrierBinding":
        try:
            binding = cls(**{
                name: payload[name] for name in cls.__dataclass_fields__
            })
        except (KeyError, TypeError) as exc:
            raise DisplayCarrierError(
                "verified display grant lacks carrier binding fields") from exc
        binding.validate()
        return binding

    def validate(self) -> None:
        for name in (
            "authority_key_id", "primary_machine", "peer_machine",
            "trust_domain_id", "session_id", "slot_name",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise DisplayCarrierError(f"display carrier {name} is invalid")
        if self.primary_machine == self.peer_machine:
            raise DisplayCarrierError("display carrier endpoints must differ")
        if (not isinstance(self.generation, int)
                or isinstance(self.generation, bool) or self.generation <= 0):
            raise DisplayCarrierError("display carrier generation is invalid")
        if (not isinstance(self.lease_expires_at, int)
                or isinstance(self.lease_expires_at, bool)
                or self.lease_expires_at <= 0):
            raise DisplayCarrierError("display carrier lease expiry is invalid")
        for name in (
            "carrier_secret_sha256", "primary_tls_cert_sha256",
            "peer_tls_cert_sha256",
        ):
            value = getattr(self, name)
            if (not isinstance(value, str) or len(value) != 64
                    or any(c not in "0123456789abcdef" for c in value)):
                raise DisplayCarrierError(f"display carrier {name} is invalid")

    def canonical(self) -> bytes:
        return _canonical({
            name: getattr(self, name) for name in self.__dataclass_fields__
        })


def _binding_digest(binding: DisplayCarrierBinding) -> bytes:
    return hashlib.sha256(binding.canonical()).digest()


def _proof(secret: bytes, label: bytes, *parts: bytes) -> bytes:
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise DisplayCarrierError("display carrier secret must be 32 bytes")
    message = b"qdistro-display-carrier-v1\0" + label
    for part in parts:
        message += struct.pack("!I", len(part)) + part
    return hmac.new(secret, message, hashlib.sha256).digest()


def authenticate_peer(sock: socket.socket, *, binding: DisplayCarrierBinding,
                      secret: bytes, nonce_factory: Callable[[int], bytes] = os.urandom,
                      ) -> None:
    """Run the peer half of the post-TLS, grant-bound handoff proof."""
    nonce = nonce_factory(32)
    if not isinstance(nonce, bytes) or len(nonce) != 32:
        raise DisplayCarrierError("carrier nonce source returned invalid data")
    digest = _binding_digest(binding)
    hello = {
        "schema": SCHEMA,
        "session_id": binding.session_id,
        "generation": binding.generation,
        "slot_name": binding.slot_name,
        "nonce": _b64encode(nonce),
        "proof": _b64encode(_proof(secret, b"peer", digest, nonce)),
    }
    _send_frame(sock, _canonical(hello))
    response = _parse_json(_recv_frame(sock), label="carrier primary response")
    if set(response) != _SERVER_FIELDS or response.get("schema") != SCHEMA:
        raise DisplayCarrierError("carrier primary response fields are invalid")
    server_nonce = _b64decode(
        response.get("nonce"), label="carrier primary nonce", size=32)
    actual = _b64decode(
        response.get("proof"), label="carrier primary proof", size=32)
    expected = _proof(secret, b"primary", digest, nonce, server_nonce)
    if not hmac.compare_digest(actual, expected):
        raise DisplayCarrierError("carrier primary secret proof is invalid")


def authenticate_primary(sock: socket.socket, *, binding: DisplayCarrierBinding,
                         secret: bytes, used_peer_nonces: set[bytes],
                         nonce_factory: Callable[[int], bytes] = os.urandom,
                         ) -> None:
    """Validate the peer handoff proof and send the primary proof."""
    hello = _parse_json(_recv_frame(sock), label="carrier peer hello")
    if set(hello) != _CLIENT_FIELDS or hello.get("schema") != SCHEMA:
        raise DisplayCarrierError("carrier peer hello fields are invalid")
    if (hello.get("session_id") != binding.session_id
            or hello.get("generation") != binding.generation
            or hello.get("slot_name") != binding.slot_name):
        raise DisplayCarrierError("carrier peer hello targets another grant")
    nonce = _b64decode(hello.get("nonce"), label="carrier peer nonce", size=32)
    if nonce in used_peer_nonces:
        raise DisplayCarrierError("carrier peer nonce was replayed")
    actual = _b64decode(hello.get("proof"), label="carrier peer proof", size=32)
    digest = _binding_digest(binding)
    expected = _proof(secret, b"peer", digest, nonce)
    if not hmac.compare_digest(actual, expected):
        raise DisplayCarrierError("carrier peer secret proof is invalid")
    # Consume the nonce before replying.  A failed response cannot make the
    # same captured admission message usable on a later connection.
    used_peer_nonces.add(nonce)
    server_nonce = nonce_factory(32)
    if not isinstance(server_nonce, bytes) or len(server_nonce) != 32:
        raise DisplayCarrierError("carrier nonce source returned invalid data")
    response = {
        "schema": SCHEMA,
        "nonce": _b64encode(server_nonce),
        "proof": _b64encode(
            _proof(secret, b"primary", digest, nonce, server_nonce)),
    }
    _send_frame(sock, _canonical(response))


def _certificate_der(pem: bytes, *, label: str) -> bytes:
    if not isinstance(pem, bytes) or not pem or len(pem) > MAX_TLS_PEM_BYTES:
        raise DisplayCarrierError(f"{label} PEM is invalid")
    try:
        certificate = x509.load_pem_x509_certificate(pem)
    except ValueError as exc:
        raise DisplayCarrierError(f"{label} PEM is invalid") from exc
    return certificate.public_bytes(serialization.Encoding.DER)


def _verify_pin(der: bytes | None, expected: str, *, label: str) -> None:
    if der is None or not hmac.compare_digest(
            hashlib.sha256(der).hexdigest(), expected):
        raise DisplayCarrierError(f"{label} certificate pin mismatch")


def validate_tls_material(*, role: str, binding: DisplayCarrierBinding,
                          local_certificate_pem: bytes,
                          local_private_key_pem: bytes,
                          peer_certificate_pem: bytes) -> None:
    if role not in {"primary", "peer"}:
        raise DisplayCarrierError("display carrier role is invalid")
    local_pin = (binding.primary_tls_cert_sha256 if role == "primary"
                 else binding.peer_tls_cert_sha256)
    peer_pin = (binding.peer_tls_cert_sha256 if role == "primary"
                else binding.primary_tls_cert_sha256)
    local_der = _certificate_der(local_certificate_pem, label="local TLS certificate")
    peer_der = _certificate_der(peer_certificate_pem, label="peer TLS certificate")
    _verify_pin(local_der, local_pin, label="local TLS")
    _verify_pin(peer_der, peer_pin, label="peer TLS")
    if (not isinstance(local_private_key_pem, bytes)
            or not local_private_key_pem
            or len(local_private_key_pem) > MAX_TLS_PEM_BYTES):
        raise DisplayCarrierError("local TLS private key PEM is invalid")
    try:
        private_key = serialization.load_pem_private_key(
            local_private_key_pem, password=None)
    except (TypeError, ValueError) as exc:
        raise DisplayCarrierError("local TLS private key PEM is invalid") from exc
    certificate = x509.load_der_x509_certificate(local_der)
    cert_public = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    key_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    if not hmac.compare_digest(cert_public, key_public):
        raise DisplayCarrierError(
            "local TLS private key does not match certificate")


def _sealed_pem_fd(name: str, payload: bytes) -> int:
    fd = os.memfd_create(name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
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


def build_tls_context(*, role: str, binding: DisplayCarrierBinding,
                      local_certificate_pem: bytes,
                      local_private_key_pem: bytes,
                      peer_certificate_pem: bytes) -> ssl.SSLContext:
    validate_tls_material(
        role=role, binding=binding,
        local_certificate_pem=local_certificate_pem,
        local_private_key_pem=local_private_key_pem,
        peer_certificate_pem=peer_certificate_pem)
    purpose = ssl.Purpose.CLIENT_AUTH if role == "primary" else ssl.Purpose.SERVER_AUTH
    context = ssl.create_default_context(purpose)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    try:
        context.load_verify_locations(cadata=peer_certificate_pem.decode("ascii"))
    except (UnicodeDecodeError, ssl.SSLError) as exc:
        raise DisplayCarrierError("peer TLS certificate cannot be trusted") from exc
    cert_fd = _sealed_pem_fd("qdistro-display-carrier-cert", local_certificate_pem)
    key_fd = _sealed_pem_fd("qdistro-display-carrier-key", local_private_key_pem)
    try:
        context.load_cert_chain(
            certfile=f"/proc/self/fd/{cert_fd}",
            keyfile=f"/proc/self/fd/{key_fd}")
    except ssl.SSLError as exc:
        raise DisplayCarrierError("cannot load local TLS identity") from exc
    finally:
        os.close(cert_fd)
        os.close(key_fd)
    return context


@dataclass(frozen=True)
class RelayResult:
    closed_by: str
    left_to_right: int
    right_to_left: int


def relay_streams(left: socket.socket, right: socket.socket, *,
                  lease_expires_at: int,
                  clock: Callable[[], float] = time.time,
                  stop_requested: Callable[[], bool] | None = None,
                  ) -> RelayResult:
    """Relay bounded chunks until EOF or lease expiry; retain no stale pixels."""
    if clock() >= lease_expires_at:
        raise DisplayCarrierError("display carrier lease has expired")
    counters = {left: 0, right: 0}
    while True:
        if stop_requested is not None and stop_requested():
            return RelayResult(
                "controller-close", counters[left], counters[right])
        remaining = lease_expires_at - clock()
        if remaining <= 0:
            return RelayResult("lease-expired", counters[left], counters[right])
        poll = 0.1 if stop_requested is not None else 1.0
        readable, _, _ = select.select(
            [left, right], [], [], min(poll, remaining))
        for source in readable:
            target = right if source is left else left
            try:
                payload = source.recv(COPY_CHUNK_BYTES)
            except ssl.SSLWantReadError:
                continue
            except (ConnectionResetError, ConnectionAbortedError,
                    ssl.SSLEOFError):
                closed_by = "left" if source is left else "right"
                return RelayResult(
                    closed_by, counters[left], counters[right])
            if not payload:
                closed_by = "left" if source is left else "right"
                return RelayResult(closed_by, counters[left], counters[right])
            try:
                target.sendall(payload)
            except (BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError, ssl.SSLEOFError):
                closed_by = "left" if target is left else "right"
                return RelayResult(
                    closed_by, counters[left], counters[right])
            counters[source] += len(payload)


def wrap_authenticated_tls(raw: socket.socket, *, role: str,
                           binding: DisplayCarrierBinding, secret: bytes,
                           local_certificate_pem: bytes,
                           local_private_key_pem: bytes,
                           peer_certificate_pem: bytes,
                           used_peer_nonces: set[bytes] | None = None,
                           ) -> ssl.SSLSocket:
    """Establish TLS 1.3, enforce the exact leaf pin, then prove the grant secret."""
    context = build_tls_context(
        role=role, binding=binding,
        local_certificate_pem=local_certificate_pem,
        local_private_key_pem=local_private_key_pem,
        peer_certificate_pem=peer_certificate_pem)
    tls = context.wrap_socket(raw, server_side=role == "primary",
                              server_hostname=None)
    expected = (binding.peer_tls_cert_sha256 if role == "primary"
                else binding.primary_tls_cert_sha256)
    try:
        _verify_pin(tls.getpeercert(binary_form=True), expected, label="live peer TLS")
        if role == "primary":
            authenticate_primary(
                tls, binding=binding, secret=secret,
                used_peer_nonces=(used_peer_nonces
                                  if used_peer_nonces is not None else set()))
        else:
            authenticate_peer(tls, binding=binding, secret=secret)
        return tls
    except Exception:
        tls.close()
        raise


@dataclass(frozen=True)
class DisplayCarrierIdentity:
    role: str
    binding: DisplayCarrierBinding
    secret: bytes
    local_certificate_pem: bytes
    local_private_key_pem: bytes
    peer_certificate_pem: bytes

    def validate(self) -> None:
        self.binding.validate()
        if not hmac.compare_digest(
                hashlib.sha256(self.secret).hexdigest(),
                self.binding.carrier_secret_sha256):
            raise DisplayCarrierError("display carrier secret digest mismatch")
        validate_tls_material(
            role=self.role, binding=self.binding,
            local_certificate_pem=self.local_certificate_pem,
            local_private_key_pem=self.local_private_key_pem,
            peer_certificate_pem=self.peer_certificate_pem)


def serve_primary_once(identity: DisplayCarrierIdentity, *,
                       listener: socket.socket, backend_unix_path: str,
                       clock: Callable[[], float] = time.time,
                       on_ready: Callable[[], None] | None = None,
                       stop_requested: Callable[[], bool] | None = None,
                       ) -> RelayResult:
    """Admit one peer, connect qdwin's private listener, and relay until detach."""
    identity.validate()
    if identity.role != "primary":
        raise DisplayCarrierError("primary carrier requires primary identity")
    listener.settimeout(CONNECT_TIMEOUT_SECONDS)
    raw, _address = listener.accept()
    # accept() returns a blocking socket on modern Python even when the
    # listener has a timeout. Bound the unauthenticated TLS handshake too, so
    # a connect-and-stall peer cannot pin this sealed generation forever.
    raw.settimeout(CONNECT_TIMEOUT_SECONDS)
    tls = None
    backend = None
    try:
        tls = wrap_authenticated_tls(
            raw, role="primary", binding=identity.binding,
            secret=identity.secret,
            local_certificate_pem=identity.local_certificate_pem,
            local_private_key_pem=identity.local_private_key_pem,
            peer_certificate_pem=identity.peer_certificate_pem,
            used_peer_nonces=set())
        backend = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        backend.settimeout(CONNECT_TIMEOUT_SECONDS)
        backend.connect(backend_unix_path)
        backend.settimeout(None)
        tls.settimeout(None)
        if on_ready is not None:
            on_ready()
        return relay_streams(
            tls, backend, lease_expires_at=identity.binding.lease_expires_at,
            clock=clock, stop_requested=stop_requested)
    finally:
        if backend is not None:
            backend.close()
        if tls is not None:
            tls.close()
        else:
            raw.close()


def serve_peer_once(identity: DisplayCarrierIdentity, *,
                    local_listener: socket.socket, primary_host: str,
                    primary_port: int,
                    clock: Callable[[], float] = time.time,
                    on_authenticated: Callable[[], None] | None = None,
                    ) -> RelayResult:
    """Accept one local FreeRDP client and carry it to the paired primary."""
    identity.validate()
    if identity.role != "peer":
        raise DisplayCarrierError("peer carrier requires peer identity")
    local = None
    raw = None
    tls = None
    try:
        raw = socket.create_connection(
            (primary_host, primary_port), timeout=CONNECT_TIMEOUT_SECONDS)
        tls = wrap_authenticated_tls(
            raw, role="peer", binding=identity.binding,
            secret=identity.secret,
            local_certificate_pem=identity.local_certificate_pem,
            local_private_key_pem=identity.local_private_key_pem,
            peer_certificate_pem=identity.peer_certificate_pem)
        if on_authenticated is not None:
            on_authenticated()
        local_listener.settimeout(CONNECT_TIMEOUT_SECONDS)
        local, _address = local_listener.accept()
        local.settimeout(None)
        tls.settimeout(None)
        return relay_streams(
            local, tls, lease_expires_at=identity.binding.lease_expires_at,
            clock=clock)
    finally:
        if local is not None:
            local.close()
        if tls is not None:
            tls.close()
        elif raw is not None:
            raw.close()
