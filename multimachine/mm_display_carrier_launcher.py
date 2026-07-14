"""Trusted-FD launcher for one signed RDP display-carrier generation.

The authority receipt, one-time carrier secret, TLS identity, and pre-bound
listener arrive on inherited descriptors.  After verification the launcher
execs the byte relay with one immutable memfd.  Secrets and PEM private keys
never appear in argv, the environment, or a filesystem path.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import os
import socket
import time
from dataclasses import asdict, dataclass
from typing import Callable, Mapping

from .display_carrier import (
    MAX_TLS_PEM_BYTES,
    DisplayCarrierBinding,
    DisplayCarrierError,
    DisplayCarrierIdentity,
)
from .mm_display_authority import DisplayGrantVerifier
from .mm_remote_session_launcher import _read_pem_fd, _read_secret_fd
from .mm_session_launcher import (
    _read_owned_json_fd,
    _read_pinned_authority_key,
    sealed_config_fd,
)


SCHEMA = "qdistro-mm-display-carrier-endpoint-v1"
_CONFIG_FIELDS = frozenset({
    "schema", "role", "binding", "secret", "tls", "listener_fd", "target",
})
_TLS_FIELDS = frozenset({
    "local_certificate", "local_private_key", "peer_certificate",
})
_PRIMARY_TARGET_FIELDS = frozenset({"backend_unix_path"})
_PEER_TARGET_FIELDS = frozenset({"primary_host", "primary_port"})


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: object, *, label: str, maximum: int,
            exact: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise ValueError(f"{label} must be unpadded base64url")
    try:
        payload = base64.b64decode(value + "=" * (-len(value) % 4),
                                   altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{label} must be unpadded base64url") from exc
    if len(payload) > maximum or (exact is not None and len(payload) != exact):
        raise ValueError(f"{label} has invalid length")
    return payload


def _validate_listener_fd(fd: object, *, role: str) -> int:
    if not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
        raise ValueError("display carrier listener fd is invalid")
    try:
        duplicate = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"cannot duplicate display listener fd: {exc}") from exc
    try:
        if duplicate.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise ValueError("display carrier listener must be a stream socket")
        if not duplicate.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
            raise ValueError("display carrier listener must already be listening")
        if role == "peer":
            address = duplicate.getsockname()
            if (not isinstance(address, tuple) or not address
                    or address[0] not in {"127.0.0.1", "::1"}):
                raise ValueError(
                    "peer FreeRDP listener must be bound to loopback")
    finally:
        duplicate.close()
    return fd


def _validate_target(target: object, *, role: str) -> dict:
    if not isinstance(target, Mapping):
        raise ValueError("display carrier target must be an object")
    if role == "primary":
        if set(target) != _PRIMARY_TARGET_FIELDS:
            raise ValueError("primary display carrier target fields are invalid")
        path = target["backend_unix_path"]
        if (not isinstance(path, str) or not path.startswith("/run/")
                or len(path) > 107 or "/../" in path or path.endswith("/..")):
            raise ValueError("qdwin private listener path is invalid")
    else:
        if set(target) != _PEER_TARGET_FIELDS:
            raise ValueError("peer display carrier target fields are invalid")
        host = target["primary_host"]
        port = target["primary_port"]
        if (not isinstance(host, str) or not host or len(host) > 255
                or any(char.isspace() for char in host)):
            raise ValueError("primary display carrier host is invalid")
        if (not isinstance(port, int) or isinstance(port, bool)
                or port <= 0 or port > 65535):
            raise ValueError("primary display carrier port is invalid")
    return dict(target)


@dataclass(frozen=True)
class DisplayCarrierRuntime:
    identity: DisplayCarrierIdentity
    listener_fd: int
    target: dict


def parse_carrier_config(config: object, *,
                         now: Callable[[], float] = time.time,
                         ) -> DisplayCarrierRuntime:
    if not isinstance(config, Mapping) or set(config) != _CONFIG_FIELDS:
        raise ValueError("display carrier config fields do not match schema")
    if config["schema"] != SCHEMA:
        raise ValueError("unsupported display carrier endpoint schema")
    role = config["role"]
    if role not in {"primary", "peer"}:
        raise ValueError("display carrier endpoint role is invalid")
    binding_doc = config["binding"]
    if not isinstance(binding_doc, Mapping):
        raise ValueError("display carrier binding must be an object")
    try:
        binding = DisplayCarrierBinding(**binding_doc)
        binding.validate()
    except (TypeError, DisplayCarrierError) as exc:
        raise ValueError(f"invalid display carrier binding: {exc}") from exc
    if now() >= binding.lease_expires_at:
        raise ValueError("display carrier lease has expired")
    secret = _decode(
        config["secret"], label="display carrier secret", maximum=32, exact=32)
    tls = config["tls"]
    if not isinstance(tls, Mapping) or set(tls) != _TLS_FIELDS:
        raise ValueError("display carrier TLS fields do not match schema")
    identity = DisplayCarrierIdentity(
        role=role, binding=binding, secret=secret,
        local_certificate_pem=_decode(
            tls["local_certificate"], label="local TLS certificate",
            maximum=MAX_TLS_PEM_BYTES),
        local_private_key_pem=_decode(
            tls["local_private_key"], label="local TLS private key",
            maximum=MAX_TLS_PEM_BYTES),
        peer_certificate_pem=_decode(
            tls["peer_certificate"], label="peer TLS certificate",
            maximum=MAX_TLS_PEM_BYTES),
    )
    try:
        identity.validate()
    except DisplayCarrierError as exc:
        raise ValueError(f"invalid display carrier identity: {exc}") from exc
    listener_fd = _validate_listener_fd(config["listener_fd"], role=role)
    target = _validate_target(config["target"], role=role)
    return DisplayCarrierRuntime(identity, listener_fd, target)


def build_verified_carrier_config(
        receipt: object, secret: bytes, authority_public_key: bytes, *,
        role: str, local_machine_id: str, session_id: str,
        local_certificate_pem: bytes, local_private_key_pem: bytes,
        peer_certificate_pem: bytes, listener_fd: int,
        backend_unix_path: str | None = None,
        primary_host: str | None = None, primary_port: int | None = None,
        now: Callable[[], float] | None = None) -> dict:
    verifier_args = {"role": role, "local_machine_id": local_machine_id}
    if now is not None:
        verifier_args["now"] = now
    verifier = DisplayGrantVerifier.from_public_bytes(
        authority_public_key, **verifier_args)
    payload = verifier.verify(
        receipt, session_id=session_id, carrier_secret=secret)
    binding = DisplayCarrierBinding.from_verified_payload(payload)
    if role == "primary":
        target = {"backend_unix_path": backend_unix_path}
    else:
        target = {"primary_host": primary_host, "primary_port": primary_port}
    config = {
        "schema": SCHEMA,
        "role": role,
        "binding": asdict(binding),
        "secret": _encode(secret),
        "tls": {
            "local_certificate": _encode(local_certificate_pem),
            "local_private_key": _encode(local_private_key_pem),
            "peer_certificate": _encode(peer_certificate_pem),
        },
        "listener_fd": listener_fd,
        "target": target,
    }
    parse_carrier_config(config, now=now or time.time)
    return config


def exec_carrier(config: Mapping, program: str) -> None:
    listener_fd = config.get("listener_fd")
    if not isinstance(listener_fd, int) or isinstance(listener_fd, bool):
        raise ValueError("display carrier listener fd is invalid")
    fd = sealed_config_fd(config)
    was_inheritable = os.get_inheritable(listener_fd)
    try:
        os.set_inheritable(listener_fd, True)
        os.execvp(program, [program, "--config-fd", str(fd)])
    finally:
        # Reached only when exec fails (or under a test double).
        os.set_inheritable(listener_fd, was_inheritable)
        os.close(fd)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - exec shell
    ap = argparse.ArgumentParser(prog="qdistro-mm-display-carrier-launcher")
    ap.add_argument("--grant-fd", type=int, required=True)
    ap.add_argument("--secret-fd", type=int, required=True)
    ap.add_argument("--tls-cert-fd", type=int, required=True)
    ap.add_argument("--tls-key-fd", type=int, required=True)
    ap.add_argument("--peer-cert-fd", type=int, required=True)
    ap.add_argument("--listener-fd", type=int, required=True)
    ap.add_argument("--role", choices=("primary", "peer"), required=True)
    ap.add_argument("--local-machine-id", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--backend-unix-path")
    ap.add_argument("--primary-host")
    ap.add_argument("--primary-port", type=int)
    ap.add_argument("--carrier-program",
                    default="/usr/local/bin/qdistro-mm-display-carrier")
    args = ap.parse_args(argv)
    inherited = {
        args.grant_fd, args.secret_fd, args.tls_cert_fd, args.tls_key_fd,
        args.peer_cert_fd, args.listener_fd,
    }
    if len(inherited) != 6:
        ap.error("all inherited descriptor numbers must differ")
    receipt = _read_owned_json_fd(args.grant_fd, "display grant")
    secret = _read_secret_fd(args.secret_fd)
    local_certificate = _read_pem_fd(args.tls_cert_fd, "local TLS certificate")
    local_private_key = _read_pem_fd(args.tls_key_fd, "local TLS private key")
    peer_certificate = _read_pem_fd(args.peer_cert_fd, "peer TLS certificate")
    authority_key = _read_pinned_authority_key()
    config = build_verified_carrier_config(
        receipt, secret, authority_key, role=args.role,
        local_machine_id=args.local_machine_id, session_id=args.session_id,
        local_certificate_pem=local_certificate,
        local_private_key_pem=local_private_key,
        peer_certificate_pem=peer_certificate,
        listener_fd=args.listener_fd,
        backend_unix_path=args.backend_unix_path,
        primary_host=args.primary_host, primary_port=args.primary_port)
    exec_carrier(config, args.carrier_program)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
