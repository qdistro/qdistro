// screenlock module — outbound-only inhibit/release.
//
// The MV3 scaffolding ships only the outbound primitives — the
// content-script fullscreen observer lands later. These tests pin the
// wire shape so the bridge side can implement against a stable
// protocol surface. Multiple-inhibitor handling is exercised at the
// outbound level (the daemon counts; the extension forwards).
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension } from "./helpers.js";

describe("qdistroScreenlock", () => {
  let env;
  beforeEach(() => {
    env = loadExtension();
    env.scope.qdistroPort.connect();
  });

  function outboundOps(op) {
    return env.port.sent.filter((m) => m.op === op);
  }

  it("exposes inhibit / release functions", () => {
    expect(env.scope.qdistroScreenlock).toBeTruthy();
    expect(typeof env.scope.qdistroScreenlock.inhibit).toBe("function");
    expect(typeof env.scope.qdistroScreenlock.release).toBe("function");
  });

  it("inhibit() sends screenlock.inhibit with a default reason", () => {
    void env.scope.qdistroScreenlock.inhibit();
    const req = env.port.sent.find((m) => m.op === "screenlock.inhibit");
    expect(req).toMatchObject({ reason: "fullscreen_video" });
  });

  it("inhibit() emits a screenlock.inhibit frame with the reason and a numeric request_id", () => {
    void env.scope.qdistroScreenlock.inhibit("fullscreen_video");
    const frame = outboundOps("screenlock.inhibit")[0];
    expect(frame).toBeTruthy();
    expect(frame.reason).toBe("fullscreen_video");
    expect(typeof frame.request_id).toBe("number");
  });

  it("inhibit() coerces a non-string reason", () => {
    void env.scope.qdistroScreenlock.inhibit(42);
    const frame = outboundOps("screenlock.inhibit")[0];
    expect(frame.reason).toBe("42");
  });

  it("release() sends screenlock.release with the given reason", () => {
    void env.scope.qdistroScreenlock.release("user_dismissed");
    const req = env.port.sent.find((m) => m.op === "screenlock.release");
    expect(req).toMatchObject({ reason: "user_dismissed" });
  });

  it("release() defaults the reason to 'fullscreen_exit'", () => {
    void env.scope.qdistroScreenlock.release();
    const frame = outboundOps("screenlock.release")[0];
    expect(frame.reason).toBe("fullscreen_exit");
  });

  it("supports multiple concurrent inhibitors (each ships a frame with a distinct request_id)", () => {
    void env.scope.qdistroScreenlock.inhibit("video-a");
    void env.scope.qdistroScreenlock.inhibit("video-b");
    void env.scope.qdistroScreenlock.inhibit("presentation");
    const frames = outboundOps("screenlock.inhibit");
    expect(frames).toHaveLength(3);
    expect(frames.map((f) => f.reason)).toEqual([
      "video-a", "video-b", "presentation",
    ]);
    const ids = new Set(frames.map((f) => f.request_id));
    expect(ids.size).toBe(3);
  });

  it("inhibit + release sequence carries distinct ops", () => {
    void env.scope.qdistroScreenlock.inhibit("video-a");
    void env.scope.qdistroScreenlock.release("video-a-ended");
    expect(outboundOps("screenlock.inhibit")).toHaveLength(1);
    expect(outboundOps("screenlock.release")).toHaveLength(1);
  });

  it("inhibit() resolves on a successful bridge reply", async () => {
    const p = env.scope.qdistroScreenlock.inhibit("video");
    const frame = outboundOps("screenlock.inhibit")[0];
    env.port.deliver({
      op: "screenlock.inhibit.reply",
      request_id: frame.request_id,
      ok: true,
      cookie: "inh-1",
    });
    const r = await p;
    expect(r.ok).toBe(true);
    expect(r.cookie).toBe("inh-1");
  });

  it("release() surfaces a bridge error reply", async () => {
    const p = env.scope.qdistroScreenlock.release("done");
    const frame = outboundOps("screenlock.release")[0];
    env.port.deliver({
      op: "screenlock.release.reply",
      request_id: frame.request_id,
      ok: false,
      error: "no_active_inhibitor",
    });
    const r = await p;
    expect(r.error).toBe("no_active_inhibitor");
  });

  it("registers no inbound handlers — screenlock is one-way", () => {
    expect(env.scope.qdistroDispatcher.handlers.has("screenlock.inhibit")).toBe(false);
    expect(env.scope.qdistroDispatcher.handlers.has("screenlock.release")).toBe(false);
  });
});
