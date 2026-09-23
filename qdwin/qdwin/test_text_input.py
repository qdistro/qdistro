#!/usr/bin/env python3
"""Static-invariant checks for the text-input-unstable-v3 foundation (Bucket
A / P1; see todo/issues/qdwin/app-compat-protocol-gaps.md).

Same style as test_rdp_focus_rebind.py / test_nested_identity_policy.py: parse
qdwin.c and assert the structural properties of qdwin's zwp_text_input_v3
implementation. This is the fully-headless regression guard — the live
enter/leave path needs a wl_seat, which the weston headless backend does not
expose, so the functional probe (qdwin-textinput-probe) is seat/VM-gated
(tests/host/21-text-input.md). These source invariants run without a seat.

The two load-bearing properties:

1. enter/leave is FOCUS-driven and client-scoped: qdwin_text_input_update_focus
   is called from the keyboard focus_signal handler AND from get_text_input,
   sends zwp_text_input_v3 enter/leave, and only enters a surface owned by the
   text_input's own client.

2. the FOUNDATION-ONLY / "inert without an input-method" contract: qdwin must
   NEVER send preedit_string / commit_string / delete_surrounding_text / done
   (there is no input-method-v2 IME wired in yet). A future regression that
   starts emitting these without a real IME would be a correctness bug, so we
   assert their absence from the whole file.
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
    """Brace-balanced body of the first function *definition* matching
    signature_regex (skips forward declarations)."""
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
            continue  # forward declaration
        start = j
        depth = 0
        for i in range(start, len(source)):
            c = source[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return source[start:i + 1], None
        return None, f"{name}: unbalanced braces"
    return None, f"{name} not found (no definition)"


def check_manager_global_created_open_v1(source):
    """The manager global must be created OPEN (no bind gate) at version 1
    with the text-input bind handler."""
    code = _strip_comments(source)
    m = re.search(
        r"wl_global_create\s*\([^;]*?"
        r"zwp_text_input_manager_v3_interface\s*,\s*(\d+)\s*,"
        r"[^;]*?bind_qdwin_text_input_manager", code, re.DOTALL)
    if not m:
        return fail("zwp_text_input_manager_v3 global is not created with "
                    "bind_qdwin_text_input_manager")
    if m.group(1) != "1":
        return fail(f"text-input manager advertised at v{m.group(1)}, "
                    "expected v1 (lowest common denominator; v2 adds "
                    "input-panel/actions qdwin does not drive)")
    return 0


def check_manager_impl_has_get_text_input(source):
    """The manager impl (an aggregate initializer, not a function) must wire
    .get_text_input to the real handler."""
    m = re.search(
        r"zwp_text_input_manager_v3_interface\s+"
        r"qdwin_text_input_manager_impl\s*=\s*\{(.*?)\}\s*;",
        source, re.DOTALL)
    if not m:
        return fail("qdwin_text_input_manager_impl not found")
    body = m.group(1)
    if not re.search(r"\.get_text_input\s*=", body):
        return fail("manager impl has no .get_text_input")
    return 0


def check_get_text_input_wires_focus(source):
    """get_text_input must resolve the seat and call update_focus so a
    text_input created on an already-focused surface gets its initial
    enter."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_text_input_manager_get_text_input\s*\(",
        "qdwin_text_input_manager_get_text_input")
    if err:
        return fail(err)
    if "wl_resource_create" not in body or \
       "zwp_text_input_v3_interface" not in body:
        return fail("get_text_input does not create a zwp_text_input_v3 "
                    "resource")
    if "qdwin_text_input_update_focus" not in body:
        return fail("get_text_input does not call "
                    "qdwin_text_input_update_focus (no initial enter on an "
                    "already-focused surface)")
    return 0


def check_focus_listener_drives_text_input(source):
    """The keyboard focus_signal handler must drive text-input enter/leave."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_on_keyboard_focus_changed\s*\(",
        "qdwin_on_keyboard_focus_changed")
    if err:
        return fail(err)
    if "qdwin_text_input_update_focus" not in body:
        return fail("keyboard focus handler does not call "
                    "qdwin_text_input_update_focus (enter/leave would not "
                    "follow focus)")
    return 0


def check_update_focus_is_client_scoped(source):
    """update_focus must send enter/leave and only enter a surface owned by
    the text_input's own client (the wl_surface resource passed in enter must
    belong to that client — no cross-client enter)."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_text_input_update_focus\s*\(",
        "qdwin_text_input_update_focus")
    if err:
        return fail(err)
    if "zwp_text_input_v3_send_enter" not in body:
        return fail("update_focus never sends enter")
    if "zwp_text_input_v3_send_leave" not in body:
        return fail("update_focus never sends leave")
    if "wl_resource_get_client" not in body or "ti->client" not in body:
        return fail("update_focus does not gate enter on the focused "
                    "surface's client matching the text_input's client "
                    "(cross-client enter risk)")
    return 0


def check_ime_events_only_from_input_method(source):
    """IME-driven contract (input-method-v2 now wired in): qdwin may send
    preedit_string / commit_string / delete_surrounding_text / done to a
    text_input, but ONLY from the input-method commit path
    (qdwin_im_req_commit), never spontaneously from a text-input request or
    focus path. A stray send elsewhere would mean the compositor fabricates
    composed text with no IME behind it — exactly the regression the old
    "inert" guard protected against, narrowed to allow the real IME bridge."""
    code = _strip_comments(source)
    forbidden = (
        "zwp_text_input_v3_send_preedit_string",
        "zwp_text_input_v3_send_commit_string",
        "zwp_text_input_v3_send_delete_surrounding_text",
        "zwp_text_input_v3_send_done",
    )
    commit_body, err = _function_body(
        source, r"static void\s+qdwin_im_req_commit\s*\(",
        "qdwin_im_req_commit")
    if err:
        return fail(err)
    commit_body = _strip_comments(commit_body)
    for name in forbidden:
        total = code.count(name)
        in_commit = commit_body.count(name)
        if total != in_commit:
            return fail("IME event "
                        f"{name} is emitted outside qdwin_im_req_commit "
                        f"({total - in_commit} stray use(s)) — composed text "
                        "must only flow from an input-method commit.")
    return 0


def check_entered_destroy_is_leave_free(source):
    """When the entered surface is destroyed, qdwin must clear tracking
    WITHOUT sending leave (the wl_surface resource is already gone — sending
    leave with it would be a use-after-free)."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_text_input_entered_destroyed\s*\(",
        "qdwin_text_input_entered_destroyed")
    if err:
        return fail(err)
    if "qdwin_text_input_clear_entered" not in body:
        return fail("entered-destroy handler does not clear tracking")
    if "send_leave" in body:
        return fail("entered-destroy handler sends leave on an already-"
                    "destroyed surface (use-after-free)")
    return 0


def check_resource_destroy_unlinks(source):
    """Destroying a text_input must clear its entered listener and unlink it
    from the tracking list (no dangling list node / leaked listener)."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_text_input_resource_destroy\s*\(",
        "qdwin_text_input_resource_destroy")
    if err:
        return fail(err)
    if "qdwin_text_input_clear_entered" not in body:
        return fail("resource destroy does not clear the entered listener")
    if "wl_list_remove(&ti->link)" not in body:
        return fail("resource destroy does not unlink ti from the list")
    return 0


def check_teardown_drains_text_inputs(source):
    """qdwin_destroy must drain live text_input objects before free(qdwin),
    and the drain must neutralize each resource's user_data (so a late
    resource-destroy callback no-ops instead of touching the freed list) and
    unlink the node. Otherwise a text_input outliving the plugin runs its
    destroy handler against the freed qdwin->text_inputs head (UAF)."""
    destroy, err = _function_body(
        source, r"static void\s+qdwin_destroy\s*\(", "qdwin_destroy")
    if err:
        return fail(err)
    if "qdwin_text_inputs_destroy_all" not in _strip_comments(destroy):
        return fail("qdwin_destroy does not drain text_inputs before "
                    "free(qdwin) (UAF on a text_input outliving the plugin)")
    drain, err = _function_body(
        source, r"static void\s+qdwin_text_inputs_destroy_all\s*\(",
        "qdwin_text_inputs_destroy_all")
    if err:
        return fail(err)
    if "wl_list_for_each_safe" not in drain:
        return fail("text_inputs drain does not iterate safely (frees while "
                    "iterating)")
    if not re.search(r"wl_resource_set_user_data\s*\(\s*ti->resource\s*,"
                     r"\s*NULL\s*\)", drain):
        return fail("text_inputs drain does not neutralize ti->resource "
                    "user_data (late destroy callback would UAF)")
    if "wl_list_remove(&ti->link)" not in drain:
        return fail("text_inputs drain does not unlink ti from the list")
    return 0


def check_get_text_input_null_guards_manager(source):
    """get_text_input must tolerate a neutralized manager resource (user_data
    cleared at teardown): resolve qdwin via the manager node and guard NULL,
    not dereference it blindly."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_text_input_manager_get_text_input\s*\(",
        "qdwin_text_input_manager_get_text_input")
    if err:
        return fail(err)
    if not re.search(r"mgr\s*\?\s*mgr->qdwin\s*:\s*NULL", body):
        return fail("get_text_input does not NULL-guard the manager node "
                    "(neutralized manager would deref freed qdwin)")
    if not re.search(r"if\s*\(\s*!qdwin\s*\)", body):
        return fail("get_text_input does not branch on a NULL qdwin")
    return 0


def check_teardown_drains_managers(source):
    """qdwin_destroy must also drain live manager resources before
    free(qdwin), neutralizing each resource's user_data so a late
    get_text_input / resource-destroy no-ops instead of touching freed
    qdwin."""
    destroy, err = _function_body(
        source, r"static void\s+qdwin_destroy\s*\(", "qdwin_destroy")
    if err:
        return fail(err)
    if "qdwin_text_input_managers_destroy_all" not in _strip_comments(destroy):
        return fail("qdwin_destroy does not drain text_input_managers before "
                    "free(qdwin)")
    drain, err = _function_body(
        source, r"static void\s+qdwin_text_input_managers_destroy_all\s*\(",
        "qdwin_text_input_managers_destroy_all")
    if err:
        return fail(err)
    if "wl_list_for_each_safe" not in drain:
        return fail("manager drain does not iterate safely")
    if not re.search(r"wl_resource_set_user_data\s*\(\s*mgr->resource\s*,"
                     r"\s*NULL\s*\)", drain):
        return fail("manager drain does not neutralize mgr->resource "
                    "user_data (late get_text_input/destroy would UAF)")
    if "wl_list_remove(&mgr->link)" not in drain:
        return fail("manager drain does not unlink mgr from the list")
    return 0


def main():
    if len(sys.argv) != 2:
        return fail("usage: test_text_input.py <qdwin.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")

    checks = (
        check_manager_global_created_open_v1,
        check_manager_impl_has_get_text_input,
        check_get_text_input_wires_focus,
        check_get_text_input_null_guards_manager,
        check_focus_listener_drives_text_input,
        check_update_focus_is_client_scoped,
        check_ime_events_only_from_input_method,
        check_entered_destroy_is_leave_free,
        check_resource_destroy_unlinks,
        check_teardown_drains_text_inputs,
        check_teardown_drains_managers,
    )
    for check in checks:
        rc = check(source)
        if rc:
            return rc

    print("PASS: text-input-v3 foundation (manager open at v1, get_text_input "
          "+ focus_signal both drive client-scoped enter/leave, IME events "
          "flow only from the input-method commit path, entered-destroy is "
          "leave-free, resource destroy unlinks cleanly, teardown drains live "
          "objects before free)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
