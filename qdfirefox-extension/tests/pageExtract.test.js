// pageExtract module — context-menu-driven capture.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadExtension, makeFakeBrowser, makeFakeBrowserAllOrigins, makeFakePort } from "./helpers.js";

describe("qdistroPageExtract", () => {
  let env;
  beforeEach(() => {
    // Origin gate is closed by default since J11; these tests exercise
    // the extract flow, so opt in to all origins (`*`). Origin filtering
    // for extract is covered in gate.test.js.
    const browser = makeFakeBrowserAllOrigins();
    browser.scripting.executeScript = () => Promise.resolve([{
      result: { selected_text: "hello", url: "https://x/", title: "X" },
    }]);
    env = loadExtension({ browser, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
  });

  it("capture() returns the script's result payload", async () => {
    const cap = await env.scope.qdistroPageExtract.capture(7);
    expect(cap).toEqual({
      selected_text: "hello",
      url: "https://x/",
      title: "X",
    });
  });

  it("extract() forwards the captured payload to the bridge", async () => {
    const token = await env.scope.qdistroIntent.mint("page.extract");
    env.scope.qdistroPageExtract.extract(7, "selection", token);
    const req = await vi.waitFor(
      () => {
        const m = env.port.sent.find((x) => x.op === "page.extract");
        if (!m) throw new Error("page.extract not yet sent");
        return m;
      },
      { timeout: 1000 },
    );
    expect(req).toMatchObject({
      selected_text: "hello",
      url: "https://x/",
      title: "X",
      destination: "selection",
    });
    expect(req.intent_token.request_id).toBeTruthy();
    expect(req.intent_token.op).toBe("page.extract");
    expect(req.intent_token.hmac).toMatch(/^[0-9a-f]{64}$/);
  });

  it("installContextMenu() registers the qdistro-share-to entry", () => {
    const calls = [];
    const browser = makeFakeBrowser();
    browser.contextMenus.create = (def) => { calls.push(def); };
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    env2.scope.qdistroPageExtract.installContextMenu();
    expect(calls[0]).toMatchObject({
      id: "qdistro-share-to",
      contexts: ["selection", "page", "link"],
    });
  });

  it("captures empty selection without throwing", async () => {
    const browser = makeFakeBrowser();
    browser.scripting.executeScript = () => Promise.resolve([{ result: undefined }]);
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    const cap = await env2.scope.qdistroPageExtract.capture(1);
    expect(cap).toEqual({ selected_text: "", url: "", title: "" });
  });

  describe("page.extract.request (bridge → ext)", () => {
    function makeEnv(executeScript) {
      // Origin gate is closed by default since J11; the bridge→ext
      // extract tests target a normal tab, so opt in to all origins.
      const browser = makeFakeBrowserAllOrigins();
      browser.scripting.executeScript = executeScript;
      const e = loadExtension({ browser, portHandle: makeFakePort() });
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
