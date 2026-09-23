#!/usr/bin/env python3
"""Lock teardown fail-secure invariants (finding F9).

A locker crash must NOT drop the lock. The two lock-teardown callbacks
(qdwin_lock_surface_destroyed_cb, qdwin_lock_surface_resource_destroyed) must
behave like qdwin_locker_resource_destroy(): when the surface/resource is
destroyed while still locked and no reattach is in flight, they must HOLD the
lock (no `locked = 0`, no qdwin_show_non_lock_layers, no locked_changed(0)) and
instead demote any toplevel + schedule a repaint so the empty lock layer
composites to black. Recovery is systemd Restart=always re-binding a fresh
locker — not auto-unlock.

Separately, the default keyboard grab must drop keys while locked, so the
post-crash window (lock held, overlay grab gone) cannot leak keystrokes to the
focused desktop client.
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


# The two callbacks that previously auto-unlocked on teardown.
_TEARDOWN_CALLBACKS = (
    (r"static void\s+qdwin_lock_surface_destroyed_cb\s*\(",
     "qdwin_lock_surface_destroyed_cb"),
    (r"static void\s+qdwin_lock_surface_resource_destroyed\s*\(",
     "qdwin_lock_surface_resource_destroyed"),
)


def check_teardown_holds_lock(source):
    for sig, name in _TEARDOWN_CALLBACKS:
        body, err = _function_body(source, sig, name)
        if err:
            return fail(err)
        code = _strip_comments(body)
        # Must not auto-unlock.
        if re.search(r"qdwin->locked\s*=\s*0", code):
            return fail(f"{name} still clears qdwin->locked (auto-unlock); "
                        "must hold the lock fail-secure")
        if "qdwin_show_non_lock_layers" in code:
            return fail(f"{name} still reveals the desktop via "
                        "qdwin_show_non_lock_layers; must stay black")
        if re.search(r"send_locked_changed\s*\([^;]*,\s*0\s*\)", code):
            return fail(f"{name} still emits locked_changed(0) on teardown")
        # Must hold + go black: demote any toplevel and force a repaint.
        if "qdwin_demote_lock_toplevel" not in code:
            return fail(f"{name} does not demote the stale lock toplevel")
        if "weston_compositor_schedule_repaint" not in code:
            return fail(f"{name} does not schedule a repaint to black the "
                        "empty lock layer")
    return 0


def check_default_keygrab_drops_while_locked(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_proxy_default_grab_key\s*\(",
        "qdwin_proxy_default_grab_key")
    if err:
        return fail(err)
    code = _strip_comments(body)
    guard = re.search(
        r"if\s*\(\s*qdwin_singleton\s*&&\s*qdwin_singleton->locked\s*\)\s*"
        r"return\s*;",
        code)
    if not guard:
        return fail("qdwin_proxy_default_grab_key lacks a locked guard "
                    "(`if (qdwin_singleton && qdwin_singleton->locked) "
                    "return;`) — keystrokes could leak to a desktop client "
                    "while the lock is held without the overlay grab")
    send = code.find("weston_keyboard_send_key")
    if send != -1 and guard.start() > send:
        return fail("locked guard runs AFTER weston_keyboard_send_key; the "
                    "key would already be delivered")
    idle = code.find("qdwin_idle_note_activity")
    if idle != -1 and guard.start() > idle:
        return fail("locked guard runs AFTER qdwin_idle_note_activity; the "
                    "guard must be the first statement so a lock-held key is "
                    "dropped without side effects")
    return 0


def check_default_modifiers_drop_while_locked(source):
    body, err = _function_body(
        source,
        r"static void\s+qdwin_proxy_default_grab_modifiers\s*\(",
        "qdwin_proxy_default_grab_modifiers")
    if err:
        return fail(err)
    code = _strip_comments(body)
    guard = re.search(
        r"if\s*\(\s*qdwin_singleton\s*&&\s*qdwin_singleton->locked\s*\)\s*"
        r"return\s*;",
        code)
    if not guard:
        return fail("qdwin_proxy_default_grab_modifiers lacks a locked guard — "
                    "modifier state (Shift/Ctrl/Alt) could leak to a desktop "
                    "client while the lock is held without the overlay grab")
    send = code.find("weston_keyboard_send_modifiers")
    if send != -1 and guard.start() > send:
        return fail("locked guard runs AFTER weston_keyboard_send_modifiers")
    return 0


def check_bind_rearms_grab_when_locked(source):
    """A locker that binds into an already-locked session (the crash →
    systemd-respawn recovery path) must re-arm the role=2 overlay keyboard
    grab. set_locked(1) is NOT re-issued on an inherited lock, so without this
    the recovered lock screen reappears but swallows every keystroke — the user
    cannot type the unlock password (crash-recovery lockout). The grab must be
    guarded by qdwin->locked so the initial, still-unlocked bind does not grab.
    """
    body, err = _function_body(
        source,
        r"static void\s+qdwin_handle_bind_as_locker\s*\(",
        "qdwin_handle_bind_as_locker")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if "qdwin_overlay_grab_start" not in code:
        return fail("qdwin_handle_bind_as_locker does not re-arm the overlay "
                    "grab; a locker inheriting the lock after a crash could not "
                    "receive keystrokes to unlock (crash-recovery lockout)")
    # The grab start must be *directly controlled* by `if (qdwin->locked)` — not
    # merely co-present with some unrelated locked guard elsewhere in the body —
    # so the initial, still-unlocked bind never grabs the keyboard.
    if not re.search(
            r"if\s*\(\s*qdwin->locked\s*\)\s*\{?\s*"
            r"qdwin_overlay_grab_start\s*\(\s*qdwin\s*,\s*2\s*\)",
            code):
        return fail("qdwin_handle_bind_as_locker must re-arm the overlay grab "
                    "with `if (qdwin->locked) qdwin_overlay_grab_start(qdwin, "
                    "2);` — the grab start must be guarded by qdwin->locked so "
                    "the initial unlocked bind does not grab the keyboard")
    return 0


def check_overlay_modifiers_contained_for_locker(source):
    """The role=2 (locker) overlay grab must NOT forward wl_keyboard.modifiers
    to focus clients. Keys reach the locker as overlay_key with pre-shifted
    utf8, so it never needs modifier events; forwarding them via
    weston_keyboard_send_modifiers() would leak Shift/Ctrl/Alt state to a
    still-focused desktop client during the locker crash→rebind window (the
    same leak the default-grab locked guard prevents). The modifiers callback
    must return early for overlay_grab_role==2 BEFORE any send.
    """
    body, err = _function_body(
        source,
        r"static void\s+qdwin_overlay_grab_modifiers\s*\(",
        "qdwin_overlay_grab_modifiers")
    if err:
        return fail(err)
    code = _strip_comments(body)
    guard = re.search(
        r"if\s*\(\s*qdwin->overlay_grab_role\s*==\s*2\s*\)\s*return\s*;", code)
    if not guard:
        return fail("qdwin_overlay_grab_modifiers lacks an early "
                    "`if (qdwin->overlay_grab_role == 2) return;` guard — "
                    "locker-role modifier state could leak to a focused desktop "
                    "client while the screen is locked")
    send = code.find("weston_keyboard_send_modifiers")
    if send != -1 and guard.start() > send:
        return fail("qdwin_overlay_grab_modifiers role==2 guard runs AFTER "
                    "weston_keyboard_send_modifiers; modifiers would already be "
                    "delivered to the focused client")
    return 0


def check_locker_unlock_resyncs_modifiers(source):
    """Ending the locker grab after a normal unlock must publish the current
    modifier snapshot to the focused desktop client. Modifier callbacks are
    intentionally suppressed while locked, so without this explicit replay the
    client retains stale Ctrl+Alt state from the lock chord. The replay must be
    guarded by !qdwin->locked: locker-crash teardown ends the grab while the
    compositor remains locked and must not leak input state to the desktop.
    """
    body, err = _function_body(
        source,
        r"static void\s+qdwin_overlay_grab_end\s*\(",
        "qdwin_overlay_grab_end")
    if err:
        return fail(err)
    code = _strip_comments(body)
    guard = re.search(
        r"if\s*\(\s*role\s*==\s*2\s*&&\s*!\s*qdwin->locked\s*\)\s*\{",
        code)
    if not guard:
        return fail("qdwin_overlay_grab_end does not guard locker modifier "
                    "resync with `role == 2 && !qdwin->locked`; post-unlock "
                    "clients can retain stale Ctrl+Alt, or crash teardown can "
                    "leak modifier state while still locked")
    send = code.find("weston_keyboard_send_modifiers", guard.end())
    if send == -1:
        return fail("qdwin_overlay_grab_end does not send the current modifier "
                    "snapshot after normal locker unlock")
    guard_close = code.find("}", guard.end())
    if guard_close != -1 and send > guard_close:
        return fail("locker modifier resync is outside the unlocked-only guard")
    for field in ("mods_depressed", "mods_latched", "mods_locked", "group"):
        if f"kb->modifiers.{field}" not in code[send:]:
            return fail(f"locker modifier resync omits current {field}")
    if "wl_display_next_serial" not in code[guard.end():send]:
        return fail("locker modifier resync does not allocate a fresh Wayland "
                    "serial before sending wl_keyboard.modifiers")
    return 0


def check_locker_disconnect_holds_and_audits(source):
    """The PRIMARY locker-death path — qdwin_locker_resource_destroy (the
    wl_client disconnect when qdlocker dies) — must hold the lock fail-secure
    (never clear qdwin->locked / reveal the desktop / emit locked_changed(0))
    and, when holding, emit the `lock held (fail-secure)` audit line so the hold
    is observable in the journal even in the surfaceless case (locker died with
    no promoted toplevel to demote), which would otherwise be silent.
    """
    body, err = _function_body(
        source,
        r"static void\s+qdwin_locker_resource_destroy\s*\(",
        "qdwin_locker_resource_destroy")
    if err:
        return fail(err)
    code = _strip_comments(body)
    if re.search(r"qdwin->locked\s*=\s*0", code):
        return fail("qdwin_locker_resource_destroy clears qdwin->locked "
                    "(auto-unlock on locker death); must hold fail-secure")
    if "qdwin_show_non_lock_layers" in code:
        return fail("qdwin_locker_resource_destroy reveals the desktop via "
                    "qdwin_show_non_lock_layers; must stay black")
    if re.search(r"send_locked_changed\s*\([^;]*,\s*0\s*\)", code):
        return fail("qdwin_locker_resource_destroy emits locked_changed(0) on "
                    "locker death; must hold fail-secure")
    if "lock held (fail-secure)" not in code:
        return fail("qdwin_locker_resource_destroy does not log the "
                    "`lock held (fail-secure)` audit line; the fail-secure hold "
                    "on the primary locker-death path would be unobservable in "
                    "the surfaceless (no-toplevel) case")
    return 0


def main():
    if len(sys.argv) != 2:
        return fail("usage: test_lock_fail_secure.py <qdwin.c>")
    source = Path(sys.argv[1]).read_text(encoding="utf-8")
    for check in (
            check_teardown_holds_lock,
            check_default_keygrab_drops_while_locked,
            check_default_modifiers_drop_while_locked,
            check_bind_rearms_grab_when_locked,
            check_overlay_modifiers_contained_for_locker,
            check_locker_unlock_resyncs_modifiers,
            check_locker_disconnect_holds_and_audits):
        rc = check(source)
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
