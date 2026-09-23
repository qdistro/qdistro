"""
qdwin internal CFFI binding — Phase 6.0 spike, Day 3.

Install-time binding against /usr/lib64/libweston-16.so. NOT a public
SDK: consumers are qdwin internals only. The module is regenerated on
every install so the generated glue tracks whatever libweston version
the distro ships — no vendored .so, no committed generated code.

Invocation (install-time, from bootstrap-qdwin-in-vm.sh):

    python3 -m qdwin.backend.cffi.build

or standalone:

    python3 /path/to/qdwin/backend/cffi/build.py

Output: _qdwin_ffi*.so next to this file. Tests import as
`from qdwin.backend.cffi import _qdwin_ffi`.

The CDEF surface is intentionally minimal for the spike — just enough
to prove round-trip from Python into a real libweston symbol. The
Phase-6.1 port grows this to cover weston_compositor access, layer
walking, view inspection, and qdwin_shell_v1 helpers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cffi import FFI


def _pkgconf(flag: str, pkg: str = "libweston-16") -> list[str]:
    out = subprocess.check_output(["pkg-config", flag, pkg], text=True)
    return out.split()


def build() -> Path:
    here = Path(__file__).resolve().parent
    ffi = FFI()

    # CDEF — opaque structs for everything we don't need to poke into,
    # plus the handful of functions the spike exercises.
    ffi.cdef(
        """
        /* Opaque types; we never deref these from Python. */
        struct weston_log_context;
        struct weston_compositor;
        struct weston_view;
        struct weston_output;

        /* Version accessor — no state, three int out-params. */
        void weston_version(int *major, int *minor, int *micro);

        /* Log context lifecycle — smallest stateful pair in libweston. */
        struct weston_log_context *weston_log_ctx_create(void);
        void weston_log_ctx_destroy(struct weston_log_context *ctx);
        """
    )

    # API-mode set_source links against libweston-16 via pkg-config.
    cflags = _pkgconf("--cflags")
    libs = _pkgconf("--libs")

    # Split cflags into -I and other. CFFI's extra_compile_args handles
    # both but it helps readability to keep -I in include_dirs.
    include_dirs = [c[2:] for c in cflags if c.startswith("-I")]
    extra_cflags = [c for c in cflags if not c.startswith("-I")]

    # Split libs into -L, -l, and other.
    library_dirs = [l[2:] for l in libs if l.startswith("-L")]
    libraries = [l[2:] for l in libs if l.startswith("-l")]
    extra_link = [l for l in libs if not l.startswith(("-L", "-l"))]

    ffi.set_source(
        "_qdwin_ffi",
        """
        #include <libweston/libweston.h>
        """,
        include_dirs=include_dirs,
        library_dirs=library_dirs,
        libraries=libraries,
        extra_compile_args=extra_cflags,
        extra_link_args=extra_link,
    )

    # Compile. tmpdir defaults to here, so the .so lands next to this file.
    os.chdir(here)
    out = ffi.compile(verbose=False)
    return Path(out)


if __name__ == "__main__":
    so = build()
    print(f"qdwin CFFI built: {so}", file=sys.stderr)
