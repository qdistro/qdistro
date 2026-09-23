// Pure "known tray items" policy logic, extracted from Tray.qml /
// TraySettings.qml so it can be unit-tested under Node
// (see tests/test_tray_known_items.js) while still being imported from QML
// (`import "TrayKnownItems.js" as TrayKnownItems`).
//
// Everything here operates ONLY on plain arrays/strings. There is NO access to
// the SystemTray / Settings / Process singletons — the QML side reads those and
// passes the resulting primitives in. This keeps the functions deterministic
// and testable.
//
// SECURITY: a tray item id and title are UNTRUSTED, attacker-influenced data
// (any app on the bus can register an SNI item with an arbitrary id/title).
// They are treated here as OPAQUE keys/labels only. We never interpolate them
// into a shell command, regex, or any executable string — they are compared
// with === and stored verbatim. The QML layer must likewise never build a
// command from these values.

// Known-item visibility policies.
var POLICY_DEFAULT = "default"; // follow normal filtering (no override)
var POLICY_SHOW = "show"; // always show this item
var POLICY_HIDE = "hide"; // always hide this item

var VALID_POLICIES = [POLICY_DEFAULT, POLICY_SHOW, POLICY_HIDE];

// Coerce an arbitrary value to a plain string key. Missing / non-string values
// collapse to "" so callers can treat "" as "no stable id".
function asKey(v) {
  if (typeof v !== "string")
    return "";
  return v;
}

// Derive a stable identity for a seen tray item. Prefer the item id; fall back
// to title only if no id is present. Returns "" when neither is usable so the
// caller can skip merging an unidentifiable item.
function itemKey(item) {
  if (!item || typeof item !== "object")
    return "";
  var id = asKey(item.id);
  if (id.length > 0)
    return id;
  return asKey(item.title);
}

// Normalize a stored policy value to one of VALID_POLICIES; unknown / missing
// values fall back to "default".
function sanitizePolicy(policy) {
  if (VALID_POLICIES.indexOf(policy) !== -1)
    return policy;
  return POLICY_DEFAULT;
}

// Make a defensive, normalized copy of one known-item entry.
function normalizeEntry(entry) {
  var e = (entry && typeof entry === "object") ? entry : {};
  return {
    "id": asKey(e.id),
    "title": asKey(e.title),
    "policy": sanitizePolicy(e.policy),
  };
}

// Normalize an entire known-items list: drop entries with no stable id, dedup
// by id (last write wins for title/policy), and return a fresh array of
// normalized entries.
function normalizeList(list) {
  var out = [];
  var indexById = {};
  var arr = Array.isArray(list) ? list : [];
  for (var i = 0; i < arr.length; i++) {
    var e = normalizeEntry(arr[i]);
    if (e.id.length === 0)
      continue;
    if (Object.prototype.hasOwnProperty.call(indexById, e.id)) {
      out[indexById[e.id]] = e;
    } else {
      indexById[e.id] = out.length;
      out.push(e);
    }
  }
  return out;
}

// Merge a newly-seen tray item into the known-items list WITHOUT creating
// duplicates (keyed by the item's stable id). Returns a NEW list (the input is
// not mutated).
//
//  - brand-new id  -> appended with the given/default policy
//  - duplicate id  -> existing entry kept; its title is refreshed if the
//                     item now reports a (non-empty) title, but its stored
//                     policy is preserved (the user's choice wins)
//
// `seen` is the untrusted {id, title} pair. `defaultPolicy` is used only for
// brand-new entries.
function mergeSeenItem(list, seen, defaultPolicy) {
  var normalized = normalizeList(list);
  var id = itemKey(seen);
  if (id.length === 0)
    return normalized; // unidentifiable item — never tracked
  var title = asKey(seen && seen.title);

  for (var i = 0; i < normalized.length; i++) {
    if (normalized[i].id === id) {
      var updated = {
        "id": normalized[i].id,
        "title": title.length > 0 ? title : normalized[i].title,
        "policy": normalized[i].policy,
      };
      var copy = normalized.slice();
      copy[i] = updated;
      return copy;
    }
  }

  normalized.push({
    "id": id,
    "title": title,
    "policy": sanitizePolicy(defaultPolicy),
  });
  return normalized;
}

// Set the per-item policy for a known item by id. No-op (returns a normalized
// copy) if the id is not present. Returns a NEW list.
function setPolicy(list, id, policy) {
  var normalized = normalizeList(list);
  var key = asKey(id);
  if (key.length === 0)
    return normalized;
  for (var i = 0; i < normalized.length; i++) {
    if (normalized[i].id === key) {
      var copy = normalized.slice();
      copy[i] = {
        "id": normalized[i].id,
        "title": normalized[i].title,
        "policy": sanitizePolicy(policy),
      };
      return copy;
    }
  }
  return normalized;
}

// Look up the stored policy for an item id. Returns "default" when the id is
// unknown.
function policyForId(list, id) {
  var normalized = normalizeList(list);
  var key = asKey(id);
  for (var i = 0; i < normalized.length; i++) {
    if (normalized[i].id === key)
      return normalized[i].policy;
  }
  return POLICY_DEFAULT;
}

// Compute effective visibility for an item given its known-items policy and the
// visibility that normal filtering (blacklist / hide-passive) would otherwise
// produce.
//
//  - policy "show"    -> always visible (overrides normal filtering)
//  - policy "hide"    -> always hidden  (overrides normal filtering)
//  - policy "default" -> defer to `defaultVisible`
function effectiveVisible(policy, defaultVisible) {
  var p = sanitizePolicy(policy);
  if (p === POLICY_SHOW)
    return true;
  if (p === POLICY_HIDE)
    return false;
  return !!defaultVisible;
}

// Reset the known-items list: returns a fresh empty list.
function reset() {
  return [];
}

var api = {
  POLICY_DEFAULT: POLICY_DEFAULT,
  POLICY_SHOW: POLICY_SHOW,
  POLICY_HIDE: POLICY_HIDE,
  VALID_POLICIES: VALID_POLICIES,
  itemKey: itemKey,
  sanitizePolicy: sanitizePolicy,
  normalizeEntry: normalizeEntry,
  normalizeList: normalizeList,
  mergeSeenItem: mergeSeenItem,
  setPolicy: setPolicy,
  policyForId: policyForId,
  effectiveVisible: effectiveVisible,
  reset: reset,
};

if (typeof module !== "undefined") {
  module.exports = api;
}
