// page.extract module tests — context menu registration, click flow,
// page.extract frame shape, intent-token forwarding, broker-denied
// reply handling.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadExtension, makeFakeChromeAllOrigins, makeFakePort, makeEvent } from "./helpers.js";

describe("qdistroPageExtract", () => {
  let env;
  let chrome;
  let menuCreated;
  let executedFns;

  beforeEach(() => {
    // Origin gate is closed by default since J11; these tests exercise
    // the extract flow, so opt in to all origins (`*`). Origin filtering
    // for extract is covered in gate.test.js.
    chrome = makeFakeChromeAllOrigins();
    menuCreated = [];
    chrome.contextMenus.create = (def, cb) => {
      menuCreated.push(def);
      if (cb) cb();
    };
    executedFns = [];
    chrome.scripting.executeScript = (opts) => {
      executedFns.push(opts);
      // Simulate the content-script returning the captured selection.
      return Promise.resolve([{
        result: {
          selected_text: "highlighted",
          url: "https://page.example/article",
          title: "Article",
        },
      }]);
    };
    env = loadExtension({ chrome, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
  });

  function lastOutbound(op) {
    return env.port.sent.find((m) => m.op === op);
  }

  function waitForOutbound(op) {
    return vi.waitFor(() => {
      const m = env.port.sent.find((x) => x.op === op);
      if (!m) throw new Error(`${op} not yet sent`);
      return m;
    }, { timeout: 1000 });
  }

  it("exposes qdistroPageExtract", () => {
    expect(env.scope.qdistroPageExtract).toBeTruthy();
    expect(typeof env.scope.qdistroPageExtract.installContextMenu).toBe("function");
  });

  it("installContextMenu registers the 'Send to qdistro…' item", () => {
    env.scope.qdistroPageExtract.installContextMenu();
    expect(menuCreated).toHaveLength(1);
    expect(menuCreated[0]).toMatchObject({
      id: "qdistro-share-to",
      title: "Send to qdistro…",
    });
    expect(menuCreated[0].contexts).toEqual(
      expect.arrayContaining(["selection", "page", "link"]));
  });

  it("ignores clicks for other menu items", async () => {
    env.scope.qdistroPageExtract.installContextMenu();
    await chrome.contextMenus.onClicked.fire(
      { menuItemId: "other-item", selectionText: "hi" },
      { id: 7 });
    // No outbound frame.
    expect(env.port.sent.find((m) => m.op === "page.extract")).toBeUndefined();
  });

  it("ignores clicks without a tab id", async () => {
    env.scope.qdistroPageExtract.installContextMenu();
    await chrome.contextMenus.onClicked.fire(
      { menuItemId: "qdistro-share-to", selectionText: "hi" },
      null);
    expect(env.port.sent.find((m) => m.op === "page.extract")).toBeUndefined();
  });

  it("extract() captures selection and forwards a page.extract frame", async () => {
    void env.scope.qdistroPageExtract.extract(42, "selection", {
      operation: "page.extract", nonce: "n-1",
    });
    const frame = await waitForOutbound("page.extract");
    expect(frame).toMatchObject({
      selected_text: "highlighted",
      url: "https://page.example/article",
      title: "Article",
      destination: "selection",
    });
    expect(frame.intent_token).toMatchObject({ operation: "page.extract" });
  });

  it("executes the capture function in the target tab", async () => {
    void env.scope.qdistroPageExtract.extract(99, "page", null);
    await vi.waitFor(() => {
      if (executedFns.length === 0) throw new Error("executeScript not yet called");
    }, { timeout: 1000 });
    expect(executedFns).toHaveLength(1);
    expect(executedFns[0].target).toEqual({ tabId: 99 });
    expect(typeof executedFns[0].func).toBe("function");
  });

  it("falls back to empty selection when the content script returns nothing", async () => {
    chrome.scripting.executeScript = () => Promise.resolve([{ result: undefined }]);
    const p = env.scope.qdistroPageExtract.capture(5);
    const cap = await p;
    expect(cap).toEqual({ selected_text: "", url: "", title: "" });
  });

  it("click on selection mints an intent token scoped to page.extract", async () => {
    env.scope.qdistroPageExtract.installContextMenu();
    await chrome.contextMenus.onClicked.fire(
      { menuItemId: "qdistro-share-to", selectionText: "hello" },
      { id: 3 });
    const frame = await waitForOutbound("page.extract");
    expect(frame.destination).toBe("selection");
    expect(frame.intent_token.op).toBe("page.extract");
    expect(frame.intent_token.hmac).toMatch(/^[0-9a-f]{64}$/);
    expect(typeof frame.intent_token.ts).toBe("number");
  });

  it("click without selectionText falls back to page destination", async () => {
    env.scope.qdistroPageExtract.installContextMenu();
    await chrome.contextMenus.onClicked.fire(
      { menuItemId: "qdistro-share-to" },
      { id: 4 });
    const frame = await waitForOutbound("page.extract");
    expect(frame.destination).toBe("page");
  });

  it("surfaces a broker-denied reply (ok:false / error) without throwing", async () => {
    const p = env.scope.qdistroPageExtract.extract(1, "page", null);
    const frame = await waitForOutbound("page.extract");
    env.port.deliver({
      op: "page.extract.reply",
      request_id: frame.request_id,
      ok: false,
      error: "policy_denied",
    });
    const r = await p;
    expect(r.ok).toBe(false);
    expect(r.error).toBe("policy_denied");
  });

  it("successful reply resolves with the daemon's stored handle", async () => {
    const p = env.scope.qdistroPageExtract.extract(1, "selection", null);
    const frame = await waitForOutbound("page.extract");
    env.port.deliver({
      op: "page.extract.reply",
      request_id: frame.request_id,
      ok: true,
      stored_id: "extract-abc",
    });
    const r = await p;
    expect(r.ok).toBe(true);
    expect(r.stored_id).toBe("extract-abc");
  });

  it("click handler swallows downstream errors so the menu stays usable", async () => {
    // Make executeScript reject — the click handler should not throw.
    chrome.scripting.executeScript = () => Promise.reject(new Error("inject_failed"));
    env.scope.qdistroPageExtract.installContextMenu();
    // The async listener catches internally — no synchronous throw.
    expect(() =>
      chrome.contextMenus.onClicked.fire(
        { menuItemId: "qdistro-share-to", selectionText: "x" },
        { id: 2 })
    ).not.toThrow();
    // Drain microtasks so any unhandled rejection would surface.
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));
    // No page.extract frame should have been sent.
    expect(env.port.sent.find((m) => m.op === "page.extract")).toBeUndefined();
  });

  describe("page.extract.request (bridge → ext)", () => {
    function makeEnv(executeScript) {
      // Origin gate is closed by default since J11; the bridge→ext
      // extract tests target a normal tab, so opt in to all origins.
      const c = makeFakeChromeAllOrigins();
      c.scripting.executeScript = executeScript;
      const e = loadExtension({ chrome: c, portHandle: makeFakePort() });
      e.scope.qdistroPort.connect();
      return e;
    }

    it("missing tab_id replies error", async () => {
      const e = makeEnv(() => Promise.resolve([{ result: {} }]));
      await e.scope.qdistroDispatcher.handleInbound({
        op: "page.extract.request", request_id: 1, mode: "visible_text",
      });
      const reply = e.port.sent.find((m) => m.op === "page.extract.request.reply");
      expect(reply.ok).toBe(false);
      expect(reply.error).toBe("missing_tab_id");
    });

    it("visible_text mode returns content + url + title", async () => {
      const e = makeEnv(() => Promise.resolve([{
        result: {
          mode: "visible_text", content: "Hello world",
          url: "https://x/", title: "X", truncated: false,
        },
      }]));
      await e.scope.qdistroDispatcher.handleInbound({
        op: "page.extract.request", request_id: 2, tab_id: 5,
        mode: "visible_text",
      });
      const reply = e.port.sent.find((m) => m.op === "page.extract.request.reply");
      expect(reply.ok).toBe(true);
      expect(reply).toMatchObject({
        content: "Hello world", url: "https://x/", title: "X",
        mode: "visible_text", truncated: false,
      });
    });

    it("by_selector includes matched=true when found", async () => {
      const e = makeEnv(() => Promise.resolve([{
        result: {
          mode: "by_selector", content: "Headline",
          url: "https://x/", title: "X", matched: true,
        },
      }]));
      await e.scope.qdistroDispatcher.handleInbound({
        op: "page.extract.request", request_id: 3, tab_id: 5,
        mode: "by_selector", selector: "h1",
      });
      const reply = e.port.sent.find((m) => m.op === "page.extract.request.reply");
      expect(reply.matched).toBe(true);
      expect(reply.content).toBe("Headline");
    });

    it("by_selector missing selector returns error", async () => {
      const e = makeEnv(() => Promise.resolve([{
        result: {
          mode: "by_selector", content: "", url: "https://x/",
          title: "X", error: "missing_selector",
        },
      }]));
      await e.scope.qdistroDispatcher.handleInbound({
        op: "page.extract.request", request_id: 4, tab_id: 5,
        mode: "by_selector",
      });
      const reply = e.port.sent.find((m) => m.op === "page.extract.request.reply");
      expect(reply.error).toBe("missing_selector");
    });

    it("executeScript rejection surfaces as executeScript_failed", async () => {
      const e = makeEnv(() => Promise.reject(new Error("perm_denied")));
      await e.scope.qdistroDispatcher.handleInbound({
        op: "page.extract.request", request_id: 5, tab_id: 5,
        mode: "visible_text",
      });
      const reply = e.port.sent.find((m) => m.op === "page.extract.request.reply");
      expect(reply.ok).toBe(false);
      expect(reply.error).toBe("executeScript_failed");
      expect(reply.detail).toMatch(/perm_denied/);
    });

    it("propagates truncated flag from the in-page extractor", async () => {
      const e = makeEnv(() => Promise.resolve([{
        result: {
          mode: "outer_html", content: "<html>…snip…</html>",
          url: "https://x/", title: "X", truncated: true,
        },
      }]));
      await e.scope.qdistroDispatcher.handleInbound({
        op: "page.extract.request", request_id: 6, tab_id: 5,
        mode: "outer_html",
      });
      const reply = e.port.sent.find((m) => m.op === "page.extract.request.reply");
      expect(reply.truncated).toBe(true);
    });
  });
});
