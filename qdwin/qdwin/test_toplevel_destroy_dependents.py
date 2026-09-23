#!/usr/bin/env python3
"""Toplevel destroy must release every dependent first (iso2/10 E2).

A qdwin_toplevel is pointed at by three server-owned things that outlive an
ordinary client request: the chrome popup (qdwin_popup::parent), the active
move-drag (by handle), and any exported view_stream (qdwin_view_stream::tl).

There are TWO destroy paths. qdwin_surface_removed handles a real
xdg_toplevel; qdwin_nested_proxy_destroy handles a nested proxy, which is
reached from the advertiser's resource-destroy — i.e. when the nested
compositor crashes or unpublishes. The proxy path used to free(tl) without
touching any dependent, so a crash while qdshell had a subscribed stream or a
chrome popup on that proxy left s->tl / p->parent dangling into the input
inject and grab callbacks: a compositor use-after-free, not a client
disconnect.

The invariant pinned here: both paths route the teardown through the one
shared routine, and they call it while tl->view is still alive (unpinning a
stream and ending its grab both need a valid tl/view) and before free(tl).
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


HELPER = "qdwin_toplevel_release_dependents"

# (regex matching the definition, human name, what must come after the call)
PATHS = (
    (r"static void\s+qdwin_surface_removed\s*\(", "qdwin_surface_removed",
     ("weston_view_destroy(tl->view)", "free(tl)")),
    (r"static void\s+qdwin_nested_proxy_destroy\s*\(",
     "qdwin_nested_proxy_destroy",
     ("weston_shell_utils_curtain_destroy(", "free(tl)")),
)


def check_helper_releases_all_three(source):
    body, err = _function_body(
        source, r"static void\s+%s\s*\(\s*struct qdwin \*" % HELPER, HELPER)
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "qdwin_popup_teardown(" not in code:
        return fail(f"{HELPER} does not tear the chrome popup down")
    if "qdwin_popup_v1_send_dismissed(" not in code:
        return fail(f"{HELPER} tears the popup down without telling the client")
    if "qdwin_move_grab_end_for(" not in code:
        return fail(f"{HELPER} does not end a move-drag on this handle")
    if "qdwin_view_stream_terminate(" not in code:
        return fail(f"{HELPER} does not terminate the toplevel's view_streams")
    if "wl_list_for_each_safe(" not in code:
        return fail(f"{HELPER} walks view_streams unsafely — termination "
                    "removes the stream from the list it is iterating")
    if "vs->tl == tl" not in code:
        return fail(f"{HELPER} does not filter view_streams by source toplevel")
    return 0


def check_both_paths_route_through_helper(source):
    for regex, name, after in PATHS:
        body, err = _function_body(source, regex, name)
        if err:
            return fail(err)
        code = _strip_comments(body)
        call = code.find(HELPER + "(")
        if call < 0:
            return fail(f"{name} does not call {HELPER}() — a live popup, "
                        "move-grab or view_stream would outlive the freed "
                        "toplevel")
        # No path may re-implement the teardown inline: one routine or the
        # two paths drift apart again.
        if "qdwin_view_stream_terminate(" in code:
            return fail(f"{name} terminates view_streams inline instead of "
                        f"through {HELPER}()")
        for needle in after:
            flat = re.sub(r"\s+", "", code)
            pos = flat.find(re.sub(r"\s+", "", needle))
            if pos < 0:
                return fail(f"{name}: expected `{needle}` in this destroy path")
            if pos < len(re.sub(r"\s+", "", code[:call])):
                return fail(f"{name} calls {HELPER}() after `{needle}` — the "
                            "dependents must be released while tl and its "
                            "view are still valid")
    return 0


def check_destroy_cannot_recache_the_dying_proxy(source):
    """Ending a grab runs the default grab's focus() SYNCHRONOUSLY.

    weston_pointer_end_grab() reinstalls the default grab and calls its
    focus handler immediately; qdwin's handler re-picks a view and caches
    the proxy owning it in qdwin->active_input_proxy. During
    qdwin_nested_proxy_destroy the dying proxy's curtain is still mapped and
    still on qdwin->toplevels, so the teardown would hand the cache straight
    back to the toplevel it is about to free (codex round 1).

    Two independent mechanisms, both pinned: the picker skips a destroying
    proxy, and the cache is re-cleared after every callback-producing
    teardown.
    """
    body, err = _function_body(
        source, r"static struct qdwin_toplevel \*\s*\n?qdwin_proxy_for_view\s*\(",
        "qdwin_proxy_for_view")
    if err:
        body, err = _function_body(source, r"^qdwin_proxy_for_view\s*\(",
                                   "qdwin_proxy_for_view")
    if err:
        return fail(err)
    if "proxy_destroying" not in _strip_comments(body):
        return fail("qdwin_proxy_for_view can still select a proxy that is "
                    "being destroyed")

    body, err = _function_body(source, r"^qdwin_nested_proxy_destroy\s*\(",
                               "qdwin_nested_proxy_destroy")
    if err:
        return fail(err)
    code = _strip_comments(body)
    flat = re.sub(r"\s+", "", code)
    set_flag = flat.find("tl->proxy_destroying=true")
    if set_flag < 0:
        return fail("qdwin_nested_proxy_destroy does not mark the toplevel "
                    "as destroying")
    call = flat.find(HELPER + "(")
    if set_flag > call:
        return fail("qdwin_nested_proxy_destroy marks the toplevel destroying "
                    "only after releasing its dependents — the grab-focus "
                    "callback runs in between")
    clear = "if(qdwin->active_input_proxy==tl)qdwin->active_input_proxy=NULL;"
    if flat.count(clear) < 2:
        return fail("qdwin_nested_proxy_destroy does not re-clear "
                    "active_input_proxy after the dependent teardown")
    last_clear = flat.rfind(clear)
    unlink = flat.find("wl_list_remove(&tl->link)")
    if last_clear > unlink or last_clear < call:
        return fail("the final active_input_proxy clear must sit between the "
                    "dependent teardown and the unlink/free")
    return 0


def check_helper_is_declared_before_use(source):
    decl = re.search(r"static void %s\s*\(struct qdwin \*" % HELPER, source)
    define = re.search(r"static void\s*\n%s\s*\(struct qdwin \*" % HELPER,
                       source)
    if not decl or not define:
        return fail(f"{HELPER} needs a forward declaration and a definition")
    if decl.start() >= define.start():
        return fail(f"{HELPER} forward declaration must precede its definition")
    return 0


def main():
    if len(sys.argv) != 2:
        return fail("usage: test_toplevel_destroy_dependents.py <qdwin.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    for check in (check_helper_releases_all_three,
                  check_both_paths_route_through_helper,
                  check_destroy_cannot_recache_the_dying_proxy,
                  check_helper_is_declared_before_use):
        rc = check(source)
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
