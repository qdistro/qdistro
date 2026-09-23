// PresentationPolicy — pure, Qt-free logic for the Power tab's presentation
// mode + idle-inhibition controls (xfce4-power-manager parity).
//
// The QML singleton (IdleInhibitorService / PowerTab wiring) stays thin and
// delegates every decision to the functions here so they can be unit-tested
// with plain Node (see tests/test_presentation_policy.js). Nothing in this
// module performs I/O, touches Qt, or builds shell commands — inhibitor ids
// and reasons are treated as OPAQUE, UNTRUSTED text and are never interpreted.
//
// CommonJS export at the bottom; also assigned onto a `.WlrPresentation`-free
// object so QML can `import "PresentationPolicy.js" as Policy`.

"use strict";

// Dedicated inhibitor id owned by presentation mode. A single, fixed string —
// never derived from user input — so it can never carry an injection payload.
var PRESENTATION_INHIBITOR_ID = "presentation-mode";
// Inhibitor id used for the inhibit-when-fullscreen feature (persist-only on
// qdwin until a fullscreen signal exists, but the id is reserved here).
var FULLSCREEN_INHIBITOR_ID = "fullscreen";

// ─── Inhibitor-set algebra (idempotent add/remove) ──────────────────
// These mirror IdleInhibitorService.addInhibitor/removeInhibitor semantics but
// operate on a plain array and RETURN a new array, so QML can reassign the
// property (the in-place push/splice in the service does not fire QML change
// notifications). Idempotent: adding an existing id or removing an absent id
// is a no-op that returns an equivalent array.

function addInhibitor(list, id) {
  var arr = Array.isArray(list) ? list.slice() : [];
  if (arr.indexOf(id) !== -1) {
    return arr; // already present — idempotent
  }
  arr.push(id);
  return arr;
}

function removeInhibitor(list, id) {
  var arr = Array.isArray(list) ? list.slice() : [];
  var idx = arr.indexOf(id);
  if (idx === -1) {
    return arr; // absent — idempotent
  }
  arr.splice(idx, 1);
  return arr;
}

function hasInhibitor(list, id) {
  return Array.isArray(list) && list.indexOf(id) !== -1;
}

// ─── Presentation-mode toggle resolution ────────────────────────────
// Given the desired enabled state and the current inhibitor list, compute the
// resulting inhibitor list. Pure: the caller applies the result.
function applyPresentationMode(list, enabled) {
  return enabled ? addInhibitor(list, PRESENTATION_INHIBITOR_ID) : removeInhibitor(list, PRESENTATION_INHIBITOR_ID);
}

// ─── Auto-disable timer ──────────────────────────────────────────────
// Clamp the configured auto-disable minutes to a sane range. 0 means "no
// auto-disable" (stay on until manually turned off).
function clampAutoDisableMinutes(minutes) {
  var m = Number(minutes);
  if (!isFinite(m) || m < 0) {
    return 0;
  }
  if (m > 1440) {
    return 1440; // cap at 24h
  }
  return Math.round(m);
}

// Whether a startup restore should re-enable presentation mode. Presentation
// mode is persisted but, like xfce4-power-manager, only restored if it was on
// AND (no auto-disable window OR the window has not already elapsed). We have
// no persisted timestamp, so we simply restore the boolean — the auto-disable
// timer restarts fresh on restore. This helper keeps the rule explicit/testable.
function shouldRestorePresentationMode(persistedEnabled) {
  return persistedEnabled === true;
}

// ─── Active-inhibitor viewer rows ────────────────────────────────────
// Map raw inhibitor ids into display rows for the read-only viewer. ids and
// reasons are untrusted: this returns them verbatim as `id`/`label` strings to
// be rendered as PlainText by the QML side. It performs NO escaping and NO
// command construction — it only assigns a friendly label for the known
// well-known ids and falls back to the raw id otherwise.
function inhibitorRows(list) {
  if (!Array.isArray(list)) {
    return [];
  }
  return list.map(function (id) {
    var known = null;
    if (id === PRESENTATION_INHIBITOR_ID) {
      known = "presentation";
    } else if (id === "manual") {
      known = "manual";
    } else if (id === FULLSCREEN_INHIBITOR_ID) {
      known = "fullscreen";
    }
    return {
      // Raw, untrusted id — render verbatim, never interpolate into a shell.
      id: String(id),
      // A stable key the QML layer can map to an i18n string for well-known
      // ids; null => render the raw id as PlainText.
      known: known
    };
  });
}

// ─── Disable-notifications-while-inhibited ───────────────────────────
// Decide whether notifications should be suppressed (routed through
// NotificationService.doNotDisturb) given the toggle and current inhibition.
// Pure boolean policy so the wiring is trivially testable.
function shouldSuppressNotifications(disableWhileInhibited, isInhibited) {
  return disableWhileInhibited === true && isInhibited === true;
}

var PresentationPolicy = {
  PRESENTATION_INHIBITOR_ID: PRESENTATION_INHIBITOR_ID,
  FULLSCREEN_INHIBITOR_ID: FULLSCREEN_INHIBITOR_ID,
  addInhibitor: addInhibitor,
  removeInhibitor: removeInhibitor,
  hasInhibitor: hasInhibitor,
  applyPresentationMode: applyPresentationMode,
  clampAutoDisableMinutes: clampAutoDisableMinutes,
  shouldRestorePresentationMode: shouldRestorePresentationMode,
  inhibitorRows: inhibitorRows,
  shouldSuppressNotifications: shouldSuppressNotifications
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = PresentationPolicy;
}
