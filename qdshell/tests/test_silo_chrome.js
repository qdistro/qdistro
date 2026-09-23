const assert = require("assert");
const SC = require("../Services/Qdistro/SiloChrome.js");

// Tests for silo-chrome identity/color logic extracted from
// Services/Qdistro/Tier3Apps.qml and Tier4Apps.qml.
//
// These functions gate how the shell derives silo identity from the
// wp_security_context_v1 secctx app_id, which is the load-bearing identity
// per spec/02 row 3. Getting this wrong means windows could be assigned to
// the wrong silo context, or coloured inconsistently across restarts.

// ── SILO_PALETTE: correct length and format ──
(function testPalette() {
  assert.strictEqual(SC.SILO_PALETTE.length, 10, "palette has 10 entries");
  SC.SILO_PALETTE.forEach(function(c, i) {
    assert.ok(typeof c === 'string', "entry " + i + " is a string");
    assert.ok(/^#[0-9a-f]{6}$/.test(c), "entry " + i + " is a valid #rrggbb hex: " + c);
  });
})();

// ── colourForSilo: determinism ──
(function testColourDeterminism() {
  // ensures: same silo always maps to same colour (journal grepping relies on this)
  var silos = ["user1", "user2", "user3", "vm-dev", "vm-work"];
  silos.forEach(function(s) {
    var c1 = SC.colourForSilo(s);
    var c2 = SC.colourForSilo(s);
    assert.strictEqual(c1, c2, "colourForSilo('" + s + "') is deterministic");
  });
})();

// ── colourForSilo: returns palette member ──
(function testColourInPalette() {
  var silos = ["user1", "user2", "vm-dev", "longsiloname", "x"];
  silos.forEach(function(s) {
    var c = SC.colourForSilo(s);
    assert.ok(SC.SILO_PALETTE.indexOf(c) !== -1,
      "colourForSilo('" + s + "') = " + c + " is in palette");
  });
})();

// ── colourForSilo: empty/null → palette[0] safe fallback ──
(function testColourEmpty() {
  assert.strictEqual(SC.colourForSilo(""),   SC.SILO_PALETTE[0], "empty string → palette[0]");
  assert.strictEqual(SC.colourForSilo(null), SC.SILO_PALETTE[0], "null → palette[0]");
  assert.strictEqual(SC.colourForSilo(undefined), SC.SILO_PALETTE[0], "undefined → palette[0]");
})();

// ── colourForSilo: different silos distribute across palette ──
(function testColourDistribution() {
  // With 10 palette entries and many silos, we should see > 2 distinct colours used.
  var seen = new Set();
  for (var i = 0; i < 30; i++) {
    seen.add(SC.colourForSilo("user" + i));
  }
  assert.ok(seen.size > 2, "30 silos use >2 distinct palette entries (good distribution): " + seen.size);
})();

// ── siloFromSecctxTier3: correct prefix extraction ──
(function testSiloFromSecctxTier3() {
  // ensures: silo name is derived from secctx app_id, never from window title
  assert.strictEqual(SC.siloFromSecctxTier3("qdistro.tier3.user1"),  "user1",  "user1 extracted");
  assert.strictEqual(SC.siloFromSecctxTier3("qdistro.tier3.user2"),  "user2",  "user2 extracted");
  assert.strictEqual(SC.siloFromSecctxTier3("qdistro.tier3.admin"),  "admin",  "admin extracted");

  // Non-tier-3 app_ids → "" (do not assert tier-3 identity on foreign windows)
  assert.strictEqual(SC.siloFromSecctxTier3("qdistro.tier4.vm-dev"), "", "tier4 prefix → empty");
  assert.strictEqual(SC.siloFromSecctxTier3("org.gnome.Files"),       "", "unrelated app_id → empty");
  assert.strictEqual(SC.siloFromSecctxTier3(""),                      "", "empty → empty");
  assert.strictEqual(SC.siloFromSecctxTier3(null),                    "", "null → empty");
  assert.strictEqual(SC.siloFromSecctxTier3(undefined),               "", "undefined → empty");

  // Prefix-only (no silo name after prefix) → ""
  assert.strictEqual(SC.siloFromSecctxTier3("qdistro.tier3."),        "", "bare prefix → empty");
})();

// ── isTier3: boolean gate ──
(function testIsTier3() {
  assert.strictEqual(SC.isTier3("qdistro.tier3.user1"), true);
  assert.strictEqual(SC.isTier3("qdistro.tier3."),       true,  "bare prefix is still tier3-shaped");
  assert.strictEqual(SC.isTier3("qdistro.tier4.vm"),     false, "tier4 is not tier3");
  assert.strictEqual(SC.isTier3("org.freedesktop.Foo"),  false);
  assert.strictEqual(SC.isTier3(""),                     false);
  assert.strictEqual(SC.isTier3(null),                   false);
  assert.strictEqual(SC.isTier3(undefined),              false);
})();

// ── siloFromSecctxTier4: correct prefix extraction ──
(function testSiloFromSecctxTier4() {
  assert.strictEqual(SC.siloFromSecctxTier4("qdistro.tier4.vm-dev"),  "vm-dev",  "vm-dev extracted");
  assert.strictEqual(SC.siloFromSecctxTier4("qdistro.tier4.vm-work"), "vm-work", "vm-work extracted");

  // Non-tier-4 → ""
  assert.strictEqual(SC.siloFromSecctxTier4("qdistro.tier3.user1"),  "", "tier3 prefix → empty");
  assert.strictEqual(SC.siloFromSecctxTier4("org.gnome.Files"),       "", "unrelated → empty");
  assert.strictEqual(SC.siloFromSecctxTier4(""),                      "", "empty → empty");
  assert.strictEqual(SC.siloFromSecctxTier4(null),                    "", "null → empty");

  // Prefix-only → ""
  assert.strictEqual(SC.siloFromSecctxTier4("qdistro.tier4."),        "", "bare prefix → empty");
})();

// ── isTier4: boolean gate ──
(function testIsTier4() {
  assert.strictEqual(SC.isTier4("qdistro.tier4.vm-dev"), true);
  assert.strictEqual(SC.isTier4("qdistro.tier3.user1"),  false);
  assert.strictEqual(SC.isTier4(""),                     false);
  assert.strictEqual(SC.isTier4(null),                   false);
})();

// ── hexToRgba: correct bit-packing ──
(function testHexToRgba() {
  // ensures: matches tier4_chrome.hex_to_rgba in Python so border colour is consistent
  // #4caf50 = R=0x4c=76, G=0xaf=175, B=0x50=80, alpha=0xFF
  // Expected: (76 << 24) | (175 << 16) | (80 << 8) | 255 = 0x4caf50ff = 1286832383
  assert.strictEqual(SC.hexToRgba("#4caf50"), 0x4caf50ff >>> 0,
    "#4caf50 → 0x4caf50FF");

  // White: #ffffff = 0xffffffff
  assert.strictEqual(SC.hexToRgba("#ffffff"), 0xffffffff >>> 0, "#ffffff → 0xFFFFFFFF");

  // Black: #000000 = 0x000000ff
  assert.strictEqual(SC.hexToRgba("#000000"), 0x000000ff >>> 0, "#000000 → 0x000000FF");

  // Red: #ff0000 = 0xff0000ff
  assert.strictEqual(SC.hexToRgba("#ff0000"), 0xff0000ff >>> 0, "#ff0000 → 0xFF0000FF");

  // Alpha is always 0xFF (fully opaque)
  assert.strictEqual((SC.hexToRgba("#2196f3") & 0xFF), 0xFF, "alpha byte is always 0xFF");
})();

// ── hexToRgba: safe fallback for invalid input ──
(function testHexToRgbaSafeFallback() {
  // ensures: bad input → 0 (qdwin uses neutral default border), never throws
  assert.strictEqual(SC.hexToRgba(""),        0, "empty string → 0");
  assert.strictEqual(SC.hexToRgba(null),      0, "null → 0");
  assert.strictEqual(SC.hexToRgba(undefined), 0, "undefined → 0");
  assert.strictEqual(SC.hexToRgba("#fff"),    0, "3-char hex → 0 (length check)");
  assert.strictEqual(SC.hexToRgba("ffffff"),  0, "no hash → 0");
  assert.strictEqual(SC.hexToRgba("#gggggg"), 0, "invalid hex chars → 0");
  assert.strictEqual(SC.hexToRgba("#ff00"),   0, "5-char → 0");
  assert.strictEqual(SC.hexToRgba("#ff000000"), 0, "8-char (RGBA) → 0 (length check)");
})();

// ── hexToRgba: round-trip with palette entries ──
(function testHexToRgbaPalette() {
  // Every palette colour must produce a non-zero rgba (all are valid #rrggbb).
  SC.SILO_PALETTE.forEach(function(hex) {
    var rgba = SC.hexToRgba(hex);
    assert.notStrictEqual(rgba, 0, "palette entry " + hex + " → non-zero rgba");
    assert.strictEqual(typeof rgba, 'number', "rgba is a number");
    assert.ok(rgba > 0, "rgba is positive (unsigned 32-bit)");
  });
})();

// ── tier3/tier4 identity never cross-contaminates ──
(function testNoCrossContamination() {
  // A tier-4 id must NOT be classified as tier-3 and vice versa.
  assert.ok(!SC.isTier3("qdistro.tier4.vm-dev"), "tier4 id is not tier3");
  assert.ok(!SC.isTier4("qdistro.tier3.user1"),  "tier3 id is not tier4");
  assert.strictEqual(SC.siloFromSecctxTier3("qdistro.tier4.vm-dev"), "", "tier4 id yields no tier3 silo");
  assert.strictEqual(SC.siloFromSecctxTier4("qdistro.tier3.user1"),  "", "tier3 id yields no tier4 silo");
})();

console.log("silo-chrome: all assertions passed");
