// Pure notification-theme logic, extracted so it can be unit-tested under Node
// (see tests/test_notification_theme.js) while still being imported from QML
// (`import "NotificationTheme.js" as NotificationTheme`). Modeled on the
// sibling NotificationLayout.js.
//
// A "theme" is a named, discoverable visual style for notification toasts. It
// resolves to a complete set of *semantic* visual parameters (plain primitives
// only — no Settings / Style / Color singletons). The QML side maps those
// semantic keys onto the existing qs.Commons Style/Color design tokens so that
// NO colors or pixel sizes are hardcoded here.
//
// Visual parameters returned by resolveTheme():
//   key            : the resolved theme key (string)
//   cornerRadius   : "none" | "small" | "medium" | "large"
//   borderWidth    : "none" | "thin" | "thick"
//   padding        : "tight" | "normal" | "roomy"
//   background     : "surface" | "surfaceVariant"
//   iconPlacement  : "left" | "hidden"
//   iconSize       : "small" | "medium" | "large"
//   accentBar      : boolean  (vertical colored strip on the leading edge)
//   accentBarWidth : integer px (pre-scale; 0 when accentBar is false)
//   accentSource   : "urgency" | "primary" | "none" (semantic color source)

var THEME_DEFAULT = "default";
var THEME_COMPACT = "compact";
var THEME_ROUNDED = "rounded";
var THEME_MINIMAL = "minimal";
var THEME_ACCENT_BAR = "accent-bar";

var VALID_THEMES = [THEME_DEFAULT, THEME_COMPACT, THEME_ROUNDED, THEME_MINIMAL, THEME_ACCENT_BAR];

// Allowed value domains for each semantic parameter. Used both to author the
// table below and to clamp/validate any field at resolve time.
var CORNER_RADII = ["none", "small", "medium", "large"];
var BORDER_WIDTHS = ["none", "thin", "thick"];
var PADDINGS = ["tight", "normal", "roomy"];
var BACKGROUNDS = ["surface", "surfaceVariant"];
var ICON_PLACEMENTS = ["left", "hidden"];
var ICON_SIZES = ["small", "medium", "large"];
var ACCENT_SOURCES = ["urgency", "primary", "none"];

// Absolute clamp bounds for the accent-bar leading strip width (px, pre-scale).
var ACCENT_BAR_MIN = 0;
var ACCENT_BAR_MAX = 12;

// Built-in theme table. Each entry is a complete parameter set.
var THEMES = {};
THEMES[THEME_DEFAULT] = {
  cornerRadius: "large",
  borderWidth: "thin",
  padding: "normal",
  background: "surface",
  iconPlacement: "left",
  iconSize: "medium",
  accentBar: false,
  accentBarWidth: 0,
  accentSource: "urgency"
};
THEMES[THEME_COMPACT] = {
  cornerRadius: "small",
  borderWidth: "thin",
  padding: "tight",
  background: "surface",
  iconPlacement: "left",
  iconSize: "small",
  accentBar: false,
  accentBarWidth: 0,
  accentSource: "urgency"
};
THEMES[THEME_ROUNDED] = {
  cornerRadius: "large",
  borderWidth: "none",
  padding: "roomy",
  background: "surfaceVariant",
  iconPlacement: "left",
  iconSize: "large",
  accentBar: false,
  accentBarWidth: 0,
  accentSource: "primary"
};
THEMES[THEME_MINIMAL] = {
  cornerRadius: "none",
  borderWidth: "none",
  padding: "normal",
  background: "surface",
  iconPlacement: "hidden",
  iconSize: "small",
  accentBar: false,
  accentBarWidth: 0,
  accentSource: "none"
};
THEMES[THEME_ACCENT_BAR] = {
  cornerRadius: "medium",
  borderWidth: "none",
  padding: "normal",
  background: "surface",
  iconPlacement: "left",
  iconSize: "medium",
  accentBar: true,
  accentBarWidth: 4,
  accentSource: "urgency"
};

// Validate / normalize a theme key. Unknown / empty / missing values fall back
// to the default theme.
function sanitizeTheme(key) {
  if (VALID_THEMES.indexOf(key) !== -1)
    return key;
  return THEME_DEFAULT;
}

// Clamp the accent-bar strip width to the allowed range. Non-numeric input
// falls back to 0 (no strip).
function clampAccentBarWidth(width) {
  var n = Number(width);
  if (!isFinite(n))
    return ACCENT_BAR_MIN;
  n = Math.round(n);
  if (n < ACCENT_BAR_MIN)
    return ACCENT_BAR_MIN;
  if (n > ACCENT_BAR_MAX)
    return ACCENT_BAR_MAX;
  return n;
}

// Pick a value from an allowed domain, falling back to the first domain entry
// when the candidate is not a member.
function pickFrom(domain, candidate) {
  if (domain.indexOf(candidate) !== -1)
    return candidate;
  return domain[0];
}

// Resolve a theme key to a complete, validated parameter set. Always returns a
// fresh object so callers cannot mutate the shared table. Unknown keys fall
// back to the default theme; individual fields are clamped to their domains so
// a malformed table entry can never leak invalid values to the renderer.
function resolveTheme(key) {
  var resolved = sanitizeTheme(key);
  var t = THEMES[resolved] || THEMES[THEME_DEFAULT];
  var accentBar = t.accentBar === true;
  return {
    key: resolved,
    cornerRadius: pickFrom(CORNER_RADII, t.cornerRadius),
    borderWidth: pickFrom(BORDER_WIDTHS, t.borderWidth),
    padding: pickFrom(PADDINGS, t.padding),
    background: pickFrom(BACKGROUNDS, t.background),
    iconPlacement: pickFrom(ICON_PLACEMENTS, t.iconPlacement),
    iconSize: pickFrom(ICON_SIZES, t.iconSize),
    accentBar: accentBar,
    accentBarWidth: accentBar ? clampAccentBarWidth(t.accentBarWidth) : 0,
    accentSource: pickFrom(ACCENT_SOURCES, t.accentSource)
  };
}

// Whether the leading icon should be rendered for the given theme.
function showIcon(key) {
  return resolveTheme(key).iconPlacement !== "hidden";
}

var api = {
  THEME_DEFAULT: THEME_DEFAULT,
  THEME_COMPACT: THEME_COMPACT,
  THEME_ROUNDED: THEME_ROUNDED,
  THEME_MINIMAL: THEME_MINIMAL,
  THEME_ACCENT_BAR: THEME_ACCENT_BAR,
  VALID_THEMES: VALID_THEMES,
  CORNER_RADII: CORNER_RADII,
  BORDER_WIDTHS: BORDER_WIDTHS,
  PADDINGS: PADDINGS,
  BACKGROUNDS: BACKGROUNDS,
  ICON_PLACEMENTS: ICON_PLACEMENTS,
  ICON_SIZES: ICON_SIZES,
  ACCENT_SOURCES: ACCENT_SOURCES,
  ACCENT_BAR_MIN: ACCENT_BAR_MIN,
  ACCENT_BAR_MAX: ACCENT_BAR_MAX,
  sanitizeTheme: sanitizeTheme,
  clampAccentBarWidth: clampAccentBarWidth,
  resolveTheme: resolveTheme,
  showIcon: showIcon
};

if (typeof module !== "undefined") {
  module.exports = api;
}
