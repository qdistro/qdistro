// cookies module — extension-initiated, intent-token-gated.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadExtension, makeFakeBrowser, makeFakePort } from "./helpers.js";

describe("qdistroCookies", () => {
  let env;
  beforeEach(() => {
    const browser = makeFakeBrowser();
    browser.cookies.getAll = (_q) => Promise.resolve([
      { name: "sid", value: "abc", domain: ".example.com", path: "/",
        secure: true, httpOnly: true, sameSite: "lax", expirationDate: 1700000000,
        storeId: "firefox-default", firstPartyDomain: "" },
    ]);
    env = loadExtension({ browser, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
  });

  it("requires an intent token", async () => {
    await expect(env.scope.qdistroCookies.exportForUrl("https://example.com/", null))
      .rejects.toThrow(/intent_token_required/);
  });

  it("serializes cookies with snake_case fields and store_id", async () => {
    const token = await env.scope.qdistroIntent.mint("cookies.export");
    const p = env.scope.qdistroCookies.exportForUrl("https://example.com/", token);
    // exportForUrl awaits getAllForUrl → cookies.getAll, then dispatcher.request
    // → port.send. That's a 2-hop microtask chain; vi.waitFor polls until the
    // send lands rather than gambling on macrotask ordering.
    const req = await vi.waitFor(
      () => {
        const m = env.port.sent.find((x) => x.op === "cookies.export");
        if (!m) throw new Error("cookies.export not yet sent");
        return m;
      },
      { timeout: 1000 },
    );
    expect(req.cookies[0]).toMatchObject({
      name: "sid", domain: ".example.com",
      http_only: true, same_site: "lax",
      expires: 1700000000, store_id: "firefox-default",
    });
    env.port.deliver({
      op: "cookies.export.reply", request_id: req.request_id,
      ok: true, accepted: 1,
    });
    const r = await p;
    expect(r.ok).toBe(true);
  });

  it("forwards firstPartyDomain:null on the cookies.getAll query", async () => {
    let captured = null;
    const browser = makeFakeBrowser();
    browser.cookies.getAll = (q) => { captured = q; return Promise.resolve([]); };
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    const token = await env2.scope.qdistroIntent.mint("cookies.export");
    env2.scope.qdistroCookies.exportForUrl("https://example.com/", token);
    await vi.waitFor(() => {
      if (!captured) throw new Error("getAll not yet called");
    }, { timeout: 1000 });
    expect(captured).toMatchObject({
      url: "https://example.com/",
      firstPartyDomain: null,
    });
  });

  it("scopes getAll to storeId when cookieStoreId is passed", async () => {
    let captured = null;
    const browser = makeFakeBrowser();
    browser.cookies.getAll = (q) => { captured = q; return Promise.resolve([]); };
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    const token = await env2.scope.qdistroIntent.mint("cookies.export");
    env2.scope.qdistroCookies.exportForUrl("https://example.com/", token, {
      cookieStoreId: "firefox-container-3",
    });
    await vi.waitFor(() => {
      if (!captured || !captured.storeId) throw new Error("storeId not yet seen");
    }, { timeout: 1000 });
    expect(captured.storeId).toBe("firefox-container-3");
  });

  it("surfaces a missing cookies API as cookies_api_unavailable", async () => {
    const browser = makeFakeBrowser();
    browser.cookies = undefined;
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    const token = await env2.scope.qdistroIntent.mint("cookies.export");
    await expect(env2.scope.qdistroCookies.exportForUrl("https://x/", token))
      .rejects.toThrow(/cookies_api_unavailable/);
  });

  // --- ported edge cases (parity with qdchrome cookies suite) ---------

  function waitForOutbound(e, op) {
    return vi.waitFor(() => {
      const m = e.port.sent.find((x) => x.op === op);
      if (!m) throw new Error(`${op} not yet sent`);
      return m;
    }, { timeout: 1000 });
  }

  it("floors a fractional expirationDate and marks non-session cookies", async () => {
    const browser = makeFakeBrowser();
    browser.cookies.getAll = () => Promise.resolve([{
      name: "session", value: "abc", domain: ".example.com", path: "/",
      secure: true, httpOnly: true, sameSite: "lax",
      expirationDate: 1700000000.7, session: false, storeId: "firefox-default",
    }]);
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    const token = await env2.scope.qdistroIntent.mint("cookies.export");
    env2.scope.qdistroCookies.exportForUrl("https://example.com/", token);
    const frame = await waitForOutbound(env2, "cookies.export");
    expect(frame.cookies[0]).toMatchObject({
      name: "session", value: "abc", domain: ".example.com", path: "/",
      secure: true, http_only: true, same_site: "lax",
      expires: 1700000000, // floored
      session: false,
    });
  });

  it("flags a session cookie (no expirationDate) with expires=null", async () => {
    const browser = makeFakeBrowser();
    browser.cookies.getAll = () => Promise.resolve([{
      name: "tracker", value: "xyz", domain: ".example.com", path: "/",
      session: true, // no expirationDate
    }]);
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    const token = await env2.scope.qdistroIntent.mint("cookies.export");
    env2.scope.qdistroCookies.exportForUrl("https://example.com/", token);
    const frame = await waitForOutbound(env2, "cookies.export");
    expect(frame.cookies[0].expires).toBeNull();
    expect(frame.cookies[0].session).toBe(true);
  });

  it("defaults same_site to 'no_restriction' when the cookie omits sameSite", async () => {
    const browser = makeFakeBrowser();
    browser.cookies.getAll = () => Promise.resolve([{
      name: "n", value: "v", domain: ".x", path: "/",
    }]);
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    const token = await env2.scope.qdistroIntent.mint("cookies.export");
    env2.scope.qdistroCookies.exportForUrl("https://x/", token);
    const frame = await waitForOutbound(env2, "cookies.export");
    expect(frame.cookies[0].same_site).toBe("no_restriction");
  });

  it("handles the no-cookies case (empty array) and resolves on the bridge reply", async () => {
    const browser = makeFakeBrowser();
    browser.cookies.getAll = () => Promise.resolve([]);
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    const token = await env2.scope.qdistroIntent.mint("cookies.export");
    const p = env2.scope.qdistroCookies.exportForUrl("https://empty.example/", token);
    const frame = await waitForOutbound(env2, "cookies.export");
    expect(frame.cookies).toEqual([]);
    env2.port.deliver({
      op: "cookies.export.reply", request_id: frame.request_id,
      ok: true, audit_id: "audit-1", stored: 0,
    });
    const r = await p;
    expect(r.ok).toBe(true);
    expect(r.stored).toBe(0);
  });

  it("propagates a rejected cookies.getAll as the exportForUrl rejection", async () => {
    const browser = makeFakeBrowser();
    browser.cookies.getAll = () => Promise.reject(new Error("cookies_perm_denied"));
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    const token = await env2.scope.qdistroIntent.mint("cookies.export");
    await expect(env2.scope.qdistroCookies.exportForUrl("https://x/", token))
      .rejects.toThrow(/cookies_perm_denied/);
    // No frame should have gone out — getAll rejected before the send.
    expect(env2.port.sent.find((m) => m.op === "cookies.export")).toBeUndefined();
  });

  it("surfaces an audit_id from the daemon on a successful export", async () => {
    const browser = makeFakeBrowser();
    browser.cookies.getAll = () => Promise.resolve([]);
    const env2 = loadExtension({ browser, portHandle: makeFakePort() });
    env2.scope.qdistroPort.connect();
    const token = await env2.scope.qdistroIntent.mint("cookies.export");
    const p = env2.scope.qdistroCookies.exportForUrl("https://example.com/", token);
    const frame = await waitForOutbound(env2, "cookies.export");
    env2.port.deliver({
      op: "cookies.export.reply", request_id: frame.request_id,
      ok: true, audit_id: "ax-1", stored: 2,
    });
    const r = await p;
    expect(r.audit_id).toBe("ax-1");
    expect(r.stored).toBe(2);
  });
});
