// downloads module — browser.downloads.onChanged listener forwards
// state transitions (in_progress / complete / interrupted) as
// downloads.notify frames. Firefox's downloads.search returns a Promise
// (vs Chromium's callback), and the listener swallows a rejected search
// rather than crashing.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadExtension, makeFakeBrowser, makeFakePort } from "./helpers.js";

describe("qdistroDownloads", () => {
  let env;
  let browser;
  let searchCalls;
  let nextItem;

  beforeEach(() => {
    browser = makeFakeBrowser();
    searchCalls = [];
    nextItem = null;
    browser.downloads.search = (q) => {
      searchCalls.push(q);
      return Promise.resolve(nextItem ? [nextItem] : []);
    };
    env = loadExtension({ browser, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
  });

  function outboundOps(op) {
    return env.port.sent.filter((m) => m.op === op);
  }

  function waitForOutbound(op) {
    return vi.waitFor(() => {
      const m = env.port.sent.find((x) => x.op === op);
      if (!m) throw new Error(`${op} not yet sent`);
      return m;
    }, { timeout: 1000 });
  }

  it("exposes qdistroDownloads", () => {
    expect(env.scope.qdistroDownloads).toBeTruthy();
    expect(typeof env.scope.qdistroDownloads.install).toBe("function");
    expect(typeof env.scope.qdistroDownloads.snapshot).toBe("function");
  });

  it("install() registers a browser.downloads.onChanged listener", () => {
    env.scope.qdistroDownloads.install();
    expect(browser.downloads.onChanged.listeners.length).toBe(1);
  });

  it("install() is a no-op when browser.downloads is missing", () => {
    const browser2 = makeFakeBrowser();
    browser2.downloads = undefined;
    const env2 = loadExtension({ browser: browser2, portHandle: makeFakePort() });
    expect(() => env2.scope.qdistroDownloads.install()).not.toThrow();
  });

  it("snapshot() returns the canonical wire shape (download_id, not id)", () => {
    const snap = env.scope.qdistroDownloads.snapshot({
      id: 7, url: "https://files.example/x.zip",
      filename: "/tmp/x.zip",
      state: "in_progress",
      totalBytes: 1024, bytesReceived: 128,
      mime: "application/zip", startTime: "2024-01-01T00:00:00Z",
    });
    expect(snap).toEqual({
      download_id: 7,
      url: "https://files.example/x.zip",
      filename: "/tmp/x.zip",
      state: "in_progress",
      total_bytes: 1024,
      bytes_received: 128,
      mime: "application/zip",
    });
    expect(snap.id).toBeUndefined();
  });

  it("snapshot() returns null for a null/undefined item", () => {
    expect(env.scope.qdistroDownloads.snapshot(null)).toBeNull();
    expect(env.scope.qdistroDownloads.snapshot(undefined)).toBeNull();
  });

  it("snapshot() falls back to finalUrl when url is empty", () => {
    const snap = env.scope.qdistroDownloads.snapshot({
      id: 1, url: "", finalUrl: "https://cdn.example/redirected",
    });
    expect(snap.url).toBe("https://cdn.example/redirected");
  });

  it("snapshot() defaults state to in_progress and byte counts to 0 when missing", () => {
    const snap = env.scope.qdistroDownloads.snapshot({ id: 1 });
    expect(snap.state).toBe("in_progress");
    expect(snap.total_bytes).toBe(0);
    expect(snap.bytes_received).toBe(0);
  });

  it("onChanged delta triggers a downloads.search and forwards a downloads.notify", async () => {
    env.scope.qdistroDownloads.install();
    nextItem = {
      id: 11, url: "https://x/a.zip",
      filename: "/tmp/a.zip",
      state: "in_progress",
      totalBytes: 4096, bytesReceived: 512,
    };
    browser.downloads.onChanged.fire({ id: 11, state: { current: "in_progress" } });
    const frame = await waitForOutbound("downloads.notify");
    expect(searchCalls).toEqual([{ id: 11 }]);
    expect(frame).toMatchObject({
      download_id: 11, state: "in_progress",
      total_bytes: 4096, bytes_received: 512,
    });
  });

  it("forwards a 'complete' transition", async () => {
    env.scope.qdistroDownloads.install();
    nextItem = {
      id: 12, url: "https://x/b.zip",
      filename: "/tmp/b.zip", state: "complete",
      totalBytes: 2048, bytesReceived: 2048,
    };
    browser.downloads.onChanged.fire({ id: 12, state: { current: "complete" } });
    const frame = await waitForOutbound("downloads.notify");
    expect(frame.state).toBe("complete");
    expect(frame.bytes_received).toBe(2048);
  });

  it("forwards an 'interrupted' (cancelled/failed) transition", async () => {
    env.scope.qdistroDownloads.install();
    nextItem = {
      id: 13, url: "https://x/c.zip", filename: "",
      state: "interrupted",
      totalBytes: 0, bytesReceived: 0,
    };
    browser.downloads.onChanged.fire({ id: 13, state: { current: "interrupted" } });
    const frame = await waitForOutbound("downloads.notify");
    expect(frame.state).toBe("interrupted");
    expect(frame.download_id).toBe(13);
  });

  it("skips forwarding when downloads.search resolves to no item", async () => {
    env.scope.qdistroDownloads.install();
    nextItem = null; // search resolves []
    browser.downloads.onChanged.fire({ id: 99 });
    // Give the microtask chain a chance to (not) emit.
    await new Promise((r) => setTimeout(r, 10));
    expect(outboundOps("downloads.notify")).toHaveLength(0);
  });

  it("swallows a rejected downloads.search (listener never throws)", async () => {
    env.scope.qdistroDownloads.install();
    browser.downloads.search = () => Promise.reject(new Error("perm_denied"));
    // Firing must not produce an unhandled rejection or a frame.
    expect(() =>
      browser.downloads.onChanged.fire({ id: 50 })
    ).not.toThrow();
    await new Promise((r) => setTimeout(r, 10));
    expect(outboundOps("downloads.notify")).toHaveLength(0);
  });

  it("each onChanged event produces an independent request_id", async () => {
    env.scope.qdistroDownloads.install();
    nextItem = { id: 1, state: "in_progress" };
    browser.downloads.onChanged.fire({ id: 1 });
    await waitForOutbound("downloads.notify");
    nextItem = { id: 1, state: "complete" };
    browser.downloads.onChanged.fire({ id: 1 });
    await vi.waitFor(() => {
      if (outboundOps("downloads.notify").length < 2) {
        throw new Error("second downloads.notify not yet sent");
      }
    }, { timeout: 1000 });
    const frames = outboundOps("downloads.notify");
    expect(frames).toHaveLength(2);
    expect(frames[0].request_id).not.toBe(frames[1].request_id);
  });

  it("swallows a bridge reply with ok:false (fire-and-forget) and clears the pending slot", async () => {
    env.scope.qdistroDownloads.install();
    nextItem = { id: 1, state: "complete" };
    browser.downloads.onChanged.fire({ id: 1 });
    const frame = await waitForOutbound("downloads.notify");
    const dispatcher = env.scope.qdistroDispatcher;
    expect(dispatcher.pending.has(frame.request_id)).toBe(true);

    env.port.deliver({
      op: "downloads.notify.reply",
      request_id: frame.request_id,
      ok: false,
      error: "policy_denied",
    });
    await new Promise((r) => setTimeout(r, 0));

    // Reply was correlated (slot cleared, timer killed) and produced no
    // follow-up outbound frame.
    expect(dispatcher.pending.has(frame.request_id)).toBe(false);
    expect(outboundOps("downloads.notify")).toHaveLength(1);
  });
});
