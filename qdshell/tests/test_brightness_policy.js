const assert = require("assert");
const B = require("../Services/Power/BrightnessPolicy.js");

// ─── clamp 0..100 ───────────────────────────────────────────────────
assert.strictEqual(B.clampPercent(50), 50);
assert.strictEqual(B.clampPercent(0), 0);
assert.strictEqual(B.clampPercent(100), 100);
assert.strictEqual(B.clampPercent(-10), 0);    // below range
assert.strictEqual(B.clampPercent(150), 100);  // above range
assert.strictEqual(B.clampPercent(33.6), 34);  // rounds
assert.strictEqual(B.clampPercent(NaN), 0);    // NaN -> safe floor
assert.strictEqual(B.clampPercent("80"), 80);  // numeric string
assert.strictEqual(B.clampPercent(undefined), 0);

// ─── percent <-> fraction ───────────────────────────────────────────
assert.strictEqual(B.percentToFraction(50), 0.5);
assert.strictEqual(B.percentToFraction(0), 0);
assert.strictEqual(B.percentToFraction(100), 1);
assert.strictEqual(B.percentToFraction(200), 1);   // clamped first
assert.strictEqual(B.fractionToPercent(0.5), 50);
assert.strictEqual(B.fractionToPercent(1), 100);
assert.strictEqual(B.fractionToPercent(1.5), 100); // clamped
assert.strictEqual(B.fractionToPercent(-0.2), 0);
assert.strictEqual(B.fractionToPercent(NaN), 0);

// ─── target for power source ────────────────────────────────────────
// On AC always the normal level.
assert.strictEqual(B.targetPercentForSource(true, true, 80, 30), 80);
assert.strictEqual(B.targetPercentForSource(true, false, 80, 30), 80);
// On battery + enabled -> reduced.
assert.strictEqual(B.targetPercentForSource(false, true, 80, 30), 30);
// On battery + disabled -> normal (no reduction).
assert.strictEqual(B.targetPercentForSource(false, false, 80, 30), 80);
// out-of-range levels are clamped
assert.strictEqual(B.targetPercentForSource(false, true, 80, 999), 100);
assert.strictEqual(B.targetPercentForSource(false, true, 80, -5), 0);

// ─── transition resolution ──────────────────────────────────────────
// Disabled feature never applies.
let t = B.resolveTransition(false, false, 80, 30);
assert.strictEqual(t.apply, false);
// Transition to battery (enabled) applies reduced fraction.
t = B.resolveTransition(false, true, 80, 30);
assert.strictEqual(t.apply, true);
assert.strictEqual(t.fraction, 0.3);
// Transition to AC (enabled) restores normal fraction.
t = B.resolveTransition(true, true, 80, 30);
assert.strictEqual(t.apply, true);
assert.strictEqual(t.fraction, 0.8);
// Edge: reduced level clamps into a valid 0..1 fraction even if mis-set.
t = B.resolveTransition(false, true, 80, 250);
assert.strictEqual(t.fraction, 1);

// ════════════════════════════════════════════════════════════════════
// EXPANDED COVERAGE
// ════════════════════════════════════════════════════════════════════

// ─── clampPercent extra boundary / type cases ───────────────────────
assert.strictEqual(B.clampPercent(0.4), 0, "rounds down to 0");
assert.strictEqual(B.clampPercent(0.5), 1, "rounds up to 1 at .5");
assert.strictEqual(B.clampPercent(99.5), 100, "rounds up to 100");
assert.strictEqual(B.clampPercent(100.4), 100, "just over 100 clamped, not rounded past");
assert.strictEqual(B.clampPercent(Infinity), 0, "Infinity is not finite -> 0");
assert.strictEqual(B.clampPercent(-Infinity), 0);
assert.strictEqual(B.clampPercent("not a number"), 0, "non-numeric string -> 0");
assert.strictEqual(B.clampPercent("50.6"), 51, "numeric string parsed + rounded");
assert.strictEqual(B.clampPercent(null), 0, "Number(null) is 0");
assert.strictEqual(B.clampPercent(""), 0, "Number('') is 0");
assert.strictEqual(B.clampPercent("  75  "), 75, "whitespace-padded numeric string");

// ─── percent <-> fraction round-trips ───────────────────────────────
// percent -> fraction -> percent is the identity for any clamped integer %.
[0, 1, 25, 33, 50, 67, 99, 100].forEach(function (p) {
  const back = B.fractionToPercent(B.percentToFraction(p));
  assert.strictEqual(back, p, "round-trip identity for " + p + "%");
});
// Out-of-range inputs are clamped on the way in, so the round-trip lands at a bound.
assert.strictEqual(B.fractionToPercent(B.percentToFraction(150)), 100, "over-range round-trips to 100");
assert.strictEqual(B.fractionToPercent(B.percentToFraction(-20)), 0, "under-range round-trips to 0");
// percentToFraction always yields a value within [0,1].
[-50, 0, 50, 100, 200, NaN].forEach(function (p) {
  const f = B.percentToFraction(p);
  assert.ok(f >= 0 && f <= 1, "fraction in [0,1] for input " + p);
});
// fractionToPercent rounds to the nearest integer percent.
assert.strictEqual(B.fractionToPercent(0.005), 1, "0.5% rounds up to 1");
assert.strictEqual(B.fractionToPercent(0.004), 0, "0.4% rounds down to 0");
assert.strictEqual(B.fractionToPercent(0.333), 33);
assert.strictEqual(B.fractionToPercent("0.5"), 50, "numeric string fraction");
assert.strictEqual(B.fractionToPercent(Infinity), 0, "Infinity fraction -> 0");

// ─── targetPercentForSource: clamp of normal level too ──────────────
assert.strictEqual(B.targetPercentForSource(true, true, 999, 30), 100, "over-range normal clamped on AC");
assert.strictEqual(B.targetPercentForSource(true, true, -5, 30), 0, "under-range normal clamped on AC");
// On battery + disabled, the NORMAL level is used (and clamped), not reduced.
assert.strictEqual(B.targetPercentForSource(false, false, 150, 10), 100,
  "battery+disabled uses normal (clamped), ignores reduced");

// ─── resolveTransition: no-op at target, toggle-off, source flap ────
// (1) Disabled feature: apply is false and we never touch the slider, but the
//     reported fraction is the (clamped) NORMAL level as a hint, not reduced.
let d = B.resolveTransition(false, false, 70, 20);
assert.strictEqual(d.apply, false, "disabled -> never applies");
assert.strictEqual(d.fraction, 0.7, "disabled hint fraction is NORMAL, never reduced");
d = B.resolveTransition(true, false, 70, 20);
assert.strictEqual(d.apply, false);
assert.strictEqual(d.fraction, 0.7);

// (2) Toggling auto-reduce OFF while on battery restores the NORMAL (AC) level.
//     Before: enabled on battery -> reduced.
let before = B.resolveTransition(false, true, 90, 25);
assert.strictEqual(before.apply, true);
assert.strictEqual(before.fraction, 0.25, "enabled on battery -> reduced");
//     After flipping the toggle off (still on battery): apply is false, and the
//     hint is back to the normal level (effectively "restore to AC brightness").
let after = B.resolveTransition(false, false, 90, 25);
assert.strictEqual(after.apply, false, "toggling reduction off does not fight the user");
assert.strictEqual(after.fraction, 0.9, "restore hint is the normal/AC level");

// (3) Source flap battery->AC->battery with reduction enabled yields the
//     expected target each time (idempotent per source, not path-dependent).
const onBattery = B.resolveTransition(false, true, 80, 30);
const onAC = B.resolveTransition(true, true, 80, 30);
const onBatteryAgain = B.resolveTransition(false, true, 80, 30);
assert.strictEqual(onBattery.fraction, 0.3);
assert.strictEqual(onAC.fraction, 0.8);
assert.deepStrictEqual(onBatteryAgain, onBattery, "same source -> same decision (stateless)");

// (4) "Already at target" is representable: when reduced === normal, both
//     sources resolve to the same fraction (a no-op apply at the controller).
const eq = B.resolveTransition(false, true, 60, 60);
const eqAC = B.resolveTransition(true, true, 60, 60);
assert.strictEqual(eq.fraction, 0.6);
assert.strictEqual(eqAC.fraction, 0.6);
assert.strictEqual(eq.fraction, eqAC.fraction, "reduced==normal -> no actual change between sources");

// (5) resolveTransition output shape is always { apply:bool, fraction:number }
//     with a numeric fraction in [0,1] even for garbage levels.
[B.resolveTransition(false, true, "x", "y"),
 B.resolveTransition(true, true, NaN, NaN),
 B.resolveTransition(false, false, undefined, undefined)].forEach(function (r) {
  assert.strictEqual(typeof r.apply, "boolean");
  assert.strictEqual(typeof r.fraction, "number");
  assert.ok(r.fraction >= 0 && r.fraction <= 1, "fraction stays in [0,1]");
});

console.log("brightness-policy: all assertions passed");
