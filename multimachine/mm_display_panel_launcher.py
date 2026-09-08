"""Trusted-FD launcher for one authenticated peer-panel lease generation.

The root broker verifies the signed display grant and TLS material, then execs
one inert endpoint with a sealed config memfd.  The primary endpoint receives a
pre-bound network listener plus a controller-only Unix path.  The peer endpoint
receives only the primary address and fixed local/remote systemd unit names.
"""
from __future__ import annotations

import argparse
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from .display_carrier import (
    MAX_TLS_PEM_BYTES,
    DisplayCarrierBinding,
    DisplayCarrierError,
    DisplayCarrierIdentity,
)
from .display_panel_agent import PanelLeaseSpec
from .mm_display_authority import DisplayGrantVerifier
from .mm_display_carrier_launcher import (
    _decode,
    _encode,
    _validate_listener_fd,
)
from .mm_remote_session_launcher import _read_pem_fd, _read_secret_fd
from .mm_session_launcher import (
    _read_owned_json_fd,
    _read_pinned_authority_key,
    sealed_config_fd,
)
from .remote_display_slot import DisplaySlotError


SCHEMA = "qdistro-mm-display-panel-endpoint-v1"
_CONFIG_FIELDS = frozenset({
    "schema", "role", "grant", "secret", "tls", "listener_fd", "target",
})
_TLS_FIELDS = frozenset({
    "local_certificate", "local_private_key", "peer_certificate",
})
_PRIMARY_TARGET_FIELDS = frozenset({
    "control_unix_path", "control_uid", "control_gid",
})
_PEER_TARGET_FIELDS = frozenset({
    "primary_host", "primary_port", "local_unit", "remote_unit",
})
_UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,126}\.service\Z")


@dataclass(frozen=True)
class DisplayPanelRuntime:
    identity: DisplayCarrierIdentity
    lease: PanelLeaseSpec
    listener_fd: int | None
    target: dict


def _validate_control_path(value: object) -> str:
    if (not isinstance(value, str)
            or not value.startswith("/run/qdistro/")
            or len(value.encode()) > 107
            or "/../" in value
            or value.endswith("/..")):
        raise ValueError("panel controller Unix path is invalid")
    return value


def _validate_id(value: object, *, label: str) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < 0 or value > 2**31 - 1):
        raise ValueError(f"panel controller {label} is invalid")
    return value


def _validate_unit(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _UNIT_RE.fullmatch(value) is None:
        raise ValueError(f"panel {label} systemd unit is invalid")
    return value


def _validate_host(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 255
            or any(char.isspace() for char in value)):
        raise ValueError("primary panel-control host is invalid")
    return value


def _validate_port(value: object) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or value <= 0 or value > 65535):
        raise ValueError("primary panel-control port is invalid")
    return value


def _validate_target(target: object, *, role: str) -> dict:
    if not isinstance(target, Mapping):
        raise ValueError("display panel target must be an object")
    if role == "primary":
        if set(target) != _PRIMARY_TARGET_FIELDS:
            raise ValueError("primary display panel target fields are invalid")
        return {
            "control_unix_path": _validate_control_path(
                target["control_unix_path"]),
            "control_uid": _validate_id(target["control_uid"], label="uid"),
            "control_gid": _validate_id(target["control_gid"], label="gid"),
        }
    if set(target) != _PEER_TARGET_FIELDS:
        raise ValueError("peer display panel target fields are invalid")
    local_unit = _validate_unit(target["local_unit"], label="local")
    remote_unit = _validate_unit(target["remote_unit"], label="remote")
    if local_unit == remote_unit:
        raise ValueError("panel local and remote units must differ")
    return {
        "primary_host": _validate_host(target["primary_host"]),
        "primary_port": _validate_port(target["primary_port"]),
        "local_unit": local_unit,
        "remote_unit": remote_unit,
    }


def parse_panel_config(config: object, *,
                       now: Callable[[], float] = time.time,
                       ) -> DisplayPanelRuntime:
    if not isinstance(config, Mapping) or set(config) != _CONFIG_FIELDS:
        raise ValueError("display panel config fields do not match schema")
    if config["schema"] != SCHEMA:
        raise ValueError("unsupported display panel endpoint schema")
    role = config["role"]
    if role not in {"primary", "peer"}:
        raise ValueError("display panel endpoint role is invalid")
    grant = config["grant"]
    if not isinstance(grant, Mapping):
        raise ValueError("display panel grant must be an object")
    try:
        binding = DisplayCarrierBinding.from_verified_payload(grant)
        lease = PanelLeaseSpec.from_verified_payload(grant)
    except (TypeError, ValueError, DisplayCarrierError,
            DisplaySlotError) as exc:
        raise ValueError(f"invalid display panel grant: {exc}") from exc
    if now() >= lease.lease_expires_at:
        raise ValueError("display panel lease has expired")
    secret = _decode(
        config["secret"], label="display panel secret", maximum=32, exact=32)
    tls = config["tls"]
    if not isinstance(tls, Mapping) or set(tls) != _TLS_FIELDS:
        raise ValueError("display panel TLS fields do not match schema")
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
        raise ValueError(f"invalid display panel identity: {exc}") from exc
    listener_fd = config["listener_fd"]
    if role == "primary":
        listener_fd = _validate_listener_fd(listener_fd, role="primary")
    elif listener_fd is not None:
        raise ValueError("peer panel endpoint cannot inherit a listener")
    target = _validate_target(config["target"], role=role)
    return DisplayPanelRuntime(identity, lease, listener_fd, target)


def build_verified_panel_config(
        receipt: object, secret: bytes, authority_public_key: bytes, *,
        role: str, local_machine_id: str, session_id: str,
        local_certificate_pem: bytes, local_private_key_pem: bytes,
        peer_certificate_pem: bytes, listener_fd: int | None = None,
        control_unix_path: str | None = None, control_uid: int | None = None,
        control_gid: int | None = None, primary_host: str | None = None,
        primary_port: int | None = None, local_unit: str | None = None,
        remote_unit: str | None = None,
        now: Callable[[], float] | None = None) -> dict:
    verifier_args = {"role": role, "local_machine_id": local_machine_id}
    if now is not None:
        verifier_args["now"] = now
    payload = DisplayGrantVerifier.from_public_bytes(
        authority_public_key, **verifier_args).verify(
            receipt, session_id=session_id, carrier_secret=secret)
    if role == "primary":
        target = {
            "control_unix_path": control_unix_path,
            "control_uid": control_uid,
            "control_gid": control_gid,
        }
    else:
        target = {
            "primary_host": primary_host,
            "primary_port": primary_port,
            "local_unit": local_unit,
            "remote_unit": remote_unit,
        }
    config = {
        "schema": SCHEMA,
        "role": role,
        "grant": dict(payload),
        "secret": _encode(secret),
        "tls": {
            "local_certificate": _encode(local_certificate_pem),
            "local_private_key": _encode(local_private_key_pem),
            "peer_certificate": _encode(peer_certificate_pem),
        },
        "listener_fd": listener_fd,
        "target": target,
    }
    parse_panel_config(config, now=now or time.time)
    return config


def exec_panel(config: Mapping, program: str) -> None:
    listener_fd = config.get("listener_fd")
    fd = sealed_config_fd(config)
    restore: bool | None = None
    try:
        if isinstance(listener_fd, int) and not isinstance(listener_fd, bool):
            restore = os.get_inheritable(listener_fd)
            os.set_inheritable(listener_fd, True)
        os.execvp(program, [program, "--config-fd", str(fd)])
    finally:
        if restore is not None:
            os.set_inheritable(listener_fd, restore)
        os.close(fd)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - exec shell
    ap = argparse.ArgumentParser(prog="qdistro-mm-display-panel-launcher")
    ap.add_argument("--grant-fd", type=int, required=True)
    ap.add_argument("--secret-fd", type=int, required=True)
    ap.add_argument("--tls-cert-fd", type=int, required=True)
    ap.add_argument("--tls-key-fd", type=int, required=True)
    ap.add_argument("--peer-cert-fd", type=int, required=True)
    ap.add_argument("--listener-fd", type=int)
    ap.add_argument("--role", choices=("primary", "peer"), required=True)
    ap.add_argument("--local-machine-id", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--control-unix-path")
    ap.add_argument("--control-uid", type=int)
    ap.add_argument("--control-gid", type=int)
    ap.add_argument("--primary-host")
    ap.add_argument("--primary-port", type=int)
    ap.add_argument("--local-unit")
    ap.add_argument("--remote-unit")
    ap.add_argument("--panel-program",
                    default="/usr/local/bin/qdistro-mm-display-panel")
    args = ap.parse_args(argv)
    inherited = [
        args.grant_fd, args.secret_fd, args.tls_cert_fd, args.tls_key_fd,
        args.peer_cert_fd,
    ]
    if args.listener_fd is not None:
        inherited.append(args.listener_fd)
    if len(set(inherited)) != len(inherited):
        ap.error("all inherited descriptor numbers must differ")
    receipt = _read_owned_json_fd(args.grant_fd, "display grant")
    secret = _read_secret_fd(args.secret_fd)
    local_certificate = _read_pem_fd(args.tls_cert_fd, "local TLS certificate")
    local_private_key = _read_pem_fd(args.tls_key_fd, "local TLS private key")
    peer_certificate = _read_pem_fd(args.peer_cert_fd, "peer TLS certificate")
    config = build_verified_panel_config(
        receipt, secret, _read_pinned_authority_key(), role=args.role,
        local_machine_id=args.local_machine_id, session_id=args.session_id,
        local_certificate_pem=local_certificate,
        local_private_key_pem=local_private_key,
        peer_certificate_pem=peer_certificate,
        listener_fd=args.listener_fd,
        control_unix_path=args.control_unix_path,
        control_uid=args.control_uid, control_gid=args.control_gid,
        primary_host=args.primary_host, primary_port=args.primary_port,
        local_unit=args.local_unit, remote_unit=args.remote_unit)
    exec_panel(config, args.panel_program)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
