// containers module — Firefox contextual identities (Firefox-only).
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension, makeFakeBrowser, makeFakePort } from "./helpers.js";

describe("qdistroContainers", () => {
  let env;
  let browser;
  beforeEach(() => {
    browser = makeFakeBrowser();
    browser.contextualIdentities.query = (_q) => Promise.resolve([
      { cookieStoreId: "firefox-container-1", name: "Personal",
        color: "blue", colorCode: "#37adff", icon: "fingerprint", iconUrl: "" },
      { cookieStoreId: "firefox-container-2", name: "Work",
        color: "orange", colorCode: "#ff9f00", icon: "briefcase", iconUrl: "" },
    ]);
    env = loadExtension({ browser, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
  });

  it("containers.list returns serialized identities", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "containers.list", request_id: 1,
    });
    const reply = env.port.sent.find((m) => m.op === "containers.list.reply");
    expect(reply.ok).toBe(true);
    expect(reply.containers).toHaveLength(2);
    expect(reply.containers[0]).toMatchObject({
      cookie_store_id: "firefox-container-1",
      name: "Personal", color: "blue",
    });
  });

  it("containers.create calls the API and returns the serialized result", async () => {
    const calls = [];
    browser.contextualIdentities.create = (props) => {
      calls.push(props);
      return Promise.resolve({
        cookieStoreId: "firefox-container-99",
        name: props.name, color: props.color, colorCode: "#000",
        icon: props.icon, iconUrl: "",
      });
    };
    await env.scope.qdistroDispatcher.handleInbound({
      op: "containers.create", request_id: 2,
      name: "Banking", color: "green", icon: "dollar",
    });
    expect(calls[0]).toMatchObject({
      name: "Banking", color: "green", icon: "dollar",
    });
    const reply = env.port.sent.find((m) => m.op === "containers.create.reply");
    expect(reply.container.cookie_store_id).toBe("firefox-container-99");
  });

  it("containers.remove requires a cookie_store_id", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "containers.remove", request_id: 3,
    });
    const reply = env.port.sent.find((m) => m.op === "containers.remove.reply");
    expect(reply.error).toBe("missing_cookie_store_id");
  });

  it("returns contextualIdentities_unavailable when API is missing", async () => {
    const browser2 = makeFakeBrowser();
    browser2.contextualIdentities = undefined;
    const env2 = loadExtension({ browser: browser2, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    await env2.scope.qdistroDispatcher.handleInbound({
      op: "containers.list", request_id: 4,
    });
    const reply = env2.port.sent.find((m) => m.op === "containers.list.reply");
    expect(reply.error).toBe("contextualIdentities_unavailable");
  });

  it("list() helper returns an empty array when API is missing (no throw)", async () => {
    const browser2 = makeFakeBrowser();
    browser2.contextualIdentities = undefined;
    const env2 = loadExtension({ browser: browser2, portHandle: makeFakePort() });
    expect(await env2.scope.qdistroContainers.list()).toEqual([]);
  });

  it("containers.create returns contextualIdentities_unavailable when API missing", async () => {
    const browser2 = makeFakeBrowser();
    browser2.contextualIdentities = undefined;
    const env2 = loadExtension({ browser: browser2, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    await env2.scope.qdistroDispatcher.handleInbound({
      op: "containers.create", request_id: 10,
      name: "Banking", color: "green", icon: "dollar",
    });
    const reply = env2.port.sent.find((m) => m.op === "containers.create.reply");
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("contextualIdentities_unavailable");
  });

  it("containers.remove returns contextualIdentities_unavailable when API missing", async () => {
    const browser2 = makeFakeBrowser();
    browser2.contextualIdentities = undefined;
    const env2 = loadExtension({ browser: browser2, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    await env2.scope.qdistroDispatcher.handleInbound({
      op: "containers.remove", request_id: 11,
      cookie_store_id: "firefox-container-1",
    });
    const reply = env2.port.sent.find((m) => m.op === "containers.remove.reply");
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("contextualIdentities_unavailable");
  });

  it("create() helper throws on missing API (no silent success)", async () => {
    const browser2 = makeFakeBrowser();
    browser2.contextualIdentities = undefined;
    const env2 = loadExtension({ browser: browser2, portHandle: makeFakePort() });
    await expect(env2.scope.qdistroContainers.create("X"))
      .rejects.toThrow(/contextualIdentities_unavailable/);
  });

  it("remove() helper throws on missing API", async () => {
    const browser2 = makeFakeBrowser();
    browser2.contextualIdentities = undefined;
    const env2 = loadExtension({ browser: browser2, portHandle: makeFakePort() });
    await expect(env2.scope.qdistroContainers.remove("firefox-container-1"))
      .rejects.toThrow(/contextualIdentities_unavailable/);
  });
});
