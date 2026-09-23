// TEST MIRROR — NOT imported by production QML.
// This file is a hand-kept copy of the pure routing logic in Commons/Color.qml.
// Commons/Color.qml does NOT import this module; it contains the switch
// statements directly. Tests (tests/test_colorkeys.js) require this file
// under Node.
//
// DRIFT RISK: if the switch tables in Commons/Color.qml change, this file must
// be updated manually. tests/test_drift_guard.js asserts that the switch-case
// bodies match the QML source to catch silent drift.
//
// Commons/Color.qml defines three public dispatch functions
// (resolveColorKey, resolveOnColorKey, resolveColorKeyOptional) that
// map a string key ("primary", "secondary", "tertiary", "error", or
// unknown) to a role string. In QML those roles are live Qt color
// properties on the singleton. This module captures the pure ROUTING
// LOGIC (which key → which role name) so that the branching can be
// tested under Node without a compositor.
//
// The module exports the key→role mapping rather than actual color
// values, because color values are runtime-configurable in QML; what
// must stay correct across changes is WHICH role a key resolves to —
// that is the logic under test.

"use strict";

// Maps a color key to a "foreground" role name.
// QML: Color.resolveColorKey(key) → returns Color.m<Role>
// Here: resolveColorKey(key) → role name string
function resolveColorKey(key) {
  switch (key) {
    case "primary":   return "primary";
    case "secondary": return "secondary";
    case "tertiary":  return "tertiary";
    case "error":     return "error";
    default:          return "onSurface";
  }
}

// Maps a color key to an "on-foreground" (contrast) role name.
// QML: Color.resolveOnColorKey(key) → returns Color.mOn<Role> or mSurface
function resolveOnColorKey(key) {
  switch (key) {
    case "primary":   return "onPrimary";
    case "secondary": return "onSecondary";
    case "tertiary":  return "onTertiary";
    case "error":     return "onError";
    default:          return "surface";
  }
}

// Like resolveColorKey but unknown → "transparent" (used for optional accent).
// QML: Color.resolveColorKeyOptional(key) → returns the color or "transparent"
function resolveColorKeyOptional(key) {
  switch (key) {
    case "primary":   return "primary";
    case "secondary": return "secondary";
    case "tertiary":  return "tertiary";
    case "error":     return "error";
    default:          return "transparent";
  }
}

// The set of valid foreground color keys (mirrors colorKeyModel in Color.qml,
// minus "none" which maps to transparent via resolveColorKeyOptional).
var VALID_COLOR_KEYS = ["primary", "secondary", "tertiary", "error"];

var api = {
  resolveColorKey: resolveColorKey,
  resolveOnColorKey: resolveOnColorKey,
  resolveColorKeyOptional: resolveColorKeyOptional,
  VALID_COLOR_KEYS: VALID_COLOR_KEYS,
};

if (typeof module !== "undefined") {
  module.exports = api;
}
