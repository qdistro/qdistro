// tabs module tests — inbound tabs.list / tabs.open / tabs.close
// shapes against the dispatcher.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension, makeFakeChrome, makeFakePort } from "./helpers.js";

describe("qdistroTabs", () => {
  let env;
  beforeEach(() => {
    const chrome = makeFakeChrome();
    chrome.tabs.query = (_q, cb) => cb([
      { id: 1, windowId: 1, index: 0, url: "https://a/", title: "A", active: true },
      { id: 2, windowId: 1, index: 1, url: "https://b/", title: "B" },
    ]);
    env = loadExtension({ chrome, portHandle: makeFakePort() });
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
    });
  });

  it("tabs.open rejects missing url", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.open", request_id: 2,
    });
    const reply = env.port.sent.find((m) => m.op === "tabs.open.reply");
    // Handler returned ok:false in its body but the dispatcher still
    // emits ok:true because the handler did not throw. The reply
    // carries error="missing_url" for the daemon to inspect.
    expect(reply.error).toBe("missing_url");
  });

  it("tabs.close errors on empty id list", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.close", request_id: 3, tab_ids: [],
    });
    const reply = env.port.sent.find((m) => m.op === "tabs.close.reply");
    expect(reply.error).toBe("missing_tab_ids");
  });

  it("sendMessageToTab delivers to the tab and resolves the reply", async () => {
    const calls = [];
    env.scope.chrome.tabs.sendMessage = (tabId, message, cb) => {
      calls.push({ tabId, message });
      cb({ ok: true, action: message.action });
    };
    const r = await env.scope.qdistroTabs.sendMessageToTab(7, {
      kind: "mpris.do_action", action: "play",
    });
    expect(calls).toEqual([{ tabId: 7, message: { kind: "mpris.do_action", action: "play" } }]);
    expect(r).toEqual({ ok: true, action: "play" });
  });

  it("sendMessageToTab rejects on runtime.lastError (no receiver)", async () => {
    env.scope.chrome.tabs.sendMessage = (tabId, message, cb) => {
      env.scope.chrome.runtime.lastError = { message: "Receiving end does not exist." };
      cb(undefined);
      env.scope.chrome.runtime.lastError = null;
    };
    await expect(
      env.scope.qdistroTabs.sendMessageToTab(7, { kind: "mpris.do_action", action: "play" }),
    ).rejects.toThrow(/Receiving end/);
  });
});
