const assert = require("assert");
const WM = require("../Services/Qdwin/WindowManagerPolicy.js");

// qdshell only ever runs on qdwin, whose qdwin_shell_v1 IPC has no
// window-manager-policy request yet — so WM policy is persist-only and this
// module builds NO compositor command. These tests cover the pure logic that
// keeps persisted values sane and safe: enum normalisation, numeric clamping,
// and accelerator allowlist validation/sanitisation.

// ─── Focus policy normalisation ─────────────────────────────────────
assert.strictEqual(WM.normalizeFocusPolicy("click"), "click");
assert.strictEqual(WM.normalizeFocusPolicy("follow-mouse"), "follow-mouse");
assert.strictEqual(WM.normalizeFocusPolicy("FOLLOW-MOUSE"), "follow-mouse", "case-insensitive");
assert.strictEqual(WM.normalizeFocusPolicy("  click  "), "click", "trimmed");
assert.strictEqual(WM.normalizeFocusPolicy("garbage"), "click", "unknown -> click fallback");
assert.strictEqual(WM.normalizeFocusPolicy(undefined), "click", "undefined -> click");
assert.strictEqual(WM.normalizeFocusPolicy(null), "click", "null -> click");
assert.strictEqual(WM.normalizeFocusPolicy(42), "click", "non-string -> click");

// ─── Placement normalisation ────────────────────────────────────────
assert.strictEqual(WM.normalizePlacement("center"), "center");
assert.strictEqual(WM.normalizePlacement("under-mouse"), "under-mouse");
assert.strictEqual(WM.normalizePlacement("smart"), "smart");
assert.strictEqual(WM.normalizePlacement("cascade"), "cascade");
assert.strictEqual(WM.normalizePlacement("Cascade"), "cascade", "case-insensitive");
assert.strictEqual(WM.normalizePlacement(""), "smart", "empty -> smart fallback");
assert.strictEqual(WM.normalizePlacement("tile"), "smart", "unknown -> smart fallback");

// ─── Titlebar action normalisation ──────────────────────────────────
assert.strictEqual(WM.normalizeTitlebarAction("maximize"), "maximize");
assert.strictEqual(WM.normalizeTitlebarAction("shade"), "shade");
assert.strictEqual(WM.normalizeTitlebarAction("minimize"), "minimize");
assert.strictEqual(WM.normalizeTitlebarAction("nothing"), "nothing");
assert.strictEqual(WM.normalizeTitlebarAction("MAXIMIZE"), "maximize", "case-insensitive");
assert.strictEqual(WM.normalizeTitlebarAction("roll-up"), "maximize", "unknown -> maximize fallback");
assert.strictEqual(WM.normalizeTitlebarAction(undefined), "maximize");

// ─── Numeric clamping ───────────────────────────────────────────────
assert.strictEqual(WM.clampFfmDelay(0), 0);
assert.strictEqual(WM.clampFfmDelay(250), 250);
assert.strictEqual(WM.clampFfmDelay(1000), 1000);
assert.strictEqual(WM.clampFfmDelay(-50), 0, "below min clamps to 0");
assert.strictEqual(WM.clampFfmDelay(99999), 1000, "above max clamps to 1000");
assert.strictEqual(WM.clampFfmDelay("abc"), 0, "garbage -> min");
assert.strictEqual(WM.clampFfmDelay(NaN), 0, "NaN -> min");
assert.strictEqual(WM.clampFfmDelay("300"), 300, "numeric string parsed");

assert.strictEqual(WM.clampSnapDistance(16), 16);
assert.strictEqual(WM.clampSnapDistance(1), 1);
assert.strictEqual(WM.clampSnapDistance(64), 64);
assert.strictEqual(WM.clampSnapDistance(0), 1, "below min clamps to 1");
assert.strictEqual(WM.clampSnapDistance(1000), 64, "above max clamps to 64");
assert.strictEqual(WM.clampSnapDistance(Infinity), 1, "Infinity is not a parseable int -> min");
assert.strictEqual(WM.clampSnapDistance(undefined), 1, "undefined -> min");

// ─── Accelerator validation ─────────────────────────────────────────
assert.strictEqual(WM.isValidAccelerator("Super+Shift+Left"), true);
assert.strictEqual(WM.isValidAccelerator("Alt+F4"), true);
assert.strictEqual(WM.isValidAccelerator("XF86AudioPlay"), true);
assert.strictEqual(WM.isValidAccelerator(""), false, "empty rejected");
assert.strictEqual(WM.isValidAccelerator("Alt+F4 kill"), false, "whitespace rejected");
assert.strictEqual(WM.isValidAccelerator("Alt+F4;exec foo"), false, "semicolon rejected");
assert.strictEqual(WM.isValidAccelerator("a`b`"), false, "backtick rejected");
assert.strictEqual(WM.isValidAccelerator("$(rm)"), false, "command-subst rejected");
assert.strictEqual(WM.isValidAccelerator(undefined), false);

// ─── Accelerator sanitisation (used at persist time) ────────────────
assert.strictEqual(WM.sanitizeAccelerator("Super+Left"), "Super+Left", "valid kept");
assert.strictEqual(WM.sanitizeAccelerator("Alt+F4 kill"), "", "invalid collapsed to empty");
assert.strictEqual(WM.sanitizeAccelerator(""), "", "empty stays empty");
assert.strictEqual(WM.sanitizeAccelerator(undefined), "", "undefined -> empty");

// ─── Full policy normalisation ──────────────────────────────────────
const norm = WM.normalizePolicy({
    focusPolicy: "FOLLOW-MOUSE",
    focusFollowsMouseDelay: 5000,
    raiseOnClick: 1,
    raiseOnHover: 0,
    placement: "junk",
    snapEnabled: "yes",
    snapDistance: -3,
    titlebarDoubleClick: "shade",
    decorationTheme: "Adwaita-dark",
    shortcutClose: "Alt+F4",
    shortcutTileLeft: "Super+Left"
});
assert.strictEqual(norm.focusPolicy, "follow-mouse");
assert.strictEqual(norm.focusFollowsMouseDelay, 1000, "delay clamped");
assert.strictEqual(norm.raiseOnClick, true, "truthy coerced to bool");
assert.strictEqual(norm.raiseOnHover, false, "falsy coerced to bool");
assert.strictEqual(norm.placement, "smart", "junk placement -> smart");
assert.strictEqual(norm.snapEnabled, true);
assert.strictEqual(norm.snapDistance, 1, "negative snap distance clamped");
assert.strictEqual(norm.titlebarDoubleClick, "shade");
assert.strictEqual(norm.decorationTheme, "Adwaita-dark");
assert.strictEqual(norm.shortcutClose, "Alt+F4", "valid accelerator kept");
assert.strictEqual(norm.shortcutTileLeft, "Super+Left", "valid accelerator kept");

// Empty input yields sane defaults.
const def = WM.normalizePolicy({});
assert.strictEqual(def.focusPolicy, "click");
assert.strictEqual(def.placement, "smart");
assert.strictEqual(def.titlebarDoubleClick, "maximize");
assert.strictEqual(def.snapDistance, 1, "missing snap distance -> min");
assert.strictEqual(def.decorationTheme, "");
assert.strictEqual(def.shortcutClose, "", "missing accelerator -> empty");
const defNoArg = WM.normalizePolicy();
assert.strictEqual(defNoArg.focusPolicy, "click", "undefined raw handled");

// ─── INJECTION SAFETY (persist-time sanitisation) ───────────────────
// qdshell only runs on qdwin and builds NO compositor command for WM policy,
// so there is no command argv to inject into. The defence-in-depth guarantee
// is instead that a malicious accelerator or theme string can never be
// PERSISTED as something a future backend might mis-parse as an extra command.

// A malicious accelerator that embeds command separators / whitespace must be
// rejected at validation and collapsed to "" at persist time, so it can never
// be stored as a usable accelerator.
const EVIL_ACCEL = "Alt+F4 kill; exec touch /tmp/pwned";
assert.strictEqual(WM.isValidAccelerator(EVIL_ACCEL), false,
    "malicious accelerator rejected by validation");
assert.strictEqual(WM.sanitizeAccelerator(EVIL_ACCEL), "",
    "malicious accelerator sanitised to empty");
const evilNorm = WM.normalizePolicy({
    shortcutClose: EVIL_ACCEL,
    shortcutToggleMaximize: "Super+Up",
    shortcutToggleFullscreen: "a`b`",
    shortcutTileLeft: "$(rm -rf ~)",
    shortcutTileRight: "Super+Right"
});
assert.strictEqual(evilNorm.shortcutClose, "",
    "malicious accelerator never persisted");
assert.strictEqual(evilNorm.shortcutToggleMaximize, "Super+Up",
    "valid accelerator alongside malicious ones is preserved");
assert.strictEqual(evilNorm.shortcutToggleFullscreen, "",
    "backtick accelerator sanitised away");
assert.strictEqual(evilNorm.shortcutTileLeft, "",
    "command-substitution accelerator sanitised away");
assert.strictEqual(evilNorm.shortcutTileRight, "Super+Right");
// No persisted accelerator contains a shell/command separator.
[evilNorm.shortcutClose, evilNorm.shortcutToggleMaximize,
 evilNorm.shortcutToggleFullscreen, evilNorm.shortcutTileLeft,
 evilNorm.shortcutTileRight].forEach(acc => {
    assert.strictEqual(/[;&|`$\s]/.test(acc), false,
        "persisted accelerator has no separator/metacharacter");
});

// The decoration theme name is kept verbatim as opaque DATA (it is never used
// to build a command anywhere), so even a shell-metacharacter-laden name is
// preserved but inert.
const EVIL_THEME = ";rm -rf ~";
assert.strictEqual(WM.normalizePolicy({ decorationTheme: EVIL_THEME }).decorationTheme,
    EVIL_THEME, "theme name preserved as opaque data, never used in a command");

console.log("window-manager-policy: all assertions passed");
