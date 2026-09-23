// BrightnessPolicy — pure, Qt-free logic for per-power-source brightness
// (xfce4-power-manager "Display Power and Brightness" parity).
//
// On AC the display uses the "normal" brightness level; when the machine
// transitions to battery and automatic reduction is enabled, it drops to the
// configured reduced level; on return to AC it restores the normal level.
//
// BrightnessService works in a 0..1 fraction internally; the UI/settings store
// 0..100 percent. This module owns the clamping + percent<->fraction
// conversion + the transition decision so the QML stays thin and the maths is
// unit-tested. No I/O, no Qt, no shell — brightness values are plain numbers
// and are never interpolated into a command here.

"use strict";

// Clamp an integer brightness percentage to the inclusive 0..100 range.
// Non-numeric / NaN inputs clamp to 0 (safe floor).
function clampPercent(value) {
  var v = Number(value);
  if (!isFinite(v)) {
    return 0;
  }
  if (v < 0) {
    return 0;
  }
  if (v > 100) {
    return 100;
  }
  return Math.round(v);
}

// Convert a clamped 0..100 percentage to the 0..1 fraction BrightnessService
// expects.
function percentToFraction(value) {
  return clampPercent(value) / 100;
}

// Convert a 0..1 fraction back to a clamped 0..100 percent.
function fractionToPercent(fraction) {
  var f = Number(fraction);
  if (!isFinite(f)) {
    return 0;
  }
  return clampPercent(f * 100);
}

// Decide the target brightness PERCENT for a given power source.
//   onAC === true            -> normalPercent
//   onAC === false + enabled -> reducedPercent
//   onAC === false + !enabled -> normalPercent (no change vs AC level)
// All outputs are clamped 0..100.
function targetPercentForSource(onAC, enabled, normalPercent, reducedPercent) {
  if (onAC) {
    return clampPercent(normalPercent);
  }
  if (enabled) {
    return clampPercent(reducedPercent);
  }
  return clampPercent(normalPercent);
}

// Decide whether a power-source transition should drive a brightness apply,
// and to what fraction. Returns { apply: bool, fraction: number }.
//   - apply is false when the feature is disabled (we never fight the user's
//     manual slider on AC if reduction is off).
//   - On a transition to battery with reduction enabled, apply reduced.
//   - On a transition to AC with reduction enabled, restore normal.
function resolveTransition(onAC, enabled, normalPercent, reducedPercent) {
  if (!enabled) {
    return { apply: false, fraction: percentToFraction(normalPercent) };
  }
  var pct = targetPercentForSource(onAC, enabled, normalPercent, reducedPercent);
  return { apply: true, fraction: percentToFraction(pct) };
}

var BrightnessPolicy = {
  clampPercent: clampPercent,
  percentToFraction: percentToFraction,
  fractionToPercent: fractionToPercent,
  targetPercentForSource: targetPercentForSource,
  resolveTransition: resolveTransition
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = BrightnessPolicy;
}
