// mpris module tests.
//
// `mpris.control` is a REAL inbound op: the admin media widget drives
// an org.mpris.MediaPlayer2 control which the daemon routes back to the
// originating tab via the bridge, and this module forwards it down to
// that tab's content script as `mpris.do_action`, returning the content
// script's real result (no stub ack). Outbound `update()` translates a
// content-script snapshot into the bridge's `mpris.publish` wire shape.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension, makeFakeChrome, makeFakePort } from "./helpers.js";

describe("qdistroMpris", () => {
  let env;
  let sent; // captured tabs.sendMessageToTab calls

  beforeEach(() => {
    env = loadExtension({ chrome: makeFakeChrome(), portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
    // Capture forwarded do_action messages; default to a content-script
    // success reply. Individual tests override the reply/throw.
    sent = [];
    env.scope.qdistroTabs.sendMessageToTab = async (tabId, message) => {
      sent.push({ tabId, message });
      return { ok: true, action: message.action };
    };
  });

  function lastOutbound(op) {
    return env.port.sent.find((m) => m.op === op);
  }

  it("exposes qdistroMpris.update", () => {
    expect(env.scope.qdistroMpris).toBeTruthy();
    expect(typeof env.scope.qdistroMpris.update).toBe("function");
  });

  it("registers an inbound handler for mpris.control", () => {
    expect(env.scope.qdistroDispatcher.handlers.has("mpris.control")).toBe(true);
  });

  it("forwards mpris.control to the target tab as mpris.do_action", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 1, action: "play", tab_id: 7,
    });
    expect(sent).toHaveLength(1);
    expect(sent[0].tabId).toBe(7);
    expect(sent[0].message).toEqual({ kind: "mpris.do_action", action: "play" });
  });

  it("returns the content script's reply, not a stub", async () => {
    env.scope.qdistroTabs.sendMessageToTab = async () =>
      ({ ok: true, action: "pause" });
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 2, action: "pause", tab_id: 7,
    });
    const reply = lastOutbound("mpris.control.reply");
    expect(reply.ok).toBe(true);
    expect(reply.action).toBe("pause");
    expect(reply.request_id).toBe(2);
    expect(reply.stub).toBeUndefined();
  });

  it("forwards the seek value to the tab", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 3, action: "seek", value: 42, tab_id: 7,
    });
    expect(sent[0].message).toEqual({
      kind: "mpris.do_action", action: "seek", value: 42,
    });
  });

  it("surfaces a content-script failure reply (e.g. no media element)", async () => {
    env.scope.qdistroTabs.sendMessageToTab = async () =>
      ({ ok: false, error: "no_media_element" });
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 4, action: "play", tab_id: 7,
    });
    const reply = lastOutbound("mpris.control.reply");
    // Dispatcher tags the frame ok:true (it ran), but the handler body
    // carries the real {ok:false, error}.
    expect(reply.error).toBe("no_media_element");
  });

  it("falls back to the last-published tab when tab_id is omitted", async () => {
    const p = env.scope.qdistroMpris.update({ state: "playing", tab_id: 13 });
    const pub = lastOutbound("mpris.publish");
    env.port.deliver({ op: "mpris.publish.reply", request_id: pub.request_id, ok: true });
    await p;
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 5, action: "play",
    });
    expect(sent[0].tabId).toBe(13);
  });

  it("fails closed with no_target_tab when no tab is known", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 6, action: "play",
    });
    expect(sent).toHaveLength(0);
    const reply = lastOutbound("mpris.control.reply");
    expect(reply.error).toBe("no_target_tab");
  });

  it("reports tab_delivery_failed when the tab has no content script", async () => {
    env.scope.qdistroTabs.sendMessageToTab = async () => {
      throw new Error("Could not establish connection. Receiving end does not exist.");
    };
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 7, action: "play", tab_id: 7,
    });
    const reply = lastOutbound("mpris.control.reply");
    expect(reply.error).toBe("tab_delivery_failed");
  });

  it("reports no_content_script when the reply is undefined", async () => {
    env.scope.qdistroTabs.sendMessageToTab = async () => undefined;
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 8, action: "play", tab_id: 7,
    });
    const reply = lastOutbound("mpris.control.reply");
    expect(reply.error).toBe("no_content_script");
  });

  it("update() emits mpris.publish with bridge-shaped fields", async () => {
    const p = env.scope.qdistroMpris.update({
      state: "playing",
      title: "Some Song",
      artist: "Some Artist",
      position: 12,
      tab_id: 42,
    });
    const frame = lastOutbound("mpris.publish");
    expect(frame).toBeTruthy();
    expect(frame).toMatchObject({
      title: "Some Song",
      artist: "Some Artist",
      playback_status: "playing",
      position_us: 12000000,
      tab_id: 42,
    });
    expect(typeof frame.request_id).toBe("number");
    env.port.deliver({ op: "mpris.publish.reply", request_id: frame.request_id, ok: true });
    await p;
  });

  it("update() defaults playback_status to 'none' and position_us to 0", async () => {
    const p = env.scope.qdistroMpris.update();
    const frame = lastOutbound("mpris.publish");
    expect(frame.playback_status).toBe("none");
    expect(frame.position_us).toBe(0);
    env.port.deliver({ op: "mpris.publish.reply", request_id: frame.request_id, ok: true });
    await p;
  });

  it("update() resolves on a bridge reply", async () => {
    const p = env.scope.qdistroMpris.update({ state: "paused" });
    const frame = lastOutbound("mpris.publish");
    env.port.deliver({
      op: "mpris.publish.reply",
      request_id: frame.request_id,
      ok: true,
    });
    const r = await p;
    expect(r.ok).toBe(true);
  });

  it("update() surfaces a bridge error reply without throwing", async () => {
    const p = env.scope.qdistroMpris.update({ state: "stopped" });
    const frame = lastOutbound("mpris.publish");
    env.port.deliver({
      op: "mpris.publish.reply",
      request_id: frame.request_id,
      ok: false,
      error: "no_active_player",
    });
    const r = await p;
    expect(r.ok).toBe(false);
    expect(r.error).toBe("no_active_player");
  });
});
