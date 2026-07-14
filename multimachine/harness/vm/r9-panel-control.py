#!/usr/bin/env python3
"""VM adapter from root-owned fixture files to the panel trusted-FD launcher.

The fixture files and QEMU address are apparatus.  Verification, sealed config,
role confinement, pinned TLS, controller credentials, lease expiry, and systemd
panel-owner switching all run through the product launcher and endpoint.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import stat
from pathlib import Path


def open_owned(path: Path) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o022):
        os.close(fd)
        raise ValueError(f"unsafe panel test input: {path}")
    os.set_inheritable(fd, True)
    return fd


def payload(bundle: Path) -> dict:
    with (bundle / "grant.json").open("r", encoding="utf-8") as stream:
        return json.load(stream)["payload"]


def control_path(bundle: Path) -> str:
    generation = payload(bundle)["generation"]
    return f"/run/qdistro/mm-r9-panel-g{generation}.sock"


def run_endpoint(bundle: Path, role: str, primary_host: str) -> int:
    grant = payload(bundle)
    machine = grant[f"{role}_machine"]
    session = grant["session_id"]
    names = ("grant.json", "secret", "local.crt", "local.key", "peer.crt")
    descriptors = [open_owned(bundle / name) for name in names]
    launcher = "/tmp/mm/multimachine/qdistro-mm-display-panel-launcher"
    endpoint = "/tmp/mm/multimachine/qdistro-mm-display-panel"
    command = [
        launcher,
        "--grant-fd", str(descriptors[0]),
        "--secret-fd", str(descriptors[1]),
        "--tls-cert-fd", str(descriptors[2]),
        "--tls-key-fd", str(descriptors[3]),
        "--peer-cert-fd", str(descriptors[4]),
        "--role", role,
        "--local-machine-id", machine,
        "--session-id", session,
        "--panel-program", endpoint,
    ]
    if role == "primary":
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 3388))
        listener.listen(1)
        listener.set_inheritable(True)
        command += [
            "--listener-fd", str(listener.fileno()),
            "--control-unix-path", control_path(bundle),
            "--control-uid", "0", "--control-gid", "0",
        ]
    else:
        command += [
            "--primary-host", primary_host, "--primary-port", "3388",
            "--local-unit", "mm-r9-local-panel.service",
            "--remote-unit", "mm-r9-rdp.service",
        ]
    os.execv(launcher, command)
    return 1


def run_command(bundle: Path, kind: str) -> int:
    endpoint = "/tmp/mm/multimachine/qdistro-mm-display-panel"
    os.execv(endpoint, [
        endpoint, "--control-unix-path", control_path(bundle),
        "--command", kind,
    ])
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--role", choices=("primary", "peer", "command"), required=True)
    ap.add_argument("--bundle", type=Path, required=True)
    # Retained for compatibility with the durable driver; product state now
    # lives in service units and the controller-only /run/qdistro socket.
    ap.add_argument("--runtime", type=Path)
    ap.add_argument("--primary-host", default="10.0.2.2")
    ap.add_argument(
        "--kind", choices=("reserve", "heartbeat", "release", "status"))
    args = ap.parse_args()
    if args.role == "command":
        if args.kind is None:
            ap.error("--kind is required for command role")
        return run_command(args.bundle, args.kind)
    return run_endpoint(args.bundle, args.role, args.primary_host)


if __name__ == "__main__":
    raise SystemExit(main())
