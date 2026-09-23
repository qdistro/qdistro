"""
qdwin CFFI smoke test — Phase 6.0 spike, Day 3.

Proves the generated binding loads and can round-trip a real call into
libweston-16. Not a unit test; runs once at install time to fail loud
if the generated .so is broken.

Expected output:

    qdwin-smoke: libweston version 14.0.2
    qdwin-smoke: weston_log_ctx_create returned non-null pointer
    qdwin-smoke: round-trip OK

Exit 0 on success, non-zero on any failure.
"""

from __future__ import annotations

import sys

# Import the locally-generated module next to this file.
from qdwin.backend.cffi import _qdwin_ffi  # type: ignore[attr-defined]


def main() -> int:
    lib = _qdwin_ffi.lib
    ffi = _qdwin_ffi.ffi

    major = ffi.new("int*")
    minor = ffi.new("int*")
    micro = ffi.new("int*")
    lib.weston_version(major, minor, micro)
    version = f"{major[0]}.{minor[0]}.{micro[0]}"
    print(f"qdwin-smoke: libweston version {version}")

    if (major[0], minor[0]) < (14, 0):
        print(f"qdwin-smoke: unexpected libweston version {version}",
              file=sys.stderr)
        return 1

    ctx = lib.weston_log_ctx_create()
    if ctx == ffi.NULL:
        print("qdwin-smoke: weston_log_ctx_create returned NULL",
              file=sys.stderr)
        return 1
    print("qdwin-smoke: weston_log_ctx_create returned non-null pointer")

    lib.weston_log_ctx_destroy(ctx)
    print("qdwin-smoke: round-trip OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
