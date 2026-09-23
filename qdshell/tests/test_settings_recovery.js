const assert = require("assert");
const R = require("../Services/Qdshell/SettingsRecovery.js");

// Covers settings SCHEMA-MIGRATION / MALFORMED-CONFIG-RECOVERY / widget-prune
// logic that Commons/Settings.qml delegates to. The live config load can only
// be exercised on a VM (FileView + JsonObject adapter); this pins the recovery
// RULES on the host:
//   - a truncated / hand-mangled settings.json must fall back to defaults,
//     never throw at startup (or the user gets no shell config at all);
//   - a stale persisted bar/control-center/desktop widget id must be dropped
//     (or the shell tries to build a component that no longer exists);
//   - upgradeWidget must strip deprecated keys + inject new defaults, never
//     touching `id`.

// ── parseConfig: never throws; corruption -> null ──
(function testParseConfig() {
  assert.deepStrictEqual(R.parseConfig('{"a":1}'), { a: 1 });
  assert.deepStrictEqual(R.parseConfig('  {"a":1}\n'), { a: 1 }, "leading/trailing ws tolerated");
  // ensures: a truncated write fails safe to null (caller -> defaults), no throw
  assert.strictEqual(R.parseConfig('{"a":1'), null, "truncated json -> null");
  assert.strictEqual(R.parseConfig('{bad json}'), null, "garbage -> null");
  assert.strictEqual(R.parseConfig(''), null, "empty -> null");
  assert.strictEqual(R.parseConfig('   '), null, "whitespace-only -> null");
  // a non-object top level is corruption for a settings root
  assert.strictEqual(R.parseConfig('[1,2,3]'), null, "top-level array -> null");
  assert.strictEqual(R.parseConfig('42'), null, "top-level number -> null");
  assert.strictEqual(R.parseConfig('"x"'), null, "top-level string -> null");
  assert.strictEqual(R.parseConfig('null'), null, "literal null -> null");
  assert.strictEqual(R.parseConfig(undefined), null);
  assert.strictEqual(R.parseConfig(null), null);
})();

// ── mergeDefaults: every default key present; user wins; objects recurse ──
(function testMergeDefaults() {
  const defaults = {
    bar: { position: "top", density: "default", opacity: 0.93 },
    ui: { fontDefault: "Inter" },
    list: [1, 2, 3],
  };
  const user = {
    bar: { position: "bottom" },        // partial override; density/opacity inherited
    extra: { mine: true },              // user-only key preserved
  };
  const out = R.mergeDefaults(defaults, user);
  assert.strictEqual(out.bar.position, "bottom", "user value wins");
  assert.strictEqual(out.bar.density, "default", "missing user key inherits default");
  assert.strictEqual(out.bar.opacity, 0.93, "missing user key inherits default");
  assert.strictEqual(out.ui.fontDefault, "Inter", "whole missing object inherited");
  assert.deepStrictEqual(out.list, [1, 2, 3], "arrays inherited whole");
  assert.deepStrictEqual(out.extra, { mine: true }, "user-only key preserved");

  // user array replaces default array wholesale (no element merge)
  const out2 = R.mergeDefaults({ list: [1, 2, 3] }, { list: [9] });
  assert.deepStrictEqual(out2.list, [9]);

  // A persisted null for an object section is corruption, not a user override.
  // This is the exact shape that broke launcher clicks: appLauncher was null,
  // so Settings.data.appLauncher.overviewLayer threw before the panel opened.
  const repairedSections = R.mergeDefaults({
    appLauncher: { overviewLayer: false, position: "center" },
    audio: { volumeStep: 5 },
    widgets: [{ id: "Launcher" }],
  }, {
    appLauncher: null,
    audio: null,
    widgets: null,
  });
  assert.deepStrictEqual(repairedSections.appLauncher, { overviewLayer: false, position: "center" });
  assert.deepStrictEqual(repairedSections.audio, { volumeStep: 5 });
  assert.deepStrictEqual(repairedSections.widgets, [{ id: "Launcher" }]);

  // missing-object inherit is a CLONE (mutating result must not poison defaults)
  out.ui.fontDefault = "Mono";
  assert.strictEqual(defaults.ui.fontDefault, "Inter", "merge result is decoupled from defaults");

  // empty/garbage user -> clone of defaults
  assert.deepStrictEqual(R.mergeDefaults(defaults, null).bar.position, "top");
  assert.deepStrictEqual(R.mergeDefaults(defaults, "nope").bar.position, "top");
})();

// ── recoverConfig: end-to-end recovery decision ──
(function testRecoverConfig() {
  const defaults = { bar: { position: "top" }, version: 1 };
  // ensures: malformed config recovers to defaults and flags recovered=true
  const broken = R.recoverConfig('{"bar":', defaults);
  assert.strictEqual(broken.recovered, true);
  assert.strictEqual(broken.reason, "malformed-or-missing");
  assert.strictEqual(broken.data.bar.position, "top");
  // the recovered data is a clone, not the defaults object itself
  broken.data.bar.position = "left";
  assert.strictEqual(defaults.bar.position, "top");

  // missing file (null/empty) also recovers to defaults
  assert.strictEqual(R.recoverConfig("", defaults).recovered, true);
  assert.strictEqual(R.recoverConfig(null, defaults).recovered, true);

  // a good (partial) config merges over defaults, recovered=false
  const ok = R.recoverConfig('{"bar":{"position":"bottom"}}', defaults);
  assert.strictEqual(ok.recovered, false);
  assert.strictEqual(ok.data.bar.position, "bottom");
  assert.strictEqual(ok.data.version, 1, "default-only key still present after merge");
})();

// ── classifyVersion: fresh-v1 baseline + downgrade protection ──
(function testClassifyVersion() {
  assert.strictEqual(R.classifyVersion(1, 1), "current");
  assert.strictEqual(R.classifyVersion(0, 1), "upgrade", "unstamped legacy blob -> upgrade");
  assert.strictEqual(R.classifyVersion(undefined, 1), "upgrade", "missing version treated as 0");
  assert.strictEqual(R.classifyVersion("garbage", 1), "upgrade", "non-numeric -> 0 -> upgrade");
  // ensures: a config from a NEWER qdshell is classified downgrade so we don't destroy it
  assert.strictEqual(R.classifyVersion(5, 1), "downgrade");
})();

// ── pruneUnknownWidgets: drop stale/corrupt rows, keep order ──
(function testPrune() {
  const known = new Set(["clock", "battery", "tray"]);
  const isKnown = (id) => known.has(id);
  const widgets = [
    { id: "clock" },
    { id: "obsolete_widget", foo: 1 },   // stale id -> dropped
    { id: "battery" },
    { id: "tray" },
    "not-an-object",                      // corrupt -> dropped
    { noId: true },                       // missing id -> dropped
  ];
  const res = R.pruneUnknownWidgets(widgets, isKnown);
  assert.deepStrictEqual(res.widgets.map(w => w.id), ["clock", "battery", "tray"]);
  assert.strictEqual(res.removed, 3, "obsolete + non-object + missing-id all dropped");
  // empty / garbage input
  assert.deepStrictEqual(R.pruneUnknownWidgets(null, isKnown), { widgets: [], removed: 0 });
  // all-known -> nothing removed, same order
  const allKnown = R.pruneUnknownWidgets([{ id: "tray" }, { id: "clock" }], isKnown);
  assert.strictEqual(allKnown.removed, 0);
  assert.deepStrictEqual(allKnown.widgets.map(w => w.id), ["tray", "clock"]);
})();

// ── upgradeWidget: strip deprecated keys, inject defaults, never touch id ──
(function testUpgradeWidget() {
  // metadata = the registry default schema for this widget id
  const metadata = { showLabel: true, format: "HH:mm", color: "auto" };

  // widget carries a deprecated key (oldOption) and is missing one default (color)
  const widget = { id: "clock", showLabel: false, format: "HH:mm:ss", oldOption: "x" };
  const res = R.upgradeWidget(widget, metadata);
  assert.strictEqual(res.changed, true, "deprecated removed + default injected => changed");
  assert.strictEqual(widget.id, "clock", "id never altered");
  assert.strictEqual(widget.showLabel, false, "user value preserved");
  assert.strictEqual(widget.format, "HH:mm:ss", "user value preserved");
  assert.strictEqual(widget.color, "auto", "missing default injected");
  assert.ok(!("oldOption" in widget), "deprecated key stripped");

  // a widget already matching schema => no change
  const clean = { id: "clock", showLabel: true, format: "HH:mm", color: "auto" };
  assert.strictEqual(R.upgradeWidget(clean, metadata).changed, false, "no-op when already current");

  // unknown widget (no metadata) => left untouched, not changed
  const unknown = { id: "mystery", a: 1 };
  const u = R.upgradeWidget(unknown, undefined);
  assert.strictEqual(u.changed, false);
  assert.deepStrictEqual(unknown, { id: "mystery", a: 1 }, "no schema -> leave as-is");

  // injecting a falsy default (false/0/"") still counts and is applied
  const w2 = { id: "x" };
  const r2 = R.upgradeWidget(w2, { enabled: false, count: 0, label: "" });
  assert.strictEqual(r2.changed, true);
  assert.strictEqual(w2.enabled, false);
  assert.strictEqual(w2.count, 0);
  assert.strictEqual(w2.label, "");
})();

// ── F12: mergeDefaults must not let a user-supplied __proto__ key pollute the
// result's prototype (JSON.parse exposes __proto__ as an OWN property). ──
(function testProtoPollution() {
  const defaults = { a: 1 };
  // Build a user object with __proto__ as a real own enumerable property, the
  // way JSON.parse('{"__proto__":{...}}') produces it.
  const user = JSON.parse('{"a":2,"__proto__":{"polluted":true},"b":3}');
  const out = R.mergeDefaults(defaults, user);
  // user-only key b is preserved...
  assert.strictEqual(out.b, 3, "user-only key preserved");
  // ...but __proto__ did not reparent `out` and did not pollute Object.prototype
  assert.strictEqual(out.polluted, undefined, "no polluted key visible on result");
  assert.strictEqual(({}).polluted, undefined, "Object.prototype not polluted");
  assert.strictEqual(Object.getPrototypeOf(out), Object.prototype, "result prototype intact");
  // constructor / prototype keys are likewise not copied through
  const user2 = JSON.parse('{"constructor":"x","prototype":"y","c":4}');
  const out2 = R.mergeDefaults({}, user2);
  assert.strictEqual(out2.c, 4);
  assert.strictEqual(typeof out2.constructor, "function", "constructor untouched");
})();

console.log("settings-recovery: all assertions passed");
