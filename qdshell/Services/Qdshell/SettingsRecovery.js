// Pure settings schema-recovery / widget-upgrade logic, extracted from
// Commons/Settings.qml (upgradeSettings / upgradeWidget) and the malformed-
// config / default-merge paths, so the recovery rules can be unit-tested under
// Node (tests/test_settings_recovery.js) while still being the SAME code
// Settings.qml runs (`import "...SettingsRecovery.js" as Recovery`).
//
// These functions operate ONLY on plain objects/arrays. Settings.qml owns the
// Quickshell JsonObject adapter, the FileView I/O, and the live
// BarWidgetRegistry / ControlCenterWidgetRegistry / DesktopWidgetRegistry
// singletons; it passes the relevant plain values (a widgets array, a
// "metadata map", a "known-id" set) into these helpers and writes the result
// back. That boundary is what makes schema migration / malformed-config
// recovery testable at all — the live path can only be exercised on a VM.
//
// Why this matters (state the invariant, fail safe):
//
//  * pruneUnknownWidgets MUST drop any persisted widget whose `id` is not in
//    the current registry. A stale id left in the bar/control-center/desktop
//    config would make the shell try to instantiate a component that no longer
//    exists. Dropping is the fail-safe.
//
//  * upgradeWidget MUST (a) delete user keys that are no longer part of the
//    widget's schema (deprecated settings) and (b) inject any missing key from
//    the registry default, WITHOUT ever touching `id`. A widget that keeps a
//    deprecated key or misses a new required key renders with stale/undefined
//    state.
//
//  * recoverConfig MUST turn a malformed/parscanly-broken settings blob into a
//    usable object by falling back to defaults, never throwing — a thrown
//    parse error at startup would leave the user with no shell config at all.

// ---- malformed-config recovery --------------------------------------

// Parse a settings.json string. On ANY parse failure (truncated write, hand-
// edit typo, non-object top level) return null so the caller falls back to
// defaults instead of crashing. Never throws.
function parseConfig(text) {
  if (typeof text !== "string")
    return null;
  var trimmed = text.trim();
  if (trimmed === "")
    return null;
  var parsed;
  try {
    parsed = JSON.parse(trimmed);
  } catch (e) {
    return null;
  }
  // A valid settings root must be a plain object. A top-level array, number,
  // string or null is corruption — treat as unrecoverable -> defaults.
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed))
    return null;
  return parsed;
}

// Deep-merge `user` over `defaults`: every key present in defaults is
// guaranteed in the result; user values win for primitives/arrays; objects
// recurse. Keys present ONLY in user (not in defaults) are preserved (so we
// never silently drop a still-valid user setting the defaults file lags on).
// Arrays are taken whole from user (we never element-merge arrays).
function _isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function mergeDefaults(defaults, user) {
  if (!_isPlainObject(defaults))
    return _isPlainObject(user) ? user : (user === undefined ? defaults : user);
  if (!_isPlainObject(user))
    return JSON.parse(JSON.stringify(defaults));   // user missing -> clone defaults
  var out = {};
  var k;
  for (k in defaults) {
    if (!Object.prototype.hasOwnProperty.call(defaults, k))
      continue;
    if (Object.prototype.hasOwnProperty.call(user, k)) {
      if (_isPlainObject(defaults[k])) {
        out[k] = _isPlainObject(user[k])
          ? mergeDefaults(defaults[k], user[k])
          : JSON.parse(JSON.stringify(defaults[k]));
      } else if (Array.isArray(defaults[k])) {
        out[k] = Array.isArray(user[k])
          ? user[k]
          : JSON.parse(JSON.stringify(defaults[k]));
      } else {
        out[k] = user[k];
      }
    } else {
      // missing in user -> take default (clone objects/arrays defensively)
      out[k] = _isPlainObject(defaults[k]) || Array.isArray(defaults[k])
        ? JSON.parse(JSON.stringify(defaults[k]))
        : defaults[k];
    }
  }
  // preserve user-only keys
  for (k in user) {
    // F12: never copy prototype-mutating keys. JSON.parse exposes __proto__ as
    // an own property; out["__proto__"] = ... would invoke the prototype setter
    // and reparent `out`. constructor/prototype are excluded for the same class.
    if (k === "__proto__" || k === "constructor" || k === "prototype")
      continue;
    if (Object.prototype.hasOwnProperty.call(user, k) &&
        !Object.prototype.hasOwnProperty.call(out, k))
      out[k] = user[k];
  }
  return out;
}

// Full recovery entry point: parse text, fall back to a (cloned) defaults
// object on corruption, else merge user over defaults so every default key is
// present. `recovered` reports whether a fallback/merge had to happen.
function recoverConfig(text, defaults) {
  var safeDefaults = _isPlainObject(defaults) ? defaults : {};
  var parsed = parseConfig(text);
  if (parsed === null) {
    return {
      data: JSON.parse(JSON.stringify(safeDefaults)),
      recovered: true,
      reason: "malformed-or-missing",
    };
  }
  return {
    data: mergeDefaults(safeDefaults, parsed),
    recovered: false,
    reason: "ok",
  };
}

// ---- schema version stamping ----------------------------------------
//
// qdshell ships fresh schema v1 (no Noctalia migration chain). The contract:
// stamp the current version onto a freshly-loaded config; report whether the
// stored version is ahead (downgrade — leave data, warn) or behind (would run
// migrations once a framework exists).
function classifyVersion(storedVersion, currentVersion) {
  var stored = Number(storedVersion);
  var cur = Number(currentVersion);
  if (!isFinite(stored))
    stored = 0;          // unstamped legacy/fresh blob
  if (stored < cur)
    return "upgrade";
  if (stored > cur)
    return "downgrade";  // config from a newer qdshell; don't destroy it
  return "current";
}

// ---- bar / control-center / desktop widget pruning ------------------

// Drop every widget whose `id` is not a known/registered widget id. `isKnown`
// is a predicate (id -> bool); LauncherCore/Settings passes
// BarWidgetRegistry.hasWidget etc. Returns { widgets, removed } where
// `removed` is the count dropped. Order of survivors is preserved. Entries
// that are not objects or lack a string id are also dropped (corrupt rows).
function pruneUnknownWidgets(widgets, isKnown) {
  var out = [];
  var removed = 0;
  if (!Array.isArray(widgets))
    return { widgets: out, removed: 0 };
  for (var i = 0; i < widgets.length; i++) {
    var w = widgets[i];
    if (!_isPlainObject(w) || typeof w.id !== "string" || !isKnown(w.id)) {
      removed++;
      continue;
    }
    out.push(w);
  }
  return { widgets: out, removed: removed };
}

// Mirror Settings.upgradeWidget: strip deprecated keys not in `metadata` and
// inject missing keys from `metadata` defaults. `id` is never altered. Mutates
// and returns the widget; `changed` reflects whether anything was modified
// (Settings logs only when changed). metadata is the per-id default map
// (BarWidgetRegistry.widgetMetadata[id]).
function upgradeWidget(widget, metadata) {
  if (!_isPlainObject(widget))
    return { widget: widget, changed: false };
  if (!_isPlainObject(metadata))
    return { widget: widget, changed: false };   // no schema known -> leave as-is
  var before = JSON.stringify(widget);
  var keys = Object.keys(metadata);
  var k;
  // 1. delete deprecated user keys (anything not in metadata, except id)
  for (k in widget) {
    if (!Object.prototype.hasOwnProperty.call(widget, k))
      continue;
    if (k === "id")
      continue;
    if (keys.indexOf(k) === -1)
      delete widget[k];
  }
  // 2. inject missing default keys from metadata
  for (var i = 0; i < keys.length; i++) {
    k = keys[i];
    if (k === "id")
      continue;
    if (widget[k] === undefined)
      widget[k] = metadata[k];
  }
  var after = JSON.stringify(widget);
  return { widget: widget, changed: after !== before };
}

var api = {
  parseConfig: parseConfig,
  mergeDefaults: mergeDefaults,
  recoverConfig: recoverConfig,
  classifyVersion: classifyVersion,
  pruneUnknownWidgets: pruneUnknownWidgets,
  upgradeWidget: upgradeWidget,
};

if (typeof module !== "undefined") {
  module.exports = api;
}
