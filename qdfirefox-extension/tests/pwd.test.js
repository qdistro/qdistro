// pwd module — extension-initiated only.
//
// fill() and save() return Promises that resolve when the bridge
// replies. Tests must deliver a reply (or detach with .catch) — an
// un-resolved Promise leaks into the dispatcher's pending Map and
// will emit an unhandled-rejection after the 10s default timeout.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension } from "./helpers.js";

describe("qdistroPwd", () => {
  let env;
  beforeEach(() => {
    env = loadExtension();
    env.scope.qdistroPort.connect();
  });

  function replyTo(op, body) {
    const req = env.port.sent.find((m) => m.op === op);
    expect(req).toBeTruthy();
    env.port.deliver({
      op: `${op}.reply`,
      request_id: req.request_id,
      ok: true,
      ...(body || {}),
    });
    return req;
  }

  it("fill() sends pwd.fill with intent token", async () => {
    const p = env.scope.qdistroPwd.fill("https://example.com/", "alice", { nonce: "n1" });
    const req = replyTo("pwd.fill", { credentials: [] });
    expect(req).toMatchObject({
      url: "https://example.com/",
      username: "alice",
      intent_token: { nonce: "n1" },
    });
    await p;
  });

  it("fill() coerces a missing username to null", async () => {
    const p = env.scope.qdistroPwd.fill("https://example.com/", null, { nonce: "n2" });
    const req = replyTo("pwd.fill", { credentials: [] });
    expect(req.username).toBeNull();
    await p;
  });

  it("save() sends pwd.save with all credential fields", async () => {
    const p = env.scope.qdistroPwd.save("https://example.com/", "alice", "s3cret", { nonce: "n3" });
    const req = replyTo("pwd.save", { saved: true });
    expect(req).toMatchObject({
      url: "https://example.com/",
      username: "alice",
      password: "s3cret",
      intent_token: { nonce: "n3" },
    });
    await p;
  });

  // --- pwd.fill_confirm (phase 2) ----------------------------------

  it("fillConfirm() sends pwd.fill_confirm with url/username/fill_token/intent", async () => {
    const p = env.scope.qdistroPwd.fillConfirm(
      "https://example.com/", "alice", "ft-abc", { nonce: "nc1" });
    const req = replyTo("pwd.fill_confirm", {
      credentials: [{ username: "alice", password: "s3cret" }],
    });
    expect(req).toMatchObject({
      url: "https://example.com/",
      username: "alice",
      fill_token: "ft-abc",
      intent_token: { nonce: "nc1" },
    });
    await p;
  });

  it("fillConfirm() resolves with the released password", async () => {
    const p = env.scope.qdistroPwd.fillConfirm(
      "https://example.com/", "alice", "ft-abc", { nonce: "nc2" });
    replyTo("pwd.fill_confirm", {
      credentials: [{ username: "alice", password: "s3cret", url: "https://example.com" }],
    });
    const r = await p;
    expect(r.ok).toBe(true);
    expect(r.credentials[0]).toMatchObject({ username: "alice", password: "s3cret" });
  });

  it("fillConfirm() surfaces an invalid/expired token as ok:false", async () => {
    const p = env.scope.qdistroPwd.fillConfirm(
      "https://example.com/", "alice", "stale", { nonce: "nc3" });
    const req = env.port.sent.find((m) => m.op === "pwd.fill_confirm");
    env.port.deliver({
      op: "pwd.fill_confirm.reply",
      request_id: req.request_id,
      ok: false,
      error: "invalid_token",
    });
    const r = await p;
    expect(r.ok).toBe(false);
    expect(r.error).toBe("invalid_token");
  });

  it("fill() resolves with the bridge reply body", async () => {
    const p = env.scope.qdistroPwd.fill("https://example.com/", "alice", { nonce: "n4" });
    replyTo("pwd.fill", { credentials: [{ username: "alice", password: "x" }] });
    const r = await p;
    expect(r.ok).toBe(true);
    expect(r.credentials).toHaveLength(1);
  });

  // P04-C: autofill_denied error round-trips so the content script
  // can surface the right UI affordance.
  it("fill() surfaces autofill_denied as ok:false", async () => {
    const p = env.scope.qdistroPwd.fill("https://example.com/", null, { nonce: "n5" });
    const req = env.port.sent.find((m) => m.op === "pwd.fill");
    env.port.deliver({
      op: "pwd.fill.reply",
      request_id: req.request_id,
      ok: false,
      error: "autofill_denied",
    });
    const r = await p;
    expect(r.ok).toBe(false);
    expect(r.error).toBe("autofill_denied");
  });

  it("fill() surfaces vault_locked as ok:false", async () => {
    const p = env.scope.qdistroPwd.fill("https://example.com/", null, { nonce: "n6" });
    const req = env.port.sent.find((m) => m.op === "pwd.fill");
    env.port.deliver({
      op: "pwd.fill.reply",
      request_id: req.request_id,
      ok: false,
      error: "vault_locked",
    });
    const r = await p;
    expect(r.ok).toBe(false);
    expect(r.error).toBe("vault_locked");
  });

  // --- ported parity cases (qdchrome pwd suite) -----------------------

  it("exposes fill / fillConfirm / save on the module", () => {
    expect(env.scope.qdistroPwd).toBeTruthy();
    expect(typeof env.scope.qdistroPwd.fill).toBe("function");
    expect(typeof env.scope.qdistroPwd.fillConfirm).toBe("function");
    expect(typeof env.scope.qdistroPwd.save).toBe("function");
  });

  it("fill() surfaces a credential-not-found reply as ok:false", async () => {
    const p = env.scope.qdistroPwd.fill("https://example.com/", "ghost", { nonce: "n7" });
    const req = env.port.sent.find((m) => m.op === "pwd.fill");
    env.port.deliver({
      op: "pwd.fill.reply",
      request_id: req.request_id,
      ok: false,
      error: "credential_not_found",
    });
    const r = await p;
    expect(r.ok).toBe(false);
    expect(r.error).toBe("credential_not_found");
  });

  it("save() resolves with ok:true and stored_at when the bridge confirms storage", async () => {
    const p = env.scope.qdistroPwd.save(
      "https://example.com/", "bob", "hunter2", { nonce: "n8" });
    const req = env.port.sent.find((m) => m.op === "pwd.save");
    env.port.deliver({
      op: "pwd.save.reply",
      request_id: req.request_id,
      ok: true,
      stored_at: 1700000000,
    });
    const r = await p;
    expect(r.ok).toBe(true);
    expect(r.stored_at).toBe(1700000000);
  });

  it("save() surfaces vault_locked rejection from the bridge", async () => {
    const p = env.scope.qdistroPwd.save(
      "https://example.com/", "bob", "x", { nonce: "n9" });
    const req = env.port.sent.find((m) => m.op === "pwd.save");
    env.port.deliver({
      op: "pwd.save.reply",
      request_id: req.request_id,
      ok: false,
      error: "vault_locked",
    });
    const r = await p;
    expect(r.error).toBe("vault_locked");
  });

  it("save() without an intent token still ships the frame with intent_token=null (bridge enforces)", async () => {
    const p = env.scope.qdistroPwd.save(
      "https://example.com/", "carol", "p", null);
    const req = replyTo("pwd.save", { saved: false, error: "no_intent" });
    expect(req.intent_token).toBeNull();
    await p;
  });

  it("registers no inbound handlers — pwd is one-way", () => {
    expect(env.scope.qdistroDispatcher.handlers.has("pwd.fill")).toBe(false);
    expect(env.scope.qdistroDispatcher.handlers.has("pwd.save")).toBe(false);
    expect(env.scope.qdistroDispatcher.handlers.has("pwd.fill_confirm")).toBe(false);
  });

  it("uses a fresh request_id per call (concurrent fills do not collide)", async () => {
    const a = env.scope.qdistroPwd.fill("https://a.example/", null, {});
    const b = env.scope.qdistroPwd.fill("https://b.example/", null, {});
    const frames = env.port.sent.filter((m) => m.op === "pwd.fill");
    expect(frames).toHaveLength(2);
    expect(frames[0].request_id).not.toBe(frames[1].request_id);
    env.port.deliver({ op: "pwd.fill.reply", request_id: frames[0].request_id, ok: true });
    env.port.deliver({ op: "pwd.fill.reply", request_id: frames[1].request_id, ok: true });
    await Promise.all([a, b]);
  });
});
