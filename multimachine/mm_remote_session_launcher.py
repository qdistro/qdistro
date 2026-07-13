"""Trusted-FD launcher for one R6 remote-adapter endpoint.

The signed authority grant and raw symmetric key arrive on different inherited
file descriptors.  This launcher verifies the grant against the independently
enrolled authority pin, binds it to the expected local role/machine/session/
stream, closes both input descriptors, and execs the endpoint with one sealed
in-memory configuration fd.  The key never enters argv, environment, or a
filesystem path.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import socket
import time
from dataclasses import asdict
from typing import Callable, Mapping

from .mm_remote_session_authority import RemoteSessionGrantVerifier
from .mm_session_launcher import (
    _read_owned_json_fd,
    _read_pinned_authority_key,
    sealed_config_fd,
)
from .remote_adapter import (
    RemoteAdapterError,
    RemoteSessionBinding,
    RemoteSessionKey,
)
from .remote_adapter_transport import (
    MAX_TLS_PEM_BYTES,
    EndpointRuntime,
    validate_tls_material,
)


SCHEMA = "qdistro-mm-adapter-endpoint-v1"
_CONFIG_FIELDS = frozenset({
    "schema", "role", "allow_input", "binding", "key", "tls",
    "transport", "local_fd",
})
_KEY_FIELDS = frozenset({"key_id", "issued_at", "expires_at", "secret"})
_TLS_FIELDS = frozenset({
    "local_certificate", "local_private_key", "peer_certificate",
})
_TRANSPORT_FIELDS = frozenset({"host", "port"})


def _read_secret_fd(fd: int) -> bytes:
    try:
        with os.fdopen(fd, "rb") as stream:
            secret = stream.read(33)
    except OSError as exc:
        raise ValueError(f"cannot read adapter secret fd: {exc}") from exc
    if len(secret) != 32:
        raise ValueError("adapter secret fd must contain exactly 32 bytes")
    return secret


def _read_pem_fd(fd: int, label: str) -> bytes:
    try:
        with os.fdopen(fd, "rb") as stream:
            payload = stream.read(MAX_TLS_PEM_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"cannot read {label} fd: {exc}") from exc
    if not payload or len(payload) > MAX_TLS_PEM_BYTES:
        raise ValueError(f"{label} PEM is empty or exceeds size limit")
    return payload


def _encode_secret(secret: bytes) -> str:
    return base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")


def _decode_secret(value: object) -> bytes:
    if not isinstance(value, str) or "=" in value:
        raise ValueError("adapter secret must be unpadded base64url")
    try:
        secret = base64.b64decode(value + "=" * (-len(value) % 4),
                                  altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("adapter secret must be unpadded base64url") from exc
    if len(secret) != 32:
        raise ValueError("adapter secret must decode to exactly 32 bytes")
    return secret


def _encode_pem(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_pem(value: object, label: str) -> bytes:
    if not isinstance(value, str) or "=" in value:
        raise ValueError(f"{label} must be unpadded base64url")
    try:
        payload = base64.b64decode(value + "=" * (-len(value) % 4),
                                   altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{label} must be unpadded base64url") from exc
    if not payload or len(payload) > MAX_TLS_PEM_BYTES:
        raise ValueError(f"{label} PEM is empty or exceeds size limit")
    return payload


def parse_endpoint_config(config: object, *,
                          now: Callable[[], float] = time.time,
                          ) -> tuple[str, bool, RemoteSessionBinding,
                                     RemoteSessionKey]:
    """Validate the exact sealed object the endpoint will consume."""
    if not isinstance(config, Mapping) or set(config) != _CONFIG_FIELDS:
        raise ValueError("adapter endpoint config fields do not match schema")
    if config["schema"] != SCHEMA:
        raise ValueError("unsupported adapter endpoint config schema")
    role = config["role"]
    if role not in {"source", "viewer"}:
        raise ValueError("adapter endpoint role must be source or viewer")
    allow_input = config["allow_input"]
    if not isinstance(allow_input, bool):
        raise ValueError("adapter endpoint allow_input must be boolean")
    binding_doc = config["binding"]
    if not isinstance(binding_doc, Mapping):
        raise ValueError("adapter endpoint binding must be an object")
    try:
        binding = RemoteSessionBinding(**binding_doc)
        binding.validate()
    except (TypeError, RemoteAdapterError) as exc:
        raise ValueError(f"invalid adapter endpoint binding: {exc}") from exc
    key_doc = config["key"]
    if not isinstance(key_doc, Mapping) or set(key_doc) != _KEY_FIELDS:
        raise ValueError("adapter endpoint key fields do not match schema")
    try:
        session_key = RemoteSessionKey(
            key_id=key_doc["key_id"],
            secret=_decode_secret(key_doc["secret"]),
            issued_at=key_doc["issued_at"],
            expires_at=key_doc["expires_at"],
        )
    except (KeyError, RemoteAdapterError) as exc:
        raise ValueError(f"invalid adapter endpoint key: {exc}") from exc
    if (binding.key_id != session_key.key_id
            or binding.key_expires_at != session_key.expires_at):
        raise ValueError("adapter endpoint binding/key mismatch")
    session_key.require_active(now=int(now()))
    _parse_runtime_fields(config, role=role, binding=binding)
    return role, allow_input, binding, session_key


def _parse_runtime_fields(config: Mapping, *, role: str,
                          binding: RemoteSessionBinding,
                          ) -> tuple[bytes, bytes, bytes, str, int, int]:
    tls_doc = config["tls"]
    if not isinstance(tls_doc, Mapping) or set(tls_doc) != _TLS_FIELDS:
        raise ValueError("adapter endpoint TLS fields do not match schema")
    local_certificate = _decode_pem(
        tls_doc["local_certificate"], "local TLS certificate")
    local_private_key = _decode_pem(
        tls_doc["local_private_key"], "local TLS private key")
    peer_certificate = _decode_pem(
        tls_doc["peer_certificate"], "peer TLS certificate")
    try:
        validate_tls_material(
            role=role, binding=binding,
            local_certificate_pem=local_certificate,
            local_private_key_pem=local_private_key,
            peer_certificate_pem=peer_certificate)
    except RemoteAdapterError as exc:
        raise ValueError(f"invalid adapter TLS material: {exc}") from exc
    transport = config["transport"]
    if (not isinstance(transport, Mapping)
            or set(transport) != _TRANSPORT_FIELDS):
        raise ValueError("adapter transport fields do not match schema")
    host = transport["host"]
    port = transport["port"]
    if (not isinstance(host, str) or not host or len(host) > 255
            or any(char.isspace() for char in host)):
        raise ValueError("adapter transport host is invalid")
    if (not isinstance(port, int) or isinstance(port, bool)
            or port <= 0 or port > 65535):
        raise ValueError("adapter transport port is invalid")
    local_fd = config["local_fd"]
    if (not isinstance(local_fd, int) or isinstance(local_fd, bool)
            or local_fd < 0):
        raise ValueError("adapter local fd is invalid")
    return (local_certificate, local_private_key, peer_certificate,
            host, port, local_fd)


def read_endpoint_runtime(config: object, *,
                          now: Callable[[], float] = time.time,
                          ) -> EndpointRuntime:
    role, allow_input, binding, session_key = parse_endpoint_config(
        config, now=now)
    assert isinstance(config, Mapping)
    (local_certificate, local_private_key, peer_certificate,
     host, port, local_fd) = _parse_runtime_fields(
         config, role=role, binding=binding)
    return EndpointRuntime(
        role=role, allow_input=allow_input, binding=binding,
        session_key=session_key,
        local_certificate_pem=local_certificate,
        local_private_key_pem=local_private_key,
        peer_certificate_pem=peer_certificate,
        host=host, port=port, local_fd=local_fd)


def build_verified_endpoint_config(
        receipt: object, secret: bytes, authority_public_key: bytes, *,
        role: str, local_machine_id: str, session_id: str, stream_id: str,
        local_certificate_pem: bytes, local_private_key_pem: bytes,
        peer_certificate_pem: bytes, host: str, port: int, local_fd: int,
        now: Callable[[], float] | None = None) -> dict:
    """Verify separate authority/key inputs and construct a sealed snapshot."""
    if not isinstance(secret, bytes) or len(secret) != 32:
        raise ValueError("adapter secret must be exactly 32 bytes")
    verifier_args = {
        "role": role,
        "local_machine_id": local_machine_id,
    }
    if now is not None:
        verifier_args["now"] = now
    verifier = RemoteSessionGrantVerifier.from_public_bytes(
        authority_public_key, **verifier_args)
    grant = verifier.verify(
        receipt, session_id=session_id, stream_id=stream_id)
    secret_digest = hashlib.sha256(secret).hexdigest()
    if not hmac.compare_digest(secret_digest, grant["key_sha256"]):
        raise ValueError("adapter secret does not match signed key digest")
    binding = RemoteSessionBinding(
        source_machine=grant["source_machine"],
        viewer_machine=grant["viewer_machine"],
        trust_domain_id=grant["trust_domain_id"],
        generation=grant["generation"],
        session_id=grant["session_id"],
        stream_id=grant["stream_id"],
        epoch=1,
        key_id=grant["key_id"],
        key_expires_at=grant["key_expires_at"],
        source_tls_cert_sha256=grant["source_tls_cert_sha256"],
        viewer_tls_cert_sha256=grant["viewer_tls_cert_sha256"],
    )
    config = {
        "schema": SCHEMA,
        "role": role,
        "allow_input": grant["allow_input"],
        "binding": asdict(binding),
        "key": {
            "key_id": grant["key_id"],
            "issued_at": grant["issued_at"],
            "expires_at": grant["key_expires_at"],
            "secret": _encode_secret(secret),
        },
        "tls": {
            "local_certificate": _encode_pem(local_certificate_pem),
            "local_private_key": _encode_pem(local_private_key_pem),
            "peer_certificate": _encode_pem(peer_certificate_pem),
        },
        "transport": {"host": host, "port": port},
        "local_fd": local_fd,
    }
    parse_endpoint_config(config, now=now or time.time)
    return config


def exec_endpoint(config: Mapping, endpoint_program: str) -> None:
    """Exec the adapter endpoint with only its sealed config fd in argv."""
    fd = sealed_config_fd(config)
    try:
        os.execvp(endpoint_program,
                  [endpoint_program, "--config-fd", str(fd)])
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - exec shell
    ap = argparse.ArgumentParser(prog="qdistro-mm-remote-session-launcher")
    ap.add_argument("--grant-fd", type=int, required=True)
    ap.add_argument("--secret-fd", type=int, required=True)
    ap.add_argument("--tls-cert-fd", type=int, required=True)
    ap.add_argument("--tls-key-fd", type=int, required=True)
    ap.add_argument("--peer-cert-fd", type=int, required=True)
    ap.add_argument("--local-fd", type=int, required=True)
    ap.add_argument("--role", choices=("source", "viewer"), required=True)
    ap.add_argument("--local-machine-id", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--stream-id", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument(
        "--endpoint-program",
        default="/usr/local/bin/qdistro-mm-remote-adapter")
    args = ap.parse_args(argv)
    inherited_fds = {
        args.grant_fd, args.secret_fd, args.tls_cert_fd, args.tls_key_fd,
        args.peer_cert_fd, args.local_fd,
    }
    if len(inherited_fds) != 6:
        ap.error("all inherited adapter fd arguments must differ")

    try:
        receipt = _read_owned_json_fd(args.grant_fd, "adapter session grant")
        secret = _read_secret_fd(args.secret_fd)
        local_certificate = _read_pem_fd(args.tls_cert_fd, "TLS certificate")
        local_private_key = _read_pem_fd(args.tls_key_fd, "TLS private key")
        peer_certificate = _read_pem_fd(
            args.peer_cert_fd, "peer TLS certificate")
        authority_key = _read_pinned_authority_key()
        duplicate = socket.fromfd(
            args.local_fd, socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            socket_type = duplicate.getsockopt(
                socket.SOL_SOCKET, socket.SO_TYPE)
            if socket_type != socket.SOCK_STREAM:
                raise ValueError("adapter local fd must be a stream socket")
        finally:
            duplicate.close()
        os.set_inheritable(args.local_fd, True)
        config = build_verified_endpoint_config(
            receipt, secret, authority_key, role=args.role,
            local_machine_id=args.local_machine_id, session_id=args.session_id,
            stream_id=args.stream_id,
            local_certificate_pem=local_certificate,
            local_private_key_pem=local_private_key,
            peer_certificate_pem=peer_certificate,
            host=args.host, port=args.port, local_fd=args.local_fd)
        exec_endpoint(config, args.endpoint_program)
    finally:
        # The readers normally close their owned descriptors. Closing both
        # again, while ignoring EBADF, also covers a failure before the second
        # reader starts and prevents a secret fd leaking into an error path.
        for fd in (args.grant_fd, args.secret_fd, args.tls_cert_fd,
                   args.tls_key_fd, args.peer_cert_fd, args.local_fd):
            try:
                os.close(fd)
            except OSError:
                pass
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
