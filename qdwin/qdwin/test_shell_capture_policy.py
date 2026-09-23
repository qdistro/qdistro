#!/usr/bin/env python3
"""Source invariants for shell-authorized output capture.

Ensures: enabling the VM screenshot path does not broaden capture discovery or
authorization beyond the exact bound shell client and the single designated
Virtual-1 output (an exact-name pin, not a deny-list — a new output on a
future golden must not silently widen capture), and the v32 full-damage
request remains shell-only and non-fatal for bad names.
Live ordinary/SECCTX discovery denial remains covered by test_global_filter.py;
this test pins the new late libweston-authority layer.
"""

from pathlib import Path
import re
import sys


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def function_body(source: str, name: str) -> str | None:
    match = re.search(rf"\b{name}\s*\([^;]*?\)\s*\{{", source, re.S)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    return None


def main() -> int:
    if len(sys.argv) != 3:
        return fail("usage: test_shell_capture_policy.py <qdwin.c> <protocol.xml>")
    source = Path(sys.argv[1]).read_text()
    protocol = Path(sys.argv[2]).read_text()

    if not re.search(r'<interface name="qdwin_shell_v1" version="34">', protocol):
        return fail("qdwin_shell_v1 must advertise protocol version 34")
    if not re.search(
        r'<request name="prepare_output_capture" since="32">.*?'
        r'<arg name="output_name" type="string"', protocol, re.S
    ):
        return fail("v32 prepare_output_capture(output_name) request is missing")
    if not re.search(
        r'<event name="capture_served_stale" since="33">.*?'
        r'<arg name="age_ms" type="uint"', protocol, re.S
    ):
        return fail("v33 capture_served_stale(age_ms) event is missing")
    if not re.search(
        r"wl_global_create\s*\(\s*ec->wl_display\s*,\s*"
        r"&qdwin_shell_v1_interface\s*,\s*34\s*,", source, re.S
    ):
        return fail("qdwin_shell_v1 wl_global_create version is not 34")

    handler = function_body(source, "qdwin_handle_prepare_output_capture")
    if handler is None:
        return fail("prepare_output_capture handler is missing")
    for required in (
        "qdwin_shell_require_bound",
        "compositor->output_list",
        "strcmp(output->name, output_name)",
        "qdwin_shell_capture_output_ok(output_name)",
        "weston_output_damage(output)",
    ):
        if required not in handler:
            return fail(f"prepare_output_capture is missing invariant: {required}")
    if "wl_resource_post_error" in handler:
        return fail("bad output names must not kill the shell connection")
    if not re.search(
        r"\.prepare_output_capture\s*=\s*"
        r"qdwin_handle_prepare_output_capture", source
    ):
        return fail("prepare_output_capture is not wired into the shell vtable")

    background = function_body(source, "qdwin_refresh_background")
    if background is None or "weston_output_set_ready(out)" not in background:
        return fail(
            "shell must mark outputs ready after installing its background content"
        )

    authority = function_body(source, "qdwin_capture_auth_cb")
    if authority is None:
        return fail("capture authority callback is missing")
    if not re.search(
        r'#define\s+QDWIN_SHELL_CAPTURE_OUTPUT\s+"Virtual-1"', source
    ):
        return fail("shell capture must pin the single designated output Virtual-1")
    ok_helper = function_body(source, "qdwin_shell_capture_output_ok")
    if ok_helper is None or \
            "strcmp(name, QDWIN_SHELL_CAPTURE_OUTPUT) == 0" not in ok_helper:
        return fail(
            "output check must be an exact-name match against the designated "
            "output, not a deny-list"
        )
    for required in (
        "qdwin_client_is_bound_shell(qdwin, who->client)",
        "qdwin_shell_capture_output_ok(who->output->name)",
        "attempt->authorized = true",
    ):
        if required not in authority:
            return fail(f"capture authority is missing invariant: {required}")
    if authority.count("authorized = true") != 1:
        return fail("capture authority must have exactly one allow assignment")
    if "denied =" in authority:
        return fail("capture authority must abstain, not override other authorities")

    gate_pos = source.find('getenv("QDWIN_ENABLE_SHELL_CAPTURE")')
    register_pos = source.find("weston_compositor_add_screenshot_authority(", gate_pos)
    if gate_pos < 0 or register_pos < 0:
        return fail("QDWIN_ENABLE_SHELL_CAPTURE registration gate is missing")
    gate = source[gate_pos : register_pos + 300]
    for required in (
        "geteuid() != qdwin->allowed_uid",
        "&qdwin->capture_auth_listener",
        "qdwin_capture_auth_cb",
        "capture_auth_registered = true",
    ):
        if required not in gate:
            return fail(f"capture startup gate is missing invariant: {required}")
    if "QDWIN_SECCTX_OPEN" in gate or "screenshooter_create" in gate:
        return fail("shell capture gate must not open SECCTX or create screenshooter")
    destroy = function_body(source, "qdwin_destroy")
    if destroy is None or not re.search(
        r"capture_auth_registered.*?wl_list_remove\s*\(\s*"
        r"&qdwin->capture_auth_listener\.link", destroy, re.S
    ):
        return fail("registered capture authority listener is not removed on destroy")

    print(
        "PASS: v32 capture prep is bound-shell-only; late authority grants only "
        "the exact shell wl_client on the designated Virtual-1 output; "
        "startup is env+euid gated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
