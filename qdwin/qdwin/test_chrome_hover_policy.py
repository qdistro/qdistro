#!/usr/bin/env python3
"""Source invariants for frame hover, frame cursors and proxy edge drags (v37).

qdshell paints the server-side frame but never sees wl_pointer enter or
motion on its own chrome surfaces (the same-client suppression
chrome_button works around), so qdwin reports hover itself and lets the
shell pick the frame cursor. The load-bearing pieces pinned here:

1. Protocol: chrome_motion / chrome_leave events and the
   set_chrome_cursor request are since=37, appended after the v36
   opcodes; the global advertises v37.
2. Hover uses the same hit-test as chrome_button (qdwin_chrome_at_pos),
   so a hidden window's chrome never hovers, plus a front-to-back
   stacking check so a frame under another window never hovers; events are gated on a v37
   binding; the update runs from the default grab's motion AFTER the
   focus change, and the locked branch ends the hover without touching
   the lock surface's cursor.
3. set_chrome_cursor never kills the shell for a routine race: only the
   version gate posts an error; locked, stale-handle, unknown-shape and
   mid-drag requests are ignored. It installs a registered sprite.
4. Hover state cannot outlive its toplevel or the shell binding.
5. Nested proxies are resizable: an approved curtain is sized directly
   (left/top edges move the view so the opposite edges stay put), a
   held proxy is refused, and the publisher learns the final size once,
   via qdwin_nested_v1 `configured`, when the drag ends.
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def fail(msg):
    print(f"FAIL: {msg}")
    return 1


def body(source, name):
    """Brace-balanced body of the definition of `name` (skips prototypes)."""
    for m in re.finditer(r"\b" + re.escape(name) + r"\s*\(", source):
        depth, i = 0, source.index("(", m.start())
        while True:
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        j = i + 1
        while source[j].isspace():
            j += 1
        if source[j] != "{":
            continue
        depth, k = 0, j
        while True:
            if source[k] == "{":
                depth += 1
            elif source[k] == "}":
                depth -= 1
                if depth == 0:
                    return source[j:k + 1]
            k += 1
    return None


def main():
    if len(sys.argv) != 3:
        return fail("usage: test_chrome_hover_policy.py <qdwin.c> <protocol.xml>")
    src = Path(sys.argv[1]).read_text()
    xml = Path(sys.argv[2]).read_text()

    # 1. protocol shape
    iface = next(i for i in ET.fromstring(xml).findall("interface")
                 if i.get("name") == "qdwin_shell_v1")
    if iface.get("version") != "37":
        return fail("qdwin_shell_v1 must be version 37")
    events = iface.findall("event")
    requests = iface.findall("request")
    if [e.get("name") for e in events[-2:]] != ["chrome_motion", "chrome_leave"]:
        return fail("chrome_motion/chrome_leave must be the last two events")
    if requests[-1].get("name") != "set_chrome_cursor":
        return fail("set_chrome_cursor must be the last request")
    for e in events[-2:] + requests[-1:]:
        if e.get("since") != "37":
            return fail(f"{e.get('name')} must be since=37")
    if not re.search(r"&qdwin_shell_v1_interface\s*,\s*37\s*,", src):
        return fail("qdwin_shell_v1 global must advertise v37")

    # 2. hover
    upd = body(src, "qdwin_chrome_hover_update")
    if not upd:
        return fail("qdwin_chrome_hover_update not found")
    if "qdwin_chrome_at_pos(" not in upd:
        return fail("hover must use the chrome_button hit-test (hidden-layer aware)")
    if "< 37" not in upd:
        return fail("hover events must be gated on a v37 binding")
    if "qdwin_chrome_hover_on_top(" not in upd:
        return fail("hover must be stacking-aware (no frame hover under another window)")
    top = body(src, "qdwin_chrome_hover_on_top")
    if not top or "normal_layer.view_list" not in top \
            or "t->view == v" not in top:
        return fail("the stacking check must walk normal_layer front-to-back")
    if "qdwin_shell_v1_send_chrome_motion" not in upd:
        return fail("hover must send chrome_motion")
    clr = body(src, "qdwin_chrome_hover_clear")
    if not clr or "qdwin_shell_v1_send_chrome_leave" not in clr \
            or ">= 37" not in clr:
        return fail("leaving the chrome must send a v37-gated chrome_leave")
    if "qdwin_install_default_cursor_on_pointer" not in clr \
            or "restore_cursor" not in clr:
        return fail("leaving the chrome must be able to restore the default cursor")
    motion = body(src, "qdwin_proxy_default_grab_motion")
    if not motion:
        return fail("default grab motion not found")
    i_focus = motion.find("weston_pointer_set_focus(pointer, view)")
    i_hover = motion.find("qdwin_chrome_hover_update(")
    if i_focus < 0 or i_hover < 0 or i_hover < i_focus:
        return fail("hover update must run after the pointer focus change")
    locked = motion[:motion.find("weston_compositor_pick_view")]
    if "qdwin_chrome_hover_clear(qdwin_singleton, pointer, false)" not in locked:
        return fail("the locked branch must end the hover without restoring a cursor")

    # 3. set_chrome_cursor
    req = body(src, "qdwin_handle_set_chrome_cursor")
    if not req:
        return fail("qdwin_handle_set_chrome_cursor not found")
    if req.count("wl_resource_post_error") != 1 or "< 37" not in req:
        return fail("set_chrome_cursor may only error on the version gate")
    for token, why in (
        ("qdwin->locked", "ignored while locked"),
        ("qdwin->chrome_hover_handle != handle", "ignored for a stale handle"),
        ("WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ALL_RESIZE", "shape range-checked"),
        ("sizeof qdwin->cursor_sprites", "sprite index bounded"),
        ("qdwin_resize_grab_live(qdwin)", "ignored mid-drag"),
        ("qdwin_install_cursor_sprite_view(", "installs the registered sprite"),
    ):
        if token not in req:
            return fail(f"set_chrome_cursor must be {why}")
    if ".set_chrome_cursor = qdwin_handle_set_chrome_cursor" not in src:
        return fail("set_chrome_cursor not wired into the shell impl")

    # 4. lifetime
    deps = body(src, "qdwin_toplevel_release_dependents")
    if not deps or "qdwin->chrome_hover_handle == tl->handle" not in deps:
        return fail("toplevel teardown must forget its hover")
    if not re.search(r"qdwin->shell_bound = 0;\s*qdwin->chrome_hover_handle = 0;", src):
        return fail("shell unbind must forget the hover")

    # 5. nested-proxy edge drag
    start = body(src, "qdwin_resize_grab_start")
    if not start:
        return fail("qdwin_resize_grab_start not found")
    if "ignored — nested proxy\\n" in start:
        return fail("nested proxies must no longer be refused outright")
    if "tl->nested_proxy_pending_decision" not in start \
            or "!tl->proxy_curtain" not in start:
        return fail("a held proxy (or one without a curtain) must be refused")
    if "if (tl->desktop_surface)\n\t\tweston_desktop_surface_set_resizing" not in start:
        return fail("set_resizing must not be called on a proxy (NULL desktop surface)")
    pm = body(src, "qdwin_resize_grab_motion_proxy")
    if not pm:
        return fail("qdwin_resize_grab_motion_proxy not found")
    for token, why in (
        ("qdwin_nested_proxy_set_geometry(tl, iw, ih)", "size the curtain"),
        ("resize_anchor_right - iw", "keep the right edge for a left drag"),
        ("resize_anchor_bottom - ih", "keep the bottom edge for a top drag"),
        ("qdwin_toplevel_position_chrome(tl)", "move the frame with it"),
    ):
        if token not in pm:
            return fail(f"proxy drag must {why}")
    motion = body(src, "qdwin_resize_grab_motion")
    if not motion or "qdwin_resize_grab_motion_proxy(qd, tl, ow, oh)" not in motion:
        return fail("the resize grab must route proxies to the curtain path")
    if motion.find("qdwin_resize_grab_motion_proxy") > motion.find(
            "weston_desktop_surface_get_min_size"):
        return fail("proxies must leave before any desktop-surface call")
    fin = body(src, "qdwin_resize_grab_finish")
    if not fin or "qdwin_nested_proxy_send_configured(tl)" not in fin:
        return fail("the drag end must tell the publisher the new size")
    cfg = body(src, "qdwin_nested_proxy_send_configured")
    if not cfg or "qdwin_nested_toplevel_v1_send_configured" not in cfg \
            or "configured_w ==" not in cfg:
        return fail("configured goes to the publisher once per size change")

    print("PASS: frame hover, frame cursors and proxy edge drags hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
