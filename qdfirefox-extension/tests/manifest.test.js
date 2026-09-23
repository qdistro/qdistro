// manifest.json shape tests. Pin invariants that the build script
// would silently break: which content scripts inject into iframes,
// background module order, host permissions.
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const manifest = JSON.parse(
  readFileSync(resolve(__dirname, "..", "manifest.json"), "utf8")
);

describe("manifest.json content_scripts", () => {
  it("splits pwd-content (all_frames:true) from mpris/screenlock (top frame only)", () => {
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
  });
});

// P0-5 (S8) — minimal-permission pin. With the v1 bridge op set frozen
// (D5: ping + the existing Phase-8 / intent-token / 9e-relay handlers plus
// the Firefox-only containers.* relay; no new ops), the manifest must
// declare EXACTLY the permissions the ops this extension actually SERVICES
// use. Each maps to a registered handler (see src/modules/*):
//   nativeMessaging      → the bridge port (all ops)
//   tabs                 → tabs.list/open/close (activeTab can't see other windows)
//   cookies              → cookies.export
//   downloads            → downloads.notify
//   notifications        → notifications.show
//   contextMenus         → the page.extract right-click entry (pageExtract.js)
//   contextualIdentities → containers.list/create/remove (Firefox containers)
//   scripting            → on-demand page.extract injection (scripting.executeScript);
//                          pwd-fill uses the static all_frames content script, not this
//   storage              → options-page module/origin gate persistence
// Not every D5 op is serviced HERE: `clipboard.set` is handled native-side
// by the bridge daemon (no clipboard handler or clipboard permission exists
// in this extension — a content-script clipboard write needs no manifest
// permission anyway), and the pwd/mpris/screenlock relays ride the kept
// permissions above. host_permissions <all_urls> already covers
// content-script injection + cookies + scripting.executeScript, so
// `activeTab` adds nothing, and no module registers a navigation listener,
// so `webNavigation` is dead — both are asserted ABSENT so they can't
// silently creep back.
describe("manifest.json permissions (P0-5 minimal set)", () => {
  it("declares nativeMessaging for the bridge port", () => {
    expect(manifest.permissions).toContain("nativeMessaging");
  });

  it("declares scripting for on-demand page.extract injection", () => {
    expect(manifest.permissions).toContain("scripting");
  });

  it("does NOT declare webNavigation (no navigation listener in src)", () => {
    expect(manifest.permissions).not.toContain("webNavigation");
  });

  it("does NOT declare activeTab (redundant with <all_urls> + tabs)", () => {
    expect(manifest.permissions).not.toContain("activeTab");
  });

  it("keeps contextualIdentities (Firefox containers)", () => {
    expect(manifest.permissions).toContain("contextualIdentities");
  });

  it("MV3 host_permissions covers all urls", () => {
    expect(manifest.host_permissions).toContain("<all_urls>");
  });

  // host_permissions is itself a capability surface; pin it exactly and pin
  // the optional buckets empty so the minimal set can't be widened via a
  // door the permissions-array assertions don't watch.
  it("host_permissions is exactly [<all_urls>] — no extra hosts", () => {
    expect(manifest.host_permissions).toEqual(["<all_urls>"]);
  });

  it("declares no optional_permissions / optional_host_permissions", () => {
    expect(manifest.optional_permissions ?? []).toEqual([]);
    expect(manifest.optional_host_permissions ?? []).toEqual([]);
  });

  // ensures: the Firefox add-on id stays pinned. AMO signs against a
  // fixed gecko id; if a build silently drops or rewrites it the
  // signed update breaks (and a colliding id would clash with the
  // bundled qdistro browser_bridge extension). The qdchrome repo
  // guards against gecko-id *reintroduction*; here we guard the
  // opposite direction — that the pin is present and exact.
  it("pins browser_specific_settings.gecko.id", () => {
    expect(manifest.browser_specific_settings?.gecko?.id)
      .toBe("qdistro-firefox@qdistro.local");
  });

  // P04 fix-pass S4 (test-integrity): closed-set assertion so a
  // future commit silently adding ``management`` / ``proxy`` /
  // ``bookmarks`` etc. fails the test. The full set of acceptable
  // permissions for this extension is pinned here. New permissions
  // require updating this allowlist + a security review.
  it("permissions set is closed — no silently-added permissions", () => {
    const expected = new Set([
      "nativeMessaging",
      "tabs",
      "cookies",
      "downloads",
      "notifications",
      "contextMenus",
      "contextualIdentities",
      "scripting",
      "storage",
    ]);
    const actual = new Set(manifest.permissions || []);
    for (const p of actual) {
      expect(
        expected.has(p),
        `unexpected permission ${p} — update the closed-set allowlist after security review`,
      ).toBe(true);
    }
    // And confirm every expected permission is present, so a future
    // commit also can't silently DROP a load-bearing one.
    for (const p of expected) {
      expect(actual.has(p), `missing permission ${p}`).toBe(true);
    }
  });
});
