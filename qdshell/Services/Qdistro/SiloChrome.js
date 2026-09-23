// TEST MIRROR — NOT imported by production QML.
// This file is a hand-kept copy of the security-relevant logic in
// Services/Qdistro/Tier3Apps.qml and Services/Qdistro/Tier4Apps.qml.
// Neither QML file imports this module; they contain the logic directly.
// Tests (tests/test_silo_chrome.js) require this file under Node.
//
// DRIFT RISK: if the palette, hash, or prefix constants in Tier3Apps.qml or
// Tier4Apps.qml change, this file must be updated manually AND the journal
// contract tests (s38 / s107 bats) must be updated in lockstep.
// tests/test_drift_guard.js asserts palette byte-identity and prefix string
// identity between the QML sources and this mirror to catch silent drift.
//
// This file is a mirror of the pure silo-chrome logic extracted from
// Services/Qdistro/Tier3Apps.qml and Services/Qdistro/Tier4Apps.qml so the
// security-relevant identity/routing functions can be unit-tested under Node.
//
// Both tiers share the same deterministic palette + hash algorithm
// (colourForSilo). Tier4 adds _hexToRgba (packing "#rrggbb" → uint32
// RRGGBBAA for qdwin_toplevel_border_rgba). The silo-name derivation
// (siloFromSecctx) is tier-specific because the secctx app_id prefix differs.
//
// Security relevance:
//   * siloFromSecctx is the ONLY trusted source of a silo identity for
//     tier-3/tier-4 toplevels. The spec (spec/02) says the load-bearing
//     identity is the wp_security_context_v1 secctxAppId, NOT the window
//     title. Deriving silo from an incorrect prefix would assign windows
//     to the wrong silo context.
//   * _hexToRgba must handle bad/short input safely; the return value of 0
//     causes qdwin to use the neutral default border colour (safe fallback).
//   * colourForSilo must be DETERMINISTIC (same silo → same colour) so
//     bats tests can grep for expected colour lines in the qdshell journal.

"use strict";

// 10 visible hex colours that survive both light and dark themes.
// Mirrors the palette in Tier3Apps.qml and Tier4Apps.qml.
var SILO_PALETTE = [
  "#4caf50",  // green
  "#ffb300",  // amber/yellow
  "#2196f3",  // blue
  "#ab47bc",  // magenta/purple
  "#26c6da",  // cyan
  "#8bc34a",  // bright green
  "#ffe54c",  // bright yellow
  "#64b5f6",  // bright blue
  "#ce93d8",  // bright magenta
  "#80deea",  // bright cyan
];

// Deterministic char-sum hash → palette index. Mirrors Tier3Apps.colourForSilo.
// Same silo always maps to the same colour; the hash is stable across restarts.
function colourForSilo(silo) {
  if (!silo) return SILO_PALETTE[0];
  var h = 0;
  for (var i = 0; i < silo.length; i++) {
    h = (h * 31 + silo.charCodeAt(i)) >>> 0;
  }
  return SILO_PALETTE[h % SILO_PALETTE.length];
}

// Tier-3 secctx prefix: qdistro.tier3.<silo>
var TIER3_PREFIX = "qdistro.tier3.";

// Extracts the silo name from a tier-3 secctxAppId. Returns "" for
// non-tier-3 or empty app ids. Mirrors Tier3Apps.siloFromSecctx.
function siloFromSecctxTier3(secctxAppId) {
  if (!secctxAppId || secctxAppId.indexOf(TIER3_PREFIX) !== 0)
    return "";
  var tag = secctxAppId.slice(TIER3_PREFIX.length);
  if (!tag) return "";
  return tag;
}

// Returns true iff the secctx app_id identifies a tier-3 toplevel.
function isTier3(secctxAppId) {
  return !!secctxAppId && secctxAppId.indexOf(TIER3_PREFIX) === 0;
}

// Tier-4 secctx prefix: qdistro.tier4.<vm>
var TIER4_PREFIX = "qdistro.tier4.";

// Extracts the vm/silo name from a tier-4 secctxAppId. Returns "" for
// non-tier-4 app ids. Mirrors Tier4Apps.siloFromSecctx.
function siloFromSecctxTier4(secctxAppId) {
  if (!secctxAppId || secctxAppId.indexOf(TIER4_PREFIX) !== 0)
    return "";
  var tag = secctxAppId.slice(TIER4_PREFIX.length);
  if (!tag) return "";
  return tag;
}

// Returns true iff the secctx app_id identifies a tier-4 toplevel.
function isTier4(secctxAppId) {
  return !!secctxAppId && secctxAppId.indexOf(TIER4_PREFIX) === 0;
}

// Pack "#rrggbb" → 0xRRGGBBAA (alpha=0xFF) as an unsigned 32-bit int.
// Returns 0 on invalid/short input — qdwin treats 0 as "use neutral default".
// Mirrors Tier4Apps._hexToRgba. The `>>> 0` forces the signed 32-bit result
// of the bitwise-or to the unsigned range that matches C's uint32.
function hexToRgba(hex) {
  if (!hex || hex.length !== 7 || hex[0] !== "#") return 0;
  var r = parseInt(hex.slice(1, 3), 16);
  var g = parseInt(hex.slice(3, 5), 16);
  var b = parseInt(hex.slice(5, 7), 16);
  if (isNaN(r) || isNaN(g) || isNaN(b)) return 0;
  return (((r << 24) | (g << 16) | (b << 8) | 0xFF) >>> 0);
}

var api = {
  SILO_PALETTE: SILO_PALETTE,
  TIER3_PREFIX: TIER3_PREFIX,
  TIER4_PREFIX: TIER4_PREFIX,
  colourForSilo: colourForSilo,
  siloFromSecctxTier3: siloFromSecctxTier3,
  isTier3: isTier3,
  siloFromSecctxTier4: siloFromSecctxTier4,
  isTier4: isTier4,
  hexToRgba: hexToRgba,
};

if (typeof module !== "undefined") {
  module.exports = api;
}
