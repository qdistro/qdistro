const assert = require("assert");
const P = require("../Services/Theming/ColorPalette.js");

(function testHexFromArgb() {
  assert.strictEqual(P.colorToHex("#ff070722"), "#070722");
  assert.strictEqual(P.colorToHex("#070722"), "#070722");
  assert.strictEqual(P.colorToHex("#FFF59B"), "#fff59b");
  assert.strictEqual(P.colorToHex("nope"), "");
})();

(function testCompletePalette() {
  const pal = {};
  P.COLOR_KEYS.forEach(k => {
    pal[k] = "#010203";
  });
  pal.mOnSurface = "#f3edf7";
  assert.ok(P.completePalette(pal));
  const incomplete = Object.assign({}, pal);
  delete incomplete.mError;
  assert.strictEqual(P.completePalette(incomplete), null);
})();

(function testSchemeAliases() {
  const scheme = {
    primary: "#fff59b",
    onPrimary: "#0e0e43",
    secondary: "#a9aefe",
    onSecondary: "#0e0e43",
    tertiary: "#9bfece",
    onTertiary: "#0e0e43",
    error: "#fd4663",
    onError: "#0e0e43",
    surface: "#070722",
    onSurface: "#f3edf7",
    surfaceVariant: "#11112d",
    onSurfaceVariant: "#7c80b4",
    outline: "#21215f",
    shadow: "#070722",
    hover: "#9bfece",
    onHover: "#0e0e43"
  };
  const pal = P.completePaletteFromScheme(scheme);
  assert.ok(pal);
  assert.strictEqual(pal.mPrimary, "#fff59b");
  assert.strictEqual(pal.mSurface, "#070722");
})();

(function testIncompleteSchemeRejected() {
  assert.strictEqual(P.completePaletteFromScheme({ primary: "#fff59b" }), null);
})();

(function testUntaggedFileMustNotInheritPendingId() {
  // ensures: a colors.json load during a pending wallpaper request cannot
  // acquire that request's identity.
  assert.strictEqual(P.untaggedFileMayCommit(2, 1, false), false, "pending generation blocks untagged commit");
  assert.strictEqual(P.untaggedFileMayCommit(1, 1, false), true, "no pending generation allows manual/initial commit");
  assert.strictEqual(P.untaggedFileMayCommit(0, 0, false), true, "startup ids equal");
  assert.strictEqual(P.untaggedFileMayCommit(2, 2, true), false, "producer still running blocks untagged commit even if ids match");
})();

console.log("test_color_palette.js ok");
