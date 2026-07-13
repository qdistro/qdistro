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


SCHEMA = "qdistro-mm-adapter-endpoint-v1"
_CONFIG_FIELDS = frozenset({
    "schema", "role", "allow_input", "binding", "key",
})
_KEY_FIELDS = frozenset({"key_id", "issued_at", "expires_at", "secret"})


def _read_secret_fd(fd: int) -> bytes:
    try:
        with os.fdopen(fd, "rb") as stream:
            secret = stream.read(33)
    except OSError as exc:
        raise ValueError(f"cannot read adapter secret fd: {exc}") from exc
    if len(secret) != 32:
        raise ValueError("adapter secret fd must contain exactly 32 bytes")
    return secret


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
    return role, allow_input, binding, session_key


def build_verified_endpoint_config(
        receipt: object, secret: bytes, authority_public_key: bytes, *,
        role: str, local_machine_id: str, session_id: str, stream_id: str,
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
    ap.add_argument("--role", choices=("source", "viewer"), required=True)
    ap.add_argument("--local-machine-id", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--stream-id", required=True)
    ap.add_argument(
        "--endpoint-program",
        default="/usr/local/bin/qdistro-mm-remote-adapter")
    args = ap.parse_args(argv)
    if args.grant_fd == args.secret_fd:
        ap.error("--grant-fd and --secret-fd must differ")

    try:
        receipt = _read_owned_json_fd(args.grant_fd, "adapter session grant")
        secret = _read_secret_fd(args.secret_fd)
        authority_key = _read_pinned_authority_key()
        config = build_verified_endpoint_config(
            receipt, secret, authority_key, role=args.role,
            local_machine_id=args.local_machine_id, session_id=args.session_id,
            stream_id=args.stream_id)
        exec_endpoint(config, args.endpoint_program)
    finally:
        # The readers normally close their owned descriptors. Closing both
        # again, while ignoring EBADF, also covers a failure before the second
        # reader starts and prevents a secret fd leaking into an error path.
        for fd in (args.grant_fd, args.secret_fd):
            try:
                os.close(fd)
            except OSError:
                pass
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
