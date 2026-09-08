#!/usr/bin/env python3
"""Root-side VM gate client for the installed display-dock lifecycle socket."""
from __future__ import annotations

import argparse
import json
import os
import socket

from multimachine.display_dock_service import REQUEST_SCHEMA, encode_secret


def _request(command: str, args: argparse.Namespace) -> dict:
    request = {
        "schema": REQUEST_SCHEMA,
        "request_id": os.urandom(16).hex(),
        "command": command,
    }
    if command == "attach":
        with open(args.receipt, encoding="utf-8") as source:
            receipt = json.load(source)
        with open(args.secret, "rb") as source:
            secret = source.read()
        request.update({
            "receipt": receipt,
            "session_id": args.session_id,
            "carrier_secret": encode_secret(secret),
        })
    elif command == "detach":
        request["generation"] = args.generation
    return request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=(
        "attach", "detach", "reset", "status", "shutdown"))
    ap.add_argument("--path", default="/run/qdistro-mm-display-dock/control.sock")
    ap.add_argument("--receipt")
    ap.add_argument("--secret")
    ap.add_argument("--session-id")
    ap.add_argument("--generation", type=int)
    args = ap.parse_args()
    if args.command == "attach" and not all((
            args.receipt, args.secret, args.session_id)):
        ap.error("attach requires receipt, secret, and session id")
    if args.command == "detach" and args.generation is None:
        ap.error("detach requires generation")

    encoded = (json.dumps(
        _request(args.command, args), sort_keys=True,
        separators=(",", ":")) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(60)
        client.connect(args.path)
        client.sendall(encoded)
        response = client.makefile("r", encoding="utf-8").readline()
    if not response:
        raise RuntimeError("display dock daemon returned no response")
    parsed = json.loads(response)
    print(json.dumps(parsed, sort_keys=True))
    return 0 if parsed.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
