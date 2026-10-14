#!/usr/bin/env python3
"""Round-2 popup / selection defense-in-depth invariants (D5, D6, D7, D9).

These pin the four hardening fixes so a later refactor cannot silently regress
them. Each check states the user-visible compositor behavior it protects:

  D5 — a primary-selection source cannot exhaust the compositor heap / event
       queue by advertising unbounded MIME offers (DoS guard).
  D6 — the trusted shell's mutating popup / selection / input-config requests
       are refused while the screen is locked, consistent with fullscreen/tile
       (the lock screen owns the display; set_display_power is the exception).
       Popups refuse FATALLY (post_error); the reactive, best-effort
       requests (input-preference snapshots, focus injection) refuse with a
       logged non-fatal drop so a shell restarted during a lock is not
       crash-looped — see D6_DROP_HANDLERS; a deny's
       clear_selection is deferred to unlock — see D6_DEFER_HANDLERS.
  D7 — qdwin_shell_v1.show_popup requires a valid input grab serial (like an
       xdg_popup grab) before it installs a compositor-wide pointer grab, and
       the protocol advertises this (v29 + serial arg + invalid_grab error).
  D9 — a layer-popup grab delivers input only to the popup, its layer parent,
       or a subsurface of either — never to an arbitrary same-client surface.

Usage: test_popup_grab_hardening.py <qdwin.c> <qdwin-shell-v1.xml>
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
    """Return (body_including_braces, None) or (None, error) for the first
    function whose signature matches signature_regex."""
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


# ---------------------------------------------------------------- D5

def check_d5_primary_mime_cap(source):
    code = _strip_comments(source)
    if not re.search(r"#define\s+QDWIN_PRIMARY_MIME_MAX\s+\d", source):
        return fail("D5: QDWIN_PRIMARY_MIME_MAX cap not defined")
    body, err = _function_body(
        code, r"qdwin_primary_source_offer\s*\(", "qdwin_primary_source_offer")
    if err:
        return fail(err)
    if "QDWIN_PRIMARY_MIME_MAX" not in body:
        return fail("D5: qdwin_primary_source_offer does not enforce the cap")
    cap_at = body.find("QDWIN_PRIMARY_MIME_MAX")
    calloc_at = body.find("calloc")
    if calloc_at == -1:
        return fail("D5: qdwin_primary_source_offer no longer allocates an entry")
    if cap_at > calloc_at:
        return fail("D5: cap check runs AFTER the calloc (must reject first)")
    if "wl_list_for_each" not in body[:cap_at + 40]:
        return fail("D5: cap is not counted over the existing mime_types list")
    return 0


# ---------------------------------------------------------------- D6

# Handlers whose locked gate is FATAL: refuse with
# wl_resource_post_error(ERROR_LOCKED), killing a shell that tries to mutate
# display state behind the lock screen.
D6_FATAL_HANDLERS = (
    "qdwin_handle_show_popup",
)

# Handlers whose locked gate is a logged, NON-fatal DROP of a request the
# shell emits reactively, without checking lock state. This is a deliberate,
# enumerated exception, not a weakening: qdshell pushes both input-preference
# snapshots unconditionally during connect-and-bind, so a shell that (re)starts
# while the session is locked (shell crash + systemd Restart during a lock) was
# killed by the fatal error on every respawn, crash-looped into
# StartLimitBurst=start-limit-hit, and never recovered even after unlock
# (qdlocker/tests/gui/06-shell-crash-survives.md). The two set_keyboard_focus
# handlers are the same class: injectFocus is best-effort intent issued
# reactively by the shell and by test drivers, and a lock firing mid-burst
# (qdlocker's 300 s idle threshold inside a VM test window) used to unbind
# the shell on every attempt — after which even the legitimate post-unlock
# focus restore could not arrive. The lock invariant is unchanged — the
# request is still refused BEFORE any side effect (selection clear, focus
# redirect, silo tracking write) — only the *answer* is a log line instead
# of a protocol kill, and a dropped request leaves no stale intent to
# replay at unlock.
#
# Keeping these in a separate, explicitly-named tuple is the point: a future
# refactor that downgrades one of the FATAL handlers to a drop has to move it
# here and justify it, rather than silently losing the post_error.
D6_DROP_HANDLERS = (
    "qdwin_handle_set_pointer_config",
    "qdwin_handle_set_key_repeat",
    "qdwin_handle_set_keyboard_focus",
    "qdwin_handle_set_keyboard_focus_v2",
)

# Handlers whose locked gate DEFERS the request to unlock. clear_selection
# moved here from D6_FATAL_HANDLERS: qdshell decides clipboard verdicts
# asynchronously (up to 2 s, ClipboardGate + BrokerCallRunner), so a deny for
# a copy made just before the lock legitimately arrives after set_locked(1);
# the fatal error reset the shell's connection on copy-then-lock. The lock
# invariant is unchanged — nothing mutates the selection behind the lock
# screen — and the clear is not lost: both unlock paths apply it.
D6_DEFER_HANDLERS = {
    "qdwin_handle_clear_selection": "qdwin_defer_clear_selection",
}
# Every unlock path (the shell's and the locker's set_locked) must apply the
# deferred requests.
D6_UNLOCK_HANDLERS = (
    "qdwin_handle_set_locked",
    "qdwin_handle_locker_set_locked",
)
D6_UNLOCK_FLUSH = "qdwin_flush_deferred_clears(qdwin, true)"


def _else_branch_of(body, if_regex):
    """(start, end) of the `else { ... }` block of the first if matching
    if_regex (which must end at its opening brace), or None."""
    m = re.search(if_regex, body)
    if not m:
        return None

    def close(i):
        depth = 1
        while i < len(body) and depth:
            depth += {"{": 1, "}": -1}.get(body[i], 0)
            i += 1
        return i

    after = close(m.end())
    e = re.match(r"\s*else\s*\{", body[after:])
    if not e:
        return None
    start = after + e.end()
    return start, close(start)


def check_d6_locked_gate(source):
    code = _strip_comments(source)
    for name, defer_fn in D6_DEFER_HANDLERS.items():
        body, err = _function_body(code, name + r"\s*\(", name)
        if err:
            return fail(err)
        if "QDWIN_SHELL_V1_ERROR_LOCKED" in body:
            return fail(f"D6: {name} is a DEFER handler but still posts "
                        f"ERROR_LOCKED")
        rb = body.find("qdwin_shell_require_bound")
        locked_at = body.find("qdwin->locked")
        if locked_at == -1:
            return fail(f"D6: {name} missing the locked gate")
        if rb == -1 or locked_at < rb:
            return fail(f"D6: {name} locked gate must follow require_bound")
        # The lock branch is exactly "defer, then return": not negated, no
        # apply inside it, and the one apply call follows the branch.
        branch = re.search(
            r"if\s*\(\s*qdwin->locked\s*\)\s*\{\s*"
            + re.escape(defer_fn) + r"\s*\([^;]*\)\s*;\s*return\s*;\s*\}",
            body)
        if not branch:
            return fail(f"D6: {name} lock branch must be exactly "
                        f"'if (qdwin->locked) {{ {defer_fn}(...); return; }}'")
        applies = [m.start() for m in
                   re.finditer(r"qdwin_clear_selection_apply\s*\(", body)]
        if len(applies) != 1 or applies[0] < branch.end():
            return fail(f"D6: {name} must apply the clear exactly once, "
                        f"after its locked branch")
    for name in D6_UNLOCK_HANDLERS:
        body, err = _function_body(code, name + r"\s*\(", name)
        if err:
            return fail(err)
        # The flush belongs to the unlock (else) branch of `if (want)`.
        span = _else_branch_of(body, r"if\s*\(\s*want\s*\)\s*\{")
        flushes = [k.start() for k in
                   re.finditer(re.escape(D6_UNLOCK_FLUSH), body)]
        if span is None or len(flushes) != 1 \
                or not span[0] <= flushes[0] < span[1]:
            return fail(f"D6: unlock path {name} must apply deferred requests "
                        f"once, in its unlock (else) branch "
                        f"({D6_UNLOCK_FLUSH})")
    for name in D6_FATAL_HANDLERS + D6_DROP_HANDLERS:
        body, err = _function_body(code, name + r"\s*\(", name)
        if err:
            return fail(err)
        fatal = name in D6_FATAL_HANDLERS

        # Every gated handler must test the lock, whichever way it answers.
        if "qdwin->locked" not in body:
            return fail(f"D6: {name} missing the locked gate (no qdwin->locked "
                        f"test)")
        if fatal:
            if "QDWIN_SHELL_V1_ERROR_LOCKED" not in body:
                return fail(f"D6: {name} must refuse with a FATAL locked gate "
                            f"(qdwin->locked -> ERROR_LOCKED)")
            gate = body.find("QDWIN_SHELL_V1_ERROR_LOCKED")
        else:
            # A drop handler must NOT post the fatal error (that is the
            # regression it exists to prevent) and must log the refusal, so the
            # drop is observable in the compositor log rather than silent.
            # The lock branch must be exactly "log, then return": a dropped
            # `return` would let a locked request reach the mutation code
            # below and silently defeat the lock.
            if "QDWIN_SHELL_V1_ERROR_LOCKED" in body:
                return fail(f"D6: {name} is a non-fatal DROP handler but still "
                            f"posts ERROR_LOCKED")
            drop_branch = re.search(
                r"if\s*\(\s*qdwin->locked\s*\)\s*\{\s*"
                r"weston_log\s*\([^;]*\)\s*;\s*return\s*;\s*\}",
                body)
            if not drop_branch:
                return fail(f"D6: {name} lock branch must be exactly "
                            f"'if (qdwin->locked) {{ weston_log(...); "
                            f"return; }}' (a drop without return reaches "
                            f"the mutation path while locked)")
            gate = body.find("qdwin->locked")
        # the gate must precede any state mutation; require_bound must come first
        rb = body.find("qdwin_shell_require_bound")
        if rb == -1 or gate < rb:
            return fail(f"D6: {name} locked gate must follow require_bound")
    return 0


# ---------------------------------------------------------------- D7

def check_d7_show_popup_serial_source(source):
    code = _strip_comments(source)
    if "qdwin_seat_grab_serial_matches" not in code:
        return fail("D7: shared grab-serial helper missing")
    # A device that never produced input keeps grab_serial == 0, so the
    # helper must refuse serial 0 itself or a literal-0 serial matches.
    hbody, err = _function_body(
        code, r"qdwin_seat_grab_serial_matches\s*\(",
        "qdwin_seat_grab_serial_matches")
    if err:
        return fail(err)
    guard = re.search(r"if\s*\(([^)]*\bserial\s*==\s*0\b[^)]*)\)\s*return\s+false",
                      hbody)
    if not guard or hbody.find(guard.group(0)) > hbody.find("grab_serial"):
        return fail("D7: grab-serial helper must refuse serial 0 before "
                    "comparing device grab serials (never-used devices hold 0)")
    body, err = _function_body(
        code, r"qdwin_handle_show_popup\s*\(", "qdwin_handle_show_popup")
    if err:
        return fail(err)
    if "qdwin_seat_grab_serial_matches" not in body:
        return fail("D7: show_popup does not validate the grab serial")
    if "QDWIN_SHELL_V1_ERROR_INVALID_GRAB" not in body:
        return fail("D7: show_popup does not raise invalid_grab on a bad serial")
    serial_at = body.find("qdwin_seat_grab_serial_matches")
    create_at = body.find("wl_resource_create(client, &qdwin_popup_v1_interface")
    if create_at == -1:
        return fail("D7: show_popup no longer creates the popup new_id")
    if serial_at > create_at:
        return fail("D7: serial validation runs AFTER the new_id is created")
    # the layer-popup path must share the same helper (no duplicate inline check)
    lbody, err = _function_body(
        code, r"qdwin_layer_popup_layer_grab_handler\s*\(",
        "qdwin_layer_popup_layer_grab_handler")
    if err:
        return fail(err)
    if "qdwin_seat_grab_serial_matches" not in lbody:
        return fail("D7: layer-popup grab handler does not share the helper")
    advertised = re.search(
        r"&qdwin_shell_v1_interface,\s*(\d+),\s*qdwin,\s*"
        r"bind_qdwin_shell",
        code,
    )
    if not advertised or int(advertised.group(1)) < 29:
        return fail("D7: shell global is not advertised at version >= 29")
    # The second accepted serial is the latest chrome press qdwin itself
    # forwarded — never 0, and cleared by any press elsewhere, so it
    # cannot go stale behind a later interaction.
    if "serial != qdwin->chrome_press_serial" not in body \
            or "serial == 0" not in body:
        return fail("D7: show_popup's chrome-press serial must be exact and non-zero")
    if not re.search(r"chrome_press_serial\s*=\s*chrome_tl && side >= 0\s*"
                     r"\?\s*wl_display_get_serial\([^;]*:\s*0;", code):
        return fail("D7: a press off the chrome must clear chrome_press_serial")
    # A parent that died with toplevel_removed in flight must not kill the
    # shell's connection: inert popup + immediate dismissed, no error.
    stale = re.search(r"parent <= qdwin->next_handle\)\s*\{(.*?)\bif \(!tl\)",
                      body, re.S)
    if not stale or "qdwin_popup_v1_send_dismissed" not in stale.group(1) \
            or "post_error" in stale.group(1):
        return fail("D7: a stale parent must be dismissed, not a protocol error")
    # The chrome_button/popup_button emits must forward the CURRENT button
    # serial (wl_display_get_serial), NOT the stale pointer->grab_serial that
    # notify_button only assigns AFTER the grab callback returns — otherwise a
    # shell echoing it to show_popup always hits invalid_grab.
    for emit in ("qdwin_shell_v1_send_chrome_button",
                 "qdwin_shell_v1_send_popup_button"):
        i = code.find(emit + "(")
        if i == -1:
            return fail("D7: %s emit not found" % emit)
        call = code[i:code.find(";", i)]
        if "wl_display_get_serial" not in call:
            return fail("D7: %s must forward wl_display_get_serial, not the "
                        "stale pointer->grab_serial" % emit)
    return 0


def check_d7_show_popup_serial_protocol(xml):
    iface = re.search(r'interface name="qdwin_shell_v1" version="(\d+)"', xml)
    if not iface or int(iface.group(1)) < 29:
        return fail("D7: qdwin_shell_v1 interface version must be >= 29")
    m = re.search(r'<request name="show_popup">(.*?)</request>', xml, re.DOTALL)
    if not m:
        return fail("D7: show_popup request not found in protocol")
    if 'name="serial"' not in m.group(1):
        return fail("D7: show_popup protocol has no serial arg")
    if 'name="invalid_grab"' not in xml:
        return fail("D7: invalid_grab error not defined in protocol")
    # The events that trigger a context menu must carry the serial, otherwise a
    # show_popup caller has no valid grab serial to pass (the gate is useless).
    for ev in ("chrome_button", "popup_button"):
        m = re.search(r'<event name="%s".*?</event>' % ev, xml, re.DOTALL)
        if not m or 'name="serial"' not in m.group(0):
            return fail("D7: %s event carries no serial (a show_popup caller "
                        "would have no grab serial to pass)" % ev)
    return 0


# ---------------------------------------------------------------- D9

def check_d9_layer_popup_scope(source):
    code = _strip_comments(source)
    body, err = _function_body(
        code, r"qdwin_layer_popup_grab_refilter_focus\s*\(",
        "qdwin_layer_popup_grab_refilter_focus")
    if err:
        return fail(err)
    if "weston_surface_get_main_surface" not in body:
        return fail("D9: refilter does not fold subsurfaces via "
                    "weston_surface_get_main_surface")
    if "lp->parent" not in body:
        return fail("D9: refilter does not whitelist the layer parent surface")
    # D9 follow-up: beyond the root and its parent, only a submenu that
    # joined this grab is a target -- not any same-client xdg_popup (an
    # ungrabbed tooltip, another panel's popup).
    if "qdwin_layer_popup_chain_has" not in body:
        return fail("D9: refilter does not confine delivery to the chain's "
                    "joined submenus")
    if "qdwin_surface_is_xdg_popup" in code or "wl_resource_get_client" in body:
        return fail("D9: refilter still admits same-client popups outside "
                    "the chain")
    # the old blanket same-client helper must be gone
    if "qdwin_layer_popup_grab_client" in code:
        return fail("D9: dead same-client helper qdwin_layer_popup_grab_client "
                    "still present")
    return 0


def check_d9_nested_chain(source):
    """A nested submenu joins the root layer popup's grab: a press on it is
    inside, and a dismissal reaches every popup in the chain."""
    code = _strip_comments(source)
    body, err = _function_body(
        code, r"qdwin_layer_popup_grab_button\s*\(",
        "qdwin_layer_popup_grab_button")
    if err:
        return fail(err)
    if "qdwin_layer_popup_chain_contains" not in body:
        return fail("D9: outside-click test ignores the nested chain (a press "
                    "on a submenu would dismiss the menu)")
    if "qdwin_layer_popup_grab_dismiss" not in body:
        return fail("D9: outside click does not dismiss the nested chain")
    body, err = _function_body(
        code, r"qdwin_layer_popup_grab_cancel\s*\(",
        "qdwin_layer_popup_grab_cancel")
    if err:
        return fail(err)
    if "qdwin_layer_popup_grab_dismiss" not in body:
        return fail("D9: grab cancel does not dismiss the nested chain")
    body, err = _function_body(
        code, r"qdwin_layer_popup_grab_dismiss\s*\(",
        "qdwin_layer_popup_grab_dismiss")
    if err:
        return fail(err)
    if ("qdwin_layer_popup_dismiss_chain" not in body or
            "qdwin_layer_popup_end_grabs" not in body):
        return fail("D9: ending the chain grab does not dismiss the chain "
                    "and end both halves")
    body, err = _function_body(
        code, r"qdwin_layer_popup_layer_grab_handler\s*\(",
        "qdwin_layer_popup_layer_grab_handler")
    if err:
        return fail(err)
    if "qdwin_layer_popup_nested_grab" not in body:
        return fail("D9: layer-grab handler does not accept a nested popup")
    body, err = _function_body(
        code, r"qdwin_layer_popup_nested_grab\s*\(",
        "qdwin_layer_popup_nested_grab")
    if err:
        return fail(err)
    if "weston_pointer_start_grab" in body:
        return fail("D9: a nested popup must join the root grab, not replace it")
    if ("qdwin_layer_popup_pointer_grab_installed(root)" not in body or
            "qdwin_layer_popup_touch_grab_installed(root)" not in body):
        return fail("D9: nested grab does not require the root grab to be live")
    return 0


def check_d9_followups(source):
    """D9 follow-ups: the chain grab has a touch half, and a dismissal
    unmaps the chain before the client answers popup_done."""
    code = _strip_comments(source)
    body, err = _function_body(
        code, r"qdwin_layer_popup_layer_grab_handler\s*\(",
        "qdwin_layer_popup_layer_grab_handler")
    if err:
        return fail(err)
    if "weston_touch_start_grab" not in body:
        return fail("D9: the layer-popup grab installs no touch grab")
    body, err = _function_body(
        code, r"qdwin_layer_popup_touch_grab_down\s*\(",
        "qdwin_layer_popup_touch_grab_down")
    if err:
        return fail(err)
    if ("qdwin_layer_popup_touch_in_chain" not in body or
            "qdwin_layer_popup_grab_dismiss" not in body):
        return fail("D9: a touch down outside the chain does not dismiss it")
    body, err = _function_body(
        code, r"qdwin_layer_popup_end_grabs\s*\(",
        "qdwin_layer_popup_end_grabs")
    if err:
        return fail(err)
    if "weston_touch_end_grab" not in body or \
            "weston_pointer_end_grab" not in body:
        return fail("D9: ending the chain grab does not end both halves")
    body, err = _function_body(
        code, r"qdwin_layer_popup_dismiss_chain\s*\(",
        "qdwin_layer_popup_dismiss_chain")
    if err:
        return fail(err)
    if "weston_view_destroy(lp->view)" not in body:
        return fail("D9: dismissing the chain leaves it mapped until the "
                    "client destroys the popups")
    # sol r1: every lock path hides the layers through one helper; it must
    # end chain grabs and drop touch focus, or a hidden menu keeps input.
    body, err = _function_body(
        code, r"qdwin_hide_non_lock_layers\s*\(",
        "qdwin_hide_non_lock_layers")
    if err:
        return fail(err)
    if "qdwin_lock_revoke_routed_input" not in body:
        return fail("D9: locking leaves layer-chain grabs / touch focus "
                    "routed to the hidden layers")
    body, err = _function_body(
        code, r"qdwin_lock_revoke_routed_input\s*\(",
        "qdwin_lock_revoke_routed_input")
    if err:
        return fail(err)
    if ("qdwin_layer_popups_end_for_lock" not in body or
            "weston_touch_set_focus(touch, NULL)" not in body):
        return fail("D9: lock does not end chain grabs and revoke touch "
                    "focus")
    # sol r2: the session is cancelled (not just dropped), before the
    # dismissal can clear the focus silently.
    cancel = body.find("wl_touch_send_cancel")
    if cancel < 0 or cancel > body.find("qdwin_layer_popups_end_for_lock"):
        return fail("D9: lock drops touch focus without wl_touch.cancel "
                    "first")
    body, err = _function_body(
        code, r"qdwin_layer_popup_layer_grab_handler\s*\(",
        "qdwin_layer_popup_layer_grab_handler")
    if err:
        return fail(err)
    locked = body.find("qdwin->locked")
    if locked < 0 or locked > body.find("weston_pointer_start_grab"):
        return fail("D9: a layer-chain grab can be installed while locked")
    return 0


def main():
    if len(sys.argv) != 3:
        return fail("usage: test_popup_grab_hardening.py <qdwin.c> "
                    "<qdwin-shell-v1.xml>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    xml = Path(sys.argv[2]).read_text(encoding="utf-8")
    for check, arg in (
            (check_d5_primary_mime_cap, source),
            (check_d6_locked_gate, source),
            (check_d7_show_popup_serial_source, source),
            (check_d7_show_popup_serial_protocol, xml),
            (check_d9_layer_popup_scope, source),
            (check_d9_nested_chain, source),
            (check_d9_followups, source)):
        rc = check(arg)
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
