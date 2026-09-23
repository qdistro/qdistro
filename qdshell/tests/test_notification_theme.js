const assert = require("assert");
const NT = require("../Modules/Notification/NotificationTheme.js");

// ── theme key validation ──
(function testSanitizeTheme() {
  NT.VALID_THEMES.forEach(function (k) {
    assert.strictEqual(NT.sanitizeTheme(k), k);
  });
  // unknown / empty / missing -> default.
  assert.strictEqual(NT.sanitizeTheme("fancy"), NT.THEME_DEFAULT);
  assert.strictEqual(NT.sanitizeTheme(""), NT.THEME_DEFAULT);
  assert.strictEqual(NT.sanitizeTheme(undefined), NT.THEME_DEFAULT);
  assert.strictEqual(NT.sanitizeTheme(null), NT.THEME_DEFAULT);
})();

// ── every built-in theme resolves to a complete, valid parameter set ──
(function testResolveCompleteness() {
  NT.VALID_THEMES.forEach(function (k) {
    const r = NT.resolveTheme(k);
    assert.strictEqual(r.key, k);
    assert.ok(NT.CORNER_RADII.indexOf(r.cornerRadius) !== -1, k + " cornerRadius");
    assert.ok(NT.BORDER_WIDTHS.indexOf(r.borderWidth) !== -1, k + " borderWidth");
    assert.ok(NT.PADDINGS.indexOf(r.padding) !== -1, k + " padding");
    assert.ok(NT.BACKGROUNDS.indexOf(r.background) !== -1, k + " background");
    assert.ok(NT.ICON_PLACEMENTS.indexOf(r.iconPlacement) !== -1, k + " iconPlacement");
    assert.ok(NT.ICON_SIZES.indexOf(r.iconSize) !== -1, k + " iconSize");
    assert.ok(NT.ACCENT_SOURCES.indexOf(r.accentSource) !== -1, k + " accentSource");
    assert.strictEqual(typeof r.accentBar, "boolean", k + " accentBar type");
    assert.strictEqual(typeof r.accentBarWidth, "number", k + " accentBarWidth type");
    // accentBarWidth is always within bounds, and 0 when the bar is off.
    assert.ok(r.accentBarWidth >= NT.ACCENT_BAR_MIN && r.accentBarWidth <= NT.ACCENT_BAR_MAX);
    if (!r.accentBar)
      assert.strictEqual(r.accentBarWidth, 0, k + " accentBarWidth off");
  });
})();

// ── unknown / empty theme falls back to the default parameter set ──
(function testFallback() {
  const def = NT.resolveTheme(NT.THEME_DEFAULT);
  assert.deepStrictEqual(NT.resolveTheme("nope"), def);
  assert.deepStrictEqual(NT.resolveTheme(""), def);
  assert.deepStrictEqual(NT.resolveTheme(undefined), def);
})();

// ── distinctness: themes actually differ from each other ──
(function testDistinct() {
  const a = NT.resolveTheme(NT.THEME_ACCENT_BAR);
  assert.strictEqual(a.accentBar, true);
  assert.ok(a.accentBarWidth > 0);
  const m = NT.resolveTheme(NT.THEME_MINIMAL);
  assert.strictEqual(m.iconPlacement, "hidden");
  assert.strictEqual(m.cornerRadius, "none");
  assert.strictEqual(NT.showIcon(NT.THEME_MINIMAL), false);
  assert.strictEqual(NT.showIcon(NT.THEME_DEFAULT), true);
})();

// ── accent-bar width clamping ──
(function testClampAccentBarWidth() {
  assert.strictEqual(NT.clampAccentBarWidth(4), 4);
  // below floor / negative.
  assert.strictEqual(NT.clampAccentBarWidth(-3), NT.ACCENT_BAR_MIN);
  // above ceil.
  assert.strictEqual(NT.clampAccentBarWidth(9999), NT.ACCENT_BAR_MAX);
  // non-numeric -> floor.
  assert.strictEqual(NT.clampAccentBarWidth("abc"), NT.ACCENT_BAR_MIN);
  assert.strictEqual(NT.clampAccentBarWidth(undefined), NT.ACCENT_BAR_MIN);
  // rounding.
  assert.strictEqual(NT.clampAccentBarWidth(4.7), 5);
})();

// ── immutability: resolveTheme returns a fresh object; mutating it must not
// affect subsequent resolutions of the same theme ──
(function testImmutable() {
  const first = NT.resolveTheme(NT.THEME_DEFAULT);
  first.cornerRadius = "tampered";
  first.accentBar = true;
  const second = NT.resolveTheme(NT.THEME_DEFAULT);
  assert.notStrictEqual(second.cornerRadius, "tampered");
  assert.strictEqual(second.accentBar, false);
})();

// ── injection safety: the theme layer is pure visual parameters and must never
// build a shell/command string. Assert every resolved field is a primitive and
// no string field smells like a shell command. ──
(function testNoShellString() {
  NT.VALID_THEMES.concat(["", "nope"]).forEach(function (k) {
    const r = NT.resolveTheme(k);
    Object.keys(r).forEach(function (field) {
      const v = r[field];
      const ty = typeof v;
      assert.ok(ty === "string" || ty === "number" || ty === "boolean",
                "field " + field + " must be primitive, got " + ty);
      if (ty === "string") {
        // No shell metacharacters / command separators should ever appear in a
        // semantic visual token.
        assert.ok(!/[;&|`$<>(){}\\'"\n]/.test(v),
                  "field " + field + " contains shell metachars: " + v);
      }
    });
  });
})();

console.log("notification-theme: all assertions passed");
