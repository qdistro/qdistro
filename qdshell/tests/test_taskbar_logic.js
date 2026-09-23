const assert = require("assert");
const TaskbarLogic = require("../Modules/Bar/Widgets/TaskbarLogic.js");

// Build a plain "running window" entry like updateCombinedModel() produces.
// `win` carries the window-object stand-in (identity + isFocused).
function runEntry(id, appId, title, isFocused) {
  return {
    "id": id,
    "type": "running",
    "window": { "id": id, "isFocused": !!isFocused },
    "appId": appId,
    "title": title,
  };
}

// A pinned-not-running / placeholder entry (window === null) is never grouped.
function pinnedEntry(id, appId, title) {
  return { "id": id, "type": "pinned", "window": null, "appId": appId, "title": title };
}

function ids(entries) {
  return entries.map(function (e) { return e.id; });
}

function noDuplicateIds(entries) {
  const seen = new Set();
  entries.forEach(function (e) {
    assert.ok(!seen.has(e.id), "duplicate entry id in output: " + e.id);
    seen.add(e.id);
  });
}

// ── grouping "never": every window stays its own button, order preserved ──
(function testGroupingNever() {
  const wins = [
    runEntry("a1", "firefox", "Firefox 1"),
    runEntry("a2", "firefox", "Firefox 2"),
    runEntry("b1", "kitty", "kitty"),
  ];
  const grouping = TaskbarLogic.shouldGroup(wins.length, { "groupingMode": "never" });
  assert.strictEqual(grouping, false);
  // "never" -> caller does not group; the model equals the input order.
  assert.deepStrictEqual(ids(wins), ["a1", "a2", "b1"]);
})();

// ── grouping "always": same appId collapses into one group with the right
// member count; empty/undefined appId windows are NOT grouped together ──
(function testGroupingAlways() {
  const wins = [
    runEntry("a1", "firefox", "Firefox 1"),
    runEntry("a2", "firefox", "Firefox 2", true),
    runEntry("b1", "kitty", "kitty"),
    runEntry("a3", "firefox", "Firefox 3"),
  ];
  assert.strictEqual(TaskbarLogic.shouldGroup(wins.length, { "groupingMode": "always" }), true);

  const out = TaskbarLogic.groupApps(wins);
  // firefox group emitted once at the slot of its first member, then kitty.
  assert.strictEqual(out.length, 2);
  noDuplicateIds(out);

  const ff = out[0];
  assert.strictEqual(ff.isGroup, true);
  assert.strictEqual(ff.appId, "firefox");
  assert.strictEqual(ff.windows.length, 3, "firefox group should have 3 members");
  assert.strictEqual(ff.windowEntries.length, 3);
  // Representative title/window comes from the focused member (a2).
  assert.strictEqual(ff.title, "Firefox 2");
  assert.strictEqual(ff.window.id, "a2");

  // kitty stays an individual entry (single window collapses back to plain).
  assert.strictEqual(out[1].id, "b1");
  assert.strictEqual(out[1].isGroup, undefined);
})();

// ── empty / undefined appId guard: such windows must NEVER be grouped
// together — each stays a separate button ──
(function testEmptyAppIdNotGrouped() {
  const wins = [
    runEntry("e1", "", "Untitled A"),
    runEntry("e2", undefined, "Untitled B"),
    runEntry("e3", "   ", "Whitespace"),   // normalizes to "" too
    runEntry("ff1", "firefox", "Firefox 1"),
    runEntry("ff2", "firefox", "Firefox 2"),
  ];
  const out = TaskbarLogic.groupApps(wins);
  noDuplicateIds(out);
  // e1, e2, e3 each stay their own button; firefox collapses to one group
  // (a multi-window group carries the synthetic id "group:firefox").
  assert.deepStrictEqual(ids(out), ["e1", "e2", "e3", "group:firefox"]);
  // None of the empty-appId entries became a group.
  ["e1", "e2", "e3"].forEach(function (id) {
    const e = out.find(function (x) { return x.id === id; });
    assert.notStrictEqual(e, undefined);
    assert.notStrictEqual(e.isGroup, true, id + " must not be a group");
  });
  // The single firefox group carries both members.
  const ff = out.find(function (x) { return x.isGroup === true; });
  assert.strictEqual(ff.windows.length, 2);
})();

// ── pinned-not-running / placeholder entries pass through ungrouped and keep
// their slot ──
(function testNonRunningPassThrough() {
  const wins = [
    runEntry("ff1", "firefox", "Firefox 1"),
    pinnedEntry("pin-kitty", "kitty", "kitty"),
    runEntry("ff2", "firefox", "Firefox 2"),
  ];
  const out = TaskbarLogic.groupApps(wins);
  noDuplicateIds(out);
  // firefox group emitted at the first firefox slot; pinned keeps its slot.
  assert.strictEqual(out.length, 2);
  assert.strictEqual(out[0].isGroup, true);
  assert.strictEqual(out[0].appId, "firefox");
  assert.strictEqual(out[1].id, "pin-kitty");
  assert.strictEqual(out[1].isGroup, undefined);
})();

// ── grouping "limited": groups only when the width budget is exceeded; else
// behaves like "never" ──
(function testGroupingLimited() {
  // 5 entries, budget fits ~3 (perEntry without titles = itemSize + marginXL).
  const opts = {
    "groupingMode": "limited",
    "isVerticalBar": false,
    "maxTaskbarWidth": 300,
    "showTitle": false,
    "itemSize": 40,
    "marginXL": 60,   // perEntry = 100 -> fits floor(300/100) = 3
    "marginS": 0,
    "titleWidth": 0,
  };
  // Under budget (3 entries) -> no grouping.
  assert.strictEqual(TaskbarLogic.shouldGroup(3, opts), false);
  // Over budget (5 entries) -> grouping.
  assert.strictEqual(TaskbarLogic.shouldGroup(5, opts), true);

  // Vertical bar or no width cap -> never groups in limited mode.
  assert.strictEqual(TaskbarLogic.shouldGroup(50, Object.assign({}, opts, { "isVerticalBar": true })), false);
  assert.strictEqual(TaskbarLogic.shouldGroup(50, Object.assign({}, opts, { "maxTaskbarWidth": 0 })), false);

  // When titles are shown, per-entry width includes spacing + titleWidth.
  const optsTitled = Object.assign({}, opts, { "showTitle": true, "marginS": 4, "titleWidth": 56 });
  // perEntry = 40 + 4 + 56 + 60 = 160 -> fits floor(300/160) = 1.
  assert.strictEqual(TaskbarLogic.shouldGroup(1, optsTitled), false);
  assert.strictEqual(TaskbarLogic.shouldGroup(2, optsTitled), true);
})();

// ── sort "none": preserves launch/stable order (returns entries unchanged) ──
(function testSortNone() {
  const wins = [
    runEntry("z", "zed", "Zed"),
    runEntry("a", "alpha", "Alpha"),
    runEntry("m", "mid", "Mid"),
  ];
  const out = TaskbarLogic.applySortMode(wins, "none");
  assert.deepStrictEqual(ids(out), ["z", "a", "m"]);
  noDuplicateIds(out);
})();

// ── sort "title": orders by window title (case-insensitive) ──
(function testSortTitle() {
  const wins = [
    runEntry("z", "zed", "Zebra"),
    runEntry("a", "alpha", "apple"),
    runEntry("m", "mid", "Mango"),
  ];
  const out = TaskbarLogic.applySortMode(wins, "title");
  assert.deepStrictEqual(out.map(function (e) { return e.title; }), ["apple", "Mango", "Zebra"]);
  noDuplicateIds(out);
  // Non-mutating: input order preserved.
  assert.deepStrictEqual(ids(wins), ["z", "a", "m"]);
})();

// ── sort "group": orders by appId, then keeps members together (then title) ──
(function testSortGroup() {
  const wins = [
    runEntry("k1", "kitty", "kitty B"),
    runEntry("f1", "firefox", "Firefox 2"),
    runEntry("k2", "kitty", "kitty A"),
    runEntry("f2", "firefox", "Firefox 1"),
  ];
  const out = TaskbarLogic.applySortMode(wins, "group");
  noDuplicateIds(out);
  // firefox before kitty; within each app, members are adjacent and
  // ordered by title.
  assert.deepStrictEqual(out.map(function (e) { return e.appId; }), ["firefox", "firefox", "kitty", "kitty"]);
  assert.deepStrictEqual(out.map(function (e) { return e.title; }), ["Firefox 1", "Firefox 2", "kitty A", "kitty B"]);
})();

// ── single ordered pass with no duplicated entries (regression guard) ──
(function testNoDuplicationRegression() {
  const wins = [
    runEntry("a1", "firefox", "Firefox 1"),
    runEntry("b1", "kitty", "kitty 1"),
    runEntry("a2", "firefox", "Firefox 2"),
    runEntry("b2", "kitty", "kitty 2"),
    runEntry("a3", "firefox", "Firefox 3"),
  ];
  const out = TaskbarLogic.groupApps(wins);
  noDuplicateIds(out);
  // Two groups only, emitted at the slot of each group's first member.
  assert.strictEqual(out.length, 2);
  assert.strictEqual(out[0].appId, "firefox");
  assert.strictEqual(out[1].appId, "kitty");
  // Total members across the output equals the input running-window count.
  const totalMembers = out.reduce(function (n, e) {
    return n + (e.isGroup ? e.windows.length : 1);
  }, 0);
  assert.strictEqual(totalMembers, wins.length);
})();

// ── prototype-pollution guard: app ids that collide with Object.prototype
// member names (toString / __proto__ / hasOwnProperty) must group like any
// other key, not corrupt or suppress emission ──
(function testPrototypePollutionKeys() {
  const wins = [
    runEntry("p1", "__proto__", "Proto 1"),
    runEntry("p2", "__proto__", "Proto 2"),
    runEntry("t1", "toString", "ToStr 1"),
    runEntry("t2", "toString", "ToStr 2"),
    runEntry("h1", "hasOwnProperty", "HasOwn 1"),
  ];
  const out = TaskbarLogic.groupApps(wins);
  noDuplicateIds(out);
  // __proto__ and toString each collapse to a 2-member group; hasOwnProperty
  // stays a single plain entry. Order preserved at first-member slots.
  assert.strictEqual(out.length, 3);
  assert.strictEqual(out[0].appId, "__proto__");
  assert.strictEqual(out[0].isGroup, true);
  assert.strictEqual(out[0].windows.length, 2);
  assert.strictEqual(out[1].appId, "toString");
  assert.strictEqual(out[1].isGroup, true);
  assert.strictEqual(out[1].windows.length, 2);
  assert.strictEqual(out[2].id, "h1");
  assert.notStrictEqual(out[2].isGroup, true);
})();

// ── normalizeAppId guard behavior ──
(function testNormalizeAppId() {
  assert.strictEqual(TaskbarLogic.normalizeAppId(undefined), "");
  assert.strictEqual(TaskbarLogic.normalizeAppId(null), "");
  assert.strictEqual(TaskbarLogic.normalizeAppId(""), "");
  assert.strictEqual(TaskbarLogic.normalizeAppId(123), "");
  assert.strictEqual(TaskbarLogic.normalizeAppId("  Firefox  "), "firefox");
})();

// ── qdistro isolation menu (D16 v1) ──
(function testSiloTierKey() {
  // The stable, language-independent enum the QML side maps to an I18n key.
  assert.strictEqual(
    TaskbarLogic.siloTierKey("qdistro.disp.deadbeef", "qdistro.tier2"), "disposable");
  assert.strictEqual(TaskbarLogic.siloTierKey("qdistro.tier5.work-vm", ""), "tier5");
  assert.strictEqual(TaskbarLogic.siloTierKey("qdistro.tier4.work-vm", ""), "tier4");
  assert.strictEqual(TaskbarLogic.siloTierKey("qdistro.tier3.dev", ""), "tier3");
  assert.strictEqual(
    TaskbarLogic.siloTierKey("c1/weston-terminal", "qdistro.tier2"), "tier2");
  assert.strictEqual(TaskbarLogic.siloTierKey("", ""), "native");
  // disposable wins over the tier2 engine it rides on
  assert.strictEqual(
    TaskbarLogic.siloTierKey("qdistro.disp.abc", "qdistro.tier2"), "disposable");
})();

(function testSnapshotConfigForWindow() {
  // Persistent tier-2: "tier2/<name>" -> Snapper config <name>.
  assert.deepStrictEqual(
    TaskbarLogic.snapshotConfigForWindow({
      secctxAppId: "c1/weston-terminal", sandboxEngine: "qdistro.tier2", silo: "tier2/c1" }),
    { snapshottable: true, config: "c1" });
  // Disposable: never snapshottable (ephemeral home), even on the tier2 engine.
  assert.deepStrictEqual(
    TaskbarLogic.snapshotConfigForWindow({
      secctxAppId: "qdistro.disp.deadbeef", sandboxEngine: "qdistro.tier2",
      silo: "tier2/qdistro.disp.deadbeef" }),
    { snapshottable: false, config: "" });
  // VM tiers: not a host Snapper config — not offered.
  assert.deepStrictEqual(
    TaskbarLogic.snapshotConfigForWindow({
      secctxAppId: "qdistro.tier5.work-vm", sandboxEngine: "qdistro.tier5", silo: "vm-work-vm" }),
    { snapshottable: false, config: "" });
  assert.deepStrictEqual(
    TaskbarLogic.snapshotConfigForWindow({
      secctxAppId: "qdistro.tier3.dev", sandboxEngine: "qdistro.tier3", silo: "dev" }),
    { snapshottable: false, config: "" });
  // Native: no identity -> not snapshottable.
  assert.deepStrictEqual(
    TaskbarLogic.snapshotConfigForWindow({}), { snapshottable: false, config: "" });
  // A tier-2 silo without the "tier2/" prefix (e.g. an unexpected label) is
  // NOT snapshottable — we never guess a bare name.
  assert.deepStrictEqual(
    TaskbarLogic.snapshotConfigForWindow({
      secctxAppId: "c1/x", sandboxEngine: "qdistro.tier2", silo: "dev" }),
    { snapshottable: false, config: "" });
  // A residual '/' after stripping (e.g. nested label) is rejected.
  assert.deepStrictEqual(
    TaskbarLogic.snapshotConfigForWindow({
      secctxAppId: "c1/x", sandboxEngine: "qdistro.tier2", silo: "tier2/a/b" }),
    { snapshottable: false, config: "" });
  // A config name that fails the strict shape (leading '-', odd chars) is
  // rejected so it can never reach the broker argv.
  assert.deepStrictEqual(
    TaskbarLogic.snapshotConfigForWindow({
      secctxAppId: "c1/x", sandboxEngine: "qdistro.tier2", silo: "tier2/-evil" }),
    { snapshottable: false, config: "" });
})();

(function testSiloTierLabel() {
  assert.strictEqual(
    TaskbarLogic.siloTierLabel("qdistro.disp.deadbeef", "qdistro.tier2"),
    "disposable (tier 2)");
  assert.strictEqual(
    TaskbarLogic.siloTierLabel("qdistro.tier5.work-vm", ""), "tier 5 (VM)");
  assert.strictEqual(
    TaskbarLogic.siloTierLabel("qdistro.tier4.work-vm", ""), "tier 4 (VM)");
  assert.strictEqual(
    TaskbarLogic.siloTierLabel("qdistro.tier3.dev", ""), "tier 3 (VM app)");
  assert.strictEqual(
    TaskbarLogic.siloTierLabel("c1/weston-terminal", "qdistro.tier2"),
    "tier 2 (container)");
  assert.strictEqual(TaskbarLogic.siloTierLabel("", ""), "native (tier 0/1)");
  // disposable wins over the tier2 engine it rides on
  assert.strictEqual(
    TaskbarLogic.siloTierLabel("qdistro.disp.abc", "qdistro.tier2"),
    "disposable (tier 2)");
})();

(function testIsDisposableWindow() {
  // The authoritative signal is the secctx app_id, not the silo name.
  assert.strictEqual(
    TaskbarLogic.isDisposableWindow({ secctxAppId: "qdistro.disp.abc123" }), true);
  assert.strictEqual(
    TaskbarLogic.isDisposableWindow({ secctxAppId: "qdistro.tier2", silo: "work" }), false);
  // A persistent silo merely NAMED disp-... is NOT disposable (no false
  // positive off the silo name).
  assert.strictEqual(
    TaskbarLogic.isDisposableWindow({ secctxAppId: "qdistro.tier2", silo: "disp-trap" }), false);
  assert.strictEqual(TaskbarLogic.isDisposableWindow(null), false);
  assert.strictEqual(TaskbarLogic.isDisposableWindow({}), false);
})();

(function testDisposeWindowPlan() {
  // Disposable + well-formed launch token in instanceId -> tear down by token.
  const tok = "0123456789abcdef0123456789abcdef";
  assert.deepStrictEqual(
    TaskbarLogic.disposeWindowPlan({ secctxAppId: "qdistro.disp.SECRET", instanceId: tok }),
    { dispose: true, byToken: true, token: tok });
  // Disposable but NO instanceId on the wire (untagged spawn) -> window-close
  // fallback, never a token call.
  assert.deepStrictEqual(
    TaskbarLogic.disposeWindowPlan({ secctxAppId: "qdistro.disp.SECRET", instanceId: "" }),
    { dispose: true, byToken: false, token: "" });
  // Malformed/injection-ish instanceId -> NOT used as a token (fallback).
  assert.deepStrictEqual(
    TaskbarLogic.disposeWindowPlan({ secctxAppId: "qdistro.disp.x", instanceId: "-rm; reboot" }),
    { dispose: true, byToken: false, token: "" });
  // Uppercase hex is rejected (the session manager's _TOKEN_RE is lowercase).
  assert.deepStrictEqual(
    TaskbarLogic.disposeWindowPlan({ secctxAppId: "qdistro.disp.x", instanceId: "ABCDEF0123456789" }),
    { dispose: true, byToken: false, token: "" });
  // The token comes from instanceId, NOT the (independent) secctx app_id hex.
  const plan = TaskbarLogic.disposeWindowPlan({ secctxAppId: "qdistro.disp.deadbeefcafe", instanceId: tok });
  assert.strictEqual(plan.token, tok);
  // A non-disposable window is never disposed.
  assert.deepStrictEqual(
    TaskbarLogic.disposeWindowPlan({ secctxAppId: "qdistro.tier2", instanceId: tok }),
    { dispose: false, byToken: false, token: "" });
  assert.deepStrictEqual(
    TaskbarLogic.disposeWindowPlan(null),
    { dispose: false, byToken: false, token: "" });
})();

(function testBuildIsolationMenu_native_empty() {
  // A native window (no secctx identity) gets NO qdistro section.
  assert.deepStrictEqual(TaskbarLogic.buildIsolationMenuItems({}), []);
  assert.deepStrictEqual(
    TaskbarLogic.buildIsolationMenuItems({ secctxAppId: "", sandboxEngine: "", silo: "" }), []);
})();

(function testBuildIsolationMenu_tier2_noDispose() {
  // A persistent tier-2 silo's derived string is "tier2/<name>" (see
  // ClipboardSilo.fromSecctx) — that is what reaches buildIsolationMenuItems.
  const items = TaskbarLogic.buildIsolationMenuItems({
    secctxAppId: "c1/weston-terminal", sandboxEngine: "qdistro.tier2", silo: "tier2/dev"
  });
  const actions = items.map(function (i) { return i.action; });
  // identity rows + snapshot + permissions, but NO dispose for a persistent silo
  assert.ok(actions.indexOf("qd-snapshot") !== -1);
  assert.ok(actions.indexOf("qd-permissions") !== -1);
  assert.strictEqual(actions.indexOf("qd-dispose"), -1);
  // the snapshot row carries the RESOLVED Snapper config (tier2/ stripped).
  const snapRow = items.find(function (i) { return i.action === "qd-snapshot"; });
  assert.strictEqual(snapRow.snapConfig, "dev");
  // identity rows are disabled + carry the tier + silo
  const tierRow = items.find(function (i) { return i.action === "qd-id-tier"; });
  assert.strictEqual(tierRow.enabled, false);
  assert.ok(tierRow.label.indexOf("tier 2") !== -1);
  // i18n: rows carry a labelKey; the tier row also carries the tierKey enum.
  assert.strictEqual(tierRow.labelKey, "bar.taskbar.isolation.tier");
  assert.strictEqual(tierRow.tierKey, "tier2");
  const siloRow = items.find(function (i) { return i.action === "qd-id-silo"; });
  assert.ok(siloRow.label.indexOf("dev") !== -1);
  assert.strictEqual(siloRow.labelParams.silo, "tier2/dev");
  // every row is tagged so the QML/menu can distinguish the qdistro section
  items.forEach(function (i) { assert.strictEqual(i.isQdistro, true); });
})();

(function testBuildIsolationMenu_disposable_hasDispose_noSnapshot() {
  const items = TaskbarLogic.buildIsolationMenuItems({
    secctxAppId: "qdistro.disp.deadbeef", sandboxEngine: "qdistro.tier2",
    silo: "tier2/qdistro.disp.deadbeef", instanceId: "0123456789abcdef"
  });
  const actions = items.map(function (i) { return i.action; });
  assert.ok(actions.indexOf("qd-dispose") !== -1);   // dispose IS shown
  // Snapshot is HIDDEN for a disposable: its home is ephemeral (tmpfs/--rm),
  // so a host Snapper snapshot is a category error.
  assert.strictEqual(actions.indexOf("qd-snapshot"), -1);
  assert.ok(actions.indexOf("qd-permissions") !== -1);
  const tierRow = items.find(function (i) { return i.action === "qd-id-tier"; });
  assert.ok(tierRow.label.indexOf("disposable") !== -1);
  assert.strictEqual(tierRow.tierKey, "disposable");
  // the secctx context row is present and shows the app_id
  const ctxRow = items.find(function (i) { return i.action === "qd-id-secctx"; });
  assert.ok(ctxRow.label.indexOf("qdistro.disp.deadbeef") !== -1);
})();

(function testBuildIsolationMenu_vmTier_noSnapshot() {
  // VM tiers get no host Snapper snapshot item (their snapshot story is
  // VM-disk, not a host Snapper config). Identity rows + permissions remain.
  const items = TaskbarLogic.buildIsolationMenuItems({
    secctxAppId: "qdistro.tier5.work-vm", sandboxEngine: "qdistro.tier5", silo: "vm-work-vm"
  });
  const actions = items.map(function (i) { return i.action; });
  assert.strictEqual(actions.indexOf("qd-snapshot"), -1);
  assert.strictEqual(actions.indexOf("qd-dispose"), -1);
  assert.ok(actions.indexOf("qd-permissions") !== -1);
})();

console.log("taskbar-logic: all assertions passed");
