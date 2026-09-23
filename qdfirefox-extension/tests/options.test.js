/** @vitest-environment jsdom */
// options.js — settings page. Reads/writes browser.storage.local
// (Promise API). Module list includes 'containers' for the
// Firefox-only contextual-identities track.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

const ROOT = resolve(__dirname, "..", "src");
const OPTIONS_HTML = readFileSync(resolve(ROOT, "options.html"), "utf8");
const OPTIONS_JS_PATH = resolve(ROOT, "options.js");
const OPTIONS_JS = readFileSync(OPTIONS_JS_PATH, "utf8");

const MODULES = [
  "tabs", "pwd", "pageExtract", "cookies", "containers",
  "mpris", "downloads", "notifications", "screenlock",
];

function makeFakeBrowser(initial = {}) {
  const stored = { ...initial };
  const setFn = vi.fn(async (value) => { Object.assign(stored, value); });
  return {
    stored,
    setFn,
    browser: {
      storage: {
        local: {
          get: async (keys) => {
            const out = {};
            for (const k of keys) if (k in stored) out[k] = stored[k];
            return out;
          },
          set: setFn,
        },
      },
    },
  };
}

async function loadOptions(env) {
  const bodyMatch = OPTIONS_HTML.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  document.body.innerHTML = bodyMatch ? bodyMatch[1] : OPTIONS_HTML;
  globalThis.browser = env.browser;
  // Compile with the real on-disk `filename` (vs `new Function`'s anonymous,
  // URL-less script) so V8 coverage attributes lines to src/options.js.
  // No parsingContext => current (jsdom) context, so document stays available.
  vm.compileFunction(OPTIONS_JS, [], { filename: OPTIONS_JS_PATH })();
  // load() runs at module load (async; awaits browser.storage.local.get).
  await new Promise((r) => setTimeout(r, 0));
}

describe("options.js (Firefox)", () => {
  let env;
  beforeEach(() => { env = makeFakeBrowser(); });
  afterEach(() => {
    delete globalThis.browser;
    document.body.innerHTML = "";
  });

  it("leaves checkbox defaults intact when storage is empty", async () => {
    const bodyMatch = OPTIONS_HTML.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
    document.body.innerHTML = bodyMatch ? bodyMatch[1] : OPTIONS_HTML;
    const defaults = {};
    for (const m of MODULES) {
      const el = document.getElementById(`mod-${m}`);
      defaults[m] = el ? el.checked : null;
    }
    document.body.innerHTML = "";
    await loadOptions(env);
    for (const m of MODULES) {
      const el = document.getElementById(`mod-${m}`);
      expect(el.checked).toBe(defaults[m]);
    }
  });

  it("includes containers in the module list (Firefox-specific)", async () => {
    await loadOptions(env);
    expect(document.getElementById("mod-containers")).toBeTruthy();
  });

  it("reflects stored module flags on the checkboxes", async () => {
    env = makeFakeBrowser({
      modules: { tabs: false, containers: false, mpris: true },
    });
    await loadOptions(env);
    expect(document.getElementById("mod-tabs").checked).toBe(false);
    expect(document.getElementById("mod-containers").checked).toBe(false);
    expect(document.getElementById("mod-mpris").checked).toBe(true);
  });

  it("seeds the origin allowlist textarea from storage", async () => {
    env = makeFakeBrowser({
      origin_allowlist: ["https://example.com", "https://internal.corp"],
    });
    await loadOptions(env);
    expect(document.getElementById("origin-allowlist").value)
      .toBe("https://example.com\nhttps://internal.corp");
  });

  it("save writes the current checkbox state to storage.local", async () => {
    await loadOptions(env);
    document.getElementById("mod-tabs").checked = false;
    document.getElementById("mod-containers").checked = false;
    document.getElementById("save").click();
    await vi.waitFor(() => {
      if (env.setFn.mock.calls.length === 0) throw new Error("set pending");
    }, { timeout: 1000 });
    const [value] = env.setFn.mock.calls[0];
    expect(value.modules.tabs).toBe(false);
    expect(value.modules.containers).toBe(false);
    expect(value.modules.pwd).toBe(true);
  });

  it("save splits the allowlist textarea into a trimmed non-empty array", async () => {
    await loadOptions(env);
    document.getElementById("origin-allowlist").value =
      "https://a.example\n  https://b.example  \n\n\nhttps://c.example\n";
    document.getElementById("save").click();
    await vi.waitFor(() => {
      if (env.setFn.mock.calls.length === 0) throw new Error("set pending");
    }, { timeout: 1000 });
    const [value] = env.setFn.mock.calls[0];
    expect(value.origin_allowlist).toEqual([
      "https://a.example", "https://b.example", "https://c.example",
    ]);
  });

  it("save flashes the 'Saved.' indicator and hides it after the timeout", async () => {
    await loadOptions(env);
    const saved = document.getElementById("saved");
    expect(saved.style.display).toBe("");
    document.getElementById("save").click();
    await vi.waitFor(() => {
      if (saved.style.display !== "inline") throw new Error("not flashed yet");
    }, { timeout: 1000 });
    // Fake the 1500ms timeout; vi.advanceTimersByTime doesn't work
    // when save() used real setTimeout. The hide path uses setTimeout
    // on the real timer, so just wait.
    await vi.waitFor(() => {
      if (saved.style.display !== "none") throw new Error("not hidden yet");
    }, { timeout: 2000, interval: 50 });
  });
});
