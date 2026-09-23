// manifest.chromium.json shape tests + Firefox-canonicalization guard.
//
// This repo is Chromium-only. The Firefox extension comes from its own
// sources — ../qdfirefox-extension (standalone; the maintained one v1
// users load) and ../browser_bridge/extension (bundled; a LEGACY
// compatibility artifact with no origin allowlist, J11) — NOT built here. A
// legacy Firefox MV2 target used to be emitted from this repo under
// gecko id `qdistro@qdistro.local`, colliding with the bundled
// extension's id (two distinct codebases, same id). The guard suite
// below keeps that target from being reintroduced.
//
// The chromium tests pin invariants the build script would silently
// break: which content scripts inject into iframes (pwd-content needs
// all_frames:true to reach federated SSO iframes; mpris/screenlock must
// stay top-frame-only).
import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";

function load(name) {
  return JSON.parse(
    readFileSync(resolve(__dirname, "..", name), "utf8")
  );
}

function assertSplit(manifest) {
  const cs = manifest.content_scripts;
  expect(Array.isArray(cs)).toBe(true);
  expect(cs).toHaveLength(2);

  const pwd = cs.find((e) => e.js.some((p) => p.endsWith("pwd-content.js")));
  expect(pwd).toBeTruthy();
  expect(pwd.all_frames).toBe(true);
  expect(pwd.js).toEqual(["src/content/pwd-content.js"]);

  const others = cs.find((e) => e.js.some((p) => p.endsWith("mpris-content.js")));
  expect(others).toBeTruthy();
  expect(others.all_frames).toBe(false);
  expect(others.js).toContain("src/content/mpris-content.js");
  expect(others.js).toContain("src/content/screenlock-content.js");
  expect(others.js).not.toContain("src/content/pwd-content.js");
}

describe("manifest.chromium.json content_scripts", () => {
  it("splits pwd-content (all_frames:true) from mpris/screenlock", () => {
    assertSplit(load("manifest.chromium.json"));
  });
});

// P0-5 (S8) — minimal-permission pin. With the v1 bridge op set frozen
// (D5: ping + the existing Phase-8 / intent-token / 9e-relay handlers; no
// new ops), the manifest must declare EXACTLY the permissions the ops this
// extension actually SERVICES use — no broader. Each permission below maps
// to a registered handler (see src/modules/*):
//   nativeMessaging → the bridge port (all ops)
//   tabs            → tabs.list/open/close (activeTab can't see other windows)
//   cookies         → cookies.export
//   downloads       → downloads.notify
//   notifications   → notifications.show
//   contextMenus    → the page.extract right-click entry (pageExtract.js)
//   scripting       → on-demand page.extract injection (scripting.executeScript);
//                     pwd-fill uses the static all_frames content script, not this
//   storage         → options-page module/origin gate persistence
// Not every D5 op is serviced HERE: `clipboard.set` is handled native-side
// by the bridge daemon (no clipboard handler or clipboard permission exists
// in this extension — a content-script clipboard write needs no manifest
// permission anyway), and the pwd/mpris/screenlock relays ride the kept
// permissions above. host_permissions <all_urls> already covers
// content-script injection + cookies + scripting.executeScript, so
// `activeTab` adds nothing, and no module registers a navigation listener,
// so `webNavigation` is dead — both are asserted ABSENT so they can't
// silently creep back.
describe("manifest.chromium.json permissions (P0-5 minimal set)", () => {
  const m = load("manifest.chromium.json");
  it("declares nativeMessaging", () => {
    expect(m.permissions).toContain("nativeMessaging");
  });
  it("declares scripting (MV3 on-demand injection)", () => {
    expect(m.permissions).toContain("scripting");
  });
  it("MV3 host_permissions covers all urls", () => {
    expect(m.host_permissions).toContain("<all_urls>");
  });
  it("does NOT declare activeTab (redundant with <all_urls> + tabs)", () => {
    expect(m.permissions).not.toContain("activeTab");
  });
  it("does NOT declare webNavigation (no navigation listener in src)", () => {
    expect(m.permissions).not.toContain("webNavigation");
  });
  // host_permissions is itself a capability surface; pin it exactly and pin
  // the optional buckets empty so the minimal set can't be widened via a
  // door the permissions-array assertions don't watch.
  it("host_permissions is exactly [<all_urls>] — no extra hosts", () => {
    expect(m.host_permissions).toEqual(["<all_urls>"]);
  });
  it("declares no optional_permissions / optional_host_permissions", () => {
    expect(m.optional_permissions ?? []).toEqual([]);
    expect(m.optional_host_permissions ?? []).toEqual([]);
  });
});

// P04 fix-pass S4 (test-integrity): closed-set assertions so a
// future commit silently adding a broad permission fails the test.
// Tightened under P0-5 — activeTab + webNavigation dropped.
describe("chrome extension manifest — closed permission set", () => {
  const chromiumExpected = new Set([
    "nativeMessaging",
    "tabs",
    "cookies",
    "downloads",
    "notifications",
    "contextMenus",
    "scripting",
    "storage",
  ]);

  it("manifest.chromium.json permissions are exactly the expected set", () => {
    const m = load("manifest.chromium.json");
    const actual = new Set(m.permissions || []);
    for (const p of actual) {
      expect(
        chromiumExpected.has(p),
        `unexpected permission ${p} in chromium manifest — update the allowlist after security review`,
      ).toBe(true);
    }
    for (const p of chromiumExpected) {
      expect(actual.has(p), `missing permission ${p}`).toBe(true);
    }
  });
});

// ---- Firefox-canonicalization guard -------------------------------
// Keeps the removed Firefox MV2 target from being reintroduced. The
// Firefox extension lives elsewhere (qdfirefox-extension for v1;
// qdistro browser_bridge/extension as the legacy artifact); building one here under
// `qdistro@qdistro.local` re-creates the id-collision drift trap.
describe("Firefox build target is not shipped from this repo", () => {
  it("manifest.firefox.json is absent", () => {
    expect(existsSync(resolve(__dirname, "..", "manifest.firefox.json")))
      .toBe(false);
  });

  it("build-extension.sh emits no Firefox output", () => {
    const sh = readFileSync(
      resolve(__dirname, "..", "scripts", "build-extension.sh"), "utf8");
    // Strip comment lines so the explanatory header (which names the
    // removed artifacts) does not trip the guard; only live build
    // statements should be checked.
    const code = sh
      .split("\n")
      .filter((line) => !line.trimStart().startsWith("#"))
      .join("\n");
    for (const needle of [
      "firefox.xpi",
      "background.bundle.js",
      "manifest.firefox.json",
      "dist/firefox",
      "$DIST/firefox",
    ]) {
      expect(
        code.includes(needle),
        `build-extension.sh still references ${needle} — this repo is Chromium-only`,
      ).toBe(false);
    }
  });
});
