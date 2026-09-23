#!/usr/bin/env python3
"""idle-inhibit visibility invariants (iso2/11 E2).

zwp_idle_inhibitor_v1 bumps weston's ec->idle_inhibit, which gates
idle-triggered lock and DPMS. Before this fix the bump was unconditional at
create time and was only ever released on surface destroy, so a silo could
create a wl_surface, never attach a buffer, take an inhibitor, and suppress
idle-lock for the rest of the session with nothing on screen.

The invariant pinned here: the hold is a *function of the surface's mapped
state*, re-evaluated for the life of the inhibitor.

  - create_inhibitor must not call activate() directly; it goes through the
    single re-evaluation point, qdwin_idle_inhibitor_sync().
  - sync() decides via the pure kernel qdwin_idle_inhibit_should_hold() and
    consults weston_surface_is_mapped().
  - the inhibitor subscribes to the surface's map_signal and unmap_signal, so
    the hold is released when the surface unmaps (including the NULL-buffer
    unmapping commit) and re-taken when it maps again.
  - every teardown path unhooks all three surface listeners.

Occlusion is deliberately NOT part of the test: a minimised or covered media
window keeping the session awake is a product feature, not a defect.
"""

from pathlib import Path
import re
import sys


def fail(message):
    print(f"FAIL: {message}")
    return 1


def _strip_comments(code):
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", " ", code)
    return code


def _function_body(source, signature_regex, name):
    for m in re.finditer(signature_regex, source, re.MULTILINE | re.DOTALL):
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


def check_struct_tracks_map_state(source):
    code = _strip_comments(source)
    m = re.search(r"struct\s+qdwin_idle_inhibitor\s*\{(?P<body>.*?)\}\s*;",
                  code, re.DOTALL)
    if not m:
        return fail("struct qdwin_idle_inhibitor not found")
    body = m.group("body")
    for needle in ("surface_map_listener", "surface_unmap_listener",
                   "surface_destroy_listener"):
        if not re.search(r"struct\s+wl_listener\s+%s\s*;" % needle, body):
            return fail(f"qdwin_idle_inhibitor has no {needle}")
    if not re.search(r"\bint\s+active\s*;", body):
        return fail("qdwin_idle_inhibitor has no `active` idempotency flag")
    return 0


def check_sync_is_mapped_driven(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_idle_inhibitor_sync\s*\(",
        "qdwin_idle_inhibitor_sync")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "qdwin_idle_inhibit_should_hold(" not in code:
        return fail("sync does not use the qdwin_idle_inhibit_should_hold kernel")
    if "weston_surface_is_mapped(" not in code:
        return fail("sync does not consult weston_surface_is_mapped()")
    if "buffer_ref.buffer" not in code:
        return fail("sync does not require an attached buffer")
    if "wl_list_empty(&inh->surface->views)" not in code:
        return fail("sync does not require a live view — a destroyed "
                    "wl_subsurface role leaves a mapped, buffered orphan")
    if "qdwin_idle_inhibitor_activate(" not in code or \
       "qdwin_idle_inhibitor_deactivate(" not in code:
        return fail("sync does not drive both activate and deactivate")
    return 0


def check_create_does_not_unconditionally_activate(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_idle_inhibit_create_inhibitor\s*\(",
        "qdwin_idle_inhibit_create_inhibitor")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "qdwin_idle_inhibitor_activate(" in code:
        return fail("create_inhibitor activates directly instead of via sync() "
                    "— an unmapped surface would inhibit idle immediately")
    if "qdwin_idle_inhibitor_sync(" not in code:
        return fail("create_inhibitor does not call qdwin_idle_inhibitor_sync()")
    # codex r1 #6: pin the (signal, listener, callback) triple, not just the
    # presence of a subscription. A listener wired to the wrong notify makes
    # the first unmap tear everything down and never re-acquire.
    wiring = (
        ("map_signal", "surface_map_listener",
         "qdwin_idle_inhibitor_surface_mapped"),
        ("unmap_signal", "surface_unmap_listener",
         "qdwin_idle_inhibitor_surface_unmapped"),
        ("destroy_signal", "surface_destroy_listener",
         "qdwin_idle_inhibitor_surface_destroyed"),
    )
    for signal, listener, notify in wiring:
        if not re.search(
                r"wl_signal_add\(\s*&surface->%s\s*,\s*&inh->%s\s*\)"
                % (signal, listener), code):
            return fail(f"create_inhibitor does not add &inh->{listener} to "
                        f"surface->{signal}")
        if not re.search(
                r"inh->%s\.notify\s*=\s*%s\s*;" % (listener, notify), code):
            return fail(f"inh->{listener} is not wired to {notify}()")
        if not re.search(r"wl_list_init\(&inh->%s\.link\)" % listener, code):
            return fail(f"inh->{listener}.link is not initialized before use")
    # codex r2 #3: the backstop must exist before any hold is accepted.
    if "qdwin_idle_inhibit_recheck_ensure(" not in code:
        return fail("create_inhibitor does not ensure the backstop timer "
                    "exists before accepting a hold")
    if "wl_client_post_no_memory(client)" not in code:
        return fail("create_inhibitor does not fail closed when the backstop "
                    "is unavailable")
    # codex r2 #1: arming per create is a debounce a client can starve.
    if "was_empty" not in code:
        return fail("create_inhibitor does not arm on the empty -> non-empty "
                    "transition — arming per create lets a client churning "
                    "inhibitors postpone reconciliation indefinitely")
    if not re.search(r"was_empty\s*=\s*wl_list_empty\(&qdwin->idle_inhibitors\)",
                     code):
        return fail("was_empty is not sampled from the inhibitor list")
    if not re.search(r"if\s*\(\s*was_empty\s*&&", code):
        return fail("backstop scheduling at create is not gated on was_empty")
    return 0


def check_map_unmap_handlers_resync(source):
    for fn in ("qdwin_idle_inhibitor_surface_mapped",
               "qdwin_idle_inhibitor_surface_unmapped"):
        body, err = _function_body(
            source, r"static void\s+%s\s*\(" % fn, fn)
        if err:
            return fail(err)
        if "qdwin_idle_inhibitor_sync(" not in _strip_comments(body):
            return fail(f"{fn} does not re-evaluate the hold")
    return 0


def check_teardown_unhooks_every_listener(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_idle_inhibitor_surface_destroyed\s*\(",
        "qdwin_idle_inhibitor_surface_destroyed")
    if err:
        return fail(err)
    code = _strip_comments(body)
    for needle in ("surface_destroy_listener.link",
                   "surface_map_listener.link",
                   "surface_unmap_listener.link"):
        if f"wl_list_remove(&inh->{needle})" not in code:
            return fail(f"surface_destroyed leaves {needle} hooked")
        if f"wl_list_init(&inh->{needle})" not in code:
            return fail(f"surface_destroyed does not re-init {needle}")
    if "inh->surface = NULL" not in code:
        return fail("surface_destroyed does not clear inh->surface")
    # codex r1 #6: the hold must actually be released here, and only after
    # inh->surface is cleared, so the predicate short-circuits safely.
    clear_at = code.find("inh->surface = NULL")
    sync_at = code.find("qdwin_idle_inhibitor_sync(inh)")
    if sync_at == -1:
        return fail("surface_destroyed does not release the hold via sync()")
    if clear_at > sync_at:
        return fail("surface_destroyed syncs before clearing inh->surface "
                    "— the predicate would read a dying surface")

    body, err = _function_body(
        source,
        r"static void\s+qdwin_idle_inhibitor_resource_destroy\s*\(",
        "qdwin_idle_inhibitor_resource_destroy")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "qdwin_idle_inhibitor_deactivate(inh)" not in code:
        return fail("resource destroy does not release the hold")
    for needle in ("surface_destroy_listener.link",
                   "surface_map_listener.link",
                   "surface_unmap_listener.link"):
        if f"wl_list_remove(&inh->{needle})" not in code:
            return fail(f"resource destroy leaves {needle} hooked")
    if "wl_list_remove(&inh->link)" not in code:
        return fail("resource destroy does not unlink from qdwin->idle_inhibitors")
    return 0


def check_backstop_rechecks_every_inhibitor(source):
    """codex r1 #1/#2: weston_surface_is_mapped() recurses up the subsurface
    tree, so an ancestor unmap flips this surface's effective state with no
    signal on it. A periodic sweep of the whole inhibitor list is what makes
    the release policy hold in that case."""
    body, err = _function_body(
        source, r"static int\s+qdwin_idle_inhibit_recheck\s*\(",
        "qdwin_idle_inhibit_recheck")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "&qdwin->idle_inhibitors" not in code:
        return fail("backstop does not sweep qdwin->idle_inhibitors")
    if "qdwin_idle_inhibitor_sync(inh)" not in code:
        return fail("backstop does not re-evaluate each inhibitor")
    if "qdwin_idle_inhibit_recheck_schedule(" not in code:
        return fail("backstop timer does not re-arm itself")
    # codex r2 #3: a backstop that stops is a permanent hold.
    if "qdwin_idle_inhibitors_release_all(" not in code:
        return fail("backstop does not fail closed when it cannot re-arm — "
                    "surviving holds would become permanent")
    # codex r3 #1: releasing without latching is momentary — a later map
    # re-acquires a hold nothing will ever re-evaluate.
    if "idle_inhibit_disabled = true" not in code:
        return fail("backstop failure is not latched; holds could be "
                    "re-acquired with no re-evaluation scheduled")
    return 0


def check_disable_latch_is_terminal(source):
    """codex r3 #1: the latch must gate both re-acquisition and new holds."""
    body, err = _function_body(
        source, r"static void\s+qdwin_idle_inhibitor_sync\s*\(",
        "qdwin_idle_inhibitor_sync")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "idle_inhibit_disabled" not in code:
        return fail("sync ignores the disabled latch — a surviving inhibitor "
                    "could re-acquire on a later map")
    gate = code.find("idle_inhibit_disabled")
    decide = code.find("qdwin_idle_inhibit_should_hold(")
    if decide != -1 and gate > decide:
        return fail("sync consults the latch after deciding the hold")
    if "qdwin_idle_inhibitor_deactivate(inh)" not in code:
        return fail("the latched branch does not release an existing hold")

    body, err = _function_body(
        source, r"static bool\s+qdwin_idle_inhibit_recheck_ensure\s*\(",
        "qdwin_idle_inhibit_recheck_ensure")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "idle_inhibit_disabled" not in code:
        return fail("ensure ignores the disabled latch — new inhibitors "
                    "would be accepted after the backstop failed")
    gate = code.find("idle_inhibit_disabled")
    shortcut = code.find("idle_inhibit_recheck_timer")
    if shortcut != -1 and gate > shortcut:
        return fail("ensure checks the latch after its surviving-timer "
                    "shortcut, so a stale timer pointer reads as success")
    return 0


def check_teardown_drains_inhibitors(source):
    """codex r1 #4: wl_global_destroy() does not destroy bound resources."""
    body, err = _function_body(
        source, r"static void\s+qdwin_idle_inhibitors_destroy_all\s*\(",
        "qdwin_idle_inhibitors_destroy_all")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "wl_resource_destroy(inh->resource)" not in code:
        return fail("drain does not destroy the inhibitor resources")
    if "wl_event_source_remove" not in code:
        return fail("drain leaves the backstop timer armed")

    body, err = _function_body(
        source, r"static void\s+qdwin_destroy\s*\(", "qdwin_destroy")
    if err:
        return fail(err)
    if "qdwin_idle_inhibitors_destroy_all(qdwin)" not in _strip_comments(body):
        return fail("qdwin_destroy does not drain live idle inhibitors")
    return 0


def check_release_rearms_idle_timer(source):
    """codex r1 #3: weston's idle_source is one-shot and idle_handler()
    returns early while inhibited, so a deadline that passes under a hold
    would otherwise never fire after the hold is dropped."""
    body, err = _function_body(
        source, r"static void\s+qdwin_idle_inhibitor_deactivate\s*\(",
        "qdwin_idle_inhibitor_deactivate")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "idle_source" not in code or "wl_event_source_timer_update" not in code:
        return fail("dropping the last hold does not re-arm weston's "
                    "one-shot idle timer")
    if "WESTON_COMPOSITOR_ACTIVE" not in code:
        return fail("idle re-arm is not gated on an active compositor — it "
                    "must not disturb an already idle/asleep session")
    return 0


def check_kernel_is_mapped_only(logic):
    body, err = _function_body(
        logic,
        r"^bool\s*\n\s*qdwin_idle_inhibit_should_hold\s*\(",
        "qdwin_idle_inhibit_should_hold")
    if err:
        return fail(err)
    code = _strip_comments(body)
    for term in ("have_surface", "surface_mapped", "has_buffer", "has_view"):
        if term not in code:
            return fail(f"should_hold does not gate on {term}")
    return 0


def main():
    if len(sys.argv) != 3:
        return fail("usage: test_idle_inhibit_visibility.py "
                    "<qdwin.c> <qdwin-logic.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    logic = Path(sys.argv[2]).read_text(encoding="utf-8")
    for check in (
            check_struct_tracks_map_state,
            check_sync_is_mapped_driven,
            check_create_does_not_unconditionally_activate,
            check_map_unmap_handlers_resync,
            check_teardown_unhooks_every_listener,
            check_backstop_rechecks_every_inhibitor,
            check_teardown_drains_inhibitors,
            check_release_rearms_idle_timer,
            check_disable_latch_is_terminal):
        rc = check(source)
        if rc:
            return rc
    return check_kernel_is_mapped_only(logic)


if __name__ == "__main__":
    sys.exit(main())
