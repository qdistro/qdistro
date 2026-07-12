"""Trusted-FD launcher for one multi-machine viewer session.

The pairing authority and stream setup are deliberately separate producers:
the former vouches machine/trust-domain grants, while the latter holds
single-use RDP OTPs and per-source control capabilities. This launcher accepts
both snapshots only on inherited file descriptors, combines and validates them,
then execs ``qdistro-mm-broker`` with an immutable in-memory config fd.

No grant, OTP, or control capability is placed in argv, the environment, or a
filesystem path. The external pairing service remains out of scope here; this
is the narrow handoff seam it can invoke after making its policy decision.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
from typing import Mapping

from .mm_broker import parse_session_config


def combine_session_snapshots(pairing: object, session: object) -> dict:
    """Join separate trusted inputs and validate the complete broker snapshot."""
    if not isinstance(pairing, Mapping) or set(pairing) != {"origins"}:
        raise ValueError("pairing snapshot must contain exactly 'origins'")
    if not isinstance(session, Mapping):
        raise ValueError("stream session snapshot must be an object")
    unknown = set(session) - {"control_host", "streams"}
    if unknown:
        raise ValueError(f"unknown stream session fields: {sorted(unknown)!r}")
    if "streams" not in session:
        raise ValueError("stream session snapshot must contain 'streams'")

    combined = {
        "origins": copy.deepcopy(pairing["origins"]),
        "streams": copy.deepcopy(session["streams"]),
    }
    if "control_host" in session:
        combined["control_host"] = session["control_host"]
    # This call is intentionally discarded: it proves the exact raw object that
    # will cross exec is complete and authorized before any broker is started.
    parse_session_config(combined)
    return combined


def sealed_config_fd(config: Mapping) -> int:
    """Serialize *config* into a sealed memfd positioned for broker reading."""
    flags = os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    fd = os.memfd_create("qdistro-mm-session", flags)
    try:
        payload = json.dumps(config, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        os.write(fd, payload)
        os.lseek(fd, 0, os.SEEK_SET)
        seals = (fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK
                 | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
        # memfd_create used CLOEXEC while building the snapshot; deliberately
        # pass this one fd across the final exec after it becomes immutable.
        os.set_inheritable(fd, True)
        return fd
    except Exception:
        os.close(fd)
        raise


def exec_broker(config: Mapping, broker_program: str) -> None:
    """Replace this process with the broker, passing only the sealed fd number."""
    fd = sealed_config_fd(config)
    try:
        os.execvp(broker_program,
                  [broker_program, "--config-fd", str(fd)])
    finally:
        # Reached only when exec fails (or under a test double).
        os.close(fd)


def _read_owned_json_fd(fd: int, label: str) -> object:
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} fd: {exc}") from exc


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - exec shell
    ap = argparse.ArgumentParser(prog="qdistro-mm-session-launcher")
    ap.add_argument("--pairing-fd", type=int, required=True,
                    help="inherited fd containing {'origins': [...]} grants")
    ap.add_argument("--streams-fd", type=int, required=True,
                    help="inherited fd containing control_host + streams")
    ap.add_argument("--broker-program", default="/usr/local/bin/qdistro-mm-broker")
    args = ap.parse_args(argv)
    if args.pairing_fd == args.streams_fd:
        ap.error("--pairing-fd and --streams-fd must differ")

    pairing = _read_owned_json_fd(args.pairing_fd, "pairing snapshot")
    session = _read_owned_json_fd(args.streams_fd, "stream session")
    combined = combine_session_snapshots(pairing, session)
    exec_broker(combined, args.broker_program)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
