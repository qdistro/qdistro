// downloads module tests — chrome.downloads.onChanged listener
// installation, downloads.update frame shape, state transitions
// (in_progress / complete / interrupted), payload fields.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension, makeFakeChrome, makeFakePort } from "./helpers.js";

describe("qdistroDownloads", () => {
  let env;
  let chrome;
  let searchCalls;
  let nextItem;

  beforeEach(() => {
    chrome = makeFakeChrome();
    searchCalls = [];
    nextItem = null;
    chrome.downloads.search = (q, cb) => {
      searchCalls.push(q);
      cb(nextItem ? [nextItem] : []);
    };
    env = loadExtension({ chrome, portHandle: makeFakePort() });
    env.scope.qdistroPort.connect();
  });

  function outboundOps(op) {
    return env.port.sent.filter((m) => m.op === op);
  }

  it("exposes qdistroDownloads", () => {
    expect(env.scope.qdistroDownloads).toBeTruthy();
    expect(typeof env.scope.qdistroDownloads.install).toBe("function");
    expect(typeof env.scope.qdistroDownloads.snapshot).toBe("function");
  });

  it("install() registers a chrome.downloads.onChanged listener", () => {
    env.scope.qdistroDownloads.install();
    expect(chrome.downloads.onChanged.listeners.length).toBe(1);
  });

  it("install() is a no-op when chrome.downloads is missing", () => {
    chrome.downloads = undefined;
    const env2 = loadExtension({ chrome, portHandle: makeFakePort() });
    // Should not throw.
    expect(() => env2.scope.qdistroDownloads.install()).not.toThrow();
  });

  it("snapshot() returns the canonical wire shape", () => {
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

  it("snapshot() defaults state to in_progress when missing", () => {
    const snap = env.scope.qdistroDownloads.snapshot({ id: 1 });
    expect(snap.state).toBe("in_progress");
    expect(snap.total_bytes).toBe(0);
    expect(snap.bytes_received).toBe(0);
  });

  it("onChanged delta triggers a downloads.search and forwards a downloads.update", () => {
    env.scope.qdistroDownloads.install();
    nextItem = {
      id: 11, url: "https://x/a.zip",
      filename: "/tmp/a.zip",
      state: "in_progress",
      totalBytes: 4096, bytesReceived: 512,
    };
    chrome.downloads.onChanged.fire({ id: 11, state: { current: "in_progress" } });
    expect(searchCalls).toEqual([{ id: 11 }]);
    const frames = outboundOps("downloads.notify");
    expect(frames).toHaveLength(1);
    expect(frames[0]).toMatchObject({
      download_id: 11, state: "in_progress",
      total_bytes: 4096, bytes_received: 512,
    });
  });

  it("forwards a 'complete' transition", () => {
    env.scope.qdistroDownloads.install();
    nextItem = {
      id: 12, url: "https://x/b.zip",
      filename: "/tmp/b.zip", state: "complete",
      totalBytes: 2048, bytesReceived: 2048,
    };
    chrome.downloads.onChanged.fire({ id: 12, state: { current: "complete" } });
    const frame = outboundOps("downloads.notify")[0];
    expect(frame.state).toBe("complete");
    expect(frame.bytes_received).toBe(2048);
  });

  it("forwards an 'interrupted' (cancelled/failed) transition", () => {
    env.scope.qdistroDownloads.install();
    nextItem = {
      id: 13, url: "https://x/c.zip", filename: "",
      state: "interrupted",
      totalBytes: 0, bytesReceived: 0,
    };
    chrome.downloads.onChanged.fire({ id: 13, state: { current: "interrupted" } });
    const frame = outboundOps("downloads.notify")[0];
    expect(frame.state).toBe("interrupted");
    expect(frame.download_id).toBe(13);
  });

  it("skips forwarding when downloads.search returns no item", () => {
    env.scope.qdistroDownloads.install();
    nextItem = null; // search returns []
    chrome.downloads.onChanged.fire({ id: 99 });
    expect(outboundOps("downloads.notify")).toHaveLength(0);
  });

  it("skips forwarding when chrome.runtime.lastError is set on search", () => {
    env.scope.qdistroDownloads.install();
    chrome.downloads.search = (q, cb) => {
      chrome.runtime.lastError = { message: "perm_denied" };
      cb([{ id: q.id, state: "in_progress" }]);
      chrome.runtime.lastError = null;
    };
    chrome.downloads.onChanged.fire({ id: 50 });
    expect(outboundOps("downloads.notify")).toHaveLength(0);
  });

  it("each onChanged event produces an independent request_id", () => {
    env.scope.qdistroDownloads.install();
    nextItem = { id: 1, state: "in_progress" };
    chrome.downloads.onChanged.fire({ id: 1 });
    nextItem = { id: 1, state: "complete" };
    chrome.downloads.onChanged.fire({ id: 1 });
    const frames = outboundOps("downloads.notify");
    expect(frames).toHaveLength(2);
    expect(frames[0].request_id).not.toBe(frames[1].request_id);
  });

  it("swallows a bridge reply with ok:false (fire-and-forget) and clears the pending slot", async () => {
    env.scope.qdistroDownloads.install();
    nextItem = { id: 1, state: "complete" };
    chrome.downloads.onChanged.fire({ id: 1 });
    const frame = outboundOps("downloads.notify")[0];
    expect(frame).toBeTruthy();
    // The outbound request is tracked as pending until a correlated
    // reply lands.
    const dispatcher = env.scope.qdistroDispatcher;
    expect(dispatcher.pending.has(frame.request_id)).toBe(true);

    // Deliver an error reply. The module .catch()s the rejected
    // round-trip, so there's no unhandled rejection — but the reply
    // must still be CORRELATED: the dispatcher removes the pending
    // slot (clearing its timeout) rather than treating it as an
    // orphan. A swallowed reply that left the slot pending would leak
    // the 10s timer and eventually reject.
    env.port.deliver({
      op: "downloads.notify.reply",
      request_id: frame.request_id,
      ok: false,
      error: "policy_denied",
    });
    // Let the microtask queue drain so the module's .catch() runs.
    await new Promise((r) => setTimeout(r, 0));

    // Pending slot cleared (reply was correlated and consumed)...
    expect(dispatcher.pending.has(frame.request_id)).toBe(false);
    // ...and the error reply produced no follow-up outbound frame
    // (fire-and-forget: the extension does not retry or re-emit).
    expect(outboundOps("downloads.notify")).toHaveLength(1);
  });
});
