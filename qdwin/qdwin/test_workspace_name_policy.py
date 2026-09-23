#!/usr/bin/env python3
"""test_workspace_name_policy.py — source-level invariants for the v27
ext-workspace-v1 workspace-NAME parity feature
(qdwin_shell_v1.set_workspace_name). Asserts the C source:

  * implements the set_workspace_name handler and wires it into the
    qdwin_shell_v1 interface vtable,
  * gates the request behind the shell-bound check (only the shell may
    rename workspaces),
  * treats an empty name as a revert to the positional default,
  * bounds the stored name length (fail-safe against an unbounded shell),
  * advertises the stored (or positional-default) name on the STANDARD
    ext_workspace_handle_v1.name event, so every ext-workspace client
    (not just qdshell) sees the user's custom names.

Pure source check, host-independent, so it lives in the unit test() set
(see meson.build). The live-VM half — a real third-party ext-workspace
bar (waybar) showing the user's names — is tracked PENDING in
tests/host/21-ext-workspace-names.md.
"""
import re
import sys
import pathlib


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    src = pathlib.Path(sys.argv[1]).read_text()

    # Handler is defined.
    m = re.search(
        r"qdwin_handle_set_workspace_name\s*\(\s*struct wl_client[^)]*"
        r"uint32_t\s+index\s*,\s*const char\s*\*\s*name\s*\)",
        src,
        re.S,
    )
    if not m:
        fail("set_workspace_name handler signature not found")

    # Handler is wired into the qdwin_shell_v1 vtable.
    if not re.search(r"\.set_workspace_name\s*=\s*qdwin_handle_set_workspace_name",
                     src):
        fail("set_workspace_name not wired into qdwin_shell_impl vtable")

    # Slice out the handler body to check its invariants in isolation.
    start = m.start()
    body = src[start:start + 3000]

    if "qdwin_shell_require_bound" not in body:
        fail("set_workspace_name does not gate on shell-bound check")

    # Empty name => revert to positional default (free + NULL the slot).
    if not re.search(r"!name\s*\|\|\s*!name\[0\]", body):
        fail("set_workspace_name does not treat empty name as a revert")

    # Bounded copy (fail-safe length cap).
    if "strndup" not in body or "QDWIN_WORKSPACE_NAME_MAX" not in body:
        fail("set_workspace_name does not bound the stored name length")

    # The cap macro is actually defined.
    if not re.search(r"#define\s+QDWIN_WORKSPACE_NAME_MAX\s+\d+", src):
        fail("QDWIN_WORKSPACE_NAME_MAX not defined")

    # Per-index storage exists.
    if not re.search(r"workspace_names\s*\[\s*QDWIN_MAX_WORKSPACES\s*\]", src):
        fail("workspace_names[] storage not declared")

    # The stored/positional name is advertised on the STANDARD
    # ext_workspace_handle_v1.name event (parity for all clients).
    if "ext_workspace_handle_v1_send_name" not in src:
        fail("name is not advertised via ext_workspace_handle_v1.name")

    # Detail emitter falls back to the positional default when unset.
    if "workspace_names[index]" not in src:
        fail("handle-detail emitter does not consult workspace_names[index]")

    print("ok: workspace-name parity source invariants hold")


if __name__ == "__main__":
    main()
