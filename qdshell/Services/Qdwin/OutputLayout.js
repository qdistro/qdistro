// OutputLayout — pure, side-effect-free helpers for the Display layout tab.
// NO Process / Settings / Quickshell access: only plain data transforms over
// the output/head model the qml-plugin (QdwinBinding.outputs) exposes. Usable
// from both QML (import "OutputLayout.js" as OutputLayout) and Node
// (require("./OutputLayout.js")) so the layout/validation logic can be unit-
// tested headless. UI QML stays thin: it calls into here for every decision.
//
// qdshell runs only on qdwin, which implements wlr-output-management-v1; the
// compositor itself is the source of truth for what applies live. This module
// is purely about preparing/validating a proposed layout before it is handed
// to QdwinBinding.applyLayout, and about modelling the confirm-or-revert
// baseline:
//   1. Normalise an output snapshot into a stable layout array.
//   2. Validate a proposed layout: at least one enabled output; no two
//      enabled outputs overlapping; at most one primary (zero is tolerated
//      and resolved deterministically by choosePrimary); each requested
//      mode/scale/transform is in range. Gaps between outputs are allowed
//      (X-style layouts permit them) — only overlap is rejected.
//   3. Normalise positions (snap to a gapless top-left-anchored arrangement)
//      so dragging in the UI can't persist negative or floating coordinates.
//   4. Choose the primary output deterministically.
//   5. Compute the effective pixel rect of an output (mode size adjusted for
//      rotation + scale) so overlap is computed in global compositor space.
//
// All strings (name/description/make/model/serial) are pass-through only; this
// module never interpolates them into a command. The UI renders them as
// PlainText. See todo/decisions/qdwin-output-management.md.

// wl_output.transform enum values.
var TRANSFORM_NORMAL = 0;
var TRANSFORM_90 = 1;
var TRANSFORM_180 = 2;
var TRANSFORM_270 = 3;
var TRANSFORM_FLIPPED = 4;
var TRANSFORM_FLIPPED_90 = 5;
var TRANSFORM_FLIPPED_180 = 6;
var TRANSFORM_FLIPPED_270 = 7;

function isInt(v) {
    return typeof v === "number" && isFinite(v) && Math.floor(v) === v;
}

function clampInt(v, lo, hi) {
    var n = parseInt(v, 10);
    if (isNaN(n)) n = lo;
    if (n < lo) n = lo;
    if (n > hi) n = hi;
    return n;
}

// A transform that swaps width/height (90 / 270, flipped or not).
function transformSwapsAxes(t) {
    return t === TRANSFORM_90 || t === TRANSFORM_270 ||
           t === TRANSFORM_FLIPPED_90 || t === TRANSFORM_FLIPPED_270;
}

function isValidTransform(t) {
    return isInt(t) && t >= TRANSFORM_NORMAL && t <= TRANSFORM_FLIPPED_270;
}

// The logical (compositor-space) size of an output given its mode pixel size,
// its scale, and its transform. Logical size = pixel / scale, axes swapped on
// a 90/270 rotation. Returns {w, h} in integer logical pixels (>=1).
function logicalSize(modeW, modeH, scale, transform) {
    var sc = scale > 0 ? scale : 1;
    var w = Math.max(1, Math.round(modeW / sc));
    var h = Math.max(1, Math.round(modeH / sc));
    if (transformSwapsAxes(transform)) {
        var t = w; w = h; h = t;
    }
    return { w: w, h: h };
}

// Build the effective rect {x, y, w, h} of one entry. modeW/modeH come from
// the entry's chosen mode (width/height), falling back to 0 if unknown.
function effectiveRect(entry) {
    var ls = logicalSize(entry.width || 0, entry.height || 0,
                         entry.scale || 1, entry.transform || 0);
    return {
        x: isInt(entry.x) ? entry.x : 0,
        y: isInt(entry.y) ? entry.y : 0,
        w: ls.w,
        h: ls.h
    };
}

// Do two axis-aligned rects overlap (strictly — touching edges is allowed)?
function rectsOverlap(a, b) {
    return a.x < b.x + b.w && b.x < a.x + a.w &&
           a.y < b.y + b.h && b.y < a.y + a.h;
}

// Normalise a single output snapshot (a QdwinBinding.outputs entry) into a
// layout entry with the chosen mode flattened to width/height/refresh. Picks
// the current mode if present, else the preferred mode, else the first mode.
function entryFromSnapshot(snap) {
    var modes = snap.modes || [];
    var idx = isInt(snap.currentMode) && snap.currentMode >= 0
        ? snap.currentMode : -1;
    if (idx < 0) {
        for (var i = 0; i < modes.length; i++) {
            if (modes[i].preferred) { idx = i; break; }
        }
    }
    if (idx < 0 && modes.length > 0) idx = 0;
    var m = idx >= 0 ? modes[idx] : { width: 0, height: 0, refresh: 0 };
    return {
        name: snap.name,
        description: snap.description || snap.name,
        enabled: !!snap.enabled,
        x: isInt(snap.x) ? snap.x : 0,
        y: isInt(snap.y) ? snap.y : 0,
        width: m.width || 0,
        height: m.height || 0,
        refresh: m.refresh || 0,
        scale: snap.scale > 0 ? snap.scale : 1,
        transform: isValidTransform(snap.transform) ? snap.transform : 0,
        primary: false
    };
}

// Build the working layout array from the full outputs snapshot list.
function layoutFromSnapshots(snapshots) {
    var out = [];
    for (var i = 0; i < (snapshots || []).length; i++)
        out.push(entryFromSnapshot(snapshots[i]));
    return out;
}

// Is `modeReq` ({width,height,refresh}) present in `modes` (the advertised
// list)? refresh 0 means "any refresh at that size".
function modeIsAvailable(modes, modeReq) {
    for (var i = 0; i < (modes || []).length; i++) {
        var m = modes[i];
        if (m.width === modeReq.width && m.height === modeReq.height &&
            (modeReq.refresh === 0 || m.refresh === modeReq.refresh))
            return true;
    }
    return false;
}

// Validate a proposed layout. `availableModesByName` maps output name → its
// advertised mode array (for mode validation); pass {} to skip mode checks.
// Returns { ok: bool, errors: [string] }.
function validateLayout(layout, availableModesByName) {
    var errors = [];
    layout = layout || [];
    availableModesByName = availableModesByName || {};

    var enabled = layout.filter(function (e) { return e.enabled; });
    if (enabled.length === 0)
        errors.push("at-least-one-enabled");

    // Exactly one primary among the enabled set (zero is tolerated here and
    // resolved by choosePrimary; two or more is an error).
    var primaries = enabled.filter(function (e) { return e.primary; });
    if (primaries.length > 1)
        errors.push("multiple-primary");

    // Per-entry validation.
    for (var i = 0; i < enabled.length; i++) {
        var e = enabled[i];
        if (!e.name)
            errors.push("missing-name");
        if (!isValidTransform(e.transform))
            errors.push("invalid-transform:" + e.name);
        if (!(e.scale > 0))
            errors.push("invalid-scale:" + e.name);
        if (!(e.width > 0 && e.height > 0))
            errors.push("invalid-mode:" + e.name);
        else if (availableModesByName[e.name] !== undefined &&
                 !modeIsAvailable(availableModesByName[e.name], {
                     width: e.width, height: e.height, refresh: e.refresh
                 }))
            errors.push("mode-unavailable:" + e.name);
    }

    // Overlap detection over the enabled set (in global compositor space).
    var rects = enabled.map(effectiveRect);
    for (var a = 0; a < rects.length; a++)
        for (var b = a + 1; b < rects.length; b++)
            if (rectsOverlap(rects[a], rects[b])) {
                errors.push("overlap:" + enabled[a].name + ":" + enabled[b].name);
            }

    return { ok: errors.length === 0, errors: errors };
}

// Normalise positions: shift the whole enabled arrangement so its bounding
// box's top-left is at (0,0), and round every coordinate to an integer.
// Disabled outputs keep their stored position (the compositor ignores them).
// Returns a NEW layout array (does not mutate the input).
function normalizePositions(layout) {
    layout = layout || [];
    var enabled = layout.filter(function (e) { return e.enabled; });
    var minX = 0, minY = 0, first = true;
    for (var i = 0; i < enabled.length; i++) {
        var r = effectiveRect(enabled[i]);
        if (first) { minX = r.x; minY = r.y; first = false; }
        else { minX = Math.min(minX, r.x); minY = Math.min(minY, r.y); }
    }
    return layout.map(function (e) {
        var copy = {};
        for (var k in e) copy[k] = e[k];
        if (e.enabled) {
            copy.x = Math.round((isInt(e.x) ? e.x : 0) - minX);
            copy.y = Math.round((isInt(e.y) ? e.y : 0) - minY);
        }
        return copy;
    });
}

// Deterministically choose the primary output. Honours an explicit
// `primary: true` flag on an enabled output if present; else picks the
// enabled output at (0,0) if any; else the first enabled output by name.
// Returns the chosen output's name, or "" if none enabled.
function choosePrimary(layout, preferredName) {
    layout = layout || [];
    var enabled = layout.filter(function (e) { return e.enabled; });
    if (enabled.length === 0) return "";
    if (preferredName) {
        for (var p = 0; p < enabled.length; p++)
            if (enabled[p].name === preferredName) return enabled[p].name;
    }
    for (var i = 0; i < enabled.length; i++)
        if (enabled[i].primary) return enabled[i].name;
    for (var j = 0; j < enabled.length; j++)
        if ((enabled[j].x || 0) === 0 && (enabled[j].y || 0) === 0)
            return enabled[j].name;
    // Stable by name so the choice doesn't flap on reorder.
    var names = enabled.map(function (e) { return e.name; }).sort();
    return names[0];
}

// Apply choosePrimary's result back onto the layout (sets primary flags so
// exactly the chosen output is primary). Returns a new array.
function withPrimary(layout, preferredName) {
    var chosen = choosePrimary(layout, preferredName);
    return (layout || []).map(function (e) {
        var copy = {};
        for (var k in e) copy[k] = e[k];
        copy.primary = (e.enabled && e.name === chosen);
        return copy;
    });
}

// ---- confirm-or-revert state modelling ----
// The Display tab applies a proposed layout, then shows a 15-second dialog.
// If the user does not confirm, it re-applies the captured baseline. This
// helper models that state machine purely (no timers): given a phase and an
// event it returns the next phase + whether to revert.
//
// Phases: "idle" → "applying" → "confirming" → ("idle" | reverting → "idle").
function nextRevertState(phase, event) {
    switch (phase) {
        case "idle":
            if (event === "apply") return { phase: "applying", revert: false };
            return { phase: "idle", revert: false };
        case "applying":
            if (event === "apply-ok")
                return { phase: "confirming", revert: false };
            if (event === "apply-failed")
                return { phase: "idle", revert: false };  // compositor already reverted
            return { phase: "applying", revert: false };
        case "confirming":
            if (event === "confirm")
                return { phase: "idle", revert: false };
            if (event === "timeout" || event === "cancel")
                return { phase: "reverting", revert: true };
            return { phase: "confirming", revert: false };
        case "reverting":
            // Any terminal event after issuing the revert returns to idle.
            return { phase: "idle", revert: false };
        default:
            return { phase: "idle", revert: false };
    }
}

// Build the QVariantList-shaped layout the binding's applyLayout expects:
// strip UI-only fields, keep name + enabled + geometry. Primary is a shell-
// side concept (the compositor has no "primary" in this protocol — it is a
// persisted hint the bar consumes), so it is NOT sent to the compositor.
function toApplyList(layout) {
    return (layout || []).map(function (e) {
        var o = { name: e.name, enabled: !!e.enabled };
        if (e.enabled) {
            if (isInt(e.x)) o.x = e.x;
            if (isInt(e.y)) o.y = e.y;
            if (e.width > 0 && e.height > 0) {
                o.width = e.width;
                o.height = e.height;
                o.refresh = e.refresh || 0;
            }
            if (e.scale > 0) o.scale = e.scale;
            if (isValidTransform(e.transform)) o.transform = e.transform;
        }
        return o;
    });
}

if (typeof module !== "undefined") {
    module.exports = {
        TRANSFORM_NORMAL: TRANSFORM_NORMAL,
        TRANSFORM_90: TRANSFORM_90,
        TRANSFORM_180: TRANSFORM_180,
        TRANSFORM_270: TRANSFORM_270,
        TRANSFORM_FLIPPED: TRANSFORM_FLIPPED,
        TRANSFORM_FLIPPED_90: TRANSFORM_FLIPPED_90,
        TRANSFORM_FLIPPED_180: TRANSFORM_FLIPPED_180,
        TRANSFORM_FLIPPED_270: TRANSFORM_FLIPPED_270,
        clampInt: clampInt,
        isValidTransform: isValidTransform,
        transformSwapsAxes: transformSwapsAxes,
        logicalSize: logicalSize,
        effectiveRect: effectiveRect,
        rectsOverlap: rectsOverlap,
        entryFromSnapshot: entryFromSnapshot,
        layoutFromSnapshots: layoutFromSnapshots,
        modeIsAvailable: modeIsAvailable,
        validateLayout: validateLayout,
        normalizePositions: normalizePositions,
        choosePrimary: choosePrimary,
        withPrimary: withPrimary,
        nextRevertState: nextRevertState,
        toApplyList: toApplyList
    };
}
