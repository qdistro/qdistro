// tabs module — inbound tabs.list / tabs.open / tabs.close.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension, makeFakeBrowser, makeFakePort } from "./helpers.js";

describe("qdistroTabs", () => {
  let env;
  beforeEach(() => {
    const browser = makeFakeBrowser();
    browser.tabs.query = (_q) => Promise.resolve([
      { id: 1, windowId: 1, index: 0, url: "https://a/", title: "A", active: true,
        cookieStoreId: "firefox-default" },
      { id: 2, windowId: 1, index: 1, url: "https://b/", title: "B" },
    ]);
    env = loadExtension({ browser, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
  });

  it("serializes a tabs.list reply with the expected fields", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.list", request_id: 1,
    });
    const reply = env.port.sent.find((m) => m.op === "tabs.list.reply");
    expect(reply).toBeTruthy();
    expect(reply.tabs).toHaveLength(2);
    expect(reply.tabs[0]).toMatchObject({
      id: 1, url: "https://a/", title: "A", active: true, status: "complete",
      cookie_store_id: "firefox-default",
    });
  });

  it("tabs.open rejects missing url", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.open", request_id: 2,
    });
    const reply = env.port.sent.find((m) => m.op === "tabs.open.reply");
    expect(reply.error).toBe("missing_url");
  });

  it("tabs.open passes cookie_store_id through to tabs.create", async () => {
    const calls = [];
    const browser = makeFakeBrowser();
    browser.tabs.create = (props) => {
      calls.push(props);
      return Promise.resolve({ id: 7, windowId: 1, index: 0,
        url: props.url, title: "x", active: !!props.active,
        cookieStoreId: props.cookieStoreId });
    };
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    await env2.scope.qdistroDispatcher.handleInbound({
      op: "tabs.open", request_id: 9,
      url: "https://x/", cookie_store_id: "firefox-container-2",
    });
    expect(calls[0].cookieStoreId).toBe("firefox-container-2");
    const reply = env2.port.sent.find((m) => m.op === "tabs.open.reply");
    expect(reply.tab.cookie_store_id).toBe("firefox-container-2");
  });

  it("tabs.close errors on empty id list", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.close", request_id: 3, tab_ids: [],
    });
    const reply = env.port.sent.find((m) => m.op === "tabs.close.reply");
    expect(reply.error).toBe("missing_tab_ids");
  });

  it("tabs.close accepts a single tab_id", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.close", request_id: 4, tab_id: 42,
    });
    const reply = env.port.sent.find((m) => m.op === "tabs.close.reply");
    expect(reply.ok).toBe(true);
    expect(reply.closed).toEqual([42]);
  });

  it("sendMessageToTab delivers to the tab and resolves the reply", async () => {
    const calls = [];
    env.scope.qdistroApi.tabs.sendMessage = (tabId, message) => {
      calls.push({ tabId, message });
      return Promise.resolve({ ok: true, action: message.action });
    };
    const r = await env.scope.qdistroTabs.sendMessageToTab(7, {
      kind: "mpris.do_action", action: "play",
    });
    expect(calls).toEqual([{ tabId: 7, message: { kind: "mpris.do_action", action: "play" } }]);
    expect(r).toEqual({ ok: true, action: "play" });
  });

  it("sendMessageToTab rejects when no content script is listening", async () => {
    env.scope.qdistroApi.tabs.sendMessage = () =>
      Promise.reject(new Error("Could not establish connection."));
    await expect(
      env.scope.qdistroTabs.sendMessageToTab(7, { kind: "mpris.do_action", action: "play" }),
    ).rejects.toThrow(/Could not establish/);
  });
});
