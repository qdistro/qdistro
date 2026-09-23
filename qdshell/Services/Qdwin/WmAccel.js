// WmAccel — pure, side-effect-free accelerator parser. Translates a
// human accelerator string ("Super+Shift+Left") into the (modifier
// bitmask, linux input keycode) pair that qdwin_shell_v1.register_hotkey
// expects. NO Process / Settings / Quickshell access — only string maths,
// so it is unit-testable headless (require("./WmAccel.js")).
//
// The compositor registers a binding on (modifiers, keycode); it has no
// notion of an accelerator *string*. WindowManagerService parses the
// persisted accelerator here and registers the resulting combo. The
// accelerator has already been allowlist-validated
// (WindowManagerPolicy.isValidAccelerator) before it reaches us, but we
// re-validate defensively and return null for anything we can't map so a
// garbage accelerator simply registers no hotkey (rather than a wrong one).

// Modifier bitmask — mirrors qdwin_shell_v1.modifier (ctrl=1, alt=2,
// super=4, shift=8) and the compositor's enum weston_keyboard_modifier.
var MOD = {
    ctrl: 1, control: 1,
    alt: 2, meta: 2,        // X11/GTK "meta" usually maps to Alt
    super: 4, win: 4, cmd: 4, command: 4, logo: 4,
    shift: 8,
};

// key name (lowercased) -> linux input-event-codes.h keycode. Covers the
// keys a desktop WM shortcut realistically uses; unknown names yield null.
var KEY = {
    // letters
    a: 30, b: 48, c: 46, d: 32, e: 18, f: 33, g: 34, h: 35, i: 23,
    j: 36, k: 37, l: 38, m: 50, n: 49, o: 24, p: 25, q: 16, r: 19,
    s: 31, t: 20, u: 22, v: 47, w: 17, x: 45, y: 21, z: 44,
    // digits (top row)
    "0": 11, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8,
    "8": 9, "9": 10,
    // function keys
    f1: 59, f2: 60, f3: 61, f4: 62, f5: 63, f6: 64, f7: 65, f8: 66,
    f9: 67, f10: 68, f11: 87, f12: 88,
    // arrows
    up: 103, down: 108, left: 105, right: 106,
    // navigation / editing
    space: 57, spacebar: 57, return: 28, enter: 28, tab: 15,
    escape: 1, esc: 1, backspace: 14, delete: 111, del: 111,
    insert: 110, ins: 110, home: 102, end: 107,
    pageup: 104, prior: 104, pagedown: 109, next: 109,
    // punctuation
    minus: 12, equal: 13, plus: 13, comma: 51, period: 52, dot: 52,
    slash: 53, backslash: 43, semicolon: 39, apostrophe: 40,
    grave: 41, bracketleft: 26, bracketright: 27,
};

var _ACCEL_RE = /^[A-Za-z0-9_+-]+$/;

// Parse an accelerator into { modifiers, key } (key = linux keycode), or
// null if it is empty, unmappable, or has no non-modifier key. A bare
// modifier ("Super") returns null — the compositor doesn't support
// modifier-only hotkeys (register_hotkey with key=0 is a no-op).
function parse(accel) {
    var a = String(accel === undefined || accel === null ? "" : accel).trim();
    if (a.length === 0 || !_ACCEL_RE.test(a))
        return null;
    // A lone "+" or trailing/leading "+" tokens split to empties — drop
    // them so "Ctrl++" (the literal plus key would be "plus") is still
    // sane: empty tokens are ignored, "plus" maps via KEY.
    var parts = a.split("+");
    var mods = 0;
    var key = 0;
    for (var i = 0; i < parts.length; i++) {
        var tok = parts[i].trim().toLowerCase();
        if (tok.length === 0)
            continue;
        if (MOD.hasOwnProperty(tok)) {
            mods |= MOD[tok];
            continue;
        }
        if (KEY.hasOwnProperty(tok)) {
            // Last non-modifier token wins (a well-formed accelerator has
            // exactly one); this tolerates odd input without erroring.
            key = KEY[tok];
            continue;
        }
        // An unknown non-modifier token makes the whole accelerator
        // unmappable — refuse rather than register a partial combo.
        return null;
    }
    if (key === 0)
        return null;
    return { modifiers: mods, key: key };
}

if (typeof module !== "undefined") {
    module.exports = {
        MOD: MOD,
        KEY: KEY,
        parse: parse,
    };
}
