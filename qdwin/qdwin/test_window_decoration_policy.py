#!/usr/bin/env python3
"""Source invariants for server-side window frames and drags (v36).

qdwin tells qdshell which toplevels negotiated server-side decorations
(qdwin_shell_v1.toplevel_decoration) and runs the move/resize drags both
for the shell's frame and for a self-decorated client's own title bar.
The load-bearing pieces pinned here:

1. A client's xdg_toplevel.move/.resize starts a compositor pointer grab,
   so it is honoured only for the press that is still driving the
   pointer: live grab serial, a held button, the default grab installed
   (no popup/drag/confine is replaced), pointer focus on this very
   toplevel, compositor not locked.
2. The shell's begin_interactive_resize requires its serial to be the
   seat's held pointer press, like a client resize.
3. Decoration-object lifetime: the object follows its xdg_toplevel via a
   resource destroy listener (no dangling desktop-surface pointer when
   the toplevel dies first, even one that never committed), and
   destroying the object reports client_side to the shell.
4. toplevel_decoration is gated on a v36 binding and replayed with
   toplevel_added; the global and the XML both advertise v36.
5. Every drag teardown path that ends a move drag also ends a resize
   drag (toplevel destroy, shell unbind).
"""
import re
import sys
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
        # A call site inside another function is followed by ';' or an
        # operator, never '{' — only the definition reaches here.
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
        return fail("usage: test_window_decoration_policy.py <qdwin.c> <protocol.xml>")
    src = Path(sys.argv[1]).read_text()
    xml = Path(sys.argv[2]).read_text()

    gate = body(src, "qdwin_client_drag_pointer")
    if not gate:
        return fail("qdwin_client_drag_pointer not found")
    for token, why in (
        ("qdwin->locked", "locked compositor"),
        ("pointer->button_count == 0", "no held button"),
        ("pointer->grab_serial != serial", "stale/forged serial"),
        ("pointer->grab != &pointer->default_grab", "another grab installed"),
        ("weston_surface_get_main_surface(pointer->focus->surface)",
         "pointer focus on another surface"),
    ):
        if token not in gate:
            return fail(f"client drag gate does not refuse: {why}")
    for cb, start in (("qdwin_surface_move", "qdwin_move_grab_start"),
                      ("qdwin_surface_resize", "qdwin_resize_grab_start")):
        b = body(src, cb)
        if not b or "qdwin_client_drag_pointer(" not in b or start + "(" not in b:
            return fail(f"{cb} must gate on qdwin_client_drag_pointer before {start}")
        if b.index("qdwin_client_drag_pointer(") > b.index(start + "("):
            return fail(f"{cb} starts the drag before the gate")

    rs = body(src, "qdwin_handle_begin_interactive_resize")
    if not rs or "pointer->grab_serial != serial" not in rs \
            or "pointer->button_count == 0" not in rs:
        return fail("begin_interactive_resize must require the held press serial")

    get = body(src, "qdwin_xdg_decoration_manager_get_toplevel_decoration")
    if not get or "wl_resource_add_destroy_listener(xdg_toplevel_resource" not in get:
        return fail("decoration object must track its xdg_toplevel's destroy")
    if "ZXDG_TOPLEVEL_DECORATION_V1_ERROR_ALREADY_CONSTRUCTED" not in get:
        return fail("a second decoration object per toplevel must be refused")
    gone = body(src, "qdwin_xdg_decoration_toplevel_destroyed")
    if not gone or "d->toplevel_res = NULL" not in gone:
        return fail("xdg_toplevel destroy must clear the decoration's toplevel")
    # libweston frees the desktop surface when the wl_surface dies and only
    # NULLs the xdg_toplevel resource's user data: a cached
    # weston_desktop_surface pointer would dangle.
    m = re.search(r"struct qdwin_xdg_decoration \{(.*?)\};", src, re.S)
    if not m or "weston_desktop_surface" in re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S):
        return fail("qdwin_xdg_decoration must not cache a weston_desktop_surface")
    teardown = body(src, "qdwin_destroy")
    if not teardown or "qdwin_xdg_decorations_release_all(qdwin)" not in teardown:
        return fail("compositor teardown must release the decoration list")
    neut = body(src, "qdwin_neutralize_binding_resource")
    if not neut or "zxdg_decoration_manager_v1_interface.name" not in neut:
        return fail("decoration manager resources must be neutralized at teardown")
    dead = body(src, "qdwin_toplevel_decoration_resource_destroyed")
    if not dead or "tl->ssd_negotiated = false" not in dead \
            or "qdwin_emit_toplevel_decoration" not in dead \
            or "wl_list_remove(&d->toplevel_destroy.link)" not in dead:
        return fail("destroying the decoration object must unhook and report client_side")

    emit = body(src, "qdwin_emit_toplevel_decoration")
    if not emit or "< 36" not in emit:
        return fail("toplevel_decoration must be gated on a v36 binding")
    added = body(src, "qdwin_send_toplevel_added")
    if not added or "qdwin_emit_toplevel_decoration(qdwin, tl)" not in added:
        return fail("toplevel_added (incl. bind replay) must carry toplevel_decoration")
    # v37 (chrome hover) builds on v36; the global advertises the newest.
    if not re.search(r"&qdwin_shell_v1_interface\s*,\s*37\s*,", src):
        return fail("qdwin_shell_v1 global must advertise v37")
    if not re.search(r'<event name="toplevel_decoration" since="36">', xml):
        return fail("toplevel_decoration must be a since=36 event")

    deps = body(src, "qdwin_toplevel_release_dependents")
    if not deps or "qdwin_resize_grab_end_for(qdwin, tl->handle)" not in deps:
        return fail("toplevel teardown must end a resize drag on it")
    if src.count("qdwin_resize_grab_end_for(qdwin, qdwin->resize_grab_handle)") < 1:
        return fail("shell unbind must end an active resize drag")

    print("PASS: frame decoration mode + client/shell drag gates hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
