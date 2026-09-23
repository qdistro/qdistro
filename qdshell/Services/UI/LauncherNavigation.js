// Pure launcher selection / navigation / result-ordering logic, extracted
// from Modules/Panels/Launcher/LauncherCore.qml so it can be unit-tested
// under Node (see tests/test_launcher_navigation.js) while still being the
// SAME code LauncherCore runs at runtime
// (`import "...LauncherNavigation.js" as Nav`).
//
// Everything here operates ONLY on plain numbers/arrays/objects — there is
// NO access to Quickshell / Settings / FuzzySort singletons. LauncherCore
// reads its `results`, `selectedIndex`, `gridColumns`, page size, etc. and
// passes the resulting primitives in. Each "select*" function returns the
// NEW selectedIndex (it does not mutate); LauncherCore assigns the return
// value back to its `selectedIndex` property. This keeps the navigation math
// deterministic and exhaustively testable, exactly the boundary that the
// agent-assisted UI harness (which only runs on a VM) cannot reach.
//
// State shape used by the grid/list functions:
//   count        - number of results (results.length)
//   index        - current selectedIndex
//   columns      - grid columns (gridColumns); only used by grid* fns
//   allowWrap    - provider's wrapNavigation (default true)
//   pageSize     - rows per page for PageUp/PageDown (>=1)
//
// All functions clamp/guard so an out-of-range index or an empty result set
// never produces NaN or a negative/overflowing index.

// ---- helpers ---------------------------------------------------------

function _toInt(n, fallback) {
  var v = Math.trunc(Number(n));
  return (isFinite(v) ? v : fallback);
}

// Clamp idx into [0, count-1]; returns 0 when there are no results.
function clampIndex(idx, count) {
  var c = _toInt(count, 0);
  if (c <= 0)
    return 0;
  var i = _toInt(idx, 0);
  if (i < 0)
    return 0;
  if (i > c - 1)
    return c - 1;
  return i;
}

// ---- linear (list) navigation ---------------------------------------

// Down with no wrap: stop at the last item.
function selectNext(idx, count) {
  var c = _toInt(count, 0);
  if (c <= 0)
    return 0;
  var i = clampIndex(idx, c);
  return (i < c - 1) ? i + 1 : i;
}

// Up with no wrap: stop at the first item.
function selectPrevious(idx, count) {
  var c = _toInt(count, 0);
  if (c <= 0)
    return 0;
  var i = clampIndex(idx, c);
  return (i > 0) ? i - 1 : i;
}

// Down WITH wrap (the default list Down/Tab binding). Falls back to the
// non-wrapping move when the provider disables wrap.
function selectNextWrapped(idx, count, allowWrap) {
  var c = _toInt(count, 0);
  if (c <= 0)
    return 0;
  var i = clampIndex(idx, c);
  if (allowWrap === false)
    return selectNext(i, c);
  return (i + 1) % c;
}

// Up WITH wrap (the default list Up/Backtab binding).
function selectPreviousWrapped(idx, count, allowWrap) {
  var c = _toInt(count, 0);
  if (c <= 0)
    return 0;
  var i = clampIndex(idx, c);
  if (allowWrap === false)
    return selectPrevious(i, c);
  return ((i - 1) % c + c) % c;
}

function selectFirst() {
  return 0;
}

function selectLast(count) {
  var c = _toInt(count, 0);
  return c > 0 ? c - 1 : 0;
}

function selectNextPage(idx, count, pageSize) {
  var c = _toInt(count, 0);
  if (c <= 0)
    return 0;
  var page = Math.max(1, _toInt(pageSize, 1));
  return Math.min(clampIndex(idx, c) + page, c - 1);
}

function selectPreviousPage(idx, count, pageSize) {
  var c = _toInt(count, 0);
  if (c <= 0)
    return 0;
  var page = Math.max(1, _toInt(pageSize, 1));
  return Math.max(clampIndex(idx, c) - page, 0);
}

// ---- grid navigation -------------------------------------------------
//
// Mirrors LauncherCore.selectPreviousRow/selectNextRow/selectPreviousColumn/
// selectNextColumn EXACTLY, including the wrap-to-opposite-edge behavior and
// the "clamp to the last populated cell of a short final row" behavior.

function selectPreviousRow(idx, count, columns) {
  var c = _toInt(count, 0);
  var cols = _toInt(columns, 0);
  if (c <= 0 || cols <= 0)
    return clampIndex(idx, c);
  var i = clampIndex(idx, c);
  var currentRow = Math.floor(i / cols);
  var currentCol = i % cols;

  if (currentRow > 0) {
    var targetRow = currentRow - 1;
    var itemsInTargetRow = Math.min(cols, c - targetRow * cols);
    if (currentCol < itemsInTargetRow)
      return targetRow * cols + currentCol;
    return targetRow * cols + itemsInTargetRow - 1;
  }
  // Wrap to last row, same column.
  var totalRows = Math.ceil(c / cols);
  var lastRow = totalRows - 1;
  var itemsInLastRow = Math.min(cols, c - lastRow * cols);
  if (currentCol < itemsInLastRow)
    return lastRow * cols + currentCol;
  return c - 1;
}

function selectNextRow(idx, count, columns) {
  var c = _toInt(count, 0);
  var cols = _toInt(columns, 0);
  if (c <= 0 || cols <= 0)
    return clampIndex(idx, c);
  var i = clampIndex(idx, c);
  var currentRow = Math.floor(i / cols);
  var currentCol = i % cols;
  var totalRows = Math.ceil(c / cols);

  if (currentRow < totalRows - 1) {
    var targetRow = currentRow + 1;
    var targetIndex = targetRow * cols + currentCol;
    if (targetIndex < c)
      return targetIndex;
    var itemsInTargetRow = c - targetRow * cols;
    if (itemsInTargetRow > 0)
      return targetRow * cols + itemsInTargetRow - 1;
    return Math.min(currentCol, c - 1);
  }
  // Wrap to first row, same column.
  return Math.min(currentCol, c - 1);
}

function selectPreviousColumn(idx, count, columns) {
  var c = _toInt(count, 0);
  var cols = _toInt(columns, 0);
  if (c <= 0 || cols <= 0)
    return clampIndex(idx, c);
  var i = clampIndex(idx, c);
  var currentRow = Math.floor(i / cols);
  var currentCol = i % cols;
  if (currentCol > 0)
    return currentRow * cols + (currentCol - 1);
  if (currentRow > 0)
    return (currentRow - 1) * cols + (cols - 1);
  var totalRows = Math.ceil(c / cols);
  var lastRowIndex = (totalRows - 1) * cols + (cols - 1);
  return Math.min(lastRowIndex, c - 1);
}

function selectNextColumn(idx, count, columns) {
  var c = _toInt(count, 0);
  var cols = _toInt(columns, 0);
  if (c <= 0 || cols <= 0)
    return clampIndex(idx, c);
  var i = clampIndex(idx, c);
  var currentRow = Math.floor(i / cols);
  var currentCol = i % cols;
  var itemsInCurrentRow = Math.min(cols, c - currentRow * cols);

  if (currentCol < itemsInCurrentRow - 1)
    return currentRow * cols + (currentCol + 1);
  var totalRows = Math.ceil(c / cols);
  if (currentRow < totalRows - 1)
    return (currentRow + 1) * cols;
  return 0;
}

// ---- result ordering -------------------------------------------------
//
// Mirrors LauncherCore.updateResults' merge+sort step for the regular
// (non-command) search path: results from every provider are concatenated,
// then — when the query is non-blank — sorted by descending _score, with
// items lacking a _score treated as score 0. Array.prototype.sort is NOT
// guaranteed stable across all engines, but V8 (Node) and Qt's V4 are both
// stable for our purposes; we additionally keep a deterministic tie-break by
// original position so the order is fully specified.
function orderResults(allResults, query) {
  if (!Array.isArray(allResults))
    return [];
  var indexed = allResults.map(function (obj, i) {
    return { obj: obj, i: i };
  });
  var hasQuery = typeof query === "string" && query.trim() !== "";
  if (hasQuery) {
    indexed.sort(function (a, b) {
      var sa = (a.obj && a.obj._score !== undefined) ? a.obj._score : 0;
      var sb = (b.obj && b.obj._score !== undefined) ? b.obj._score : 0;
      if (sb !== sa)
        return sb - sa;        // higher score first
      return a.i - b.i;        // stable tie-break: keep provider order
    });
  }
  return indexed.map(function (e) { return e.obj; });
}

// Resolve which result `activate()` would fire: the item at the (clamped)
// selected index, or null when there is nothing to activate.
function resultAt(results, idx) {
  if (!Array.isArray(results) || results.length === 0)
    return null;
  var i = clampIndex(idx, results.length);
  return results[i] !== undefined ? results[i] : null;
}

// ---- mode detection (search-text prefix routing) ---------------------
//
// LauncherCore routes ">" / ">clip" / ">cmd" / ">emoji" / ">win" /
// ">settings" prefixes to command mode and a single ">" to "show all
// commands". This is the routing the IPC launcher.clipboard()/command()/etc.
// helpers and updateResults() rely on.
function isCommandMode(searchText) {
  return typeof searchText === "string" && searchText.startsWith(">");
}

function commandModeKind(searchText) {
  if (!isCommandMode(searchText))
    return "";
  var body = searchText.slice(1);                 // drop leading ">"
  if (body === "")
    return "all";                                 // ">" alone: list all commands
  var token = body.split(/\s+/)[0].toLowerCase();
  switch (token) {
  case "clip":
    return "clip";
  case "cmd":
    return "cmd";
  case "emoji":
    return "emoji";
  case "win":
    return "win";
  case "settings":
    return "settings";
  default:
    return "filter";                              // ">xyz": filter command list
  }
}

var api = {
  clampIndex: clampIndex,
  selectNext: selectNext,
  selectPrevious: selectPrevious,
  selectNextWrapped: selectNextWrapped,
  selectPreviousWrapped: selectPreviousWrapped,
  selectFirst: selectFirst,
  selectLast: selectLast,
  selectNextPage: selectNextPage,
  selectPreviousPage: selectPreviousPage,
  selectPreviousRow: selectPreviousRow,
  selectNextRow: selectNextRow,
  selectPreviousColumn: selectPreviousColumn,
  selectNextColumn: selectNextColumn,
  orderResults: orderResults,
  resultAt: resultAt,
  isCommandMode: isCommandMode,
  commandModeKind: commandModeKind,
};

if (typeof module !== "undefined") {
  module.exports = api;
}
