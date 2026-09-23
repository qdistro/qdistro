// PointerInputConfig.js — pure mapping from the persisted Settings.data.pointer
// policy to the numeric arguments of qdwin_shell_v1.set_pointer_config (v28),
// as exposed by QdwinBinding.setPointerConfig / Qdwin.applyPointerConfig.
//
// Dual CommonJS / QML module (same shape as WindowManagerPolicy.js): the QML
// side imports it as `"PointerInputConfig.js" as PointerCfg`; node tests
// require() it directly. NO compositor dispatch happens here — this is pure
// normalisation + clamping so the unit test can pin every edge.
//
// The compositor ALSO clamps/normalises server-side (fail-safe), but we send
// canonical values so the wire never carries garbage and the persisted UI
// state round-trips predictably.
//
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Module shape: top-level declarations + a guarded `module.exports`, mirroring
// the sibling dual modules WindowManagerPolicy.js / PointerInputParse.js. This
// is load-bearing: a QML `import "PointerInputConfig.js" as PointerCfg` only
// exposes the file's TOP-LEVEL function/var declarations as members of the
// import namespace. An earlier IIFE wrapper (assigning the api to globalThis)
// hid every function from the QML side, so PointerInputService.applyToCompositor
// threw `TypeError: Property 'toBindingArgs' ... is not a function` on every
// call and the libinput pointer policy (accel profile/speed, natural scroll,
// scroll method, tap-to-click, …) was NEVER pushed to qdwin. Keep the symbols
// top-level so both QML (`PointerCfg.fn`) and Node (`require().fn`) see them.

// qdwin_shell_v1.accel_profile enum.
var ACCEL_PROFILES = ["adaptive", "flat"]; // index == wire value
// qdwin_shell_v1.scroll_method enum.
var SCROLL_METHODS = ["none", "two_finger", "edge", "on_button_down"];

// accel_speed is sent in milli-units in [-1000, 1000] (⇒ libinput
// [-1.0, 1.0]). Settings.data.pointer.pointerSpeed is a UI value in
// [0.0, 1.0] where 0.5 is "no acceleration adjustment" (libinput 0.0).
var SPEED_MIN = -1000;
var SPEED_MAX = 1000;

function _num(v, fallback) {
    var n = (typeof v === "number") ? v : parseFloat(v);
    return (typeof n === "number" && isFinite(n)) ? n : fallback;
}

function _bool(v) {
    return v ? 1 : 0;
}

// Map a UI pointerSpeed (0..1, 0.5 = neutral) to the wire milli-units
// (-1000..1000), clamped. Out-of-range / non-numeric ⇒ neutral (0).
function speedToMilliUnits(pointerSpeed) {
    var s = _num(pointerSpeed, 0.5);
    if (s < 0) s = 0;
    if (s > 1) s = 1;
    var mu = Math.round((s * 2 - 1) * 1000);
    if (mu < SPEED_MIN) mu = SPEED_MIN;
    if (mu > SPEED_MAX) mu = SPEED_MAX;
    return mu;
}

// Normalise an accel-profile string to its wire enum value. Unknown ⇒
// adaptive (0), matching the compositor fallback.
function accelProfileEnum(profile) {
    var i = ACCEL_PROFILES.indexOf(String(profile == null ? "" : profile)
        .trim().toLowerCase());
    return i < 0 ? 0 : i;
}

// Normalise a scroll-method string to its wire enum value. Unknown ⇒
// two_finger (1), matching the compositor fallback.
function scrollMethodEnum(method) {
    var i = SCROLL_METHODS.indexOf(String(method == null ? "" : method)
        .trim().toLowerCase());
    return i < 0 ? 1 : i;
}

// Build the full positional argument vector for
// Qdwin.applyPointerConfig(...) / QdwinBinding.setPointerConfig(...) from a
// Settings.data.pointer-shaped object. Field names mirror the QML settings.
function toBindingArgs(pointer) {
    var p = pointer || {};
    return {
        accelSpeed: speedToMilliUnits(p.pointerSpeed),
        accelProfile: accelProfileEnum(p.accelProfile),
        naturalScroll: _bool(p.naturalScroll),
        tapToClick: _bool(p.tapToClick),
        leftHanded: _bool(p.leftHanded),
        // Settings call it middleClickEmulation; the wire field is
        // middle_emulation.
        middleEmulation: _bool(p.middleClickEmulation),
        disableWhileTyping: _bool(p.disableWhileTyping),
        scrollMethod: scrollMethodEnum(p.scrollMethod)
    };
}

if (typeof module !== "undefined") {
    module.exports = {
        ACCEL_PROFILES: ACCEL_PROFILES,
        SCROLL_METHODS: SCROLL_METHODS,
        SPEED_MIN: SPEED_MIN,
        SPEED_MAX: SPEED_MAX,
        speedToMilliUnits: speedToMilliUnits,
        accelProfileEnum: accelProfileEnum,
        scrollMethodEnum: scrollMethodEnum,
        toBindingArgs: toBindingArgs
    };
}
