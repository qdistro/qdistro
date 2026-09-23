const assert = require("assert");
const PermissionsLogic = require("../Modules/Bar/Widgets/PermissionsLogic.js");

// busctl --json=short renders aa{sv} as {"type":"aa{sv}","data":[[ {…}, … ]]}
// where each dict VALUE is a variant rendered as {"type":t,"data":val} (short
// mode keeps the per-value variant wrapper). Wrap each rule field the way
// busctl really does so the test exercises the production shape, not a
// pre-unwrapped fixture.
function wrapVal(v) {
  if (typeof v === "number")
    return { "type": "i", "data": v };
  return { "type": "s", "data": String(v) };
}
function busctlOut(rules) {
  var wrapped = rules.map(function (r) {
    if (r === null || typeof r !== "object")
      return r;  // exercise the "non-dict row is skipped" path verbatim
    var o = {};
    for (var k in r)
      o[k] = wrapVal(r[k]);
    return o;
  });
  return JSON.stringify({ "type": "aa{sv}", "data": [wrapped] });
}

// ── parseListRules: well-formed, empty, malformed inputs ──
(function testParseListRules() {
  const raw = busctlOut([
    { name: "r1", decision: "allow", app_id: "qdistro.tier2.work",
      sandbox_engine: "qdistro.tier2", action: "fs.read", rationale: "ok", uid: 1000 },
    { name: "r2", decision: "deny", app_id: "", sandbox_engine: "",
      action: "net.connect", rationale: "global default" },
  ]);
  const rules = PermissionsLogic.parseListRules(raw);
  assert.strictEqual(rules.length, 2);
  assert.strictEqual(rules[0].name, "r1");
  assert.strictEqual(rules[0].decision, "allow");
  assert.strictEqual(rules[0].app_id, "qdistro.tier2.work");
  assert.strictEqual(rules[0].uid, 1000);
  // Defensive coercion + fail-safe parsing.
  assert.deepStrictEqual(PermissionsLogic.parseListRules(""), []);
  assert.deepStrictEqual(PermissionsLogic.parseListRules("not json"), []);
  assert.deepStrictEqual(PermissionsLogic.parseListRules("{}"), []);
  assert.deepStrictEqual(PermissionsLogic.parseListRules(busctlOut([])), []);
  // a non-object row is skipped, not crashed on
  const mixed = PermissionsLogic.parseListRules(busctlOut([null, "x", { name: "ok" }]));
  assert.strictEqual(mixed.length, 1);
  assert.strictEqual(mixed[0].name, "ok");
  // uid missing -> -1 sentinel
  assert.strictEqual(mixed[0].uid, -1);
  // Regression: the per-value variant wrapper {"type":"s","data":"work"} MUST
  // be unwrapped, never stringified into "[object Object]". Feed the raw
  // wrapped shape directly (bypassing the helper) to lock this in.
  const rawWrapped = JSON.stringify({ "type": "aa{sv}", "data": [[
    { "name": { "type": "s", "data": "w1" },
      "decision": { "type": "s", "data": "allow" },
      "app_id": { "type": "s", "data": "qdistro.tier2.work" },
      "uid": { "type": "i", "data": 1000 } } ]] });
  const wrapped = PermissionsLogic.parseListRules(rawWrapped);
  assert.strictEqual(wrapped.length, 1);
  assert.strictEqual(wrapped[0].name, "w1");
  assert.strictEqual(wrapped[0].decision, "allow");
  assert.strictEqual(wrapped[0].app_id, "qdistro.tier2.work");
  assert.strictEqual(wrapped[0].uid, 1000);
  // a value that is unexpectedly a nested container coerces to "" (never
  // "[object Object]").
  const nested = PermissionsLogic.parseListRules(JSON.stringify({ "type": "aa{sv}",
    "data": [[ { "name": { "type": "as", "data": ["a", "b"] } } ]] }));
  assert.strictEqual(nested[0].name, "");
})();

// ── ruleAppliesToWindow: app_id / engine match semantics ──
(function testRuleApplies() {
  const win = { secctxAppId: "qdistro.tier2.work", sandboxEngine: "qdistro.tier2" };
  // exact app_id match -> applies (identity-scoped)
  assert.strictEqual(PermissionsLogic.ruleAppliesToWindow(
    { app_id: "qdistro.tier2.work" }, win, false), true);
  // exact engine match (no app_id) -> applies (identity-scoped)
  assert.strictEqual(PermissionsLogic.ruleAppliesToWindow(
    { sandbox_engine: "qdistro.tier2" }, win, false), true);
  // different app_id -> excluded
  assert.strictEqual(PermissionsLogic.ruleAppliesToWindow(
    { app_id: "qdistro.tier2.other" }, win, false), false);
  // different engine -> excluded
  assert.strictEqual(PermissionsLogic.ruleAppliesToWindow(
    { sandbox_engine: "qdistro.tier5" }, win, false), false);
  // global default (no app_id, no engine): excluded unless includeGlobal
  assert.strictEqual(PermissionsLogic.ruleAppliesToWindow({}, win, false), false);
  assert.strictEqual(PermissionsLogic.ruleAppliesToWindow({}, win, true), true);
  // a rule that constrains app_id to this window but engine to a DIFFERENT
  // value is excluded (both constrained fields must be compatible).
  assert.strictEqual(PermissionsLogic.ruleAppliesToWindow(
    { app_id: "qdistro.tier2.work", sandbox_engine: "qdistro.tier5" }, win, false), false);
})();

// ── buildPermissionsMenu: scoped vs global partition, none case ──
(function testBuildPermissionsMenu() {
  const win = { secctxAppId: "qdistro.tier2.work", sandboxEngine: "qdistro.tier2" };
  const rules = [
    { name: "scoped-allow", decision: "allow", app_id: "qdistro.tier2.work",
      action: "fs.read", rationale: "read project files" },
    { name: "scoped-deny", decision: "deny", sandbox_engine: "qdistro.tier2",
      action: "usb.attach" },
    { name: "global", decision: "deny", action: "net.raw" },        // global default
    { name: "other-silo", decision: "allow", app_id: "qdistro.tier2.other",
      action: "fs.read" },                                          // excluded
  ];
  const items = PermissionsLogic.buildPermissionsMenu(rules, win);
  const actions = items.map(function (i) { return i.action; });
  // header, two scoped rules, a global sub-header, one global rule
  assert.strictEqual(actions[0], "qd-perm-header");
  const scoped = items.filter(function (i) { return i.action === "qd-perm-rule"; });
  assert.strictEqual(scoped.length, 2);
  const globalHdr = items.find(function (i) { return i.action === "qd-perm-global-header"; });
  assert.notStrictEqual(globalHdr, undefined);
  const globalRows = items.filter(function (i) { return i.action === "qd-perm-global"; });
  assert.strictEqual(globalRows.length, 1);
  // the other-silo rule never appears
  assert.ok(items.every(function (i) { return (i.ruleName || "") !== "other-silo"; }));
  // every row is read-only (disabled) and tagged
  items.forEach(function (i) {
    assert.strictEqual(i.enabled, false);
    assert.strictEqual(i.isQdistro, true);
  });
  // allow/deny rows carry the right icon + labelKey + interpolation params
  const allowRow = scoped.find(function (i) { return i.ruleName === "scoped-allow"; });
  assert.strictEqual(allowRow.icon, "check");
  assert.strictEqual(allowRow.labelKey, "bar.taskbar.permissions.rule-allow");
  assert.strictEqual(allowRow.labelParams.action, "fs.read");
  assert.strictEqual(allowRow.rationale, "read project files");
  const denyRow = scoped.find(function (i) { return i.ruleName === "scoped-deny"; });
  assert.strictEqual(denyRow.icon, "x");
  assert.strictEqual(denyRow.labelKey, "bar.taskbar.permissions.rule-deny");
  assert.strictEqual(denyRow.labelParams.action, "usb.attach");
  // a rule with NO action falls back to the "any action" placeholder label.
  const anyAction = PermissionsLogic.buildPermissionsMenu(
    [{ name: "any", decision: "allow", app_id: "qdistro.tier2.work" }], win)
    .find(function (i) { return i.action === "qd-perm-rule"; });
  assert.strictEqual(anyAction.labelParams.action, "any action");
})();

(function testBuildPermissionsMenu_none() {
  const win = { secctxAppId: "qdistro.tier2.work", sandboxEngine: "qdistro.tier2" };
  // only a rule for a different silo -> nothing applies
  const items = PermissionsLogic.buildPermissionsMenu(
    [{ name: "x", decision: "allow", app_id: "qdistro.tier2.other", action: "fs.read" }], win);
  const actions = items.map(function (i) { return i.action; });
  assert.deepStrictEqual(actions, ["qd-perm-header", "qd-perm-none"]);
  items.forEach(function (i) { assert.strictEqual(i.enabled, false); });
})();

console.log("taskbar-permissions: all assertions passed");
