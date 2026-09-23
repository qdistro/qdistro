const assert = require("assert");
const Nav = require("../Services/UI/LauncherNavigation.js");

// These cover the launcher SELECTION/NAVIGATION/RESULT-ORDERING logic that
// LauncherCore.qml delegates to. The agent-assisted UI harness (tests/ui)
// can only RUN on a VM and only screenshots that a surface is visible; this
// is the host-runnable depth: it pins the exact selectedIndex transitions for
// every arrow/Tab/PageUp/Home/End keystroke and grid wrap edge, plus the
// score-descending result order activate() relies on.

// ── clampIndex: never returns NaN / out of range ──
(function testClamp() {
  assert.strictEqual(Nav.clampIndex(0, 0), 0, "empty list clamps to 0");
  assert.strictEqual(Nav.clampIndex(5, 0), 0, "empty list ignores idx");
  assert.strictEqual(Nav.clampIndex(-3, 4), 0, "negative clamps to 0");
  assert.strictEqual(Nav.clampIndex(9, 4), 3, "overflow clamps to last");
  assert.strictEqual(Nav.clampIndex(2, 4), 2, "in-range unchanged");
  assert.strictEqual(Nav.clampIndex(NaN, 4), 0, "NaN idx -> 0");
})();

// ── linear Down/Up without wrap ──
(function testLinearNoWrap() {
  assert.strictEqual(Nav.selectNext(0, 3), 1);
  assert.strictEqual(Nav.selectNext(2, 3), 2, "Down at last stays put (no wrap)");
  assert.strictEqual(Nav.selectPrevious(2, 3), 1);
  assert.strictEqual(Nav.selectPrevious(0, 3), 0, "Up at first stays put (no wrap)");
  // empty results never move past 0
  assert.strictEqual(Nav.selectNext(0, 0), 0);
  assert.strictEqual(Nav.selectPrevious(0, 0), 0);
})();

// ── linear Down/Up WITH wrap (default Tab/Down binding) ──
(function testLinearWrap() {
  // wrap default (allowWrap omitted -> wraps)
  assert.strictEqual(Nav.selectNextWrapped(2, 3), 0, "Down at last wraps to first");
  assert.strictEqual(Nav.selectNextWrapped(0, 3), 1);
  assert.strictEqual(Nav.selectPreviousWrapped(0, 3), 2, "Up at first wraps to last");
  assert.strictEqual(Nav.selectPreviousWrapped(1, 3), 0);
  // provider with wrapNavigation:false -> clamps instead of wraps
  assert.strictEqual(Nav.selectNextWrapped(2, 3, false), 2, "no-wrap Down at last stays");
  assert.strictEqual(Nav.selectPreviousWrapped(0, 3, false), 0, "no-wrap Up at first stays");
  assert.strictEqual(Nav.selectNextWrapped(2, 3, true), 0, "explicit wrap=true wraps");
  // single result: wrap is a no-op (stays at 0)
  assert.strictEqual(Nav.selectNextWrapped(0, 1), 0);
  assert.strictEqual(Nav.selectPreviousWrapped(0, 1), 0);
  // empty
  assert.strictEqual(Nav.selectNextWrapped(0, 0), 0);
})();

// ── Home/End/PageUp/PageDown ──
(function testHomeEndPage() {
  assert.strictEqual(Nav.selectFirst(), 0);
  assert.strictEqual(Nav.selectLast(7), 6);
  assert.strictEqual(Nav.selectLast(0), 0, "End on empty -> 0");
  // page down clamps to last; page up clamps to 0
  assert.strictEqual(Nav.selectNextPage(0, 100, 10), 10);
  assert.strictEqual(Nav.selectNextPage(95, 100, 10), 99, "PageDown near end clamps to last");
  assert.strictEqual(Nav.selectPreviousPage(95, 100, 10), 85);
  assert.strictEqual(Nav.selectPreviousPage(3, 100, 10), 0, "PageUp near top clamps to 0");
  // a zero/garbage page size is forced to at least 1 (never stalls)
  assert.strictEqual(Nav.selectNextPage(0, 5, 0), 1);
  assert.strictEqual(Nav.selectNextPage(0, 5, NaN), 1);
})();

// ── grid Down/Up between rows (5 cols, 12 items => rows of 5,5,2) ──
(function testGridRows() {
  const N = 12, C = 5;
  // from index 0 (row0,col0) Down -> index 5 (row1,col0)
  assert.strictEqual(Nav.selectNextRow(0, N, C), 5);
  // from index 5 (row1,col0) Down -> row2 has only cols 0,1; col0 valid -> 10
  assert.strictEqual(Nav.selectNextRow(5, N, C), 10);
  // from index 7 (row1,col2) Down -> row2 col2 doesn't exist (only 0,1) -> clamp to last populated (11)
  assert.strictEqual(Nav.selectNextRow(7, N, C), 11);
  // from last row, Down wraps to first row same column: index 11 (row2,col1) -> row0 col1 -> 1
  assert.strictEqual(Nav.selectNextRow(11, N, C), 1);
  // Up from row0 wraps to last row same column: index 2 (row0,col2) -> last row col2 missing -> clamp to last (11)
  assert.strictEqual(Nav.selectPreviousRow(2, N, C), 11);
  // Up from row0 col1 -> last row col1 exists (index 11)
  assert.strictEqual(Nav.selectPreviousRow(1, N, C), 11);
  // Up from row1 -> row0 same col
  assert.strictEqual(Nav.selectPreviousRow(6, N, C), 1);
})();

// ── grid Left/Right between columns ──
(function testGridCols() {
  const N = 12, C = 5;
  // Right within a row
  assert.strictEqual(Nav.selectNextColumn(0, N, C), 1);
  // Right at end of full row0 (index 4) -> first cell of next row (5)
  assert.strictEqual(Nav.selectNextColumn(4, N, C), 5);
  // Right at end of short last row (index 11 = row2,col1, only 2 items) -> wraps to 0
  assert.strictEqual(Nav.selectNextColumn(11, N, C), 0);
  // Left within a row
  assert.strictEqual(Nav.selectPreviousColumn(2, N, C), 1);
  // Left at start of row1 (index 5) -> last col of previous row (4)
  assert.strictEqual(Nav.selectPreviousColumn(5, N, C), 4);
  // Left at very first cell (0) -> last logical grid cell clamped to last item (11)
  assert.strictEqual(Nav.selectPreviousColumn(0, N, C), 11);
})();

// ── grid functions are safe with 0 columns / empty (no NaN) ──
(function testGridGuards() {
  assert.strictEqual(Nav.selectNextRow(3, 10, 0), 3, "0 cols -> clamp unchanged");
  assert.strictEqual(Nav.selectNextColumn(3, 0, 5), 0, "empty -> 0");
  assert.ok(!isNaN(Nav.selectPreviousColumn(0, 1, 5)), "single item never NaN");
})();

// ── result ordering: descending _score, 0 default, stable tie-break ──
(function testOrderResults() {
  const a = { name: "a", _score: 10 };
  const b = { name: "b", _score: 50 };
  const c = { name: "c" };               // no score -> 0
  const d = { name: "d", _score: 50 };   // tie with b
  // non-blank query -> sorted by score desc, b before d (stable: b came first)
  const out = Nav.orderResults([a, b, c, d], "foo");
  assert.deepStrictEqual(out.map(x => x.name), ["b", "d", "a", "c"]);
  // scoreless item sinks to the bottom, not the top (matches activate() target)
  assert.strictEqual(out[out.length - 1].name, "c");

  // blank query -> NO reordering (provider order preserved verbatim)
  const blank = Nav.orderResults([a, b, c, d], "");
  assert.deepStrictEqual(blank.map(x => x.name), ["a", "b", "c", "d"]);
  const ws = Nav.orderResults([a, b, c, d], "   ");
  assert.deepStrictEqual(ws.map(x => x.name), ["a", "b", "c", "d"], "whitespace query is blank");

  // garbage input never throws
  assert.deepStrictEqual(Nav.orderResults(null, "x"), []);
  assert.deepStrictEqual(Nav.orderResults("nope", "x"), []);
})();

// ── resultAt: what activate() would fire ──
(function testResultAt() {
  const r = [{ name: "x" }, { name: "y" }, { name: "z" }];
  assert.strictEqual(Nav.resultAt(r, 1).name, "y");
  assert.strictEqual(Nav.resultAt(r, 9).name, "z", "overflow clamps to last");
  assert.strictEqual(Nav.resultAt(r, -1).name, "x", "negative clamps to first");
  assert.strictEqual(Nav.resultAt([], 0), null, "empty -> null (nothing to activate)");
  assert.strictEqual(Nav.resultAt(null, 0), null);
})();

// ── command-mode prefix routing (>, >clip, >cmd, >emoji, >win, >settings) ──
(function testCommandMode() {
  assert.strictEqual(Nav.isCommandMode(">"), true);
  assert.strictEqual(Nav.isCommandMode("firefox"), false);
  assert.strictEqual(Nav.isCommandMode(""), false);
  assert.strictEqual(Nav.commandModeKind(">"), "all", "bare > lists all commands");
  assert.strictEqual(Nav.commandModeKind(">clip "), "clip");
  assert.strictEqual(Nav.commandModeKind(">cmd ls"), "cmd");
  assert.strictEqual(Nav.commandModeKind(">emoji smile"), "emoji");
  assert.strictEqual(Nav.commandModeKind(">win"), "win");
  assert.strictEqual(Nav.commandModeKind(">settings audio"), "settings");
  assert.strictEqual(Nav.commandModeKind(">CLIP"), "clip", "kind match is case-insensitive");
  assert.strictEqual(Nav.commandModeKind(">zzz"), "filter", "unknown >token filters command list");
  assert.strictEqual(Nav.commandModeKind("plain"), "", "non-command -> empty kind");
})();

console.log("launcher-navigation: all assertions passed");
