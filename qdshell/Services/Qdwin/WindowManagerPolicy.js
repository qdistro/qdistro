// WindowManagerPolicy — pure, side-effect-free helpers extracted from
// WindowManagerService.qml. NO Process / Settings / Quickshell access: only
// string/array transforms and enum normalisation. Usable from both QML
// (import "WindowManagerPolicy.js" as WMPolicy) and Node
// (require("./WindowManagerPolicy.js")) so the policy logic can be unit-tested
// headless.
//
// qdshell only ever runs on qdwin, whose qdwin_shell_v1 IPC has no
// window-manager-policy mutation request yet. So this module does NOT build any
// compositor command (no sway/labwc/hyprctl argv) — WM policy is persist-only.
// Its job is purely to keep persisted values sane and safe:
//   1. Normalise/validate the window-manager policy enums (focus policy,
//      new-window placement, titlebar double-click action) to canonical
//      tokens, with safe fallbacks for unknown/garbage input.
//   2. Clamp the numeric policy values (focus-follows-mouse delay, snap
//      distance) into their valid ranges.
//   3. Validate keyboard-shortcut accelerator strings against a strict
//      allowlist so a malicious accelerator can never be persisted as
//      something a future backend might mis-parse as an extra command.

// ─── Enum vocabularies ───────────────────────────────────────────────
var FOCUS_POLICIES = ["click", "follow-mouse"];
var PLACEMENTS = ["center", "under-mouse", "smart", "cascade"];
var TITLEBAR_ACTIONS = ["maximize", "shade", "minimize", "nothing"];

// ─── Bounds (mirror the NSpinBox/NValueSlider limits in the QML tab) ──
var FFM_DELAY_MIN = 0;     // ms
var FFM_DELAY_MAX = 1000;  // ms
var SNAP_DISTANCE_MIN = 1; // px
var SNAP_DISTANCE_MAX = 64; // px

function _normEnum(value, vocab, fallback) {
    var v = String(value === undefined || value === null ? "" : value).trim().toLowerCase();
    for (var i = 0; i < vocab.length; i++) {
        if (vocab[i] === v)
            return v;
    }
    return fallback;
}

// Focus policy: "click" (click-to-focus) | "follow-mouse"
// (focus-follows-mouse). Anything else falls back to click-to-focus, the
// conservative default.
function normalizeFocusPolicy(value) {
    return _normEnum(value, FOCUS_POLICIES, "click");
}

// New-window placement strategy.
function normalizePlacement(value) {
    return _normEnum(value, PLACEMENTS, "smart");
}

// Titlebar double-click action canonical token.
function normalizeTitlebarAction(value) {
    return _normEnum(value, TITLEBAR_ACTIONS, "maximize");
}

// Clamp an integer into [min,max]; non-finite/garbage -> min.
function _clampInt(value, min, max) {
    var n = parseInt(value, 10);
    if (!isFinite(n))
        n = min;
    if (n < min)
        n = min;
    if (n > max)
        n = max;
    return n;
}

function clampFfmDelay(value) {
    return _clampInt(value, FFM_DELAY_MIN, FFM_DELAY_MAX);
}

function clampSnapDistance(value) {
    return _clampInt(value, SNAP_DISTANCE_MIN, SNAP_DISTANCE_MAX);
}

// A keyboard-shortcut accelerator is a `+`-joined list of modifier/key tokens,
// e.g. "Super+Shift+Left". We VALIDATE it against a strict allowlist: it must
// contain ONLY ASCII letters, digits, `+`, `_`, and `-`. Anything else
// (whitespace, `;`, backticks, `$(...)`, etc.) makes the whole accelerator
// invalid. This is what keeps a malicious accelerator like
// "Alt+F4 kill; exec touch /tmp/pwned" from ever being treated as valid — it
// is rejected here, so a future backend can never be handed a string that
// smuggles in extra commands.
var _ACCEL_RE = /^[A-Za-z0-9_+-]+$/;

function isValidAccelerator(accel) {
    var a = String(accel === undefined || accel === null ? "" : accel);
    if (a.length === 0)
        return false;
    return _ACCEL_RE.test(a);
}

// Sanitise an accelerator for PERSISTENCE: a valid accelerator is kept; an
// invalid one (empty, whitespace, shell/command separators, etc.) is collapsed
// to "" so a dangerous string is never persisted as if it were a usable
// accelerator. Used by normalizePolicy so the stored policy can only ever hold
// safe accelerator tokens.
function sanitizeAccelerator(accel) {
    return isValidAccelerator(accel) ? String(accel) : "";
}

// Coerce an untrusted free-text value (the decoration theme name) to a string.
// Kept verbatim as DATA — it is never built into a command. We strip leading /
// trailing whitespace only; the name has no special meaning to qdshell.
function _str(value) {
    return String(value === undefined || value === null ? "" : value);
}

// Produce a fully-normalised, persist-safe policy object from a raw
// settings-shaped object. Booleans are coerced with !! so any truthy/falsy
// persisted value is sane. Accelerators are sanitised so only allowlisted
// tokens survive; the decoration theme name is kept verbatim (opaque data,
// never used to build a command).
function normalizePolicy(raw) {
    raw = raw || {};
    return {
        focusPolicy: normalizeFocusPolicy(raw.focusPolicy),
        focusFollowsMouseDelay: clampFfmDelay(raw.focusFollowsMouseDelay),
        raiseOnClick: !!raw.raiseOnClick,
        raiseOnHover: !!raw.raiseOnHover,
        placement: normalizePlacement(raw.placement),
        snapEnabled: !!raw.snapEnabled,
        snapDistance: clampSnapDistance(raw.snapDistance),
        titlebarDoubleClick: normalizeTitlebarAction(raw.titlebarDoubleClick),
        // Decoration theme name is UNTRUSTED free text — kept as-is (opaque
        // data, never used to build a command).
        decorationTheme: _str(raw.decorationTheme),
        // WM keyboard shortcut accelerators are UNTRUSTED free text. Only
        // allowlisted tokens survive; anything malicious collapses to "".
        shortcutClose: sanitizeAccelerator(raw.shortcutClose),
        shortcutToggleMaximize: sanitizeAccelerator(raw.shortcutToggleMaximize),
        shortcutToggleFullscreen: sanitizeAccelerator(raw.shortcutToggleFullscreen),
        shortcutTileLeft: sanitizeAccelerator(raw.shortcutTileLeft),
        shortcutTileRight: sanitizeAccelerator(raw.shortcutTileRight)
    };
}

if (typeof module !== "undefined") {
    module.exports = {
        FOCUS_POLICIES: FOCUS_POLICIES,
        PLACEMENTS: PLACEMENTS,
        TITLEBAR_ACTIONS: TITLEBAR_ACTIONS,
        FFM_DELAY_MIN: FFM_DELAY_MIN,
        FFM_DELAY_MAX: FFM_DELAY_MAX,
        SNAP_DISTANCE_MIN: SNAP_DISTANCE_MIN,
        SNAP_DISTANCE_MAX: SNAP_DISTANCE_MAX,
        normalizeFocusPolicy: normalizeFocusPolicy,
        normalizePlacement: normalizePlacement,
        normalizeTitlebarAction: normalizeTitlebarAction,
        clampFfmDelay: clampFfmDelay,
        clampSnapDistance: clampSnapDistance,
        isValidAccelerator: isValidAccelerator,
        sanitizeAccelerator: sanitizeAccelerator,
        normalizePolicy: normalizePolicy,
    };
}
