#!/usr/bin/env python3
"""Round-2 popup / selection defense-in-depth invariants (D5, D6, D7, D9).

These pin the four hardening fixes so a later refactor cannot silently regress
them. Each check states the user-visible compositor behavior it protects:

  D5 — a primary-selection source cannot exhaust the compositor heap / event
       queue by advertising unbounded MIME offers (DoS guard).
  D6 — the trusted shell's mutating popup / selection / input-config requests
       are refused while the screen is locked, consistent with fullscreen/tile
       (the lock screen owns the display; set_display_power is the exception).
       Popup/selection refuse FATALLY (post_error); the two input-preference
       snapshots refuse with a logged non-fatal drop so a shell restarted
       during a lock is not crash-looped — see D6_DROP_HANDLERS.
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
    "qdwin_handle_clear_selection",
)

# Handlers whose locked gate is a logged, NON-fatal DROP. This is a deliberate,
# enumerated exception, not a weakening: qdshell pushes both input-preference
# snapshots unconditionally during connect-and-bind, so a shell that (re)starts
# while the session is locked (shell crash + systemd Restart during a lock) was
# killed by the fatal error on every respawn, crash-looped into
# StartLimitBurst=start-limit-hit, and never recovered even after unlock
# (qdlocker/tests/gui/06-shell-crash-survives.md). The lock invariant is
# unchanged — the config is still refused, so nothing mutates behind the lock —
# only the *answer* is a log line instead of a protocol kill.
#
# Keeping these in a separate, explicitly-named tuple is the point: a future
# refactor that downgrades one of the FATAL handlers to a drop has to move it
# here and justify it, rather than silently losing the post_error.
D6_DROP_HANDLERS = (
    "qdwin_handle_set_pointer_config",
    "qdwin_handle_set_key_repeat",
)


def check_d6_locked_gate(source):
    code = _strip_comments(source)
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
            if "QDWIN_SHELL_V1_ERROR_LOCKED" in body:
                return fail(f"D6: {name} is a non-fatal DROP handler but still "
                            f"posts ERROR_LOCKED")
            locked_at = body.find("qdwin->locked")
            if "weston_log" not in body[locked_at:]:
                return fail(f"D6: {name} drops while locked without logging "
                            f"the refusal (weston_log after the gate)")
            gate = locked_at
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
    # same-client delivery must now be gated on xdg_popup role, so a nested
    # submenu still works but a hidden toplevel / unrelated panel does not.
    if "qdwin_surface_is_xdg_popup" not in body:
        return fail("D9: same-client delivery is not gated on xdg_popup role "
                    "(would still leak grabbed input to toplevels/panels)")
    # the old blanket same-client helper must be gone
    if "qdwin_layer_popup_grab_client" in code:
        return fail("D9: dead same-client helper qdwin_layer_popup_grab_client "
                    "still present")
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
            (check_d9_layer_popup_scope, source)):
        rc = check(arg)
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
