#!/usr/bin/env python3
"""Pin R8/A4 output-loss rescue and session-boundary cleanup ordering."""

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


def require_all(haystack: str, tokens: tuple[str, ...], scope: str) -> int:
    missing = [token for token in tokens if token not in haystack]
    if missing:
        return fail(f"{scope} missing invariant(s): {', '.join(missing)}")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        return fail("usage: test_output_boundary_policy.py qdwin.c")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    try:
        callback = body(source, "qdwin_on_output_destroyed")
        transition = body(source, "qdwin_output_boundary_transition")
        cleanup = body(source, "qdwin_output_boundary_cancel_state")
        rehome = body(source, "qdwin_output_boundary_rehome_toplevel")
        primary = body(source, "qdwin_primary_output")
    except ValueError as error:
        return fail(str(error))

    transition_at = callback.find("qdwin_output_boundary_transition")
    removed_at = callback.find("qdwin_send_output_removed")
    if transition_at < 0 or removed_at < 0 or transition_at > removed_at:
        return fail("output boundary must complete before output_removed is published")

    if require_all(cleanup, (
        "qdwin_popup_teardown", "qdwin_layer_popup_destroy",
        "qdwin_xdg_popup_dismiss_layer_grab_sym",
        "qdwin_move_grab_end_for", "qdwin_switcher_grab_end",
        "qdwin_overlay_grab_end", "qdwin_stream_confine_grab_end",
        "qdwin_im_deactivate", "wl_resource_destroy(im->grab->resource)",
        "pointer->grab->interface->cancel", "touch->grab->interface->cancel",
        "keyboard->grab->interface->cancel", "weston_pointer_set_focus",
        "weston_keyboard_set_focus", "weston_touch_set_focus",
        "seat->output = NULL", "weston_seat_set_selection",
        "qdwin_primary_seat_clear_selection",
    ), "output-boundary cleanup"):
        return 1
    popup_dismiss_at = cleanup.find("qdwin_xdg_popup_dismiss_layer_grab_sym")
    popup_destroy_at = cleanup.find("qdwin_layer_popup_destroy", popup_dismiss_at)
    if popup_dismiss_at < 0 or popup_destroy_at < popup_dismiss_at:
        return fail("layer popup must receive popup_done before grab-owning destroy")

    if require_all(rehome, (
        "qdwin_output_boundary_rehome_saved",
        "QDWIN_TS_FULLSCREEN", "qdwin_toplevel_apply_fullscreen_geometry",
        "QDWIN_TS_MAXIMIZED", "weston_desktop_surface_set_maximized",
        "QDWIN_TILE_LEFT", "QDWIN_TILE_RIGHT",
        "qdwin_rehome_outer_rect", "qdwin_nested_proxy_set_geometry",
        "weston_view_set_output", "qdwin_shell_v1_send_toplevel_geometry",
    ), "toplevel output rescue"):
        return 1

    cancel_at = transition.find("qdwin_output_boundary_cancel_state")
    panel_at = transition.find("wl_list_for_each(panel")
    toplevel_at = transition.find("wl_list_for_each(tl")
    if min(cancel_at, panel_at, toplevel_at) < 0 or not (
            cancel_at < panel_at < toplevel_at):
        return fail("boundary ordering must be cleanup -> chrome/prompts -> toplevel rescue")

    if require_all(transition, (
        "panel->output = survivor", "notification->output = survivor",
        "launcher->output = survivor", "ls->output = survivor",
        "qdwin_layer_surface_send_configure", "stream->prev_output = survivor",
        "qdwin_lock_surface_place", "qdwin_install_lock_curtain",
    ), "boundary surface rescue"):
        return 1

    if "out->destroying" not in primary:
        return fail("primary-output selection can return a destroying output")

    print("PASS: R8/A4 output loss drains transient authority and rescues every surface class")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
