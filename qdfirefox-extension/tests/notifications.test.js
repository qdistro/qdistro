// notifications module — show only (inbound). Outbound click/close
// emission was dropped: the bridge has no handler and the
// browser.notifications API only sees extension-owned notifications.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension, makeFakeBrowser, makeFakePort } from "./helpers.js";

describe("qdistroNotifications", () => {
  let env;
  let browser;
  let createCalls;

  beforeEach(() => {
    browser = makeFakeBrowser();
    createCalls = [];
    browser.notifications.create = (id, opts) => {
      createCalls.push({ id, opts });
      return Promise.resolve("notif-generated");
    };
    env = loadExtension({ browser, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
    env.scope.qdistroNotifications.install();
  });

  function lastOutbound(op) {
    return env.port.sent.find((m) => m.op === op);
  }

  it("exposes qdistroNotifications.install", () => {
    expect(env.scope.qdistroNotifications).toBeTruthy();
    expect(typeof env.scope.qdistroNotifications.install).toBe("function");
  });

  it("install() does not register onClicked/onClosed listeners", () => {
    // install() is called in beforeEach; assert it added no listeners.
    expect(browser.notifications.onClicked.listeners.length).toBe(0);
    expect(browser.notifications.onClosed.listeners.length).toBe(0);
  });

  it("install() is a no-op when browser.notifications is missing", () => {
    const browser2 = makeFakeBrowser();
    browser2.notifications = undefined;
    const env2 = loadExtension({ browser: browser2, portHandle: makeFakePort() });
    expect(() => env2.scope.qdistroNotifications.install()).not.toThrow();
  });

  it("registers an inbound notifications.show handler", () => {
    expect(env.scope.qdistroDispatcher.handlers.has("notifications.show")).toBe(true);
  });

  it("does not emit notifications.event on click/close", async () => {
    browser.notifications.onClicked.fire("n-123");
    browser.notifications.onClosed.fire("n-7", true);
    await new Promise((r) => setTimeout(r, 0));
    const events = env.port.sent.filter((m) => m.op === "notifications.event");
    expect(events).toHaveLength(0);
  });

  it("notifications.show creates a notification with the right opts and replies with the id", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "notifications.show",
      request_id: 1,
      title: "Hello",
      message: "World",
      icon_url: "icons/x.png",
    });
    expect(createCalls).toHaveLength(1);
    expect(createCalls[0].opts).toMatchObject({
      type: "basic",
      title: "Hello",
      message: "World",
      iconUrl: "icons/x.png",
    });
    const reply = lastOutbound("notifications.show.reply");
    expect(reply.ok).toBe(true);
    expect(reply.notification_id).toBe("notif-generated");
  });

  it("notifications.show defaults the icon when icon_url is missing", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "notifications.show",
      request_id: 2,
      title: "T", message: "M",
    });
    expect(createCalls[0].opts.iconUrl).toBe("icons/icon-48.png");
  });

  it("notifications.show coerces missing title/message to safe defaults", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "notifications.show", request_id: 3,
    });
    expect(createCalls[0].opts.title).toBe("qdistro");
    expect(createCalls[0].opts.message).toBe("");
  });

  it("notifications.show returns notifications_unavailable when API is missing", async () => {
    const browser2 = makeFakeBrowser();
    browser2.notifications = undefined;
    const env2 = loadExtension({ browser: browser2, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    await env2.scope.qdistroDispatcher.handleInbound({
      op: "notifications.show", request_id: 1,
      title: "T", message: "M",
    });
    const reply = env2.port.sent.find((m) => m.op === "notifications.show.reply");
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("notifications_unavailable");
  });
});
