"""Trusted-FD launcher for one multi-machine viewer session.

The pairing authority and stream setup are deliberately separate producers.
The former signs machine/trust-domain grants; the latter holds single-use RDP
OTPs and per-source control capabilities. This launcher accepts the receipt and
stream snapshot only on inherited file descriptors, verifies the receipt
against a root-owned local public-key pin and its viewer/session binding, then
execs ``qdistro-mm-broker`` with an immutable in-memory config fd.

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
import stat
from typing import Mapping

from .mm_broker import parse_session_config
from .mm_pairing_authority import PairingReceiptVerifier


PINNED_AUTHORITY_KEY = "/etc/qdistro/multimachine/pairing-authority.ed25519.pub"
_MAX_JSON_BYTES = 1024 * 1024


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


def combine_verified_session(receipt: object, session: object,
                             authority_public_key: bytes,
                             viewer_machine_id: str, *, now=None) -> dict:
    """Verify an external authority receipt and bind it to stream secrets."""
    if not isinstance(session, Mapping):
        raise ValueError("stream session snapshot must be an object")
    session_id = session.get("pairing_session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("stream session snapshot needs pairing_session_id")
    streams = dict(session)
    del streams["pairing_session_id"]
    kwargs = {"viewer_machine_id": viewer_machine_id}
    if now is not None:
        kwargs["now"] = now
    verifier = PairingReceiptVerifier.from_public_bytes(
        authority_public_key, **kwargs)
    pairing = verifier.verify(receipt, session_id=session_id)
    return combine_session_snapshots(pairing, streams)


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
        with os.fdopen(fd, "rb") as stream:
            payload = stream.read(_MAX_JSON_BYTES + 1)
        if len(payload) > _MAX_JSON_BYTES:
            raise ValueError(f"{label} exceeds {_MAX_JSON_BYTES} bytes")
        return json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} fd: {exc}") from exc


def _read_pinned_authority_key(path: str = PINNED_AUTHORITY_KEY) -> bytes:
    """Read an independently enrolled root-owned Ed25519 trust anchor."""
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open pinned authority key: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("pinned authority key is not a regular file")
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(
                "pinned authority key must be root-owned and not group/other writable")
        raw = os.read(fd, 33)
        if len(raw) != 32:
            raise ValueError("pinned authority key must contain exactly 32 bytes")
        return raw
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - exec shell
    ap = argparse.ArgumentParser(prog="qdistro-mm-session-launcher")
    ap.add_argument("--pairing-fd", type=int, required=True,
                    help="inherited fd containing a signed pairing receipt")
    ap.add_argument("--streams-fd", type=int, required=True,
                    help="inherited fd containing control_host + streams")
    ap.add_argument("--viewer-machine-id", required=True)
    ap.add_argument("--broker-program", default="/usr/local/bin/qdistro-mm-broker")
    args = ap.parse_args(argv)
    if args.pairing_fd == args.streams_fd:
        ap.error("--pairing-fd and --streams-fd must differ")

    receipt = _read_owned_json_fd(args.pairing_fd, "pairing receipt")
    authority_key = _read_pinned_authority_key()
    session = _read_owned_json_fd(args.streams_fd, "stream session")
    combined = combine_verified_session(
        receipt, session, authority_key, args.viewer_machine_id)
    exec_broker(combined, args.broker_program)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
