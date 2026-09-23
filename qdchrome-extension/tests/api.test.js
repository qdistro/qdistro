// api.js tests — the cross-browser global-selection branch.
//
// src/api.js picks the WebExtension API surface at module load:
//
//   const api = (typeof browser !== "undefined") ? browser : chrome;
//   root.qdistroApi = api;
//
// The shared tests/helpers.js evals api.js with `chrome` injected as a
// function parameter, which masks BOTH the `typeof browser` and the
// `typeof chrome` global lookups — so neither real branch is exercised
// by the rest of the suite. Here we run the actual source in a fresh
// `vm` context where we control which globals exist, and assert the
// selection lands on the right object each way.
//
// ensures: on a real Chromium worker (no `browser` global) the shim
// binds to `chrome`; if a future build runs the same source on Firefox
// (where `browser` exists) it prefers `browser` — so the bridge keeps
// working on both targets.
import { describe, it, expect } from "vitest";
import vm from "node:vm";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const API_SRC = fs.readFileSync(
  path.resolve(__dirname, "..", "src", "api.js"), "utf8");

// Run the real api.js inside a context whose globals we control.
// `root` is the worker scope (the IIFE attaches qdistroApi to it).
function runApi({ withBrowser, withChrome }) {
  const root = {};
  const ctx = { self: root };
  if (withBrowser) ctx.browser = { __surface: "browser" };
  if (withChrome) ctx.chrome = { __surface: "chrome" };
  vm.createContext(ctx);
  vm.runInContext(API_SRC, ctx, {
    filename: path.resolve(__dirname, "..", "src", "api.js"),
  });
  return root.qdistroApi;
}

describe("api.js surface selection (Chromium repo)", () => {
  it("chrome-only scope (real Chromium): binds qdistroApi to chrome", () => {
    const api = runApi({ withBrowser: false, withChrome: true });
    expect(api).toBeTruthy();
    expect(api.__surface).toBe("chrome");
  });

  it("browser-present scope (Firefox-style): prefers browser over chrome", () => {
    const api = runApi({ withBrowser: true, withChrome: true });
    expect(api).toBeTruthy();
    expect(api.__surface).toBe("browser");
  });

  it("browser-present with no chrome at all: still binds to browser", () => {
    const api = runApi({ withBrowser: true, withChrome: false });
    expect(api.__surface).toBe("browser");
  });
});
