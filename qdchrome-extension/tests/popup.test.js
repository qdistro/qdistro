/** @vitest-environment jsdom */
// popup.js — toolbar action UI. No native-messaging port lives here;
// the popup drives the background service worker via runtime.sendMessage.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

const ROOT = resolve(__dirname, "..", "src");
const POPUP_HTML = readFileSync(resolve(ROOT, "popup.html"), "utf8");
const POPUP_JS_PATH = resolve(ROOT, "popup.js");
const POPUP_JS = readFileSync(POPUP_JS_PATH, "utf8");

function makeFakeChrome() {
  const sent = [];
  const tabsQ = { result: [{ id: 7, url: "https://example.com/" }] };
  let nextReply = { ok: true, connected: true };
  return {
    sent,
    setReply(r) { nextReply = r; },
    setTabs(arr) { tabsQ.result = arr; },
    chrome: {
      runtime: {
        sendMessage(msg, cb) {
          sent.push(msg);
          // status query has its own reply shape; pass-through otherwise.
          if (msg.kind === "status") {
            cb({ ok: true, connected: true });
          } else {
            cb(nextReply);
          }
        },
        openOptionsPage: vi.fn(),
      },
      tabs: {
        query(_q, cb) { cb(tabsQ.result); },
      },
    },
  };
}

async function loadPopup(env) {
  // Replace the body — popup.js binds listeners at top level, so the
  // DOM has to exist before we eval.
  const bodyMatch = POPUP_HTML.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  document.body.innerHTML = bodyMatch ? bodyMatch[1] : POPUP_HTML;
  globalThis.chrome = env.chrome;
  // popup.js references `browser` lazily via typeof; we don't set it.
  // Compile directly into the current realm (no parsingContext) so
  // document.getElementById resolves to jsdom's document, AND pass the real
  // on-disk `filename` so V8 coverage attributes lines to src/popup.js (a bare
  // `new Function` produces an anonymous, URL-less script -> 0% coverage).
  vm.compileFunction(POPUP_JS, [], { filename: POPUP_JS_PATH })();
  // refreshStatus() runs at module load; drain a microtask so its
  // assertion fires before the test asserts.
  await Promise.resolve();
  await Promise.resolve();
}

describe("popup.js", () => {
  let env;

  beforeEach(() => {
    env = makeFakeChrome();
  });

  afterEach(() => {
    delete globalThis.chrome;
    document.body.innerHTML = "";
  });

  it("queries status on load and shows 'connected'", async () => {
    await loadPopup(env);
    expect(env.sent[0]).toEqual({ kind: "status" });
    expect(document.getElementById("status").textContent).toBe("connected");
    expect(document.getElementById("status").className).toBe("ok");
  });

  it("shows 'disconnected' when background reports !connected", async () => {
    env.chrome.runtime.sendMessage = (msg, cb) => {
      env.sent.push(msg);
      cb({ ok: true, connected: false });
    };
    await loadPopup(env);
    expect(document.getElementById("status").textContent).toBe("disconnected");
    expect(document.getElementById("status").className).toBe("err");
  });

  it("ping button sends kind:'ping' and renders the response into #out", async () => {
    env.setReply({ ok: true, response: { hello: "world" } });
    await loadPopup(env);
    // First call was the load-time status query; clear so we can
    // assert the ping was the next call.
    const before = env.sent.length;
    document.getElementById("ping").click();
    await vi.waitFor(() => {
      if (!env.sent.slice(before).find((m) => m.kind === "ping")) {
        throw new Error("ping not yet sent");
      }
    }, { timeout: 1000 });
    const out = document.getElementById("out").textContent;
    expect(out).toContain('"hello": "world"');
  });

  it("cookies-export button looks up the active tab URL and forwards it", async () => {
    env.setReply({ ok: true, audit_id: "ax-1" });
    await loadPopup(env);
    document.getElementById("cookies-export").click();
    await vi.waitFor(() => {
      if (!env.sent.find((m) => m.kind === "cookies.export")) {
        throw new Error("cookies.export not yet sent");
      }
    }, { timeout: 1000 });
    const frame = env.sent.find((m) => m.kind === "cookies.export");
    expect(frame.url).toBe("https://example.com/");
    expect(document.getElementById("out").textContent).toContain('"audit_id"');
  });

  it("cookies-export with no active tab renders no_active_tab without sending", async () => {
    env.setTabs([]);
    await loadPopup(env);
    document.getElementById("cookies-export").click();
    await vi.waitFor(() => {
      if (!document.getElementById("out").textContent.includes("no_active_tab")) {
        throw new Error("not yet rendered");
      }
    }, { timeout: 1000 });
    expect(env.sent.find((m) => m.kind === "cookies.export")).toBeUndefined();
  });

  it("settings link opens the options page", async () => {
    await loadPopup(env);
    document.getElementById("open-options").click();
    expect(env.chrome.runtime.openOptionsPage).toHaveBeenCalled();
  });

  it("treats a null sendMessage response as no_response", async () => {
    env.chrome.runtime.sendMessage = (msg, cb) => {
      env.sent.push(msg);
      cb(null);
    };
    await loadPopup(env);
    expect(document.getElementById("status").textContent).toBe("disconnected");
  });
});
