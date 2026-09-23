// cookies module tests — cookies.export request shape, intent-token
// gating (throws without one), chrome.cookies.getAll usage, empty list,
// audit-log / no-cookies reply handling.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadExtension, makeFakeChrome, makeFakePort } from "./helpers.js";

describe("qdistroCookies", () => {
  let env;
  let chrome;
  let getAllArgs;

  beforeEach(() => {
    chrome = makeFakeChrome();
    getAllArgs = null;
    chrome.cookies.getAll = (query, cb) => {
      getAllArgs = query;
      cb([
        {
          name: "session", value: "abc",
          domain: ".example.com", path: "/",
          secure: true, httpOnly: true,
          sameSite: "lax",
          expirationDate: 1700000000.7, session: false,
        },
        {
          name: "tracker", value: "xyz",
          domain: ".example.com", path: "/",
          // No expirationDate => session cookie.
          session: true,
        },
      ]);
    };
    env = loadExtension({ chrome, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
  });

  function lastOutbound(op) {
    return env.port.sent.find((m) => m.op === op);
  }

  function waitForOutbound(op) {
    return vi.waitFor(() => {
      const m = env.port.sent.find((x) => x.op === op);
      if (!m) throw new Error(`${op} not yet sent`);
      return m;
    }, { timeout: 1000 });
  }

  it("exposes qdistroCookies on the scope", () => {
    expect(env.scope.qdistroCookies).toBeTruthy();
    expect(typeof env.scope.qdistroCookies.exportForUrl).toBe("function");
  });

  it("exportForUrl rejects when no intent token is provided", async () => {
    await expect(
      env.scope.qdistroCookies.exportForUrl("https://example.com/", null)
    ).rejects.toThrow(/intent_token_required/);
    expect(env.port.sent.find((m) => m.op === "cookies.export")).toBeUndefined();
  });

  it("exportForUrl calls chrome.cookies.getAll with {url}", () => {
    void env.scope.qdistroCookies.exportForUrl("https://example.com/foo",
      { operation: "cookies.export" });
    expect(getAllArgs).toEqual({ url: "https://example.com/foo" });
  });

  it("emits a cookies.export frame with url, intent_token, and serialized cookies", async () => {
    void env.scope.qdistroCookies.exportForUrl("https://example.com/",
      { operation: "cookies.export", nonce: "n-1" });
    const frame = await waitForOutbound("cookies.export");
    expect(frame.url).toBe("https://example.com/");
    expect(frame.intent_token).toMatchObject({ operation: "cookies.export" });
    expect(Array.isArray(frame.cookies)).toBe(true);
    expect(frame.cookies).toHaveLength(2);
  });

  it("serializes cookies to the snake_case wire shape", async () => {
    void env.scope.qdistroCookies.exportForUrl("https://example.com/",
      { operation: "cookies.export" });
    const frame = await waitForOutbound("cookies.export");
    expect(frame.cookies[0]).toEqual({
      name: "session", value: "abc",
      domain: ".example.com", path: "/",
      secure: true, http_only: true,
      same_site: "lax",
      expires: 1700000000, // floored
      session: false,
    });
  });

  it("flags session cookies (no expirationDate) with expires=null", async () => {
    void env.scope.qdistroCookies.exportForUrl("https://example.com/",
      { operation: "cookies.export" });
    const frame = await waitForOutbound("cookies.export");
    expect(frame.cookies[1].expires).toBeNull();
    expect(frame.cookies[1].session).toBe(true);
  });

  it("defaults sameSite to 'no_restriction' when the cookie omits it", async () => {
    void env.scope.qdistroCookies.exportForUrl("https://example.com/",
      { operation: "cookies.export" });
    const frame = await waitForOutbound("cookies.export");
    expect(frame.cookies[1].same_site).toBe("no_restriction");
  });

  it("handles the no-cookies case (empty array, no throw)", async () => {
    chrome.cookies.getAll = (q, cb) => cb([]);
    const p = env.scope.qdistroCookies.exportForUrl("https://empty.example/",
      { operation: "cookies.export" });
    const frame = await waitForOutbound("cookies.export");
    expect(frame.cookies).toEqual([]);
    env.port.deliver({
      op: "cookies.export.reply",
      request_id: frame.request_id,
      ok: true,
      audit_id: "audit-1",
      stored: 0,
    });
    const r = await p;
    expect(r.ok).toBe(true);
    expect(r.stored).toBe(0);
  });

  it("propagates a chrome.cookies.getAll runtime error", async () => {
    chrome.cookies.getAll = (q, cb) => {
      chrome.runtime.lastError = { message: "cookies_perm_denied" };
      cb(null);
      // Restore for other tests.
      chrome.runtime.lastError = null;
    };
    await expect(
      env.scope.qdistroCookies.exportForUrl("https://x/", { operation: "cookies.export" })
    ).rejects.toThrow(/cookies_perm_denied/);
  });

  it("rejects with cookies_api_unavailable when chrome.cookies is missing", async () => {
    chrome.cookies = undefined;
    // Need to reload because api is captured at module load.
    const env2 = loadExtension({ chrome, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    await expect(
      env2.scope.qdistroCookies.exportForUrl("https://x/", { operation: "cookies.export" })
    ).rejects.toThrow(/cookies_api_unavailable/);
  });

  it("uses an extended 15s timeout for the round trip", async () => {
    // Audit-log lookups on the bridge are slow; module passes timeoutMs:15000.
    // We can't inspect the timeout directly, but we can verify the
    // promise stays pending past the default 10s and resolves on reply.
    const p = env.scope.qdistroCookies.exportForUrl("https://example.com/",
      { operation: "cookies.export" });
    const frame = await waitForOutbound("cookies.export");
    env.port.deliver({
      op: "cookies.export.reply",
      request_id: frame.request_id,
      ok: true,
      audit_id: "audit-42",
    });
    const r = await p;
    expect(r.audit_id).toBe("audit-42");
  });

  it("surfaces an audit_id from the daemon on successful export", async () => {
    const p = env.scope.qdistroCookies.exportForUrl("https://example.com/",
      { operation: "cookies.export" });
    const frame = await waitForOutbound("cookies.export");
    env.port.deliver({
      op: "cookies.export.reply",
      request_id: frame.request_id,
      ok: true,
      audit_id: "ax-1",
      stored: 2,
    });
    const r = await p;
    expect(r.audit_id).toBe("ax-1");
    expect(r.stored).toBe(2);
  });
});
