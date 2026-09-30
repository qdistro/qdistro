// Shared palette helpers for first-party presentation export.
// Imported by Color.qml / ColorSchemeService.qml / AppPresentationService.qml.
// Node tests require this file directly.

"use strict";

var COLOR_KEYS = [
  "mPrimary",
  "mOnPrimary",
  "mSecondary",
  "mOnSecondary",
  "mTertiary",
  "mOnTertiary",
  "mError",
  "mOnError",
  "mSurface",
  "mOnSurface",
  "mSurfaceVariant",
  "mOnSurfaceVariant",
  "mOutline",
  "mShadow",
  "mHover",
  "mOnHover"
];

function colorToHex(value) {
  if (value === undefined || value === null)
    return "";
  var s = ("" + value).trim().toLowerCase();
  if (s.indexOf(" ") !== -1)
    s = s.split(" ")[0];
  if (/^#[0-9a-f]{6}$/.test(s))
    return s;
  if (/^#[0-9a-f]{8}$/.test(s))
    return "#" + s.substring(3);
  return "";
}

function pickColor(obj, a, b) {
  if (!obj)
    return "";
  if (obj[a] !== undefined && obj[a] !== null && obj[a] !== "")
    return colorToHex(obj[a]);
  if (b && obj[b] !== undefined && obj[b] !== null && obj[b] !== "")
    return colorToHex(obj[b]);
  return "";
}

function completePalette(obj) {
  if (!obj)
    return null;
  var out = {};
  for (var i = 0; i < COLOR_KEYS.length; i++) {
    var key = COLOR_KEYS[i];
    var hex = colorToHex(obj[key]);
    if (!hex)
      return null;
    out[key] = hex;
  }
  return out;
}

function completePaletteFromScheme(obj) {
  if (!obj)
    return null;
  var aliases = {
    mPrimary: ["mPrimary", "primary"],
    mOnPrimary: ["mOnPrimary", "onPrimary"],
    mSecondary: ["mSecondary", "secondary"],
    mOnSecondary: ["mOnSecondary", "onSecondary"],
    mTertiary: ["mTertiary", "tertiary"],
    mOnTertiary: ["mOnTertiary", "onTertiary"],
    mError: ["mError", "error"],
    mOnError: ["mOnError", "onError"],
    mSurface: ["mSurface", "surface"],
    mOnSurface: ["mOnSurface", "onSurface"],
    mSurfaceVariant: ["mSurfaceVariant", "surfaceVariant"],
    mOnSurfaceVariant: ["mOnSurfaceVariant", "onSurfaceVariant"],
    mOutline: ["mOutline", "outline"],
    mShadow: ["mShadow", "shadow"],
    mHover: ["mHover", "hover"],
    mOnHover: ["mOnHover", "onHover"]
  };
  var out = {};
  for (var i = 0; i < COLOR_KEYS.length; i++) {
    var key = COLOR_KEYS[i];
    var names = aliases[key];
    var hex = pickColor(obj, names[0], names[1]);
    if (!hex)
      return null;
    out[key] = hex;
  }
  return out;
}

function untaggedFileMayCommit(pendingId, acceptedId, producerRunning) {
  // Untagged colors.json contents must never inherit a pending generation's
  // identity, and must not be treated as a manual edit while a producer
  // process is still running.
  if (producerRunning)
    return false;
  return pendingId === acceptedId;
}

function palettesEqual(a, b) {
  if (!a || !b)
    return false;
  for (var i = 0; i < COLOR_KEYS.length; i++) {
    var key = COLOR_KEYS[i];
    if (a[key] !== b[key])
      return false;
  }
  return true;
}

if (typeof module !== "undefined") {
  module.exports = {
    COLOR_KEYS: COLOR_KEYS,
    colorToHex: colorToHex,
    completePalette: completePalette,
    completePaletteFromScheme: completePaletteFromScheme,
    palettesEqual: palettesEqual,
    untaggedFileMayCommit: untaggedFileMayCommit
  };
}
