// mpris module — `mpris.control` forwards inbound controls down to the
// originating tab's content script as `mpris.do_action` (real wiring,
// no stub ack); `update()` translates to the `mpris.publish` wire shape.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension } from "./helpers.js";

describe("qdistroMpris", () => {
  let env;
  let sent;
  beforeEach(() => {
    env = loadExtension();
    env.scope.qdistroPort.connect();
    sent = [];
    env.scope.qdistroTabs.sendMessageToTab = async (tabId, message) => {
      sent.push({ tabId, message });
      return { ok: true, action: message.action };
    };
  });

  function controlReply() {
    return env.port.sent.find((m) => m.op === "mpris.control.reply");
  }

  it("forwards mpris.control to the target tab as mpris.do_action", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 1, action: "play", tab_id: 7,
    });
    expect(sent).toEqual([
      { tabId: 7, message: { kind: "mpris.do_action", action: "play" } },
    ]);
  });

  it("returns the content script's reply, not a stub", async () => {
    env.scope.qdistroTabs.sendMessageToTab = async () =>
      ({ ok: true, action: "pause" });
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 2, action: "pause", tab_id: 7,
    });
    const reply = controlReply();
    expect(reply.ok).toBe(true);
    expect(reply.action).toBe("pause");
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

  it("falls back to the last-published tab when tab_id is omitted", async () => {
    env.scope.qdistroMpris.update({ state: "playing", tab_id: 13 });
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 4, action: "play",
    });
    expect(sent[0].tabId).toBe(13);
  });

  it("fails closed with no_target_tab when no tab is known", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 5, action: "play",
    });
    expect(sent).toHaveLength(0);
    expect(controlReply().error).toBe("no_target_tab");
  });

  it("reports tab_delivery_failed when delivery throws (no content script)", async () => {
    env.scope.qdistroTabs.sendMessageToTab = async () => {
      throw new Error("Could not establish connection.");
    };
    await env.scope.qdistroDispatcher.handleInbound({
      op: "mpris.control", request_id: 6, action: "play", tab_id: 7,
    });
    expect(controlReply().error).toBe("tab_delivery_failed");
  });

  it("update() sends mpris.publish with bridge-shaped fields", () => {
    env.scope.qdistroMpris.update({
      title: "Song", artist: "A", album: "B",
      state: "playing", position: 12, tab_id: 7,
    });
    const req = env.port.sent.find((m) => m.op === "mpris.publish");
    expect(req).toMatchObject({
      title: "Song", artist: "A", album: "B",
      playback_status: "playing",
      position_us: 12000000,
      tab_id: 7,
    });
  });

  it("update() defaults playback_status to 'none' and position_us to 0", () => {
    env.scope.qdistroMpris.update({ title: "X" });
    const req = env.port.sent.find((m) => m.op === "mpris.publish");
    expect(req.playback_status).toBe("none");
    expect(req.position_us).toBe(0);
  });
});
