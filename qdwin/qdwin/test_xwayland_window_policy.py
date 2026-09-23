#!/usr/bin/env python3
"""Pin XWayland identity publication and geometry-stable state restores."""

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
        return fail("usage: test_xwayland_window_policy.py qdwin.c")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    try:
        identity = body(source, "qdwin_toplevel_effective_app_id")
        announce = body(source, "qdwin_send_toplevel_added")
        seed = body(source, "qdwin_toplevel_seed_outer_from_committed")
        committed = body(source, "qdwin_surface_committed")
        maximize = body(source, "qdwin_toplevel_set_maximized")
        fullscreen = body(source, "qdwin_toplevel_set_fullscreen")
        tile = body(source, "qdwin_toplevel_set_tiled")
    except ValueError as error:
        return fail(str(error))

    if "get_xwayland_window_name(surface, WM_CLASS)" not in identity:
        return fail("empty libweston app_id does not fall back to XWayland WM_CLASS")
    if "qdwin_toplevel_effective_app_id(qdwin, tl)" not in announce:
        return fail("toplevel_added does not publish the effective XWayland app id")

    geometry_at = seed.find("weston_desktop_surface_get_geometry")
    restore_extent_at = seed.find("qdwin_committed_restore_extent")
    xwayland_at = seed.find("qdwin_toplevel_is_xwayland")
    if min(geometry_at, restore_extent_at, xwayland_at) < 0 or not (
            geometry_at < xwayland_at < restore_extent_at):
        return fail("restore seed does not select native/XWayland committed extents")
    nested_at = seed.find("if (tl->is_nested_proxy)")
    nested_return_at = seed.find("return;", nested_at)
    inset_add_at = seed.find("tl->outer_width = inner_w + tl->inset_w")
    if min(nested_at, nested_return_at, inset_add_at) < 0 or not (
            nested_at < nested_return_at < inset_add_at):
        return fail("nested proxy outer geometry must not add chrome twice")
    for scope, function_body in (
        ("maximize", maximize),
        ("fullscreen", fullscreen),
        ("tile", tile),
    ):
        if "qdwin_toplevel_seed_outer_from_committed(tl);" not in function_body:
            return fail(f"{scope} does not use the geometry-stable restore seed")

    if "tl->xwayland_configure_pending" not in committed or \
            "xwayland_configure_correct" not in committed:
        return fail("XWayland commits do not feed hidden frame extents back into configure")
    if "surface->width > 0 ? surface->width : geometry.width" not in committed:
        return fail("XWayland convergence does not compare shell-visible surface extents")
    if "tl->outer_width = inner_w + tl->inset_w" not in committed or \
            "QDWIN_TS_MAXIMIZED | QDWIN_TS_FULLSCREEN" not in committed:
        return fail("floating commits do not refresh restore geometry outside special states")

    print("PASS: XWayland identity and special-state restore invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
