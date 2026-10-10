#!/usr/bin/env python3
"""qdwin_destroy() must drain every qdwin-owned family before free(qdwin).

Before this change qdwin_destroy freed the struct while client-owned
protocol objects, list entries, listeners, timers and grabs were still
live — every family kept a `qdwin *` or a list link into storage that was
about to be freed. The first late resource destructor (a client
disconnecting after compositor teardown) ran its normal body against a
dead qdwin.

The staged order matters as much as the drain itself:

  Stage 1  Sever reach-back paths BEFORE any grab teardown: the default
           input grabs' focus/motion/button callbacks read qdwin_singleton
           and weston_*_end_grab() runs them SYNCHRONOUSLY; the display
           global filter carries data=qdwin. Both must be detached first.
  Stage 2  End interactive grabs while the seats that own them are alive
           (reuses the output-boundary cancellation routine).
  Stage 3  Drain every object family while their targets (desktop
           surfaces, views, seats, wl_surfaces) are alive. Toplevels
           first — their dependents resolve against live views, and the
           drain destroys the dsurfaces that would otherwise call
           api.surface_removed after free(qdwin). Nested toplevels next:
           their resource destructor calls qdwin_nested_proxy_destroy on
           proxy_tl, an edge the toplevel drain already broke.
  Stage 4  Neutralize the client-owned shell/locker/lock resources'
           user_data so their late destructors no-op, drop lock-surface
           listeners and the dedicated lock view.
  Stage 5  Leave the libweston desktop object alive. There is no public
           API to enumerate surviving desktop surfaces, and a late
           dsurface destroy still runs api.surface_removed which reads
           desktop->api. The tl drain severed every dsurface->tl link, so
           the callback early-returns on !tl.

The behavioural companion (test_qdwin_destroy_drain_behaviour.py) splices
the real bodies and runs them under ASan; this file pins the ordering and
the shapes a future refactor could silently break.
"""

from pathlib import Path
import re
import sys

SRC = Path(__file__).resolve().with_name("qdwin.c")


def fail(message):
    print(f"FAIL: {message}")
    return 1


def _strip_comments(code):
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", " ", code)
    return code


def _function_body(source, signature_regex, name):
    for m in re.finditer(signature_regex, source,
                         re.MULTILINE | re.DOTALL):
        paren = source.index("(", m.start())
        depth = 0
        close = None
        for i in range(paren, len(source)):
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    close = i
                    break
        if close is None:
            continue
        j = close + 1
        while j < len(source) and source[j].isspace():
            j += 1
        if j >= len(source) or source[j] != "{":
            continue
        start = j
        depth = 0
        for i in range(start, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    return source[start:i + 1], None
        return None, f"{name}: unbalanced braces"
    return None, f"{name} not found"


def _flat(code):
    return re.sub(r"\s+", "", _strip_comments(code))


def _pos(flat_body, needle):
    return flat_body.find(re.sub(r"\s+", "", needle))


def _require_before(body, first, second, label):
    flat = _flat(body)
    a = _pos(flat, first)
    if a < 0:
        return fail(f"{label}: `{first}` missing")
    b = _pos(flat, second)
    if b < 0:
        return fail(f"{label}: `{second}` missing")
    if a >= b:
        return fail(f"{label}: `{first}` must precede `{second}`")
    return 0


FAMILY_DRAINS = [
    "qdwin_toplevels_destroy_all(qdwin)",
    "qdwin_nested_toplevels_destroy_all(qdwin)",
    "qdwin_view_streams_destroy_all(qdwin)",
    "qdwin_panels_destroy_all(qdwin)",
    "qdwin_notifications_destroy_all(qdwin)",
    "qdwin_launchers_destroy_all(qdwin)",
    "qdwin_hotkeys_purge(qdwin)",
    "qdwin_idle_notifications_destroy_all(qdwin)",
    "qdwin_fractional_scales_destroy_all(qdwin)",
    "qdwin_activation_tokens_destroy_all(qdwin)",
    "qdwin_primary_seats_destroy_all(qdwin)",
    "qdwin_layer_surfaces_destroy_all(qdwin)",
    "qdwin_ext_ws_managers_destroy_all(qdwin)",
    "qdwin_om_managers_destroy_all(qdwin)",
    "qdwin_secctx_clients_destroy_all(qdwin)",
]


def check_stage_order(body):
    # Stage 1: reach-back suppression must precede the first possible
    # synchronous grab callback (any weston_*_end_grab in stage 2+).
    r = _require_before(body, "if(qdwin_singleton==qdwin)qdwin_singleton=NULL",
                        "qdwin_output_boundary_cancel_state(qdwin",
                        "stage1/singleton")
    if r:
        return r
    r = _require_before(body, "wl_display_set_global_filter(",
                        "qdwin_output_boundary_cancel_state(qdwin",
                        "stage1/global-filter")
    if r:
        return r
    # Stage 2 (grabs) must precede stage 3 (object drains): the grab-end
    # focus callbacks need the toplevel/stream lists still populated.
    r = _require_before(body, "qdwin_output_boundary_cancel_state(qdwin",
                        "qdwin_toplevels_destroy_all(qdwin)",
                        "stage2/grabs")
    if r:
        return r
    # Toplevels before nested toplevels: the nested resource destructor
    # calls qdwin_nested_proxy_destroy(t->proxy_tl); the toplevel drain
    # breaks that edge first.
    r = _require_before(body, "qdwin_toplevels_destroy_all(qdwin)",
                        "qdwin_nested_toplevels_destroy_all(qdwin)",
                        "stage3/toplevels-then-nested")
    if r:
        return r
    # Every family drain must be present AND must run before stage 4
    # neutralizes the binding resources.
    flat = _flat(body)
    neutralize = _pos(flat, "qdwin_neutralize_binding_resources(qdwin)")
    if neutralize < 0:
        return fail("stage4: qdwin_neutralize_binding_resources(qdwin) "
                    "missing — unclaimed bindings keep freed-qdwin "
                    "user_data")
    for call in FAMILY_DRAINS:
        pos = _pos(flat, call)
        if pos < 0:
            return fail(f"stage3: `{call}` missing")
        if pos > neutralize:
            return fail(f"stage3: `{call}` runs after stage-4 binding "
                        "neutralization — it may dereference shell state")
    # The libweston desktop object must NOT be destroyed: late dsurface
    # destroys still fire api.surface_removed -> qdwin_surface_removed,
    # which derefs desktop->api. Pin the intentional leak so a cleanup
    # pass cannot "fix" it without solving dsurface enumeration.
    if "weston_desktop_destroy(" in flat:
        return fail("qdwin_destroy calls weston_desktop_destroy() — the "
                    "desktop object must outlive qdwin: late dsurface "
                    "destroys still run api.surface_removed which reads "
                    "desktop->api")
    if _pos(flat, "qdwin->desktop=NULL") < 0:
        return fail("qdwin->desktop is not detached from the freed "
                    "struct")
    # free(qdwin) is the last thing.
    if not flat.rstrip("}").endswith("free(qdwin);"):
        return fail("free(qdwin) is not the last statement of "
                    "qdwin_destroy — something still runs after the "
                    "struct is freed")
    return 0


def check_shell_locker_guards(source):
    for name in ("qdwin_shell_resource_destroy",
                 "qdwin_locker_resource_destroy"):
        body, err = _function_body(
            source, r"static void\s+%s\s*\(\s*struct wl_resource \*" % name,
            name)
        if err:
            return fail(err)
        flat = _flat(body)
        guard = flat.find("if(!qdwin)return;")
        if guard < 0:
            return fail(f"{name}: missing `if (!qdwin) return;` guard — "
                        "a late client disconnect after compositor "
                        "teardown runs the unbind body against freed "
                        "qdwin")
        # The guard must precede the first qdwin dereference.
        deref = flat.find("qdwin->")
        if 0 <= deref < guard:
            return fail(f"{name}: dereferences qdwin before the NULL "
                        "guard")
    return 0


def check_toplevel_drain(source):
    body, err = _function_body(
        source, r"static void\s+qdwin_toplevels_destroy_all\s*\(",
        "qdwin_toplevels_destroy_all")
    if err:
        return fail(err)
    flat = _flat(body)
    # The dsurface drain must resolve weston_desktop_surface_destroy via
    # the soft dlsym helper (internal symbol, not public API).
    if "qdwin_desktop_surface_destroy_sym(" not in flat:
        return fail("toplevel drain does not resolve "
                    "weston_desktop_surface_destroy through the dlsym "
                    "helper")
    # Fallback path must call qdwin_surface_removed — which clears
    # dsurface user_data itself LAST — and must NOT pre-clear user_data
    # (that is the lookup the callback depends on).
    if "qdwin_surface_removed(" not in flat:
        return fail("toplevel drain fallback does not run "
                    "qdwin_surface_removed")
    if "weston_desktop_surface_set_user_data(" in flat:
        return fail("toplevel drain clears dsurface user_data itself — "
                    "qdwin_surface_removed then early-returns on !tl and "
                    "leaks the toplevel; let the callback sever the link")
    # Proxy path: both ownership edges broken BEFORE the proxy is freed.
    if "tl->proxy_nested_owner->proxy_tl=NULL" not in flat:
        return fail("toplevel drain does not clear "
                    "owner->proxy_tl before freeing the proxy — the "
                    "owner's resource destructor would call "
                    "qdwin_nested_proxy_destroy on freed memory")
    r = _require_before(body, "tl->proxy_nested_owner->proxy_tl=NULL",
                        "qdwin_nested_proxy_destroy(tl)",
                        "proxy-edge-order")
    if r:
        return r
    return 0


def check_sym_helper(source):
    body, err = _function_body(
        source, r"static qdwin_desktop_surface_destroy_fn\s+"
                r"qdwin_desktop_surface_destroy_sym\s*\(\s*void\s*\)",
        "qdwin_desktop_surface_destroy_sym")
    if err:
        return fail(err)
    flat = _flat(body)
    if 'dlsym(RTLD_DEFAULT,"weston_desktop_surface_destroy")' not in flat:
        return fail("qdwin_desktop_surface_destroy_sym does not dlsym "
                    "weston_desktop_surface_destroy")
    return 0


def check_binding_neutralizer(source):
    body, err = _function_body(
        source, r"static enum wl_iterator_result\s+"
                r"qdwin_neutralize_binding_resource\s*\(",
        "qdwin_neutralize_binding_resource")
    if err:
        return fail(err)
    flat = _flat(body)
    # Four classes share the three qdwin-user_data destructors:
    # qdwin_lock_surface_resource_destroyed is installed on BOTH
    # qdwin_lock_surface_v1 (shell path) and qdwin_locker_surface_v1
    # (locker attach path).
    for iface in ("qdwin_shell_v1_interface.name",
                  "qdwin_locker_v1_interface.name",
                  "qdwin_lock_surface_v1_interface.name",
                  "qdwin_locker_surface_v1_interface.name"):
        if iface not in flat:
            return fail(f"neutralizer does not cover {iface} — an "
                        "unclaimed binding of that class keeps freed-qdwin "
                        "user_data")
    walk, err = _function_body(
        source, r"static void\s+qdwin_neutralize_binding_resources\s*\(",
        "qdwin_neutralize_binding_resources")
    if err:
        return fail(err)
    flat = _flat(walk)
    for call in ("wl_display_get_client_list(", "wl_client_for_each(",
                 "wl_client_for_each_resource("):
        if call not in flat:
            return fail(f"neutralizer walk missing {call} — it must "
                        "enumerate every client resource, not only the "
                        "claimed bindings")
    return 0


def check_primary_device_unlink(source):
    for name in ("qdwin_primary_seat_seat_destroyed",
                 "qdwin_primary_seats_destroy_all"):
        body, err = _function_body(
            source, r"static void\s+%s\s*\(" % name, name)
        if err:
            return fail(err)
        flat = _flat(body)
        if "wl_list_remove(&device->link)" not in flat:
            return fail(f"{name}: leaves device->link on pseat->devices "
                        "— the late device resource destructor's "
                        "wl_list_remove writes through the freed seat")
    return 0


def check_promote_releases_attach(source):
    body, err = _function_body(
        source, r"qdwin_maybe_promote_lock_toplevel\s*\(",
        "qdwin_maybe_promote_lock_toplevel")
    if err:
        return fail(err)
    flat = _flat(body)
    # Attach-then-promote: a locker can attach a raw lock surface and
    # only then produce its locker-UI toplevel. Taking over
    # lock_surface/lock_view without first releasing the attach leaves
    # the listeners (links embedded in struct qdwin) armed on the old
    # surface — the later toplevel drain clears the promoted fields, so
    # nothing ever unlinks them — and lets the stale lock_resource's
    # late destructor reset the promoted state mid-session.
    if "wl_resource_destroy(qdwin->lock_resource)" not in flat:
        return fail("promote does not release a prior raw lock-surface "
                    "attach — its listeners stay armed on the old "
                    "surface forever")
    if "qdwin->lock_resource_reattach_in_progress=1" not in flat:
        return fail("promote releases the prior attach without "
                    "suppressing the fail-secure flap")
    r = _require_before(body, "wl_resource_destroy(qdwin->lock_resource)",
                        "qdwin->lock_toplevel=tl", "promote-release-order")
    return r


def check_nested_drain_edges(source):
    body, err = _function_body(
        source, r"qdwin_nested_toplevel_resource_destroy\s*\(",
        "qdwin_nested_toplevel_resource_destroy")
    if err:
        return fail(err)
    flat = _flat(body)
    # The owner destructor must NULL the tl's back-edge before freeing it.
    r = _require_before(body, "t->proxy_tl->proxy_nested_owner=NULL",
                        "qdwin_nested_proxy_destroy(t->proxy_tl)",
                        "nested-owner-edge")
    return r


def check_destroy_all_defined(source, name):
    body, err = _function_body(
        source, r"static void\s+%s\s*\(\s*struct qdwin \*" % name, name)
    if err:
        return fail(err)
    flat = _flat(body)
    if "wl_list_for_each_safe" not in flat:
        return fail(f"{name}: does not iterate safely — entries are "
                    "removed while iterating")
    return 0


def main():
    source = SRC.read_text(encoding="utf-8")

    body, err = _function_body(
        source, r"qdwin_destroy\s*\(\s*struct wl_listener \*listener",
        "qdwin_destroy")
    if err:
        return fail(err)

    for check in (
        lambda: check_stage_order(body),
        lambda: check_shell_locker_guards(source),
        lambda: check_binding_neutralizer(source),
        lambda: check_primary_device_unlink(source),
        lambda: check_toplevel_drain(source),
        lambda: check_sym_helper(source),
        lambda: check_nested_drain_edges(source),
        lambda: check_promote_releases_attach(source),
    ):
        r = check()
        if r:
            return r

    for drain in FAMILY_DRAINS:
        r = check_destroy_all_defined(source, drain.split("(")[0])
        if r:
            return r

    print("qdwin-destroy-drain source invariants OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
