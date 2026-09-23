#!/usr/bin/env python3
"""Static-invariant checks for the RDP-headless focus-listener rebind fix.

Same style as test_nested_identity_policy.py: parse qdwin.c and assert the
structural properties that make autofocus propagate on the RDP-headless
backend without relying solely on the immediate-emit workaround.

Background (see todo/issues/qdwin/qdwin-rdp-focus-signal-not-propagating.md):
the RDP backend brings its seat up lazily inside rdp_peer_activate() —
weston_seat_init() fires seat_created_signal (qdwin installs / tries to
install the kbd focus_signal listener) BEFORE weston_seat_init_keyboard()
exists, and the backend can later swap the wl_keyboard. If qdwin's
focus_signal listener stays on a stale keyboard object, the focus change from
autofocus never reaches it, so `qdwin: focus handle=` never logs and the
shell's focusedHandle never updates from autofocus alone.

The proper fix makes the focus_signal binding self-healing: qdwin tracks the
keyboard object its listener is bound to and reconciles it against the seat's
live keyboard (qdwin_seat_tracker_rebind_focus_listener), both on
updated_caps_signal AND right before activation in the autofocus path. The
immediate-emit remains a dedupe-safe backstop.

The live multi-keyboard-swap scenario needs a running RDP-headless VM and is
PENDING residue; here we lock the enforcement in place so a regression that
drops the rebind or the dedupe fails the host suite without a VM.
"""

from pathlib import Path
import re
import sys


def fail(message):
    print(f"FAIL: {message}")
    return 1


def _strip_comments(code):
    """Remove C block and line comments so ordering checks match real calls,
    not mentions inside explanatory comments."""
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", " ", code)
    return code


def _function_body(source, signature_regex, name):
    """Return the brace-balanced body of the first function *definition*
    whose opening matches signature_regex, or (None, error_message).

    Skips forward declarations by requiring the parameter-list close ')' to
    be followed by an opening '{'."""
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
            continue  # forward declaration; keep looking
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


def _struct_body(source, name):
    """Return the brace-balanced body of `struct <name> { ... }`."""
    m = re.search(r"struct\s+" + re.escape(name) + r"\s*\{", source)
    if not m:
        return None, f"struct {name} not found"
    start = source.index("{", m.start())
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1], None
    return None, f"struct {name}: unbalanced braces"


def check_tracker_records_bound_keyboard(source):
    """The seat tracker must record which weston_keyboard its focus_signal
    listener is bound to, so a backend keyboard swap is detectable
    independently of updated_caps_signal."""
    body, err = _struct_body(source, "qdwin_seat_tracker")
    if err:
        return fail(err)
    if "kbd_focus_listener_kbd" not in body:
        return fail("qdwin_seat_tracker has no kbd_focus_listener_kbd field "
                    "(cannot detect a keyboard swap)")
    if "struct weston_keyboard *kbd_focus_listener_kbd" not in body:
        return fail("kbd_focus_listener_kbd is not a weston_keyboard pointer")
    return 0


def check_install_records_keyboard(source):
    """Installing the listener must record the keyboard it bound to."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_install_focus_listener_if_needed\s*\(",
        "qdwin_install_focus_listener_if_needed",
    )
    if err:
        return fail(err)
    if "wl_signal_add(&kbd->focus_signal" not in body:
        return fail("install no longer adds the listener to kbd->focus_signal")
    if not re.search(r"kbd_focus_listener_kbd\s*=\s*kbd", body):
        return fail("install does not record kbd_focus_listener_kbd = kbd")
    return 0


def check_rebind_helper(source):
    """The rebind helper must: compare the bound keyboard to the live
    keyboard, no-op when equal (dedupe — avoids re-installing and the extra
    emit), remove a stale binding before re-arming (never on two signal
    lists at once), and clear the tracked keyboard on removal."""
    body, err = _function_body(
        source,
        r"static int\s+qdwin_seat_tracker_rebind_focus_listener\s*\(",
        "qdwin_seat_tracker_rebind_focus_listener",
    )
    if err:
        return fail(err)
    if "weston_seat_get_keyboard" not in body:
        return fail("rebind helper does not read the seat's live keyboard")
    # Dedupe / idempotence: if already bound to the live keyboard, return 0.
    if not re.search(
            r"kbd_focus_listener_installed\s*&&\s*"
            r"tr->kbd_focus_listener_kbd\s*==\s*kbd", body):
        return fail("rebind helper does not short-circuit when already bound "
                    "to the live keyboard (would double-install / re-emit)")
    # Stale binding must be removed before re-arming.
    if "wl_list_remove(&tr->kbd_focus_listener.link)" not in body:
        return fail("rebind helper does not remove the stale focus_signal "
                    "link before re-arming")
    if not re.search(r"kbd_focus_listener_installed\s*=\s*0", body):
        return fail("rebind helper does not clear the installed flag on "
                    "stale removal")
    if not re.search(r"kbd_focus_listener_kbd\s*=\s*NULL", body):
        return fail("rebind helper does not clear kbd_focus_listener_kbd on "
                    "stale removal")
    return 0


def check_autofocus_rebinds_before_activate(source):
    """The autofocus path must reconcile the listener onto the live keyboard
    BEFORE weston_view_activate_input, so the focus_signal it emits reaches a
    live listener; and it must still call the dedupe-safe immediate-emit
    backstop AFTER. Activation (not bare keyboard focus) is required so
    XWayland transfers real X11 input focus. Order matters: rebind, then
    activate, then emit."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_toplevel_autofocus_if_ready\s*\(",
        "qdwin_toplevel_autofocus_if_ready",
    )
    if err:
        return fail(err)
    body = _strip_comments(body)
    rebind = body.find("qdwin_seat_tracker_rebind_focus_listener")
    activate = body.find("weston_view_activate_input")
    emit = body.find("qdwin_seat_emit_focus_now")
    if rebind < 0:
        return fail("autofocus does not rebind the focus listener onto the "
                    "live keyboard (RDP swap would leave it stale)")
    if activate < 0:
        return fail("autofocus no longer activates the view (XWayland would "
                    "not receive real X11 input focus)")
    if emit < 0:
        return fail("autofocus dropped the immediate-emit backstop")
    if not (rebind < activate < emit):
        return fail("autofocus ordering wrong: must rebind, then activate, "
                    f"then emit (got rebind={rebind} activate={activate} "
                    f"emit={emit})")
    return 0


def check_emit_now_dedupes(source):
    """The shared emit path must dedupe on last_focused_handle so the
    immediate-emit backstop and the focus_signal listener never both emit
    seat_focus_changed for the same handle (no double-emit on either
    backend)."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_seat_emit_focus_now\s*\(",
        "qdwin_seat_emit_focus_now",
    )
    if err:
        return fail(err)
    if not re.search(r"handle\s*==\s*tr->last_focused_handle", body):
        return fail("qdwin_seat_emit_focus_now lost its last_focused_handle "
                    "dedupe guard (immediate-emit + listener would "
                    "double-emit)")
    # The guard must early-return before emitting.
    guard = re.search(
        r"if\s*\(\s*handle\s*==\s*tr->last_focused_handle\s*\)\s*return\s*;",
        body)
    if not guard:
        return fail("dedupe guard does not early-return before emitting")
    return 0


def check_caps_path_uses_rebind(source):
    """updated_caps (keyboard appeared / swapped) must go through the same
    rebind helper rather than a divergent open-coded path."""
    body, err = _function_body(
        source,
        r"static void\s+qdwin_on_seat_updated_caps\s*\(",
        "qdwin_on_seat_updated_caps",
    )
    if err:
        return fail(err)
    if "qdwin_seat_tracker_rebind_focus_listener" not in body:
        return fail("updated_caps handler does not use the rebind helper")
    return 0


def main():
    if len(sys.argv) != 2:
        return fail("usage: test_rdp_focus_rebind.py <qdwin.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")

    checks = (
        check_tracker_records_bound_keyboard,
        check_install_records_keyboard,
        check_rebind_helper,
        check_autofocus_rebinds_before_activate,
        check_emit_now_dedupes,
        check_caps_path_uses_rebind,
    )
    for check in checks:
        rc = check(source)
        if rc:
            return rc

    print("PASS: RDP focus rebind (tracker records bound keyboard, rebind "
          "helper self-heals on swap and is dedupe-idempotent, autofocus "
          "rebinds-before-activate with dedupe-safe immediate-emit backstop, "
          "updated_caps routes through the same helper)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
