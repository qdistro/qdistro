#!/usr/bin/env python3
"""Source invariant for a leaseable RDP slot's initial mode."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: test_rdp_initial_mode.py <rdp.c>", file=sys.stderr)
        return 2
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    required = (
        'getenv("QDWIN_RDP_INITIAL_MODE")',
        "if (base->current_mode)",
        'sscanf(requested, "%dx%d%c"',
        "width < 64 || width > 16384",
        "(int64_t)width * height > 67108864",
        "rdp_output_apply_initial_mode(base, mode);",
        "weston_output_set_single_mode(base, mode);",
    )
    missing = [needle for needle in required if needle not in source]
    if missing:
        print("FAIL: missing initial-mode invariants: " + ", ".join(missing))
        return 1
    if source.index("rdp_output_apply_initial_mode(base, mode);") > source.index(
            "weston_output_set_single_mode(base, mode);"):
        print("FAIL: initial mode is applied after mode publication")
        return 1
    print("PASS: trusted launcher seeds one bounded initial RDP mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
