/** @vitest-environment jsdom */
// options.js — settings page. Reads/writes chrome.storage.local
// for per-module enabled flags + an origin allowlist.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

const ROOT = resolve(__dirname, "..", "src");
const OPTIONS_HTML = readFileSync(resolve(ROOT, "options.html"), "utf8");
const OPTIONS_JS_PATH = resolve(ROOT, "options.js");
const OPTIONS_JS = readFileSync(OPTIONS_JS_PATH, "utf8");

const MODULES = [
  "tabs", "pwd", "pageExtract", "cookies",
  "mpris", "downloads", "notifications", "screenlock",
];

function makeFakeChrome(initial = {}) {
  const stored = { ...initial };
  return {
    stored,
    setCalls: [],
    chrome: {
      storage: {
        local: {
          get(keys, cb) {
            const out = {};
            for (const k of keys) {
              if (k in stored) out[k] = stored[k];
            }
            cb(out);
          },
          set: vi.fn(function (value, cb) {
            Object.assign(stored, value);
            if (cb) cb();
          }),
        },
      },
    },
  };
}

async function loadOptions(env) {
  const bodyMatch = OPTIONS_HTML.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  document.body.innerHTML = bodyMatch ? bodyMatch[1] : OPTIONS_HTML;
  globalThis.chrome = env.chrome;
  // Compile with the real on-disk `filename` (vs `new Function`'s anonymous,
  // URL-less script) so V8 coverage attributes lines to src/options.js.
  // No parsingContext => current (jsdom) context, so document stays available.
  vm.compileFunction(OPTIONS_JS, [], { filename: OPTIONS_JS_PATH })();
  // load() runs at module load.
  await Promise.resolve();
}

describe("options.js", () => {
  let env;

  beforeEach(() => {
    env = makeFakeChrome();
  });

  afterEach(() => {
    delete globalThis.chrome;
    document.body.innerHTML = "";
  });

  it("leaves checkbox defaults intact when storage is empty", async () => {
    // The HTML markup is the source of truth for first-run defaults.
    // Capture them BEFORE load() runs.
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

  it("reflects stored module flags on the checkboxes", async () => {
    env = makeFakeChrome({
      modules: { tabs: false, pwd: true, mpris: true },
    });
    await loadOptions(env);
    expect(document.getElementById("mod-tabs").checked).toBe(false);
    expect(document.getElementById("mod-pwd").checked).toBe(true);
    expect(document.getElementById("mod-mpris").checked).toBe(true);
  });

  it("seeds the origin allowlist textarea from storage", async () => {
    env = makeFakeChrome({
      origin_allowlist: ["https://example.com", "https://internal.corp"],
    });
    await loadOptions(env);
    const ta = document.getElementById("origin-allowlist");
    expect(ta.value).toBe("https://example.com\nhttps://internal.corp");
  });

  it("save writes the current checkbox state to storage.local", async () => {
    await loadOptions(env);
    document.getElementById("mod-tabs").checked = false;
    document.getElementById("mod-mpris").checked = true;
    document.getElementById("save").click();
    expect(env.chrome.storage.local.set).toHaveBeenCalled();
    const [value] = env.chrome.storage.local.set.mock.calls[0];
    expect(value.modules.tabs).toBe(false);
    expect(value.modules.mpris).toBe(true);
  });

  it("save splits the allowlist textarea into a trimmed non-empty array", async () => {
    await loadOptions(env);
    document.getElementById("origin-allowlist").value =
      "https://a.example\n  https://b.example  \n\n\nhttps://c.example\n";
    document.getElementById("save").click();
    const [value] = env.chrome.storage.local.set.mock.calls[0];
    expect(value.origin_allowlist).toEqual([
      "https://a.example", "https://b.example", "https://c.example",
    ]);
  });

  it("save flashes the 'Saved.' indicator and hides it after the timeout", async () => {
    vi.useFakeTimers();
    try {
      await loadOptions(env);
      const saved = document.getElementById("saved");
      // Initial display:none comes from the <style> block, not from
      // inline style, so style.display is empty pre-click.
      expect(saved.style.display).toBe("");
      document.getElementById("save").click();
      expect(saved.style.display).toBe("inline");
      vi.advanceTimersByTime(1600);
      expect(saved.style.display).toBe("none");
    } finally {
      vi.useRealTimers();
    }
  });
});
