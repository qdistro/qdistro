const assert = require("assert");
const FS = require("../Services/Commons/FuzzySort.js");

// Tests for the pure FuzzySort ranking/scoring logic extracted from
// Commons/FuzzySort.qml. These cover the launcher matcher — the algorithm
// that determines which apps are surfaced when the user types in the launcher.

// ── single: basic match / no-match ──
(function testSingleBasic() {
  var res = FS.single("fire", "Firefox");
  assert.ok(res !== null, "exact prefix match returns a result");
  assert.strictEqual(res.target, "Firefox");
  assert.ok(res.score > 0, "matched result has positive score");

  // no common characters → no match
  assert.strictEqual(FS.single("xyz", "Firefox"), null, "no match → null");

  // empty search → null
  assert.strictEqual(FS.single("", "Firefox"), null, "empty search → null");

  // empty target → null
  assert.strictEqual(FS.single("fi", ""), null, "empty target → null");

  // null search/target → null (never throws)
  assert.strictEqual(FS.single(null, "Firefox"), null);
  assert.strictEqual(FS.single("fi", null), null);
})();

// ── single: score ordering — better match scores higher ──
(function testSingleScoreOrdering() {
  // "Firefox" should score higher for "fire" than "Fireplace Manager" (longer, less specific)
  var r1 = FS.single("fire", "Firefox");
  var r2 = FS.single("fire", "Fireplace Manager Application");
  assert.ok(r1 !== null && r2 !== null);
  // Both match; the shorter, more specific target should score higher
  assert.ok(r1.score >= r2.score, "shorter exact match scores >= longer one");
})();

// ── single: case insensitivity ──
(function testSingleCaseInsensitive() {
  var lower = FS.single("firefox", "Firefox");
  var upper = FS.single("FIREFOX", "Firefox");
  var mixed = FS.single("FireFox", "Firefox");
  assert.ok(lower !== null, "lowercase search matches");
  assert.ok(upper !== null, "uppercase search matches");
  assert.ok(mixed !== null, "mixed-case search matches");
})();

// ── single: prepared target ──
(function testSinglePrepared() {
  var prepared = FS.prepare("Firefox");
  var res = FS.single("fire", prepared);
  assert.ok(res !== null, "single() accepts a pre-prepared target");
  assert.strictEqual(res.target, "Firefox");
})();

// ── go: basic ranked list ──
(function testGoBasic() {
  FS.cleanup();
  var targets = ["Firefox", "Terminal", "Files", "Settings", "Firewatch"];
  var results = FS.go("fire", targets);
  assert.ok(results.length > 0, "go() returns at least one result");
  // Firefox and Firewatch both start with "fire" — both should appear
  var names = results.map(function(r) { return r.target; });
  assert.ok(names.indexOf("Firefox") !== -1, "Firefox appears in fire results");
  assert.ok(names.indexOf("Terminal") === -1, "Terminal does not match fire");
  // total counts all matches (may exceed limit)
  assert.ok(results.total >= results.length);
})();

// ── go: empty search returns empty ──
(function testGoEmptySearch() {
  FS.cleanup();
  var results = FS.go("", ["Firefox", "Terminal"]);
  assert.strictEqual(results.length, 0, "empty search returns empty list");
  assert.strictEqual(results.total, 0);
})();

// ── go: limit option ──
(function testGoLimit() {
  FS.cleanup();
  var targets = ["abc", "abcd", "abcde", "abcdef", "abcdefg"];
  var results = FS.go("abc", targets, { limit: 2 });
  assert.strictEqual(results.length, 2, "limit option respected");
  assert.ok(results.total >= 2, "total reflects full match count");
})();

// ── go: threshold filters low-quality matches ──
(function testGoThreshold() {
  FS.cleanup();
  // A very high threshold should filter out poor matches
  var targets = ["Firefox", "xyz"];
  var results = FS.go("fi", targets, { threshold: 0.9 });
  var names = results.map(function(r) { return r.target; });
  // "xyz" has no 'f' or 'i' so it can't match regardless of threshold
  assert.ok(names.indexOf("xyz") === -1, "non-matching target not in results");
})();

// ── go: result ordering — best match first ──
(function testGoOrdering() {
  FS.cleanup();
  var targets = ["Firefox", "FTP Client", "Far Manager", "find files"];
  var results = FS.go("ff", targets);
  // First result should have the highest score
  if (results.length >= 2) {
    assert.ok(results[0].score >= results[1].score,
      "results are sorted best-first: " + results[0].target + " >= " + results[1].target);
  }
})();

// ── highlight: wraps matched chars in <b> tags ──
(function testHighlight() {
  FS.cleanup();
  var result = FS.single("fi", "Firefox");
  assert.ok(result !== null);
  var hl = result.highlight('<b>', '</b>');
  assert.ok(typeof hl === 'string', "highlight returns a string");
  assert.ok(hl.indexOf('<b>') !== -1, "highlight contains open tag");
  assert.ok(hl.indexOf('</b>') !== -1, "highlight contains close tag");
  // The highlighted text should still contain all chars of "Firefox"
  var stripped = hl.replace(/<\/?b>/g, '');
  assert.strictEqual(stripped, "Firefox", "stripped highlight equals original target");
})();

// ── highlight: custom tags ──
(function testHighlightCustomTags() {
  FS.cleanup();
  var result = FS.single("fi", "Firefox");
  assert.ok(result !== null);
  var hl = result.highlight('[', ']');
  assert.ok(hl.indexOf('[') !== -1, "custom open tag used");
  assert.ok(hl.indexOf(']') !== -1, "custom close tag used");
})();

// ── highlight: default tags via exported function ──
(function testHighlightExportedFunction() {
  FS.cleanup();
  var result = FS.single("fi", "Firefox");
  assert.ok(result !== null);
  var hl = FS.highlight(result);
  assert.ok(hl.indexOf('<b>') !== -1, "exported highlight() uses default <b>");
})();

// ── go: key option — search on object property ──
(function testGoKey() {
  FS.cleanup();
  var apps = [
    { name: "Firefox", id: 1 },
    { name: "Terminal", id: 2 },
    { name: "Files", id: 3 },
  ];
  var results = FS.go("fire", apps, { key: "name" });
  assert.ok(results.length > 0, "key search returned results");
  assert.ok(results[0].obj !== undefined, "result.obj is the source object");
  assert.strictEqual(results[0].obj.name, "Firefox", "obj.name is Firefox");
  assert.strictEqual(results[0].obj.id, 1, "obj.id preserved");
})();

// ── cleanup: clears prepared caches without breaking subsequent calls ──
(function testCleanup() {
  FS.cleanup();
  // After cleanup, go() and single() should still work
  var r = FS.single("term", "Terminal");
  assert.ok(r !== null, "single() works after cleanup");
  var res = FS.go("term", ["Terminal", "Firefox"]);
  assert.ok(res.length > 0, "go() works after cleanup");
  FS.cleanup();
})();

// ── accented character handling ──
(function testAccents() {
  FS.cleanup();
  // "café" normalised to "cafe" for matching purposes
  var result = FS.single("cafe", "café");
  assert.ok(result !== null, "accented target matches unaccented search");
})();

// ── numeric target coercion ──
(function testNumericTarget() {
  FS.cleanup();
  // The prepare() function coerces numbers to strings
  var p = FS.prepare(42);
  assert.strictEqual(p.target, "42", "numeric target coerced to string");
})();

console.log("fuzzysort: all assertions passed");
