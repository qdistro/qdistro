// Dispatcher tests: request_id correlation, inbound handler dispatch,
// reply shape, timeout, unknown-op.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension } from "./helpers.js";

describe("qdistroDispatcher", () => {
  let env;
  beforeEach(() => {
    env = loadExtension();
    env.scope.qdistroPort.connect();
  });

  it("matches replies to outstanding requests by request_id", async () => {
    const p = env.scope.qdistroDispatcher.request("tabs.list");
    const sent = env.port.sent.find((m) => m.op === "tabs.list");
    expect(sent).toBeTruthy();
    expect(typeof sent.request_id).toBe("number");
    env.port.deliver({
      op: "tabs.list.reply",
      request_id: sent.request_id,
      ok: true,
      tabs: [{ id: 1, url: "https://example.com", title: "x" }],
    });
    const r = await p;
    expect(r.ok).toBe(true);
    expect(r.tabs).toHaveLength(1);
  });

  it("ignores replies whose op does not match the pending request", async () => {
    const p = env.scope.qdistroDispatcher.request("tabs.list");
    const sent = env.port.sent.find((m) => m.op === "tabs.list");
    env.port.deliver({
      op: "cookies.export.reply",
      request_id: sent.request_id,
      ok: true,
    });
    env.port.deliver({
      op: "tabs.list.reply",
      request_id: sent.request_id,
      ok: true,
      tabs: [],
    });
    const r = await p;
    expect(r.op).toBe("tabs.list.reply");
  });

  it("routes inbound ops to registered handlers", async () => {
    env.scope.qdistroDispatcher.register("dummy.op", async (msg) => ({
      received_args: msg.args || null,
    }));
    await env.scope.qdistroDispatcher.handleInbound({
      op: "dummy.op", request_id: 42, args: { a: 1 },
    });
    const reply = env.port.sent.find(
      (m) => m.op === "dummy.op.reply" && m.request_id === 42);
    expect(reply).toBeTruthy();
    expect(reply.ok).toBe(true);
    expect(reply.received_args).toEqual({ a: 1 });
  });

  it("replies with unknown_op for unhandled inbound ops", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "nope.unknown", request_id: 7,
    });
    const reply = env.port.sent.find(
      (m) => m.op === "nope.unknown.reply" && m.request_id === 7);
    expect(reply).toBeTruthy();
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("unknown_op");
  });

  it("rejects requests that exceed their timeout", async () => {
    const p = env.scope.qdistroDispatcher.request("slow.op", {}, { timeoutMs: 10 });
    await expect(p).rejects.toThrow(/timeout/);
  });

  it("inbound op without request_id never produces a reply", async () => {
    env.scope.qdistroDispatcher.register("fire.forget", async () => ({ x: 1 }));
    await env.scope.qdistroDispatcher.handleInbound({ op: "fire.forget" });
    const reply = env.port.sent.find((m) => m.op === "fire.forget.reply");
    expect(reply).toBeUndefined();
  });

  it("handler exceptions surface as ok:false / handler_raised", async () => {
    env.scope.qdistroDispatcher.register("throws.op", async () => {
      throw new Error("kaboom");
    });
    await env.scope.qdistroDispatcher.handleInbound({
      op: "throws.op", request_id: 12,
    });
    const reply = env.port.sent.find((m) => m.op === "throws.op.reply");
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("handler_raised");
    expect(reply.detail).toMatch(/kaboom/);
  });

  it("routes inbound ops with string request_id (bridge format)", async () => {
    env.scope.qdistroDispatcher.register("tabs.list", async () => ({
      tabs: [{ id: 1 }],
    }));
    await env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.list", request_id: "r1-abc12345",
    });
    const reply = env.port.sent.find(
      (m) => m.op === "tabs.list.reply" && m.request_id === "r1-abc12345");
    expect(reply).toBeTruthy();
    expect(reply.ok).toBe(true);
    expect(reply.tabs).toEqual([{ id: 1 }]);
  });

  it("sends error reply for unknown op with string request_id", async () => {
    await env.scope.qdistroDispatcher.handleInbound({
      op: "no.such.op", request_id: "r99-deadbeef",
    });
    const reply = env.port.sent.find(
      (m) => m.op === "no.such.op.reply" && m.request_id === "r99-deadbeef");
    expect(reply).toBeTruthy();
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("unknown_op");
  });

  it("sends error reply when handler throws with string request_id", async () => {
    env.scope.qdistroDispatcher.register("throw.op", async () => {
      throw new Error("boom");
    });
    await env.scope.qdistroDispatcher.handleInbound({
      op: "throw.op", request_id: "r2-00ff00ff",
    });
    const reply = env.port.sent.find(
      (m) => m.op === "throw.op.reply" && m.request_id === "r2-00ff00ff");
    expect(reply).toBeTruthy();
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("handler_raised");
  });
});
