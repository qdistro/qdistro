#!/usr/bin/env python3
"""Source invariant for the authenticated R9 RDP listener boundary.

Weston 14 normally treats ``external_listener_fd`` as an already trusted local
transport and suppresses TLS even when a certificate was explicitly supplied.
FreeRDP 3 then cannot negotiate the qdistro client path, and transport admission
would be the only security layer.  The vendored backend must enable inner RDP
TLS outside the ``fd < 0`` ordinary-listener branch.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: test_rdp_external_listener_tls.py <rdp.c>",
              file=sys.stderr)
        return 2
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    start = source.find("fd = config->external_listener_fd;")
    end = source.find("wl_list_init(&b->peers);", start)
    if start < 0 or end < 0:
        print("FAIL: RDP external-listener security block not found")
        return 1
    block = source[start:end]
    match = re.search(
        r"if\s*\(b->server_cert\s*&&\s*b->server_key\)\s*\{\s*"
        r"b->tls_enabled\s*=\s*1\s*;", block, re.S)
    if not match:
        print("FAIL: explicit cert/key does not enable external-listener TLS")
        return 1
    ordinary = block.find("if (fd < 0)")
    if ordinary < 0 or match.start() < ordinary:
        print("FAIL: malformed external-listener security ordering")
        return 1
    # The TLS-enabling condition must be after the complete fd<0 block, not
    # accidentally nested in it. At that source position brace depth relative
    # to the block must be zero.
    prefix = block[:match.start()]
    depth = prefix.count("{") - prefix.count("}")
    if depth != 0:
        print("FAIL: cert/key TLS enable remains nested under fd < 0")
        return 1
    if "RDP TLS support activated on external listener" not in block:
        print("FAIL: external-listener TLS activation is not audit-logged")
        return 1
    print("PASS: external listener honors explicitly configured inner RDP TLS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
