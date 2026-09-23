// Pure decision for the clipboard deny-clear COALESCER (clipboard.md
// §"deny-storm robustness"). Extracted here as plain CommonJS — like
// ClipboardSilo.js / ClipboardBroker.js / ClipboardFocusClear.js — so the
// security-relevant decision (whether a given fail-closed deny must emit a
// fresh clear_selection wire request, or is a redundant repeat that may be
// coalesced) is unit-testable from Node without a live compositor.
// ClipboardGate.qml's _logDecisionAndMaybeClear imports this module and calls
// shouldSendClear() right at the clearSelection() call boundary. This file
// holds NO state of its own: the per-key last-cleared timestamps live in the
// QML singleton (root._lastDenyClearByKey) and are passed in + mutated here,
// the same way ClipboardGate owns _selectionSourceSilo for the focus-clear
// decision.
//
// WHY THIS EXISTS (the bug it fixes): under a deny storm — a producer
// re-asserting clipboard ownership in a tight loop, whether a buggy client, a
// clipboard manager, or a malicious tier guest deliberately flooding
// selection_set — the gate denies and calls clearSelection on EVERY incoming
// event. Each clear is a synchronous qdwin_shell_v1.clear_selection wire write
// into qdshell's fixed 4 KB libwayland output buffer. At ~127 events/s qdwin
// drains slower than qdshell fills, libwayland's marshal hits
// "Data too big for buffer", and the WHOLE privileged shell↔compositor
// connection is fatally errored — taking out the clipboard isolation channel
// and dropping any in-flight load-bearing request (e.g. a cross-silo focus
// injection) during the rebind window. Coalescing bounds the clear wire rate
// to at most one per key per window, which breaks the storm before it can
// overflow the buffer.
//
// SECURITY POSTURE — this does NOT weaken fail-closed:
//   • The FIRST deny for a given denied-offer identity ALWAYS clears.
//   • Only IDENTICAL repeats of an already-cleared offer inside a short window
//     are suppressed (re-clearing an already-cleared selection is a no-op).
//   • A changed seat / kind / source silo / destination silo / mime set is a
//     DIFFERENT key → a fresh first clear.
//   • Every CLIPBOARD_GATE verdict line is still logged by the caller
//     (unchanged, frozen contract); only the redundant WIRE call is dropped.
//   • The independent set-time and receive-time gates still run per event, and
//     the focus-aware-clear path is unaffected. A denied offer that briefly
//     persists for up to one window while being re-cleared is strictly safer
//     than the un-coalesced behaviour, where the connection dies and NOTHING
//     gets cleared at all.

// Field separator for the composite key. U+001F (US, "unit separator") is not
// expected in a silo string or mime type — but inbound security-context tuples
// and mime lists reach the gate WITHOUT control-char stripping (the binding
// rejects control chars only on the OUTBOUND broker/clearSelection path), so a
// malicious client could forge a U+001F in its own app_id → silo string. We
// therefore do NOT rely on the separator alone for non-aliasing: coalesceKey
// LENGTH-PREFIXES every field (injective regardless of field contents); the
// separator is kept only to keep the key readable in logs.
var FIELD_SEP = "\u001f";

// Default coalescing window. One suppression is all it takes to stop the tight
// loop from overrunning the 4 KB buffer; 500 ms also means a genuinely
// re-asserting source is still re-cleared twice a second — well within
// human-perceptible "the clipboard stays cleared" expectations.
var DEFAULT_WINDOW_MS = 500;

// Entries older than window * PRUNE_FACTOR are dropped on each call so the map
// can't grow without bound under a storm of ever-changing keys.
var PRUNE_FACTOR = 4;

function selectionKind(isPrimary) {
    return isPrimary ? "primary" : "clipboard";
}

// Build the composite denied-offer identity key for a decision entry. Mirrors
// exactly the fields ClipboardGate's _onSelectionSet packs into `entry`. Any
// missing field collapses to a fail-safe constant ("unknown" / "") so an
// under-specified entry still yields a STABLE key — it never accidentally
// merges into another offer's key, and its first deny still clears.
function coalesceKey(entry) {
    var fields = [
        entry.seat || "default",
        selectionKind(entry.isPrimary),
        entry.srcSilo || "unknown",
        entry.dstSilo || "unknown",
        entry.mimeCsv || ""
    ];
    // Length-prefix each field: "<len>:<field>" joined by the separator. This
    // is INJECTIVE no matter what the field contains (incl. a forged U+001F or
    // a literal "N:") — distinct field tuples always map to distinct keys —
    // because the reader would consume exactly <len> chars after each colon.
    // We never actually parse the key back (it's only ever compared for map
    // equality), so injectivity is all we need to prevent boundary-shift
    // aliasing of two distinct denied offers onto one key.
    var out = "";
    for (var i = 0; i < fields.length; i++) {
        var f = String(fields[i]);
        out += f.length + ":" + f + FIELD_SEP;
    }
    return out;
}

// Drop expired entries (older than window * PRUNE_FACTOR, or stamped in the
// future after a backward clock step) so `state` stays bounded.
function pruneExpired(state, nowMs, windowMs) {
    var maxAge = windowMs * PRUNE_FACTOR;
    var keys = Object.keys(state);
    for (var i = 0; i < keys.length; i++) {
        var ts = state[keys[i]];
        if (nowMs < ts || (nowMs - ts) > maxAge)
            delete state[keys[i]];
    }
}

// Decide whether a fail-closed deny for `entry` must issue a REAL
// clearSelection wire call, given the per-key last-cleared timestamps in
// `state` (a plain object owned by the QML singleton).
//
//   • Returns TRUE  → caller must call binding.clearSelection(); records nowMs.
//   • Returns FALSE → identical deny within the window; caller suppresses the
//                     wire call (and logs a CLIPBOARD_CLEAR_COALESCED debug
//                     line) but STILL logged the CLIPBOARD_GATE verdict above.
//
// The first deny for a key (state has no entry) clears. A backward clock step
// (nowMs < last) is treated as "window lapsed" and clears — the fail-closed
// choice, so an NTP step can never suppress a genuine clear. Mutates `state`:
// records the timestamp on a TRUE result and prunes stale entries.
function shouldSendClear(state, entry, nowMs, windowMs) {
    var w = (windowMs === undefined || windowMs === null) ? DEFAULT_WINDOW_MS
                                                          : windowMs;
    pruneExpired(state, nowMs, w);
    var key = coalesceKey(entry);
    var last = state[key];
    // Suppress ONLY a same-key repeat that lands inside the window and after
    // the recorded clear (monotonic forward). Everything else clears.
    if (last !== undefined && nowMs >= last && (nowMs - last) < w)
        return false;
    state[key] = nowMs;
    return true;
}

var api = {
    FIELD_SEP: FIELD_SEP,
    DEFAULT_WINDOW_MS: DEFAULT_WINDOW_MS,
    PRUNE_FACTOR: PRUNE_FACTOR,
    selectionKind: selectionKind,
    coalesceKey: coalesceKey,
    pruneExpired: pruneExpired,
    shouldSendClear: shouldSendClear
};

// Dual export: CommonJS for the Node tests, and the same names for a QML
// `import "ClipboardDenyCoalesce.js" as ClipboardDenyCoalesce`.
if (typeof module !== "undefined" && module.exports)
    module.exports = api;
