// Pure notification-layout logic, extracted from Notification.qml so it can be
// unit-tested under Node (see tests/test_notification_detail.js) while still
// being imported from QML (`import "NotificationLayout.js" as NotificationLayout`).
//
// Operates ONLY on plain primitives — no Settings / Style singletons. The QML
// side reads those and passes the resulting numbers/strings in.

// Detail modes for a notification toast:
//   "compact"  -> title (summary) only
//   "normal"   -> title + body
//   "detailed" -> title + body + actions + timestamp
var MODE_COMPACT = "compact";
var MODE_NORMAL = "normal";
var MODE_DETAILED = "detailed";

var VALID_MODES = [MODE_COMPACT, MODE_NORMAL, MODE_DETAILED];

// Absolute clamp bounds for the user-configurable minimum toast width (px,
// pre-scale). The lower bound keeps a toast wide enough to render an icon +
// dismiss button; the upper bound prevents a toast wider than a typical screen.
var MIN_WIDTH_FLOOR = 200;
var MIN_WIDTH_CEIL = 1200;

// Validate / normalize a detail-mode string. Unknown / missing values fall back
// to "normal".
function sanitizeDetailMode(mode) {
  if (VALID_MODES.indexOf(mode) !== -1)
    return mode;
  return MODE_NORMAL;
}

// Clamp a user-supplied minimum width to the allowed range. Non-numeric input
// falls back to the floor.
function clampMinWidth(width) {
  var n = Number(width);
  if (!isFinite(n))
    return MIN_WIDTH_FLOOR;
  n = Math.round(n);
  if (n < MIN_WIDTH_FLOOR)
    return MIN_WIDTH_FLOOR;
  if (n > MIN_WIDTH_CEIL)
    return MIN_WIDTH_CEIL;
  return n;
}

// Compute the effective toast width: the larger of the density-derived base
// width and the user's configured minimum, then scaled. `scale` defaults to 1.
function effectiveWidth(baseWidth, minWidth, scale) {
  var s = Number(scale);
  if (!isFinite(s) || s <= 0)
    s = 1;
  var base = Number(baseWidth);
  if (!isFinite(base) || base < 0)
    base = 0;
  var floor = clampMinWidth(minWidth);
  return Math.round(Math.max(base, floor) * s);
}

// Whether the body text should be shown for a given detail mode.
function showBody(mode) {
  return sanitizeDetailMode(mode) !== MODE_COMPACT;
}

// Whether action buttons should be shown for a given detail mode.
function showActions(mode) {
  return sanitizeDetailMode(mode) === MODE_DETAILED;
}

// Whether the relative timestamp should be shown for a given detail mode.
function showTimestamp(mode) {
  return sanitizeDetailMode(mode) === MODE_DETAILED;
}

var api = {
  MODE_COMPACT: MODE_COMPACT,
  MODE_NORMAL: MODE_NORMAL,
  MODE_DETAILED: MODE_DETAILED,
  VALID_MODES: VALID_MODES,
  MIN_WIDTH_FLOOR: MIN_WIDTH_FLOOR,
  MIN_WIDTH_CEIL: MIN_WIDTH_CEIL,
  sanitizeDetailMode: sanitizeDetailMode,
  clampMinWidth: clampMinWidth,
  effectiveWidth: effectiveWidth,
  showBody: showBody,
  showActions: showActions,
  showTimestamp: showTimestamp,
};

if (typeof module !== "undefined") {
  module.exports = api;
}
