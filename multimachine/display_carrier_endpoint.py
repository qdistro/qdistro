"""Sealed-config endpoint for one R9 RDP display-carrier generation."""
from __future__ import annotations

import argparse
import json
import socket

from .display_carrier import serve_peer_once, serve_primary_once
from .mm_display_carrier_launcher import parse_carrier_config
from .mm_session_launcher import _read_owned_json_fd


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - subprocess
    ap = argparse.ArgumentParser(prog="qdistro-mm-display-carrier")
    ap.add_argument("--config-fd", type=int, required=True)
    args = ap.parse_args(argv)
    runtime = parse_carrier_config(
        _read_owned_json_fd(args.config_fd, "display carrier config"))
    listener = socket.socket(fileno=runtime.listener_fd)
    try:
        if runtime.identity.role == "primary":
            result = serve_primary_once(
                runtime.identity, listener=listener,
                backend_unix_path=runtime.target["backend_unix_path"])
        else:
            result = serve_peer_once(
                runtime.identity, local_listener=listener,
                primary_host=runtime.target["primary_host"],
                primary_port=runtime.target["primary_port"])
        print(json.dumps({
            "closed_by": result.closed_by,
            "left_to_right": result.left_to_right,
            "right_to_left": result.right_to_left,
        }, sort_keys=True), flush=True)
        return 0
    finally:
        listener.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
