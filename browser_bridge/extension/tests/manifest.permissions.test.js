// Bundled browser_bridge manifest permission pins.
//
// Run with plain node:
//   node browser_bridge/extension/tests/manifest.permissions.test.js
//
// These assertions keep the bundled qdistro extension aligned with the
// frozen v1 operation set. The extension does not use activeTab or
// webNavigation; host grants are explicit and optional permission buckets
// must not become an unwatched widening path.

const fs = require("fs");
const path = require("path");
const assert = require("assert");

const ROOT = path.join(__dirname, "..");

function readManifest(name) {
  return JSON.parse(fs.readFileSync(path.join(ROOT, name), "utf8"));
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function assertNoOptionalBuckets(manifest, label) {
  for (const key of ["optional_permissions", "optional_host_permissions"]) {
    assert.deepStrictEqual(asArray(manifest[key]), [],
      label + ": " + key + " must be absent or empty");
  }
}

function assertAbsentPermissions(manifest, label) {
  const permissions = asArray(manifest.permissions);
  assert.ok(!permissions.includes("activeTab"),
    label + ": activeTab must not be declared");
  assert.ok(!permissions.includes("webNavigation"),
    label + ": webNavigation must not be declared");
}

{
  const manifest = readManifest("manifest.chromium.json");
  assert.strictEqual(manifest.manifest_version, 3,
    "chromium manifest stays MV3");
  assert.deepStrictEqual(manifest.permissions, [
    "nativeMessaging",
    "tabs",
    "scripting",
    "cookies",
  ], "chromium permissions are pinned to the used API set");
  assert.deepStrictEqual(manifest.host_permissions, ["<all_urls>"],
    "chromium host_permissions intentionally grants all http/https pages");
  assertAbsentPermissions(manifest, "chromium");
  assertNoOptionalBuckets(manifest, "chromium");
}

{
  const manifest = readManifest("manifest.firefox.json");
  assert.strictEqual(manifest.manifest_version, 2,
    "firefox manifest stays MV2");
  assert.deepStrictEqual(manifest.permissions, [
    "nativeMessaging",
    "tabs",
    "cookies",
    "contextualIdentities",
    "<all_urls>",
  ], "firefox permissions are pinned to the used API and host grant set");
  assert.deepStrictEqual(asArray(manifest.host_permissions), [],
    "firefox MV2 host grants stay in permissions, not host_permissions");
  assertAbsentPermissions(manifest, "firefox");
  assertNoOptionalBuckets(manifest, "firefox");
}

console.log("OK manifest.permissions.test.js: all assertions passed");
