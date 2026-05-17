#!/usr/bin/env python3
"""Phase 1 acceptance: from a non-admin user, request a permission and
print whether it was allowed. Exits 0 on allow, 1 on deny/timeout.

Default action is `test.action`; override with --action for scenarios
that need distinct pending rows running concurrently. --detail KEY=VAL
is repeatable and replaces the default `purpose=smoke test` payload.
"""
import argparse
import sys

import qdistro_app


def main() -> int:
    ap = argparse.ArgumentParser(prog="qdistro-test-permission")
    ap.add_argument("--action", default="test.action",
                    help="action string (default: test.action)")
    ap.add_argument("--detail", action="append", default=[], metavar="K=V",
                    help="detail key=value pair, repeatable; replaces "
                         "the default {purpose:smoke test} payload")
    args = ap.parse_args()
    if args.detail:
        details = {}
        for pair in args.detail:
            if "=" not in pair:
                ap.error(f"--detail expects K=V, got {pair!r}")
            k, v = pair.split("=", 1)
            details[k] = v
    else:
        details = {"purpose": "smoke test"}
    result = qdistro_app.request(args.action, details=details)
    print("ALLOWED" if result else "DENIED", flush=True)
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
