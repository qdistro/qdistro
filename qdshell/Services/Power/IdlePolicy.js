// IdlePolicy — pure, side-effect-free resolution of the two ext-idle-notify
// timeouts (inactivity action + display-off) from the current power policy.
// NO Settings / Qdwin / Quickshell access, so it is unit-testable headless
// (require("./IdlePolicy.js")).
//
// The compositor observes input idle via ext-idle-notify-v1; PowerService arms
// at most two notifications. This module decides, for each, the timeout in
// MILLISECONDS to arm (or 0 = do not arm / cancel):
//   * inactivity action: armed only when there is an action to take
//     (action !== "nothing") and a positive timeout;
//   * display-off: armed for any positive timeout.
// Presentation mode suppresses BOTH (the user explicitly asked to stay awake,
// and qdwin's ext-idle-notify only respects Wayland idle-inhibitors, not the
// systemd-inhibit presentation path — so the gate must live here).

function _toMs(minutes) {
    var m = parseInt(minutes, 10);
    if (!isFinite(m) || m <= 0)
        return 0;
    return m * 60 * 1000;
}

// Resolve { inactivityMs, displayOffMs } from the policy. inactivityAction is
// the canonical token ("nothing" | "suspend" | "hibernate" | …);
// inactivityMinutes / displayOffMinutes are the *effective* (AC- or
// battery-resolved) timeouts in minutes. presentationMode true zeroes both.
function resolveArming(inactivityAction, inactivityMinutes, displayOffMinutes,
                       presentationMode) {
    if (presentationMode)
        return { inactivityMs: 0, displayOffMs: 0 };
    var action = String(inactivityAction === undefined || inactivityAction === null
                        ? "" : inactivityAction).trim().toLowerCase();
    var inactivityMs = (action === "nothing" || action === "")
        ? 0 : _toMs(inactivityMinutes);
    return {
        inactivityMs: inactivityMs,
        displayOffMs: _toMs(displayOffMinutes)
    };
}

if (typeof module !== "undefined") {
    module.exports = {
        resolveArming: resolveArming,
    };
}
