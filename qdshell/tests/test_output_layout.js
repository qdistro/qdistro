const assert = require("assert");
const OL = require("../Services/Qdwin/OutputLayout.js");

// ─── logicalSize + transforms ───────────────────────────────────────
assert.deepStrictEqual(OL.logicalSize(1920, 1080, 1, OL.TRANSFORM_NORMAL),
    { w: 1920, h: 1080 }, "scale 1, no rotate");
assert.deepStrictEqual(OL.logicalSize(3840, 2160, 2, OL.TRANSFORM_NORMAL),
    { w: 1920, h: 1080 }, "scale 2 halves logical size");
assert.deepStrictEqual(OL.logicalSize(1920, 1080, 1, OL.TRANSFORM_90),
    { w: 1080, h: 1920 }, "90 rotation swaps axes");
assert.deepStrictEqual(OL.logicalSize(1920, 1080, 1, OL.TRANSFORM_270),
    { w: 1080, h: 1920 }, "270 rotation swaps axes");
assert.deepStrictEqual(OL.logicalSize(1920, 1080, 1, OL.TRANSFORM_180),
    { w: 1920, h: 1080 }, "180 keeps axes");
assert.strictEqual(OL.transformSwapsAxes(OL.TRANSFORM_FLIPPED_90), true);
assert.strictEqual(OL.transformSwapsAxes(OL.TRANSFORM_FLIPPED), false);

assert.strictEqual(OL.isValidTransform(0), true);
assert.strictEqual(OL.isValidTransform(7), true);
assert.strictEqual(OL.isValidTransform(8), false, "out of enum");
assert.strictEqual(OL.isValidTransform(-1), false);
assert.strictEqual(OL.isValidTransform(1.5), false, "non-int");

// ─── rectsOverlap (edge-touch allowed) ──────────────────────────────
assert.strictEqual(OL.rectsOverlap(
    { x: 0, y: 0, w: 100, h: 100 }, { x: 100, y: 0, w: 100, h: 100 }),
    false, "edge-touching does not overlap");
assert.strictEqual(OL.rectsOverlap(
    { x: 0, y: 0, w: 100, h: 100 }, { x: 50, y: 0, w: 100, h: 100 }),
    true, "x-overlap");
assert.strictEqual(OL.rectsOverlap(
    { x: 0, y: 0, w: 100, h: 100 }, { x: 200, y: 0, w: 100, h: 100 }),
    false, "disjoint");

// ─── entryFromSnapshot picks the right mode ─────────────────────────
var snap = {
    name: "DP-1", enabled: true, x: 0, y: 0, scale: 1, transform: 0,
    currentMode: 1,
    modes: [
        { width: 1280, height: 720, refresh: 60000, preferred: false },
        { width: 1920, height: 1080, refresh: 60000, preferred: true }
    ]
};
var e = OL.entryFromSnapshot(snap);
assert.strictEqual(e.width, 1920, "current mode chosen");
assert.strictEqual(e.height, 1080);
assert.strictEqual(e.name, "DP-1");

var snap2 = { name: "DP-2", enabled: true, currentMode: -1,
    modes: [{ width: 800, height: 600, refresh: 0, preferred: false },
            { width: 1024, height: 768, refresh: 0, preferred: true }] };
assert.strictEqual(OL.entryFromSnapshot(snap2).width, 1024,
    "no current → preferred mode");

var snap3 = { name: "DP-3", enabled: false, currentMode: -1,
    modes: [{ width: 640, height: 480, refresh: 0, preferred: false }] };
assert.strictEqual(OL.entryFromSnapshot(snap3).width, 640,
    "no current/preferred → first mode");

// ─── validateLayout ─────────────────────────────────────────────────
// Happy: two side-by-side enabled outputs, no overlap.
var good = [
    { name: "A", enabled: true, x: 0, y: 0, width: 1920, height: 1080,
      refresh: 60000, scale: 1, transform: 0, primary: true },
    { name: "B", enabled: true, x: 1920, y: 0, width: 1920, height: 1080,
      refresh: 60000, scale: 1, transform: 0, primary: false }
];
assert.strictEqual(OL.validateLayout(good, {}).ok, true, "side-by-side ok");

// All disabled → error.
var allOff = good.map(function (g) {
    var c = {}; for (var k in g) c[k] = g[k]; c.enabled = false; return c;
});
var rOff = OL.validateLayout(allOff, {});
assert.strictEqual(rOff.ok, false);
assert.ok(rOff.errors.indexOf("at-least-one-enabled") >= 0);

// Overlap → error naming both.
var overlap = [
    { name: "A", enabled: true, x: 0, y: 0, width: 1920, height: 1080,
      refresh: 0, scale: 1, transform: 0, primary: true },
    { name: "B", enabled: true, x: 100, y: 0, width: 1920, height: 1080,
      refresh: 0, scale: 1, transform: 0, primary: false }
];
var rOv = OL.validateLayout(overlap, {});
assert.strictEqual(rOv.ok, false);
assert.ok(rOv.errors.some(function (x) { return x.indexOf("overlap:A:B") === 0; }),
    "overlap error names both");

// Two primaries → error.
var twoPrim = good.map(function (g) {
    var c = {}; for (var k in g) c[k] = g[k]; c.primary = true; return c;
});
assert.ok(OL.validateLayout(twoPrim, {}).errors.indexOf("multiple-primary") >= 0);

// Invalid transform / scale / mode.
var bad = [{ name: "A", enabled: true, x: 0, y: 0, width: 0, height: 0,
    refresh: 0, scale: 0, transform: 99, primary: true }];
var rBad = OL.validateLayout(bad, {});
assert.strictEqual(rBad.ok, false);
assert.ok(rBad.errors.indexOf("invalid-transform:A") >= 0);
assert.ok(rBad.errors.indexOf("invalid-scale:A") >= 0);
assert.ok(rBad.errors.indexOf("invalid-mode:A") >= 0);

// Mode-availability check against advertised modes.
var avail = { A: [{ width: 1920, height: 1080, refresh: 60000 }] };
var wantUnavail = [{ name: "A", enabled: true, x: 0, y: 0,
    width: 1234, height: 567, refresh: 60000, scale: 1, transform: 0,
    primary: true }];
assert.ok(OL.validateLayout(wantUnavail, avail).errors.indexOf(
    "mode-unavailable:A") >= 0, "unadvertised mode rejected");
var wantAvail = [{ name: "A", enabled: true, x: 0, y: 0,
    width: 1920, height: 1080, refresh: 60000, scale: 1, transform: 0,
    primary: true }];
assert.strictEqual(OL.validateLayout(wantAvail, avail).ok, true);
assert.strictEqual(OL.modeIsAvailable(avail.A,
    { width: 1920, height: 1080, refresh: 0 }), true, "refresh 0 = any");

// ─── normalizePositions ─────────────────────────────────────────────
var shifted = [
    { name: "A", enabled: true, x: -500, y: 200, width: 1920, height: 1080,
      scale: 1, transform: 0 },
    { name: "B", enabled: true, x: 1420, y: 200, width: 1920, height: 1080,
      scale: 1, transform: 0 }
];
var norm = OL.normalizePositions(shifted);
assert.strictEqual(norm[0].x, 0, "leftmost anchored to 0");
assert.strictEqual(norm[0].y, 0, "topmost anchored to 0");
assert.strictEqual(norm[1].x, 1920, "relative spacing preserved");
assert.strictEqual(norm[1].y, 0);
// original not mutated
assert.strictEqual(shifted[0].x, -500, "input not mutated");

// disabled outputs keep their position untouched.
var withDisabled = [
    { name: "A", enabled: true, x: 100, y: 100, width: 800, height: 600,
      scale: 1, transform: 0 },
    { name: "B", enabled: false, x: 9999, y: 9999, width: 800, height: 600,
      scale: 1, transform: 0 }
];
var nd = OL.normalizePositions(withDisabled);
assert.strictEqual(nd[0].x, 0);
assert.strictEqual(nd[1].x, 9999, "disabled position untouched");

// ─── choosePrimary / withPrimary ────────────────────────────────────
assert.strictEqual(OL.choosePrimary([]), "", "none enabled → empty");
assert.strictEqual(OL.choosePrimary([
    { name: "A", enabled: false }, { name: "B", enabled: true, x: 0, y: 0 }
]), "B", "only enabled wins");
assert.strictEqual(OL.choosePrimary([
    { name: "A", enabled: true, x: 1920, y: 0, primary: true },
    { name: "B", enabled: true, x: 0, y: 0 }
]), "A", "explicit primary flag honoured over (0,0)");
assert.strictEqual(OL.choosePrimary([
    { name: "A", enabled: true, x: 1920, y: 0 },
    { name: "B", enabled: true, x: 0, y: 0 }
]), "B", "no explicit → (0,0) output");
assert.strictEqual(OL.choosePrimary([
    { name: "Zeta", enabled: true, x: 100, y: 0 },
    { name: "Alpha", enabled: true, x: 200, y: 0 }
]), "Alpha", "no flag, none at origin → stable lowest name");
assert.strictEqual(OL.choosePrimary([
    { name: "A", enabled: true, x: 0, y: 0 },
    { name: "B", enabled: true, x: 1920, y: 0 }
], "B"), "B", "persisted primary preference honoured");
assert.strictEqual(OL.choosePrimary([
    { name: "A", enabled: true, x: 0, y: 0 },
    { name: "B", enabled: false, x: 1920, y: 0 }
], "B"), "A", "disabled persisted primary ignored");

var wp = OL.withPrimary([
    { name: "A", enabled: true, x: 0, y: 0 },
    { name: "B", enabled: true, x: 1920, y: 0 }
]);
assert.strictEqual(wp[0].primary, true, "A at origin is primary");
assert.strictEqual(wp[1].primary, false);
var wpPersisted = OL.withPrimary([
    { name: "A", enabled: true, x: 0, y: 0 },
    { name: "B", enabled: true, x: 1920, y: 0 }
], "B");
assert.strictEqual(wpPersisted[0].primary, false);
assert.strictEqual(wpPersisted[1].primary, true,
    "withPrimary applies persisted primary preference");

// ─── nextRevertState (confirm-or-revert state machine) ──────────────
assert.deepStrictEqual(OL.nextRevertState("idle", "apply"),
    { phase: "applying", revert: false });
assert.deepStrictEqual(OL.nextRevertState("applying", "apply-ok"),
    { phase: "confirming", revert: false });
assert.deepStrictEqual(OL.nextRevertState("applying", "apply-failed"),
    { phase: "idle", revert: false }, "compositor already reverted on failure");
assert.deepStrictEqual(OL.nextRevertState("confirming", "confirm"),
    { phase: "idle", revert: false });
assert.deepStrictEqual(OL.nextRevertState("confirming", "timeout"),
    { phase: "reverting", revert: true }, "timeout triggers revert");
assert.deepStrictEqual(OL.nextRevertState("confirming", "cancel"),
    { phase: "reverting", revert: true }, "explicit cancel triggers revert");
assert.deepStrictEqual(OL.nextRevertState("reverting", "apply-ok"),
    { phase: "idle", revert: false }, "after revert returns to idle");

// ─── toApplyList strips UI-only fields, drops primary ───────────────
var applyList = OL.toApplyList([
    { name: "A", enabled: true, x: 0, y: 0, width: 1920, height: 1080,
      refresh: 60000, scale: 2, transform: 1, primary: true,
      description: "Foo Monitor" },
    { name: "B", enabled: false, x: 9, y: 9, width: 800, height: 600,
      primary: false }
]);
assert.strictEqual(applyList.length, 2);
assert.strictEqual(applyList[0].name, "A");
assert.strictEqual(applyList[0].enabled, true);
assert.strictEqual(applyList[0].width, 1920);
assert.strictEqual(applyList[0].scale, 2);
assert.strictEqual(applyList[0].transform, 1);
assert.strictEqual(applyList[0].primary, undefined, "primary not sent to compositor");
assert.strictEqual(applyList[0].description, undefined, "description not sent");
assert.strictEqual(applyList[1].enabled, false);
assert.strictEqual(applyList[1].width, undefined, "disabled head sends no geometry");

console.log("test_output_layout: ok");
