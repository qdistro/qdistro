const assert = require("assert");
const TKI = require("../Services/System/TrayKnownItems.js");

// ── merge a brand-new seen item ──
(function testMergeNew() {
  const out = TKI.mergeSeenItem([], { "id": "nm-applet", "title": "Network" }, TKI.POLICY_DEFAULT);
  assert.strictEqual(out.length, 1);
  assert.strictEqual(out[0].id, "nm-applet");
  assert.strictEqual(out[0].title, "Network");
  assert.strictEqual(out[0].policy, TKI.POLICY_DEFAULT);
})();

// ── merge a duplicate id: no new entry, policy preserved, title refreshed ──
(function testMergeDuplicate() {
  let list = TKI.mergeSeenItem([], { "id": "blueman", "title": "Bluetooth" }, TKI.POLICY_DEFAULT);
  list = TKI.setPolicy(list, "blueman", TKI.POLICY_HIDE);
  // Seen again with a changed title — must NOT duplicate, must keep the HIDE policy.
  const out = TKI.mergeSeenItem(list, { "id": "blueman", "title": "Bluetooth Manager" }, TKI.POLICY_DEFAULT);
  assert.strictEqual(out.length, 1, "duplicate id must not add a second entry");
  assert.strictEqual(out[0].title, "Bluetooth Manager", "title should refresh");
  assert.strictEqual(out[0].policy, TKI.POLICY_HIDE, "user policy must be preserved across re-seen");
})();

// ── merge a duplicate id where the new title is empty: keep the old title ──
(function testMergeDuplicateEmptyTitle() {
  let list = TKI.mergeSeenItem([], { "id": "x", "title": "Original" }, TKI.POLICY_DEFAULT);
  const out = TKI.mergeSeenItem(list, { "id": "x", "title": "" }, TKI.POLICY_DEFAULT);
  assert.strictEqual(out.length, 1);
  assert.strictEqual(out[0].title, "Original");
})();

// ── unidentifiable item (no id, no title) is never tracked ──
(function testMergeUnidentifiable() {
  const out = TKI.mergeSeenItem([], { "id": "", "title": "" }, TKI.POLICY_DEFAULT);
  assert.strictEqual(out.length, 0);
})();

// ── falls back to title as the key when no id is present ──
(function testKeyFallback() {
  assert.strictEqual(TKI.itemKey({ "id": "stable-id", "title": "T" }), "stable-id");
  assert.strictEqual(TKI.itemKey({ "id": "", "title": "OnlyTitle" }), "OnlyTitle");
  assert.strictEqual(TKI.itemKey({}), "");
  assert.strictEqual(TKI.itemKey(null), "");
})();

// ── effective visibility from policy (show / hide / default) ──
(function testEffectiveVisible() {
  // show overrides a "would be hidden" default.
  assert.strictEqual(TKI.effectiveVisible(TKI.POLICY_SHOW, false), true);
  // hide overrides a "would be shown" default.
  assert.strictEqual(TKI.effectiveVisible(TKI.POLICY_HIDE, true), false);
  // default defers to the normal-filtering result.
  assert.strictEqual(TKI.effectiveVisible(TKI.POLICY_DEFAULT, true), true);
  assert.strictEqual(TKI.effectiveVisible(TKI.POLICY_DEFAULT, false), false);
  // unknown policy is treated as default.
  assert.strictEqual(TKI.effectiveVisible("garbage", true), true);
  assert.strictEqual(TKI.effectiveVisible("garbage", false), false);
})();

// ── setPolicy / policyForId round-trip and unknown-id no-op ──
(function testSetPolicy() {
  let list = TKI.mergeSeenItem([], { "id": "a", "title": "A" }, TKI.POLICY_DEFAULT);
  list = TKI.setPolicy(list, "a", TKI.POLICY_SHOW);
  assert.strictEqual(TKI.policyForId(list, "a"), TKI.POLICY_SHOW);
  // unknown id -> no-op, list unchanged length, lookup returns default.
  const out = TKI.setPolicy(list, "missing", TKI.POLICY_HIDE);
  assert.strictEqual(out.length, 1);
  assert.strictEqual(TKI.policyForId(out, "missing"), TKI.POLICY_DEFAULT);
})();

// ── policy sanitization ──
(function testSanitizePolicy() {
  assert.strictEqual(TKI.sanitizePolicy(TKI.POLICY_SHOW), TKI.POLICY_SHOW);
  assert.strictEqual(TKI.sanitizePolicy("nonsense"), TKI.POLICY_DEFAULT);
  assert.strictEqual(TKI.sanitizePolicy(undefined), TKI.POLICY_DEFAULT);
})();

// ── normalizeList dedups a list that already contains duplicate ids ──
(function testNormalizeDedup() {
  const dirty = [
    { "id": "dup", "title": "first", "policy": "show" },
    { "id": "", "title": "no-id-dropped", "policy": "hide" },
    { "id": "dup", "title": "second", "policy": "hide" },
  ];
  const out = TKI.normalizeList(dirty);
  assert.strictEqual(out.length, 1);
  assert.strictEqual(out[0].id, "dup");
  assert.strictEqual(out[0].title, "second"); // last write wins
  assert.strictEqual(out[0].policy, "hide");
})();

// ── reset clears the list ──
(function testReset() {
  let list = TKI.mergeSeenItem([], { "id": "a", "title": "A" }, TKI.POLICY_DEFAULT);
  list = TKI.mergeSeenItem(list, { "id": "b", "title": "B" }, TKI.POLICY_DEFAULT);
  assert.strictEqual(list.length, 2);
  const cleared = TKI.reset();
  assert.deepStrictEqual(cleared, []);
})();

// ── INJECTION SAFETY: a malicious id/title is stored verbatim as inert data,
// never interpreted or used to build a command/regex ──
(function testInjectionSafety() {
  const evilId = "; rm -rf ~";
  const evilTitle = "$(reboot)`whoami`";
  let list = TKI.mergeSeenItem([], { "id": evilId, "title": evilTitle }, TKI.POLICY_DEFAULT);
  // Stored opaquely and recoverable byte-for-byte (not expanded/escaped/mangled).
  assert.strictEqual(list.length, 1);
  assert.strictEqual(list[0].id, evilId);
  assert.strictEqual(list[0].title, evilTitle);
  // Keyed by the exact malicious string — re-seeing it dedups, it is NOT a
  // command and produces no second entry.
  list = TKI.mergeSeenItem(list, { "id": evilId, "title": evilTitle }, TKI.POLICY_DEFAULT);
  assert.strictEqual(list.length, 1, "malicious id treated as an opaque key, not executed");
  // Policy operations key off the exact string too.
  list = TKI.setPolicy(list, evilId, TKI.POLICY_HIDE);
  assert.strictEqual(TKI.policyForId(list, evilId), TKI.POLICY_HIDE);
  assert.strictEqual(TKI.effectiveVisible(TKI.policyForId(list, evilId), true), false);
  // The module exposes no command-building/exec surface — purely data.
  const apiKeys = Object.keys(TKI);
  apiKeys.forEach(function (k) {
    assert.ok(!/exec|spawn|shell|command|run/i.test(k), "no command surface: " + k);
  });
})();

console.log("tray-known-items: all assertions passed");
