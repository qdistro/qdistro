const assert = require("assert");
const NL = require("../Modules/Notification/NotificationLayout.js");

// ── detailMode validation ──
(function testSanitizeDetailMode() {
  assert.strictEqual(NL.sanitizeDetailMode("compact"), "compact");
  assert.strictEqual(NL.sanitizeDetailMode("normal"), "normal");
  assert.strictEqual(NL.sanitizeDetailMode("detailed"), "detailed");
  // unknown / missing -> normal
  assert.strictEqual(NL.sanitizeDetailMode("verbose"), "normal");
  assert.strictEqual(NL.sanitizeDetailMode(""), "normal");
  assert.strictEqual(NL.sanitizeDetailMode(undefined), "normal");
})();

// ── per-mode element visibility ──
(function testElementVisibility() {
  // compact: title only.
  assert.strictEqual(NL.showBody("compact"), false);
  assert.strictEqual(NL.showActions("compact"), false);
  assert.strictEqual(NL.showTimestamp("compact"), false);
  // normal: title + body.
  assert.strictEqual(NL.showBody("normal"), true);
  assert.strictEqual(NL.showActions("normal"), false);
  assert.strictEqual(NL.showTimestamp("normal"), false);
  // detailed: title + body + actions + timestamp.
  assert.strictEqual(NL.showBody("detailed"), true);
  assert.strictEqual(NL.showActions("detailed"), true);
  assert.strictEqual(NL.showTimestamp("detailed"), true);
})();

// ── minWidth clamp ──
(function testClampMinWidth() {
  assert.strictEqual(NL.clampMinWidth(440), 440);
  // below floor.
  assert.strictEqual(NL.clampMinWidth(10), NL.MIN_WIDTH_FLOOR);
  assert.strictEqual(NL.clampMinWidth(-5), NL.MIN_WIDTH_FLOOR);
  // above ceil.
  assert.strictEqual(NL.clampMinWidth(99999), NL.MIN_WIDTH_CEIL);
  // non-numeric -> floor.
  assert.strictEqual(NL.clampMinWidth("abc"), NL.MIN_WIDTH_FLOOR);
  assert.strictEqual(NL.clampMinWidth(undefined), NL.MIN_WIDTH_FLOOR);
  // rounding.
  assert.strictEqual(NL.clampMinWidth(440.7), 441);
})();

// ── effectiveWidth: max(base, clamped min) * scale ──
(function testEffectiveWidth() {
  // base wins when larger than the configured minimum.
  assert.strictEqual(NL.effectiveWidth(440, 320, 1), 440);
  // configured minimum wins when larger than the density base.
  assert.strictEqual(NL.effectiveWidth(320, 500, 1), 500);
  // scale applied.
  assert.strictEqual(NL.effectiveWidth(440, 320, 2), 880);
  // a sub-floor configured min is clamped up before comparison.
  assert.strictEqual(NL.effectiveWidth(220, 50, 1), Math.max(220, NL.MIN_WIDTH_FLOOR));
  // invalid scale falls back to 1.
  assert.strictEqual(NL.effectiveWidth(440, 320, 0), 440);
  assert.strictEqual(NL.effectiveWidth(440, 320, "x"), 440);
})();

console.log("notification-detail: all assertions passed");
