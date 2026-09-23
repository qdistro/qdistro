#!/usr/bin/env python3
"""Static-invariant checks for the input-method-unstable-v2 implementation
(Bucket A / P1; see todo/issues/qdwin/app-compat-protocol-gaps.md).

input-method-v2 is the PRIVILEGED IME side of the text-input plane: a bound
input method receives raw keys (keyboard grab) and injects composed text into
the focused text_input. Because that is keystroke capture + arbitrary text
injection, the load-bearing properties are about GATING and SAFE TEARDOWN — and
those are exactly what a headless source-invariant test can pin without a
wl_seat (the live activate/compose path is seat/VM-gated, like text-input-v3:
tests/host/ probe + tests/gui/agent-protocol-audit.sh require_global).

Same parser/style as test_text_input.py. The invariants:

1. The manager global is GATED — created with bind_qdwin_input_method_manager
   at v1, and the bind handler rejects sandboxed/secctx clients AND enforces a
   uid check (allowed_ime_uid). It is NOT open like text-input-v3.
2. The global filter hides the input-method manager from sandboxed (secctx)
   clients (a silo app must not even see it).
3. One input method per seat: a second get_input_method on a seat that already
   has one sends `unavailable` (protocol-mandated), NEVER an implementation
   error, and never wires a second active IME.
4. The keyboard grab SUPPRESSES app delivery: the grab key handler forwards to
   the grab resource (zwp_input_method_keyboard_grab_v2_send_key) and must NOT
   call weston_keyboard_send_key (spec: do not further process a forwarded
   event). Releasing only ends the weston grab if it is still ours.
5. The IME->app commit is serial-guarded and activation-scoped (only forwards
   when active for a still-enabled text_input echoing our done serial).
6. Teardown: input methods are drained before text_inputs in qdwin_destroy,
   each resource neutralized, the grab ended only if still the keyboard's grab,
   and seat-destroy sends `unavailable`.
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


def check_manager_global_created_gated_v1(source):
    """The manager global must be created with the gating bind handler at
    version 1 — NOT open. (Contrast text-input-v3 which is open.)"""
    code = _strip_comments(source)
    m = re.search(
        r"wl_global_create\s*\([^;]*?"
        r"zwp_input_method_manager_v2_interface\s*,\s*(\d+)\s*,"
        r"[^;]*?bind_qdwin_input_method_manager", code, re.DOTALL)
    if not m:
        return fail("zwp_input_method_manager_v2 global is not created with "
                    "bind_qdwin_input_method_manager (must be gated, not open)")
    if m.group(1) != "1":
        return fail(f"input-method manager advertised at v{m.group(1)}, "
                    "expected v1 (the only interface version)")
    return 0


def check_bind_rejects_sandbox_and_uid(source):
    """The bind handler must run the shared identity gate
    (qdwin_ime_family_bind_allowed) and return on its failure BEFORE creating
    the manager resource (fail-closed). The gate itself must reject
    secctx/sandboxed clients AND enforce a uid check.

    The gate is shared with the virtual-keyboard companion so the two
    privileged protocols cannot drift apart; this check pins both the
    delegation+ordering at the bind and the actual gate logic in the helper."""
    body, err = _function_body(
        source, r"static void\s+bind_qdwin_input_method_manager\s*\(",
        "bind_qdwin_input_method_manager")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "qdwin_ime_family_bind_allowed" not in code:
        return fail("input-method bind does not call the shared identity gate "
                    "qdwin_ime_family_bind_allowed")
    # Fail-closed ordering: the gate call (and its `return` on failure) must
    # precede wl_resource_create.
    gate = code.find("qdwin_ime_family_bind_allowed")
    crt = code.find("wl_resource_create")
    if gate == -1 or crt == -1 or gate > crt:
        return fail("input-method bind creates the manager resource before "
                    "running the identity gate (not fail-closed)")
    if not re.search(r"if\s*\(\s*!\s*qdwin_ime_family_bind_allowed[^)]*\)[^;{]*"
                     r"return", code, re.DOTALL):
        return fail("input-method bind does not return on a failed identity "
                    "gate (would create the resource for a rejected client)")

    # The shared gate must reject sandboxed/secctx clients and enforce the uid.
    gate_body, err = _function_body(
        source, r"static bool\s+qdwin_ime_family_bind_allowed\s*\(",
        "qdwin_ime_family_bind_allowed")
    if err:
        return fail(err)
    if "qdwin_secctx_client_find" not in gate_body:
        return fail("identity gate does not consult qdwin_secctx_client_find "
                    "(a sandboxed silo client could become the IME and "
                    "keylog/inject across silos)")
    if "post_implementation_error" not in gate_body:
        return fail("identity gate never rejects (no post_implementation_error "
                    "path)")
    if "allowed_ime_uid" not in gate_body and "allowed" not in gate_body:
        return fail("identity gate has no uid gate (allowed_ime_uid)")
    # The gate must reject (post_implementation_error + return false) before it
    # ever returns true.
    if not re.search(r"post_implementation_error.*return\s+false",
                     gate_body, re.DOTALL):
        return fail("identity gate does not return false after "
                    "post_implementation_error (not fail-closed)")
    return 0


def check_global_filter_hides_ime_from_sandbox(source, logic_source):
    """The wl_global filter must hide the input-method manager global from
    sandboxed (secctx) clients."""
    classify, err = _function_body(
        source, r"static enum qdwin_global_kind\s+qdwin_classify_global\s*\(",
        "qdwin_classify_global")
    if err:
        return fail(err)
    if "input_method_manager_global" not in classify or \
       "QDWIN_GLOBAL_INPUT_METHOD" not in classify:
        return fail("global classifier does not map "
                    "input_method_manager_global to QDWIN_GLOBAL_INPUT_METHOD")

    body, err = _function_body(
        source, r"static bool\s+qdwin_secctx_global_filter\s*\(",
        "qdwin_secctx_global_filter")
    if err:
        return fail(err)
    if "qdwin_secctx_client_find" not in body:
        return fail("global filter does not classify silo clients with "
                    "qdwin_secctx_client_find")
    if "qdwin_global_visible" not in body:
        return fail("global filter does not consult the qdwin_global_visible "
                    "policy matrix")

    policy, err = _function_body(
        logic_source, r"bool\s+qdwin_global_visible\s*\(",
        "qdwin_global_visible")
    if err:
        return fail(err)
    if "QDWIN_GLOBAL_INPUT_METHOD" not in policy:
        return fail("policy matrix has no input-method global row")
    if "cred != QDWIN_CRED_SECCTX" not in _strip_comments(policy):
        return fail("policy matrix does not hide IME/VK from secctx clients")
    return 0


def check_one_ime_per_seat_unavailable(source):
    """get_input_method must enforce one IME per seat by sending `unavailable`
    (NOT an implementation error) to a second binder, and not wire it active."""
    body, err = _function_body(
        source, r"static void\s+qdwin_im_manager_get_input_method\s*\(",
        "qdwin_im_manager_get_input_method")
    if err:
        return fail(err)
    if "zwp_input_method_v2_send_unavailable" not in body:
        return fail("get_input_method never sends `unavailable` (cannot "
                    "enforce one-IME-per-seat per protocol)")
    if "post_implementation_error" in body:
        return fail("get_input_method rejects a duplicate with an "
                    "implementation error — the protocol requires "
                    "`unavailable` on the new object instead")
    if "inert" not in body:
        return fail("get_input_method does not mark a duplicate IME inert")
    # The pre-existing-IME probe MUST happen before this IME is inserted into
    # the list, else qdwin_im_for_seat finds `im` itself and the duplicate is
    # never made inert (two active IMEs per seat). Pin the ordering.
    code = _strip_comments(body)
    probe = code.find("qdwin_im_for_seat")
    insert = code.find("wl_list_insert")
    if probe == -1:
        return fail("get_input_method does not probe qdwin_im_for_seat for a "
                    "pre-existing IME")
    if insert != -1 and probe > insert:
        return fail("get_input_method probes for a pre-existing IME AFTER "
                    "inserting itself into the list — qdwin_im_for_seat would "
                    "find `im` itself and never make the duplicate inert")
    return 0


def check_grab_suppresses_app_delivery(source):
    """The keyboard-grab key handler must forward to the grab resource and
    must NOT call weston_keyboard_send_key (spec: the compositor must not
    further process an event after forwarding it to the grab holder)."""
    body, err = _function_body(
        source, r"static void\s+qdwin_im_grab_key\s*\(", "qdwin_im_grab_key")
    if err:
        return fail(err)
    if "zwp_input_method_keyboard_grab_v2_send_key" not in body:
        return fail("grab key handler does not forward to the IME grab "
                    "resource")
    if "weston_keyboard_send_key" in body:
        return fail("grab key handler ALSO calls weston_keyboard_send_key — "
                    "double delivery; a grabbed key must not reach the app")
    return 0


def check_grab_release_only_ends_own_grab(source):
    """Releasing the grab (resource destroy) must only end the weston grab if
    it is still the keyboard's active grab (no clobbering another grab)."""
    body, err = _function_body(
        source, r"static void\s+qdwin_im_grab_resource_destroy\s*\(",
        "qdwin_im_grab_resource_destroy")
    if err:
        return fail(err)
    if "weston_keyboard_end_grab" not in body:
        return fail("grab destroy never ends the weston grab")
    if not re.search(r"->grab\s*==\s*&g->base", body):
        return fail("grab destroy ends the grab without checking it is still "
                    "the keyboard's active grab (could corrupt another grab)")
    return 0


def check_commit_is_serial_and_activation_guarded(source):
    """The IME commit must only forward to the text_input when the IME is
    active for a still-enabled text_input and the serial matches our done
    count (stale/inactive commits are dropped)."""
    body, err = _function_body(
        source, r"static void\s+qdwin_im_req_commit\s*\(", "qdwin_im_req_commit")
    if err:
        return fail(err)
    if "done_count" not in body or "serial" not in body:
        return fail("IME commit does not check the serial against done_count "
                    "(stale composed state could be applied)")
    if "current_enabled" not in body:
        return fail("IME commit does not verify the target text_input is "
                    "still enabled")
    if "im->active" not in body:
        return fail("IME commit does not verify the IME is active")
    return 0


def check_manager_resource_is_neutralizable(source):
    """A manager resource can outlive the plugin; get_input_method must resolve
    qdwin via a tracked manager node and NULL-guard it (returning an inert
    object), and qdwin_destroy must drain + neutralize the manager nodes —
    otherwise a late get_input_method dereferences freed qdwin."""
    body, err = _function_body(
        source, r"static void\s+qdwin_im_manager_get_input_method\s*\(",
        "qdwin_im_manager_get_input_method")
    if err:
        return fail(err)
    if not re.search(r"mgr\s*\?\s*mgr->qdwin\s*:\s*NULL", body):
        return fail("get_input_method does not resolve qdwin via the manager "
                    "node (a neutralized manager would deref freed qdwin)")
    if not re.search(r"if\s*\(\s*!qdwin\s*\)", body):
        return fail("get_input_method does not branch on a NULL qdwin "
                    "(inert path for a manager outliving the plugin)")
    destroy, err = _function_body(
        source, r"static void\s+qdwin_destroy\s*\(", "qdwin_destroy")
    if err:
        return fail(err)
    if "qdwin_input_method_managers_destroy_all" not in _strip_comments(destroy):
        return fail("qdwin_destroy does not drain input-method managers "
                    "before free(qdwin)")
    drain, err = _function_body(
        source, r"static void\s+qdwin_input_method_managers_destroy_all\s*\(",
        "qdwin_input_method_managers_destroy_all")
    if err:
        return fail(err)
    if not re.search(r"wl_resource_set_user_data\s*\(\s*mgr->resource\s*,"
                     r"\s*NULL\s*\)", drain):
        return fail("manager drain does not neutralize mgr->resource "
                    "user_data (late get_input_method would UAF)")
    return 0


def check_seat_destroy_sends_unavailable(source):
    """When the seat is destroyed, the IME must be told it is `unavailable`
    and detached (no dangling weston_seat pointer / grab).

    UAF guard: vendored weston_seat_release() frees the seat's weston_keyboard
    (weston_keyboard_destroy) BEFORE emitting seat->destroy_signal, and that
    destroy does NOT cancel an active grab (so our .cancel never fires to null
    g->keyboard). By the time this listener runs, a cached g->keyboard is a
    dangling pointer; qdwin_im_detach then reads g->keyboard->grab and may call
    weston_keyboard_end_grab(g->keyboard) — a use-after-free. The handler must
    therefore null im->grab->keyboard up front, BEFORE qdwin_im_detach, so the
    shared detach path never dereferences the freed keyboard."""
    body, err = _function_body(
        source, r"static void\s+qdwin_im_seat_destroyed\s*\(",
        "qdwin_im_seat_destroyed")
    if err:
        return fail(err)
    if "zwp_input_method_v2_send_unavailable" not in body:
        return fail("seat-destroy does not send `unavailable` to the IME")
    if "qdwin_im_detach" not in body:
        return fail("seat-destroy does not detach the IME (dangling seat/grab)")
    code = _strip_comments(body)
    if not re.search(r"im->grab\s*->\s*keyboard\s*=\s*NULL", code):
        return fail("seat-destroy does not null im->grab->keyboard before "
                    "detach — vendored weston_seat_release frees the keyboard "
                    "before this signal, so qdwin_im_detach would deref a freed "
                    "weston_keyboard (latent UAF)")
    null_pos = code.find("im->grab")
    detach_pos = code.find("qdwin_im_detach")
    if null_pos == -1 or detach_pos == -1 or null_pos > detach_pos:
        return fail("seat-destroy nulls im->grab->keyboard AFTER qdwin_im_detach "
                    "(or not at all) — detach would already have dereferenced "
                    "the freed keyboard")
    return 0


def check_teardown_drains_input_methods_first(source):
    """qdwin_destroy must drain input methods (before text_inputs, since an
    IME references text_input via active_ti), and the drain must neutralize
    each resource's user_data and unlink it."""
    destroy, err = _function_body(
        source, r"static void\s+qdwin_destroy\s*\(", "qdwin_destroy")
    if err:
        return fail(err)
    d = _strip_comments(destroy)
    if "qdwin_input_methods_destroy_all" not in d:
        return fail("qdwin_destroy does not drain input methods before "
                    "free(qdwin)")
    im_pos = d.find("qdwin_input_methods_destroy_all")
    ti_pos = d.find("qdwin_text_inputs_destroy_all")
    if ti_pos != -1 and im_pos > ti_pos:
        return fail("qdwin_destroy drains text_inputs before input methods — "
                    "an IME's active_ti could dangle")
    drain, err = _function_body(
        source, r"static void\s+qdwin_input_methods_destroy_all\s*\(",
        "qdwin_input_methods_destroy_all")
    if err:
        return fail(err)
    if "wl_list_for_each_safe" not in drain:
        return fail("input-method drain does not iterate safely")
    if not re.search(r"wl_resource_set_user_data\s*\(\s*im->resource\s*,"
                     r"\s*NULL\s*\)", drain):
        return fail("input-method drain does not neutralize im->resource "
                    "user_data (late destroy callback would UAF)")
    if "qdwin_im_detach" not in drain:
        return fail("input-method drain does not detach (grab/seat listener "
                    "would leak/dangle)")
    return 0


def main():
    if len(sys.argv) != 3:
        return fail("usage: test_input_method.py <qdwin.c> <qdwin-logic.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    logic_source = Path(sys.argv[2]).read_text(encoding="utf-8")

    for check in (
        check_manager_global_created_gated_v1,
        check_bind_rejects_sandbox_and_uid,
        check_one_ime_per_seat_unavailable,
        check_grab_suppresses_app_delivery,
        check_grab_release_only_ends_own_grab,
        check_commit_is_serial_and_activation_guarded,
        check_manager_resource_is_neutralizable,
        check_seat_destroy_sends_unavailable,
        check_teardown_drains_input_methods_first,
    ):
        rc = check(source)
        if rc:
            return rc
    rc = check_global_filter_hides_ime_from_sandbox(source, logic_source)
    if rc:
        return rc

    print("PASS: input-method-v2 (manager gated at v1, bind rejects "
          "sandboxed/uid-mismatched clients fail-closed, global filter hides "
          "it from silos, one-IME-per-seat via unavailable, keyboard grab "
          "suppresses app delivery and releases safely, commit is "
          "serial/activation-guarded, seat-destroy + teardown detach cleanly)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
