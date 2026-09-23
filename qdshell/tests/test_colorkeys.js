const assert = require("assert");
const CK = require("../Services/Commons/ColorKeys.js");

// Tests for the pure color-key resolution logic extracted from Commons/Color.qml.
// These cover the routing tables that map a string key ("primary", "secondary",
// "tertiary", "error", or unknown) to a role name. The live QML path binds these
// role names to actual Material Design 3 color properties on the Color singleton;
// what is tested here is the ROUTING — that each key resolves to the correct role
// and that unknown keys fail safely to a default rather than resolving to nothing.

// ── resolveColorKey: known keys ──
(function testResolveColorKeyKnown() {
  assert.strictEqual(CK.resolveColorKey("primary"),   "primary",   "primary → primary");
  assert.strictEqual(CK.resolveColorKey("secondary"), "secondary", "secondary → secondary");
  assert.strictEqual(CK.resolveColorKey("tertiary"),  "tertiary",  "tertiary → tertiary");
  assert.strictEqual(CK.resolveColorKey("error"),     "error",     "error → error");
})();

// ── resolveColorKey: unknown/empty/null falls back to "onSurface" ──
(function testResolveColorKeyUnknown() {
  // ensures: an unknown key never produces undefined or crashes; it resolves to the
  // default text color (onSurface) so widgets degrade gracefully.
  assert.strictEqual(CK.resolveColorKey("none"),       "onSurface", "none → onSurface default");
  assert.strictEqual(CK.resolveColorKey(""),           "onSurface", "empty → onSurface");
  assert.strictEqual(CK.resolveColorKey("garbage"),    "onSurface", "unknown → onSurface");
  assert.strictEqual(CK.resolveColorKey(undefined),    "onSurface", "undefined → onSurface");
  assert.strictEqual(CK.resolveColorKey(null),         "onSurface", "null → onSurface");
  assert.strictEqual(CK.resolveColorKey("PRIMARY"),    "onSurface", "wrong case → onSurface (case-sensitive)");
})();

// ── resolveOnColorKey: known keys return contrast roles ──
(function testResolveOnColorKeyKnown() {
  assert.strictEqual(CK.resolveOnColorKey("primary"),   "onPrimary",   "primary → onPrimary");
  assert.strictEqual(CK.resolveOnColorKey("secondary"), "onSecondary", "secondary → onSecondary");
  assert.strictEqual(CK.resolveOnColorKey("tertiary"),  "onTertiary",  "tertiary → onTertiary");
  assert.strictEqual(CK.resolveOnColorKey("error"),     "onError",     "error → onError");
})();

// ── resolveOnColorKey: unknown falls back to "surface" ──
(function testResolveOnColorKeyUnknown() {
  // ensures: the contrast/on-color for an unknown key is "surface" (the background),
  // keeping text readable rather than producing undefined.
  assert.strictEqual(CK.resolveOnColorKey("none"),    "surface", "none → surface");
  assert.strictEqual(CK.resolveOnColorKey(""),        "surface", "empty → surface");
  assert.strictEqual(CK.resolveOnColorKey("foo"),     "surface", "unknown → surface");
  assert.strictEqual(CK.resolveOnColorKey(undefined), "surface", "undefined → surface");
  assert.strictEqual(CK.resolveOnColorKey(null),      "surface", "null → surface");
})();

// ── resolveColorKeyOptional: known keys work, unknown → "transparent" ──
(function testResolveColorKeyOptional() {
  assert.strictEqual(CK.resolveColorKeyOptional("primary"),   "primary");
  assert.strictEqual(CK.resolveColorKeyOptional("secondary"), "secondary");
  assert.strictEqual(CK.resolveColorKeyOptional("tertiary"),  "tertiary");
  assert.strictEqual(CK.resolveColorKeyOptional("error"),     "error");

  // ensures: "none" and unknown keys → "transparent", not a color name,
  // so components that use optional accent coloring skip the fill safely.
  assert.strictEqual(CK.resolveColorKeyOptional("none"),      "transparent", "none → transparent");
  assert.strictEqual(CK.resolveColorKeyOptional(""),          "transparent", "empty → transparent");
  assert.strictEqual(CK.resolveColorKeyOptional("garbage"),   "transparent", "unknown → transparent");
  assert.strictEqual(CK.resolveColorKeyOptional(undefined),   "transparent", "undefined → transparent");
  assert.strictEqual(CK.resolveColorKeyOptional(null),        "transparent", "null → transparent");
})();

// ── resolveColorKey / resolveOnColorKey are a matched pair for known keys ──
(function testColorPairConsistency() {
  // For every valid key, resolveColorKey gives the foreground role and
  // resolveOnColorKey gives the corresponding contrast role. Verify the naming
  // convention is consistent (on<Key> pattern).
  CK.VALID_COLOR_KEYS.forEach(function(key) {
    var fg = CK.resolveColorKey(key);
    var bg = CK.resolveOnColorKey(key);
    // fg should equal key; bg should be "on" + capitalize(key)
    assert.strictEqual(fg, key, "fg role for " + key + " matches the key itself");
    var expectedOn = "on" + key.charAt(0).toUpperCase() + key.slice(1);
    assert.strictEqual(bg, expectedOn, "bg role for " + key + " is " + expectedOn);
  });
})();

// ── VALID_COLOR_KEYS contains the expected set ──
(function testValidColorKeys() {
  assert.ok(Array.isArray(CK.VALID_COLOR_KEYS), "VALID_COLOR_KEYS is an array");
  ["primary", "secondary", "tertiary", "error"].forEach(function(k) {
    assert.ok(CK.VALID_COLOR_KEYS.indexOf(k) !== -1, k + " in VALID_COLOR_KEYS");
  });
  // "none" is NOT in the valid set (it maps to transparent in Optional)
  assert.ok(CK.VALID_COLOR_KEYS.indexOf("none") === -1, "none not in VALID_COLOR_KEYS");
})();

console.log("colorkeys: all assertions passed");
