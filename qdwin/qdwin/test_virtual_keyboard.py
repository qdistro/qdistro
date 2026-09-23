#!/usr/bin/env python3
"""Static-invariant checks for the virtual-keyboard-unstable-v1 implementation
(Bucket A / P1 companion; see todo/open-followups.md +
todo/issues/qdwin/app-compat-protocol-gaps.md).

virtual-keyboard-v1 is the key-injection companion of the privileged
input-method-v2: a grabbing IME passes the keys it does NOT compose back to the
focused app by injecting them through a virtual keyboard. Because that is
arbitrary keystroke injection into whatever app is focused, the load-bearing
properties are the SAME as input-method-v2: GATING (only the trusted IME, never
a silo) and SAFE TEARDOWN — exactly what a headless source-invariant test can
pin without a wl_seat (the live inject path is seat/VM-gated: qdwin-vkbd-probe +
tests/host/25-virtual-keyboard.md).

Same parser/style as test_input_method.py. The invariants:

1. The manager global is GATED — created with bind_qdwin_virtual_keyboard_manager
   at v1 (NOT open), and the bind delegates to the SHARED identity gate
   qdwin_ime_family_bind_allowed and returns on failure before creating the
   resource (fail-closed). Sharing the gate with input-method-v2 guarantees the
   two privileged protocols cannot drift apart.
2. The global filter hides the virtual-keyboard manager from sandboxed (secctx)
   clients (a silo app must not even see it).
3. key/modifiers are no_keymap-guarded: a key/modifiers before a keymap is set is
   the protocol no_keymap error; keymap validates the format
   (invalid_keymap_format) and always consumes the fd.
4. Injection goes through the seat: key uses notify_key(... STATE_UPDATE_AUTOMATIC)
   and modifiers uses xkb_state_update_mask + notify_modifiers. The shared seat
   keymap is NOT swapped to a per-virtual-keyboard one.
5. The manager resource can outlive the plugin: create_virtual_keyboard resolves
   qdwin via the tracked manager node and NULL-guards it, and qdwin_destroy
   drains + neutralizes the manager nodes.
6. Teardown / seat-destroy: virtual keyboards are drained with each resource
   neutralized and detached; seat-destroy detaches (no dangling seat pointer).
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
    version 1 — NOT open."""
    code = _strip_comments(source)
    m = re.search(
        r"wl_global_create\s*\([^;]*?"
        r"zwp_virtual_keyboard_manager_v1_interface\s*,\s*(\d+)\s*,"
        r"[^;]*?bind_qdwin_virtual_keyboard_manager", code, re.DOTALL)
    if not m:
        return fail("zwp_virtual_keyboard_manager_v1 global is not created with "
                    "bind_qdwin_virtual_keyboard_manager (must be gated)")
    if m.group(1) != "1":
        return fail(f"virtual-keyboard manager advertised at v{m.group(1)}, "
                    "expected v1 (the only interface version)")
    return 0


def check_bind_uses_shared_gate_fail_closed(source):
    """The bind handler must run the SHARED identity gate and return on its
    failure before creating the manager resource (fail-closed)."""
    body, err = _function_body(
        source, r"static void\s+bind_qdwin_virtual_keyboard_manager\s*\(",
        "bind_qdwin_virtual_keyboard_manager")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "qdwin_ime_family_bind_allowed" not in code:
        return fail("virtual-keyboard bind does not call the shared identity "
                    "gate qdwin_ime_family_bind_allowed (could drift from the "
                    "input-method gate)")
    gate = code.find("qdwin_ime_family_bind_allowed")
    crt = code.find("wl_resource_create")
    if gate == -1 or crt == -1 or gate > crt:
        return fail("virtual-keyboard bind creates the manager resource before "
                    "running the identity gate (not fail-closed)")
    if not re.search(r"if\s*\(\s*!\s*qdwin_ime_family_bind_allowed[^)]*\)[^;{]*"
                     r"return", code, re.DOTALL):
        return fail("virtual-keyboard bind does not return on a failed identity "
                    "gate (would create the resource for a rejected client)")
    return 0


def check_global_filter_hides_vk_from_sandbox(source, logic_source):
    """The wl_global filter must hide the virtual-keyboard manager global from
    sandboxed (secctx) clients."""
    classify, err = _function_body(
        source, r"static enum qdwin_global_kind\s+qdwin_classify_global\s*\(",
        "qdwin_classify_global")
    if err:
        return fail(err)
    if "virtual_keyboard_manager_global" not in classify or \
       "QDWIN_GLOBAL_VIRTUAL_KEYBOARD" not in classify:
        return fail("global classifier does not map "
                    "virtual_keyboard_manager_global to "
                    "QDWIN_GLOBAL_VIRTUAL_KEYBOARD")

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
    if "QDWIN_GLOBAL_VIRTUAL_KEYBOARD" not in policy:
        return fail("policy matrix has no virtual-keyboard global row")
    if "cred != QDWIN_CRED_SECCTX" not in _strip_comments(policy):
        return fail("policy matrix does not hide IME/VK from secctx clients")
    return 0


def check_key_and_modifiers_no_keymap_guarded(source):
    """key/modifiers must be rejected with the no_keymap protocol error before a
    keymap is set; the keymap handler must validate the format and consume fd."""
    keyb, err = _function_body(
        source, r"static void\s+qdwin_vk_req_key\s*\(", "qdwin_vk_req_key")
    if err:
        return fail(err)
    if "has_keymap" not in keyb or "NO_KEYMAP" not in keyb:
        return fail("key handler does not reject key-before-keymap with the "
                    "no_keymap protocol error")
    modb, err = _function_body(
        source, r"static void\s+qdwin_vk_req_modifiers\s*\(",
        "qdwin_vk_req_modifiers")
    if err:
        return fail(err)
    if "has_keymap" not in modb or "NO_KEYMAP" not in modb:
        return fail("modifiers handler does not reject modifiers-before-keymap "
                    "with the no_keymap protocol error")
    kmb, err = _function_body(
        source, r"static void\s+qdwin_vk_req_keymap\s*\(", "qdwin_vk_req_keymap")
    if err:
        return fail(err)
    if "INVALID_KEYMAP_FORMAT" not in kmb:
        return fail("keymap handler does not validate the format "
                    "(invalid_keymap_format)")
    if "close(" not in kmb:
        return fail("keymap handler does not close the fd (descriptor leak)")
    if "has_keymap = 1" not in kmb:
        return fail("keymap handler never marks has_keymap (key/modifiers would "
                    "stay blocked even after a valid keymap)")
    return 0


def check_injection_goes_through_the_seat(source):
    """key injects via notify_key(STATE_UPDATE_AUTOMATIC); modifiers via
    xkb_state_update_mask + notify_modifiers. The shared seat keymap must NOT be
    swapped to a per-virtual-keyboard one."""
    keyb, err = _function_body(
        source, r"static void\s+qdwin_vk_req_key\s*\(", "qdwin_vk_req_key")
    if err:
        return fail(err)
    if "notify_key" not in keyb:
        return fail("key handler does not inject via notify_key")
    if "STATE_UPDATE_AUTOMATIC" not in keyb:
        return fail("key handler does not use STATE_UPDATE_AUTOMATIC (modifier "
                    "keycodes would not update the seat xkb state)")
    modb, err = _function_body(
        source, r"static void\s+qdwin_vk_req_modifiers\s*\(",
        "qdwin_vk_req_modifiers")
    if err:
        return fail(err)
    if "xkb_state_update_mask" not in modb or "notify_modifiers" not in modb:
        return fail("modifiers handler does not set the xkb mask + "
                    "notify_modifiers on the seat")
    # The seat keymap must not be swapped (would mutate the real keyboard for
    # every client and race the hardware keyboard — see the section comment).
    kmb, err = _function_body(
        source, r"static void\s+qdwin_vk_req_keymap\s*\(", "qdwin_vk_req_keymap")
    if err:
        return fail(err)
    for forbidden in ("weston_seat_init_keyboard", "weston_keyboard_set_keymap",
                      "pending_keymap"):
        if forbidden in kmb:
            return fail(f"keymap handler swaps the seat keymap ({forbidden}) — "
                        "must not mutate the shared seat keyboard")
    return 0


def check_same_client_passthrough(source):
    """A key/modifiers injected by the IME's OWN virtual keyboard must reach the
    focused app, not loop back into the IME grab. The inject handlers mark the
    injecting client (vk_injecting_client) and the IME grab handlers bypass to
    the default grab for that client."""
    keyb, err = _function_body(
        source, r"static void\s+qdwin_vk_req_key\s*\(", "qdwin_vk_req_key")
    if err:
        return fail(err)
    if "vk_injecting_client" not in keyb:
        return fail("key inject does not set vk_injecting_client — an active "
                    "IME grab would loop the re-injected key back to the IME "
                    "instead of delivering it to the app")
    modb, err = _function_body(
        source, r"static void\s+qdwin_vk_req_modifiers\s*\(",
        "qdwin_vk_req_modifiers")
    if err:
        return fail(err)
    if "vk_injecting_client" not in modb:
        return fail("modifiers inject does not set vk_injecting_client")
    # The IME grab key handler must bypass to the default grab for the injecting
    # client (so the re-injected key is delivered to the app).
    gk, err = _function_body(
        source, r"static void\s+qdwin_im_grab_key\s*\(", "qdwin_im_grab_key")
    if err:
        return fail(err)
    if "vk_injecting_client" not in gk or "default_grab" not in gk:
        return fail("IME grab key handler does not pass same-client "
                    "virtual-keyboard injections through to the default grab "
                    "(feedback loop: re-injected keys never reach the app)")
    gm, err = _function_body(
        source, r"static void\s+qdwin_im_grab_modifiers\s*\(",
        "qdwin_im_grab_modifiers")
    if err:
        return fail(err)
    if "vk_injecting_client" not in gm or "default_grab" not in gm:
        return fail("IME grab modifiers handler does not pass same-client "
                    "virtual-keyboard injections through to the default grab")
    return 0


def check_manager_resource_is_neutralizable(source):
    """create_virtual_keyboard must resolve qdwin via a tracked manager node and
    NULL-guard it, and qdwin_destroy must drain + neutralize the manager nodes."""
    body, err = _function_body(
        source, r"static void\s+qdwin_vk_manager_create_virtual_keyboard\s*\(",
        "qdwin_vk_manager_create_virtual_keyboard")
    if err:
        return fail(err)
    if not re.search(r"mgr\s*\?\s*mgr->qdwin\s*:\s*NULL", body):
        return fail("create_virtual_keyboard does not resolve qdwin via the "
                    "manager node (a neutralized manager would deref freed qdwin)")
    if not re.search(r"if\s*\(\s*!qdwin\s*\)", body):
        return fail("create_virtual_keyboard does not branch on a NULL qdwin "
                    "(inert path for a manager outliving the plugin)")
    destroy, err = _function_body(
        source, r"static void\s+qdwin_destroy\s*\(", "qdwin_destroy")
    if err:
        return fail(err)
    d = _strip_comments(destroy)
    if "qdwin_virtual_keyboard_managers_destroy_all" not in d:
        return fail("qdwin_destroy does not drain virtual-keyboard managers "
                    "before free(qdwin)")
    drain, err = _function_body(
        source,
        r"static void\s+qdwin_virtual_keyboard_managers_destroy_all\s*\(",
        "qdwin_virtual_keyboard_managers_destroy_all")
    if err:
        return fail(err)
    if not re.search(r"wl_resource_set_user_data\s*\(\s*mgr->resource\s*,"
                     r"\s*NULL\s*\)", drain):
        return fail("manager drain does not neutralize mgr->resource user_data "
                    "(late create_virtual_keyboard would UAF)")
    return 0


def check_seat_destroy_and_teardown_detach(source):
    """seat-destroy must detach; qdwin_destroy must drain virtual keyboards with
    each resource neutralized and detached."""
    sd, err = _function_body(
        source, r"static void\s+qdwin_vk_seat_destroyed\s*\(",
        "qdwin_vk_seat_destroyed")
    if err:
        return fail(err)
    if "qdwin_virtual_keyboard_detach" not in sd:
        return fail("seat-destroy does not detach the virtual keyboard "
                    "(dangling seat pointer)")
    destroy, err = _function_body(
        source, r"static void\s+qdwin_destroy\s*\(", "qdwin_destroy")
    if err:
        return fail(err)
    if "qdwin_virtual_keyboards_destroy_all" not in _strip_comments(destroy):
        return fail("qdwin_destroy does not drain virtual keyboards before "
                    "free(qdwin)")
    drain, err = _function_body(
        source, r"static void\s+qdwin_virtual_keyboards_destroy_all\s*\(",
        "qdwin_virtual_keyboards_destroy_all")
    if err:
        return fail(err)
    if "wl_list_for_each_safe" not in drain:
        return fail("virtual-keyboard drain does not iterate safely")
    if not re.search(r"wl_resource_set_user_data\s*\(\s*vk->resource\s*,"
                     r"\s*NULL\s*\)", drain):
        return fail("virtual-keyboard drain does not neutralize vk->resource "
                    "user_data (late destroy callback would UAF)")
    if "qdwin_virtual_keyboard_detach" not in drain:
        return fail("virtual-keyboard drain does not detach (seat listener "
                    "would leak/dangle)")
    return 0


def main():
    if len(sys.argv) != 3:
        return fail("usage: test_virtual_keyboard.py <qdwin.c> <qdwin-logic.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    logic_source = Path(sys.argv[2]).read_text(encoding="utf-8")

    for check in (
        check_manager_global_created_gated_v1,
        check_bind_uses_shared_gate_fail_closed,
        check_key_and_modifiers_no_keymap_guarded,
        check_injection_goes_through_the_seat,
        check_same_client_passthrough,
        check_manager_resource_is_neutralizable,
        check_seat_destroy_and_teardown_detach,
    ):
        rc = check(source)
        if rc:
            return rc
    rc = check_global_filter_hides_vk_from_sandbox(source, logic_source)
    if rc:
        return rc

    print("PASS: virtual-keyboard-v1 (manager gated at v1 via the shared IME "
          "bind gate fail-closed, global filter hides it from silos, "
          "key/modifiers no_keymap-guarded, injection via "
          "notify_key/notify_modifiers without swapping the seat keymap, "
          "manager neutralizable, seat-destroy + teardown detach cleanly)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
