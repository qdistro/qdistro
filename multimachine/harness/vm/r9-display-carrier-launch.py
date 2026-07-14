#!/usr/bin/env python3
"""VM-gate adapter from root-owned test files to the trusted-FD launcher.

The files are staging apparatus only.  This helper opens and validates them,
creates the role-appropriate listening socket, and immediately execs the
product launcher with inherited descriptors.  The product carrier therefore
still receives its grant, secret, keys, and listener through the production
sealed-FD boundary.
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
        raise ValueError(f"unsafe carrier test input: {path}")
    os.set_inheritable(fd, True)
    return fd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=("primary", "peer"), required=True)
    ap.add_argument("--bundle", type=Path, required=True)
    args = ap.parse_args()

    with (args.bundle / "grant.json").open("r", encoding="utf-8") as stream:
        payload = json.load(stream)["payload"]
    machine = payload[f"{args.role}_machine"]
    session = payload["session_id"]
    names = ("grant.json", "secret", "local.crt", "local.key", "peer.crt")
    descriptors = [open_owned(args.bundle / name) for name in names]

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if args.role == "primary":
        listener.bind(("0.0.0.0", 3389))
    else:
        listener.bind(("127.0.0.1", 3390))
    listener.listen(1)
    listener.set_inheritable(True)

    launcher = "/tmp/mm/multimachine/qdistro-mm-display-carrier-launcher"
    carrier = "/tmp/mm/multimachine/qdistro-mm-display-carrier"
    command = [
        launcher,
        "--grant-fd", str(descriptors[0]),
        "--secret-fd", str(descriptors[1]),
        "--tls-cert-fd", str(descriptors[2]),
        "--tls-key-fd", str(descriptors[3]),
        "--peer-cert-fd", str(descriptors[4]),
        "--listener-fd", str(listener.fileno()),
        "--role", args.role,
        "--local-machine-id", machine,
        "--session-id", session,
        "--carrier-program", carrier,
    ]
    if args.role == "primary":
        command += [
            "--backend-unix-path", "/run/mm-r9-source/rdp-listener.sock",
        ]
    else:
        command += ["--primary-host", "10.0.2.2", "--primary-port", "3389"]
    os.execv(launcher, command)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
