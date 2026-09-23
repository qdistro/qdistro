// notifications module tests — inbound notifications.show only.
// Outbound click/close emission was dropped: the bridge has no
// handler and the chrome.notifications API only sees
// extension-owned notifications.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension, makeFakeChrome, makeFakePort } from "./helpers.js";

describe("qdistroNotifications", () => {
  let env;
  let chrome;
  let createCalls;

  beforeEach(() => {
    chrome = makeFakeChrome();
    createCalls = [];
    chrome.notifications.create = (id, opts, cb) => {
      createCalls.push({ id, opts });
      if (cb) cb("notif-generated");
    };
    env = loadExtension({ chrome, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
  });

  function lastOutbound(op) {
    return env.port.sent.find((m) => m.op === op);
  }

  it("exposes qdistroNotifications.install", () => {
    expect(env.scope.qdistroNotifications).toBeTruthy();
    expect(typeof env.scope.qdistroNotifications.install).toBe("function");
  });

  it("install() does not register onClicked/onClosed listeners", () => {
    env.scope.qdistroNotifications.install();
    expect(chrome.notifications.onClicked.listeners.length).toBe(0);
    expect(chrome.notifications.onClosed.listeners.length).toBe(0);
  });

  it("install() is a no-op when chrome.notifications is missing", () => {
    chrome.notifications = undefined;
    const env2 = loadExtension({ chrome, portHandle: makeFakePort() });
    expect(() => env2.scope.qdistroNotifications.install()).not.toThrow();
  });

  it("registers an inbound notifications.show handler", () => {
    expect(env.scope.qdistroDispatcher.handlers.has("notifications.show")).toBe(true);
  });

  it("notifications.show calls chrome.notifications.create with the right opts", async () => {
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
    chrome.notifications = undefined;
    const env2 = loadExtension({ chrome, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    await env2.scope.qdistroDispatcher.handleInbound({
      op: "notifications.show", request_id: 1,
      title: "T", message: "M",
    });
    const reply = env2.port.sent.find((m) => m.op === "notifications.show.reply");
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("notifications_unavailable");
  });

  it("never emits notifications.event for clicks or closes", async () => {
    env.scope.qdistroNotifications.install();
    chrome.notifications.onClicked.fire("a");
    chrome.notifications.onClosed.fire("b", true);
    await new Promise((r) => setTimeout(r, 0));
    const frames = env.port.sent.filter((m) => m.op === "notifications.event");
    expect(frames).toHaveLength(0);
  });
});
