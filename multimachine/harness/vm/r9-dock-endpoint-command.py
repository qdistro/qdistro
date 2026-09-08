#!/usr/bin/env python3
"""VM-gate client for the installed dock agent's exact Unix RPC."""
from __future__ import annotations

import argparse
import json

from multimachine.display_dock_rpc import JsonEndpointClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=("peer-panel", "carrier"), required=True)
    ap.add_argument("--path", required=True)
    ap.add_argument("--action", required=True)
    ap.add_argument("--generation", type=int, required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--slot-name", default="rdp-0")
    args = ap.parse_args()
    result = JsonEndpointClient(
        args.path, role=args.role, timeout=30).request(args.action, {
            "generation": args.generation,
            "session_id": args.session_id,
            "slot_name": args.slot_name,
        })
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
