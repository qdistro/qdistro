// api.js tests — the Firefox-native global-selection branch.
//
// src/api.js binds to `browser` when present, and only falls back when
// it is not:
//
//   if (typeof browser === "undefined") {
//     root.qdistroApi = (typeof chrome !== "undefined") ? chrome : null;
//     return;
//   }
//   root.qdistroApi = browser;
//
// The shared tests/helpers.js evals api.js with `browser` injected as a
// function parameter, which masks the `typeof browser === "undefined"`
// lookup — so the fallback branch is never exercised by the rest of the
// suite. Here we run the actual source in a fresh `vm` context where we
// control which globals exist, and drive every branch.
//
// ensures: on real Firefox (the `browser` global exists) the shim binds
// to the Promise-returning `browser.*`; if the same source is ever run
// on a chrome-only worker it degrades to `chrome` rather than crashing,
// and to `null` when neither surface exists.
import { describe, it, expect } from "vitest";
import vm from "node:vm";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const API_SRC = fs.readFileSync(
  path.resolve(__dirname, "..", "src", "api.js"), "utf8");

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

describe("api.js surface selection (Firefox repo)", () => {
  it("browser-present scope (real Firefox): binds qdistroApi to browser", () => {
    const api = runApi({ withBrowser: true, withChrome: true });
    expect(api).toBeTruthy();
    expect(api.__surface).toBe("browser");
  });

  it("browser-present even when chrome is absent: still binds to browser", () => {
    const api = runApi({ withBrowser: true, withChrome: false });
    expect(api.__surface).toBe("browser");
  });

  it("no browser, chrome-present (Chromium fallback): binds to chrome", () => {
    const api = runApi({ withBrowser: false, withChrome: true });
    expect(api).toBeTruthy();
    expect(api.__surface).toBe("chrome");
  });

  it("neither surface present: binds qdistroApi to null (no hard crash)", () => {
    const api = runApi({ withBrowser: false, withChrome: false });
    expect(api).toBeNull();
  });
});
