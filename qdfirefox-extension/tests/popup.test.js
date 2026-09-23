/** @vitest-environment jsdom */
// popup.js — toolbar action UI. No native-messaging port lives here;
// the popup drives the background event page via browser.runtime
// .sendMessage (Promise API, no callback shim).
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

const ROOT = resolve(__dirname, "..", "src");
const POPUP_HTML = readFileSync(resolve(ROOT, "popup.html"), "utf8");
const POPUP_JS_PATH = resolve(ROOT, "popup.js");
const POPUP_JS = readFileSync(POPUP_JS_PATH, "utf8");

function makeFakeBrowser() {
  const sent = [];
  let tabsResult = [{ id: 7, url: "https://example.com/" }];
  let containersResult = [
    { name: "Personal", color: "blue", cookieStoreId: "firefox-container-1" },
    { name: "Work",     color: "red",  cookieStoreId: "firefox-container-2" },
  ];
  let nextReply = { ok: true, connected: true };

  return {
    sent,
    setReply(r) { nextReply = r; },
    setTabs(arr) { tabsResult = arr; },
    setContainers(arr) { containersResult = arr; },
    browser: {
      runtime: {
        sendMessage(msg) {
          sent.push(msg);
          if (msg.kind === "status") {
            return Promise.resolve({ ok: true, connected: true });
          }
          return Promise.resolve(nextReply);
        },
        openOptionsPage: vi.fn(),
      },
      tabs: { query: () => Promise.resolve(tabsResult) },
      contextualIdentities: { query: () => Promise.resolve(containersResult) },
    },
  };
}

async function loadPopup(env) {
  const bodyMatch = POPUP_HTML.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  document.body.innerHTML = bodyMatch ? bodyMatch[1] : POPUP_HTML;
  globalThis.browser = env.browser;
  // Compile with the real on-disk `filename` (vs `new Function`'s anonymous,
  // URL-less script) so V8 coverage attributes lines to src/popup.js.
  // No parsingContext => current (jsdom) context, so document stays available.
  vm.compileFunction(POPUP_JS, [], { filename: POPUP_JS_PATH })();
  // refreshStatus + loadContainers run at module load.
  await new Promise((r) => setTimeout(r, 0));
}

describe("popup.js (Firefox)", () => {
  let env;

  beforeEach(() => { env = makeFakeBrowser(); });
  afterEach(() => {
    delete globalThis.browser;
    document.body.innerHTML = "";
  });

  it("queries status on load and shows 'connected'", async () => {
    await loadPopup(env);
    expect(env.sent[0]).toEqual({ kind: "status" });
    expect(document.getElementById("status").textContent).toBe("connected");
    expect(document.getElementById("status").className).toBe("ok");
  });

  it("populates the container dropdown from contextualIdentities.query", async () => {
    await loadPopup(env);
    const sel = document.getElementById("container-select");
    // 1 default option + 2 containers.
    expect(sel.options.length).toBe(3);
    expect(sel.options[1].value).toBe("firefox-container-1");
    expect(sel.options[1].textContent).toBe("Personal (blue)");
    expect(sel.options[2].value).toBe("firefox-container-2");
  });

  it("leaves the dropdown alone when contextualIdentities is missing", async () => {
    delete env.browser.contextualIdentities;
    await loadPopup(env);
    const sel = document.getElementById("container-select");
    expect(sel.options.length).toBe(1);
    expect(sel.options[0].value).toBe("");
  });

  it("ping button sends kind:'ping' and renders the response into #out", async () => {
    env.setReply({ ok: true, response: { hello: "world" } });
    await loadPopup(env);
    const before = env.sent.length;
    document.getElementById("ping").click();
    // Click handler sends 'ping' then sends a follow-up 'status' via
    // refreshStatus(); wait for both so the post-test teardown doesn't
    // race a pending refreshStatus into a null DOM.
    await vi.waitFor(() => {
      const after = env.sent.slice(before);
      if (!after.find((m) => m.kind === "ping")) throw new Error("ping pending");
      if (!after.find((m) => m.kind === "status")) throw new Error("status pending");
    }, { timeout: 1000 });
    expect(document.getElementById("out").textContent).toContain('"hello": "world"');
  });

  async function waitForExportRoundTrip(env, before) {
    // Click handler sends cookies.export then refreshStatus -> status.
    await vi.waitFor(() => {
      const after = env.sent.slice(before);
      if (!after.find((m) => m.kind === "cookies.export")) {
        throw new Error("cookies.export pending");
      }
      if (!after.find((m) => m.kind === "status")) {
        throw new Error("status pending");
      }
    }, { timeout: 1000 });
  }

  it("cookies-export forwards the active tab URL and selected container", async () => {
    env.setReply({ ok: true, audit_id: "ax-1" });
    await loadPopup(env);
    document.getElementById("container-select").value = "firefox-container-2";
    const before = env.sent.length;
    document.getElementById("cookies-export").click();
    await waitForExportRoundTrip(env, before);
    const frame = env.sent.find((m) => m.kind === "cookies.export");
    expect(frame.url).toBe("https://example.com/");
    expect(frame.cookie_store_id).toBe("firefox-container-2");
  });

  it("cookies-export with default container sends cookie_store_id:null", async () => {
    env.setReply({ ok: true });
    await loadPopup(env);
    const before = env.sent.length;
    document.getElementById("cookies-export").click();
    await waitForExportRoundTrip(env, before);
    const frame = env.sent.find((m) => m.kind === "cookies.export");
    expect(frame.cookie_store_id).toBeNull();
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
    expect(env.browser.runtime.openOptionsPage).toHaveBeenCalled();
  });

  it("turns a rejected sendMessage into ok:false with the error message", async () => {
    env.browser.runtime.sendMessage = () =>
      Promise.reject(new Error("port_closed"));
    await loadPopup(env);
    // status was queried, sendMessage threw, render('disconnected').
    expect(document.getElementById("status").textContent).toBe("disconnected");
  });
});
