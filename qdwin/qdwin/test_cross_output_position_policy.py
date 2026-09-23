#!/usr/bin/env python3
"""Pin the production shell-owned floating-window cross-output move path."""

from pathlib import Path
import re
import sys


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def body(source: str, name: str) -> str:
    match = re.search(rf"\n{name}\s*\([^;]*?\)\s*\{{", source, re.DOTALL)
    if not match:
        raise ValueError(f"{name} definition not found")
    start = source.index("{", match.start())
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise ValueError(f"{name} has unbalanced braces")


def main() -> int:
    if len(sys.argv) != 2:
        return fail("usage: test_cross_output_position_policy.py qdwin.c")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    try:
        handler = body(source, "qdwin_handle_request_set_position")
    except ValueError as error:
        return fail(str(error))

    required = (
        "qdwin_shell_require_bound", "QDWIN_SHELL_V1_ERROR_LOCKED",
        "nested_proxy_pending_decision", "QDWIN_TS_MAXIMIZED",
        "QDWIN_TS_FULLSCREEN", "QDWIN_TILE_NONE", "move_grab_active",
        "wl_list_for_each(o, &qdwin->compositor->output_list, link)",
        "weston_view_set_position", "cx + tl->inset_w",
        "cy + tl->inset_n", "weston_view_update_transform",
        "qdwin_toplevel_position_chrome",
        "qdwin_shell_v1_send_toplevel_geometry",
    )
    missing = [token for token in required if token not in handler]
    if missing:
        return fail("cross-output position handler missing: " + ", ".join(missing))

    # Ensures a 512px window with top-left on the local output may overlap the
    # adjacent output. Clamping the right edge to `output.width - window.width`
    # would silently restore the original one-output policy blocker.
    forbidden = (
        "out->width - tl->outer_width",
        "out->width - tl->last_width",
        "out->width - surface->width",
    )
    present = [token for token in forbidden if token in handler]
    if present:
        return fail("position handler clamps the whole window to one output: "
                    + ", ".join(present))

    if ".request_set_position = qdwin_handle_request_set_position" not in source:
        return fail("qdwin_shell_v1 does not register the position handler")

    print("PASS: bound qdshell can position a floating window across outputs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
