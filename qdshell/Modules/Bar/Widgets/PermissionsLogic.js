// Pure permissions-panel logic, extracted from Taskbar.qml so it can be
// unit-tested under Node (see tests/test_taskbar_permissions.js) while still
// being imported from QML (`import "PermissionsLogic.js" as PermissionsLogic`).
//
// The taskbar isolation menu's "Permissions…" item shows a READ-ONLY view of
// the broker rules that apply to a window's silo identity. The broker exposes
// the loaded rule set via the admin-only `org.qdistro.AdminBroker1.ListRules`
// D-Bus method (out signature aa{sv}); qdshell calls it with
// `busctl --system --json=short call` and feeds the raw stdout here.
//
// Everything here operates ONLY on plain strings/objects — NO Qdwin / Process /
// Settings singletons. The QML side runs the busctl Process, reads its stdout,
// and passes the raw text + the window's identity primitives in.

// Unwrap one busctl --json=short value. For an aa{sv} return, each dict VALUE is
// a D-Bus variant, which busctl --json=short renders as a {"type":…,"data":…}
// wrapper (the variant type is NOT stripped in short mode — only the outer
// container types are). So r.name is {"type":"s","data":"work"}, not "work". We
// unwrap that wrapper; a bare value (a hypothetical future short-mode change, or
// a non-variant field) passes through unchanged. Nested containers inside a
// variant keep their own .data shape, which is fine for the scalar fields we
// read here.
function _unwrap(v) {
  if (v && typeof v === "object" && !Array.isArray(v) &&
      typeof v.type === "string" && ("data" in v)) {
    return v.data;
  }
  return v;
}

function _str(v) {
  v = _unwrap(v);
  if (v === null || v === undefined)
    return "";
  if (typeof v === "object")
    return "";   // never stringify a container into "[object Object]"
  return String(v);
}

function _int(v) {
  v = _unwrap(v);
  return (typeof v === "number") ? v : -1;
}

// Parse the `busctl --json=short call … ListRules` stdout into a plain array
// of rule objects. busctl --json=short renders aa{sv} as
//   {"type":"aa{sv}","data":[[ {key:{"type":t,"data":val}, …}, … ]]}
// (the per-value variant wrappers are KEPT in short mode), so data[0] is the
// array of rule dicts whose values are {type,data}-wrapped — we unwrap each.
// Returns [] on empty / unparseable / unexpected shapes (fail-safe: an empty
// panel, never a crash or a "[object Object]" row).
function parseListRules(raw) {
  if (!raw)
    return [];
  var parsed = null;
  try {
    parsed = JSON.parse(String(raw).trim());
  } catch (e) {
    return [];
  }
  if (!parsed || !parsed.data || parsed.data.length < 1)
    return [];
  var rows = parsed.data[0];
  if (!Array.isArray(rows))
    return [];
  // Each row is a dict of {type,data}-wrapped values; unwrap + defensively
  // coerce field types so a malformed entry can't poison the renderer.
  var out = [];
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    if (!r || typeof r !== "object")
      continue;
    out.push({
      "name":           _str(r.name),
      "decision":       _str(r.decision),
      "source_path":    _str(r.source_path),
      "uid":            _int(r.uid),
      "action":         _str(r.action),
      "exe":            _str(r.exe),
      "app_id":         _str(r.app_id),
      "sandbox_engine": _str(r.sandbox_engine),
      "mime_type":      _str(r.mime_type),
      "scope":          _str(r.scope),
      "rationale":      _str(r.rationale),
    });
  }
  return out;
}

// Does this rule apply to the given window identity? The broker treats an empty
// rule field as "match anything" (the same "don't care" convention RulesEngine
// uses — see ListRules docstring). A rule is shown for this window when its
// app_id / sandbox_engine constraints are each EITHER unset (match-any) OR an
// exact match for the window's identity. A rule that constrains a DIFFERENT
// app_id/engine does not apply and is filtered out.
//
// We deliberately require an exact app_id OR sandbox_engine MATCH for at least
// one constrained field so the panel shows the rules genuinely scoped to this
// silo, not the entire global match-any rule set (which would be misleading as
// "this silo's permissions"). A rule with NO app_id and NO engine constraint is
// a global default; we include it only when `includeGlobal` is true.
function ruleAppliesToWindow(rule, identity, includeGlobal) {
  rule = rule || {};
  identity = identity || {};
  var winAppId = (identity.secctxAppId || "") + "";
  var winEngine = (identity.sandboxEngine || "") + "";
  var rAppId = (rule.app_id || "") + "";
  var rEngine = (rule.sandbox_engine || "") + "";

  // A constrained field that does not match this window excludes the rule.
  if (rAppId && rAppId !== winAppId)
    return false;
  if (rEngine && rEngine !== winEngine)
    return false;

  // At least one of the rule's identity fields must be a real (non-empty)
  // constraint that matches — otherwise it is a global default rule.
  var hasIdentityConstraint = (rAppId && rAppId === winAppId) ||
                              (rEngine && rEngine === winEngine);
  if (hasIdentityConstraint)
    return true;
  return !!includeGlobal;
}

// Build the read-only permissions menu model for a window identity from the
// parsed rule set. Each output row is a disabled (informational) menu item the
// QML NPopupContextMenu renders. The first row is a header; identity-scoped
// rules come first, then (optionally) global defaults under a sub-header. When
// no rule applies, a single "no rules" row is returned so the panel never shows
// an empty popup.
//
// Rows carry an i18n `labelKey` + `labelParams` for the QML side; `label` is an
// English fallback for node-side callers / tests. Live actions are NOT emitted
// (this is a read-only view), so every row is `enabled: false`.
function buildPermissionsMenu(rules, identity) {
  rules = rules || [];
  identity = identity || {};
  var winAppId = (identity.secctxAppId || "") + "";
  var winEngine = (identity.sandboxEngine || "") + "";

  // Partition into identity-scoped and global-default applicable rules.
  var scoped = [];
  var global = [];
  for (var i = 0; i < rules.length; i++) {
    var r = rules[i];
    if (ruleAppliesToWindow(r, identity, false)) {
      scoped.push(r);
    } else if (ruleAppliesToWindow(r, identity, true)) {
      global.push(r);
    }
  }

  var items = [];
  items.push({ "label": "Permissions", "action": "qd-perm-header",
               "icon": "lock", "enabled": false, "isQdistro": true,
               "labelKey": "bar.taskbar.permissions.header" });

  if (scoped.length === 0 && global.length === 0) {
    items.push({ "label": "No rules apply to this silo.",
                 "action": "qd-perm-none", "enabled": false, "isQdistro": true,
                 "labelKey": "bar.taskbar.permissions.none" });
    return items;
  }

  scoped.forEach(function (r) {
    items.push(_ruleRow(r, "qd-perm-rule"));
  });

  if (global.length > 0) {
    items.push({ "label": "Global defaults", "action": "qd-perm-global-header",
                 "enabled": false, "isQdistro": true,
                 "labelKey": "bar.taskbar.permissions.global-header" });
    global.forEach(function (r) {
      items.push(_ruleRow(r, "qd-perm-global"));
    });
  }
  return items;
}

// Render a single rule into a disabled menu row. The visible label is
// "<allow|deny> <action>" (action defaults to "any action" when unset), with
// the rationale carried as a tooltip-style param. The decision drives the icon
// (allow -> check, deny -> x) so the panel reads at a glance.
function _ruleRow(rule, action) {
  rule = rule || {};
  var decision = (rule.decision || "") + "";
  var act = (rule.action || "") + "";
  var actLabel = act || "any action";
  var label = (decision || "?") + ": " + actLabel;
  return {
    "label": label,
    "action": action,
    "enabled": false,
    "isQdistro": true,
    "icon": decision === "allow" ? "check" : (decision === "deny" ? "x" : "shield"),
    "labelKey": decision === "allow" ? "bar.taskbar.permissions.rule-allow"
              : decision === "deny" ? "bar.taskbar.permissions.rule-deny"
              : "bar.taskbar.permissions.rule-other",
    "labelParams": { "action": actLabel, "decision": decision || "?" },
    "ruleName": String(rule.name || ""),
    "rationale": String(rule.rationale || ""),
  };
}

if (typeof module !== "undefined") {
  module.exports = {
    parseListRules: parseListRules,
    ruleAppliesToWindow: ruleAppliesToWindow,
    buildPermissionsMenu: buildPermissionsMenu,
  };
}
