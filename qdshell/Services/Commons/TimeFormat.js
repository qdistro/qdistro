// TEST MIRROR — NOT imported by production QML.
// This file is a hand-kept copy of the pure logic in Commons/Time.qml.
// Commons/Time.qml does NOT import this module; it contains the logic
// directly. Tests (tests/test_timeformat.js) require this file under Node.
//
// DRIFT RISK: if the logic in Commons/Time.qml changes, this file must be
// updated manually. tests/test_drift_guard.js asserts that the function
// bodies match the QML source to catch silent drift.
//
// Known intentional deviation from the QML source:
//   `formatRelativeTime` in Time.qml calls I18n.tr() to produce translated
//   strings. I18n is a QML-only singleton that cannot be loaded under Node.
//   This module therefore accepts an optional `tr` function parameter; if
//   omitted it falls back to a minimal built-in English formatter that
//   mirrors the production string shapes. The QML path is not replicated
//   here since it has a hard runtime dependency on a Quickshell singleton.
//
// `formatVagueHumanReadableDuration` and `getFormattedTimestamp` have no
// I18n dependency and are extracted verbatim.

"use strict";

// Formats a Date object into "YYYYMMDD-HHMMSS". Mirrors Time.getFormattedTimestamp.
function getFormattedTimestamp(date) {
  if (!date) {
    date = new Date();
  }
  var year = date.getFullYear();
  var month = String(date.getMonth() + 1).padStart(2, '0');
  var day = String(date.getDate()).padStart(2, '0');
  var hours = String(date.getHours()).padStart(2, '0');
  var minutes = String(date.getMinutes()).padStart(2, '0');
  var seconds = String(date.getSeconds()).padStart(2, '0');
  return "" + year + month + day + "-" + hours + minutes + seconds;
}

// Formats a total-seconds integer into an approximate human-readable duration,
// e.g. 4h 32m, 3d 2h, 45s. Mirrors Time.formatVagueHumanReadableDuration.
function formatVagueHumanReadableDuration(totalSeconds) {
  if (typeof totalSeconds !== 'number' || totalSeconds < 0) {
    return '0s';
  }
  totalSeconds = Math.floor(totalSeconds);
  var days = Math.floor(totalSeconds / 86400);
  var hours = Math.floor((totalSeconds % 86400) / 3600);
  var minutes = Math.floor((totalSeconds % 3600) / 60);
  var seconds = totalSeconds % 60;
  var parts = [];
  if (days)
    parts.push(days + "d");
  if (hours)
    parts.push(hours + "h");
  if (minutes)
    parts.push(minutes + "m");
  if (!hours && !minutes) {
    parts.push(seconds + "s");
  }
  return parts.join(' ');
}

// Fallback English translations matching the I18n key shapes used in
// Time.formatRelativeTime. `params` is e.g. { diff: 5 }.
function _defaultTr(key, params) {
  switch (key) {
    case "notifications.time.now":   return "just now";
    case "notifications.time.diff-m":  return "1 minute ago";
    case "notifications.time.diff-mm": return (params && params.diff) + " minutes ago";
    case "notifications.time.diff-h":  return "1 hour ago";
    case "notifications.time.diff-hh": return (params && params.diff) + " hours ago";
    case "notifications.time.diff-d":  return "1 day ago";
    case "notifications.time.diff-dd": return (params && params.diff) + " days ago";
    default: return key;
  }
}

// Formats a Date into a relative string like "just now", "5 minutes ago", etc.
// Mirrors Time.formatRelativeTime. `tr` is an optional translation function
// with the signature (key, params?) → string; defaults to built-in English.
// The `now` parameter overrides Date.now() for testing determinism.
function formatRelativeTime(date, tr, now) {
  if (!date)
    return "";
  var _tr = (typeof tr === 'function') ? tr : _defaultTr;
  var _now = (typeof now === 'number') ? now : Date.now();
  var diff = _now - date.getTime();
  if (diff < 60000)
    return _tr("notifications.time.now");
  if (diff < 120000)
    return _tr("notifications.time.diff-m");
  if (diff < 3600000)
    return _tr("notifications.time.diff-mm", { diff: Math.floor(diff / 60000) });
  if (diff < 7200000)
    return _tr("notifications.time.diff-h");
  if (diff < 86400000)
    return _tr("notifications.time.diff-hh", { diff: Math.floor(diff / 3600000) });
  if (diff < 172800000)
    return _tr("notifications.time.diff-d");
  return _tr("notifications.time.diff-dd", { diff: Math.floor(diff / 86400000) });
}

var api = {
  getFormattedTimestamp: getFormattedTimestamp,
  formatVagueHumanReadableDuration: formatVagueHumanReadableDuration,
  formatRelativeTime: formatRelativeTime,
};

if (typeof module !== "undefined") {
  module.exports = api;
}
