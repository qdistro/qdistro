// Pure decision for the focus-aware-clear primitive
// (clipboard.md §"focus-aware-clear", track-04 Phase-2). Extracted here as
// plain CommonJS — like ClipboardSilo.js / ClipboardBroker.js — so the
// security-critical decision (which selection kinds to clear on a keyboard
// focus transition, and what to journal) is unit-testable from Node without
// a live compositor. ClipboardGate.qml's _onSeatFocusChanged calls
// planFocusClear() and then performs the listed side effects (clearSelection
// + CLIPBOARD_FOCUS_GATE journal line + forget the tracked source) in order.
// This file holds NO state and performs NO side effects: it only DECIDES.

// Sentinel handle meaning "no toplevel focused" on the qdwin_shell_v1 wire
// (uint32 max). A focus change to it resolves to the "unknown" destination
// silo, which differs from every known source silo and therefore clears
// (fail-closed).
var NO_HANDLE = 4294967295;

// Resolve the silo of the newly focused toplevel handle. An absent handle
// (NO_HANDLE) or a handle not present in the handle→silo map resolves to
// "unknown" — which, being unequal to any tracked (known) source silo,
// drives a fail-closed clear.
function resolveDstSilo(handle, handleToSilo) {
    if (handle === NO_HANDLE)
        return "unknown";
    var map = handleToSilo || {};
    return (map[handle] !== undefined && map[handle] !== null)
        ? map[handle] : "unknown";
}

// Decide what a single seat_focus_changed(seat, handle) must do.
//
// Inputs:
//   seat            seat name (falsy → "default" in the emitted journal line)
//   handle          newly keyboard-focused toplevel handle (or NO_HANDLE)
//   handleToSilo    map handle→silo string
//   selectionSourceSilo  map selection-kind→source silo for the silo that set
//                        the active selection. Key "0" = regular clipboard,
//                        "1" = primary. A kind is present ONLY when a known
//                        (trustworthy, non-"unknown") source owns it.
//
// Returns an ORDERED list of clear actions, one per selection kind that must
// be cleared because focus crossed OUT of its source silo:
//   { selKind, isPrimary, seat, srcSilo, dstSilo }
// Each action means: emit the CLIPBOARD_FOCUS_GATE deny line, call
// clearSelection(seat, isPrimary), and forget selectionSourceSilo[selKind].
// Kinds whose source silo equals the destination silo (same-silo paste must
// keep working) and kinds with no tracked source are omitted → no clear.
function planFocusClear(seat, handle, handleToSilo, selectionSourceSilo) {
    var dstSilo = resolveDstSilo(handle, handleToSilo);
    var src = selectionSourceSilo || {};
    // Regular clipboard ("0") and primary selection ("1") are evaluated
    // independently — a single focus change can strand either, and clearing
    // one must not touch the other.
    var kinds = [
        { selKind: "0", isPrimary: 0 },
        { selKind: "1", isPrimary: 1 }
    ];
    var actions = [];
    for (var i = 0; i < kinds.length; i++) {
        var srcSilo = src[kinds[i].selKind];
        // No tracked selection / unknown source → nothing trustworthy to
        // clear for this kind.
        if (srcSilo === undefined)
            continue;
        // Focus stayed within the source silo → same-silo paste preserved.
        if (srcSilo === dstSilo)
            continue;
        // Cross-silo focus → clear this kind for the destination silo.
        actions.push({
            selKind: kinds[i].selKind,
            isPrimary: kinds[i].isPrimary,
            seat: seat || "default",
            srcSilo: srcSilo,
            dstSilo: dstSilo
        });
    }
    return actions;
}

var api = {
    NO_HANDLE: NO_HANDLE,
    resolveDstSilo: resolveDstSilo,
    planFocusClear: planFocusClear
};

// Dual export: CommonJS for the Node tests, and a `.WrapperGate`-free plain
// object so a QML `import "ClipboardFocusClear.js" as X` sees the same names.
if (typeof module !== "undefined" && module.exports)
    module.exports = api;
