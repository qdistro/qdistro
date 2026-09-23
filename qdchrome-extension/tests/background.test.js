// background.js — runtime.onMessage entry points for popup and the
// pwd/mpris/screenlock content scripts. Chrome's listener uses
// sendResponse(cb) + return true; helpers.js's loadWithBackground
// wraps that in a Promise.
import { describe, it, expect, beforeEach } from "vitest";
import { loadWithBackground, makeFakeChromeAllOrigins, makeFakePort } from "./helpers.js";

describe("background runtime.onMessage", () => {
  let env;
  beforeEach(() => {
    // Origin gate is closed by default since J11; these tests exercise
    // op-forwarding, so opt in to all origins (`*`) — origin filtering
    // itself is covered in gate.test.js.
    env = loadWithBackground({ chrome: makeFakeChromeAllOrigins() });
    env.scope.qdistroPort.connect();
  });

  function waitForSent(op, timeoutMs = 1000) {
    return new Promise((resolve, reject) => {
      const start = Date.now();
      const tick = () => {
        const m = env.port.sent.find((x) => x.op === op);
        if (m) return resolve(m);
        if (Date.now() - start > timeoutMs) {
          return reject(new Error(`timeout waiting for ${op}`));
        }
        setTimeout(tick, 5);
      };
      tick();
    });
  }

  it("rejects messages whose sender.id != runtime.id", async () => {
    const r = await env.sendMessage({ kind: "status" }, { id: "evil-ext-id" });
    expect(r).toEqual({ ok: false, error: "untrusted_sender" });
  });

  it("rejects non-object payloads", async () => {
    const r = await env.sendMessage("nope");
    expect(r).toEqual({ ok: false, error: "bad_request" });
  });

  it("status returns the current port connectedness", async () => {
    const r = await env.sendMessage({ kind: "status" });
    expect(r).toEqual({ ok: true, connected: true });
  });

  it("unknown kind returns unknown_kind", async () => {
    const r = await env.sendMessage({ kind: "no-such-kind" });
    expect(r).toEqual({ ok: false, error: "unknown_kind" });
  });

  // A content-script sender always carries a tab; the browser sets
  // sender.tab.url to the real frame origin.
  function csSender(url) {
    return { id: env.scope.chrome.runtime.id, tab: { id: 7, url } };
  }

  describe("pwd content-script entry points", () => {
    it("pwd.request_fill derives the URL from sender.tab.url and forwards as pwd.fill", async () => {
      const p = env.sendMessage({
        kind: "pwd.request_fill",
        url: "https://example.com/login",
        username: "alice",
      }, csSender("https://example.com/login"));
      const req = await waitForSent("pwd.fill");
      expect(req.url).toBe("https://example.com/login");
      expect(req.username).toBe("alice");
      expect(req.intent_token.op).toBe("pwd.fill");
      env.port.deliver({
        op: "pwd.fill.reply", request_id: req.request_id, ok: true,
        credentials: [{ username: "alice", password: "secret" }],
      });
      const r = await p;
      expect(r.ok).toBe(true);
      expect(r.response.credentials).toHaveLength(1);
    });

    it("pwd.request_fill REJECTS when req.url mismatches sender.tab.url (finding #10)", async () => {
      const r = await env.sendMessage({
        kind: "pwd.request_fill",
        url: "https://evil.example/phish", // page-supplied lie
        username: "alice",
      }, csSender("https://bank.example/login"));
      expect(r).toEqual({ ok: false, error: "url_mismatch" });
      expect(env.port.sent.find((m) => m.op === "pwd.fill")).toBeUndefined();
    });

    it("pwd.request_fill REJECTS when there is no tab origin (finding #10)", async () => {
      const r = await env.sendMessage({
        kind: "pwd.request_fill",
        url: "https://example.com/login",
      });
      expect(r).toEqual({ ok: false, error: "no_tab_url" });
      expect(env.port.sent.find((m) => m.op === "pwd.fill")).toBeUndefined();
    });

    it("pwd.request_fill_confirm forwards url/username/fill_token as pwd.fill_confirm", async () => {
      const p = env.sendMessage({
        kind: "pwd.request_fill_confirm",
        url: "https://example.com/login",
        username: "alice",
        fill_token: "ft-xyz",
      }, csSender("https://example.com/login"));
      const req = await waitForSent("pwd.fill_confirm");
      expect(req.url).toBe("https://example.com/login");
      expect(req.username).toBe("alice");
      expect(req.fill_token).toBe("ft-xyz");
      expect(req.intent_token.op).toBe("pwd.fill_confirm");
      env.port.deliver({
        op: "pwd.fill_confirm.reply", request_id: req.request_id, ok: true,
        credentials: [{ username: "alice", password: "secret", url: "https://example.com" }],
      });
      const r = await p;
      expect(r.ok).toBe(true);
      expect(r.response.credentials[0].password).toBe("secret");
    });

    it("pwd.request_fill_confirm REJECTS a mismatched page-supplied URL (finding #10)", async () => {
      const r = await env.sendMessage({
        kind: "pwd.request_fill_confirm",
        url: "https://evil.example/phish",
        username: "alice",
        fill_token: "ft-xyz",
      }, csSender("https://bank.example/login"));
      expect(r).toEqual({ ok: false, error: "url_mismatch" });
      expect(env.port.sent.find((m) => m.op === "pwd.fill_confirm")).toBeUndefined();
    });

    it("pwd.request_fill_confirm REJECTS a missing username or fill_token", async () => {
      const noUser = await env.sendMessage({
        kind: "pwd.request_fill_confirm",
        url: "https://example.com/login",
        fill_token: "ft-xyz",
      }, csSender("https://example.com/login"));
      expect(noUser).toEqual({ ok: false, error: "invalid_request" });
      const noToken = await env.sendMessage({
        kind: "pwd.request_fill_confirm",
        url: "https://example.com/login",
        username: "alice",
      }, csSender("https://example.com/login"));
      expect(noToken).toEqual({ ok: false, error: "invalid_request" });
      expect(env.port.sent.find((m) => m.op === "pwd.fill_confirm")).toBeUndefined();
    });

    it("pwd.request_save derives the URL from sender.tab.url and forwards credentials", async () => {
      const p = env.sendMessage({
        kind: "pwd.request_save",
        url: "https://example.com/login",
        username: "alice",
        password: "s3cret!",
      }, csSender("https://example.com/login"));
      const req = await waitForSent("pwd.save");
      expect(req).toMatchObject({
        url: "https://example.com/login",
        username: "alice",
        password: "s3cret!",
      });
      expect(req.intent_token.op).toBe("pwd.save");
      env.port.deliver({
        op: "pwd.save.reply", request_id: req.request_id, ok: true, saved: true,
      });
      await p;
    });

    it("pwd.request_save REJECTS a mismatched page-supplied URL", async () => {
      const r = await env.sendMessage({
        kind: "pwd.request_save",
        url: "https://evil.example/",
        username: "alice",
        password: "s3cret!",
      }, csSender("https://bank.example/login"));
      expect(r).toEqual({ ok: false, error: "url_mismatch" });
      expect(env.port.sent.find((m) => m.op === "pwd.save")).toBeUndefined();
    });
  });

  describe("cookies.export consent gate (finding #11)", () => {
    const popupUrl = "chrome-extension://test-ext-id/popup.html";

    function popupSender() {
      return { id: env.scope.chrome.runtime.id, url: popupUrl };
    }

    it("DENIES a content-script sender (sender.tab present)", async () => {
      const r = await env.sendMessage(
        { kind: "cookies.export", url: "https://example.com/" },
        { id: env.scope.chrome.runtime.id, tab: { id: 3, url: "https://example.com/" }, url: popupUrl },
      );
      expect(r).toEqual({ ok: false, error: "popup_required" });
      expect(env.port.sent.find((m) => m.op === "cookies.export")).toBeUndefined();
    });

    it("DENIES a non-popup extension-page sender", async () => {
      const r = await env.sendMessage(
        { kind: "cookies.export", url: "https://example.com/" },
        { id: env.scope.chrome.runtime.id, url: "chrome-extension://test-ext-id/options.html" },
      );
      expect(r).toEqual({ ok: false, error: "popup_required" });
      expect(env.port.sent.find((m) => m.op === "cookies.export")).toBeUndefined();
    });

    it("ALLOWS the popup and derives the URL from the active tab (ignores req.url)", async () => {
      env.scope.chrome.tabs.query = (_q, cb) =>
        cb([{ id: 1, url: "https://real-active.example/" }]);
      const p = env.sendMessage(
        { kind: "cookies.export", url: "https://attacker-supplied.example/" },
        popupSender(),
      );
      const req = await waitForSent("cookies.export");
      expect(req.url).toBe("https://real-active.example/");
      env.port.deliver({ op: "cookies.export.reply", request_id: req.request_id, ok: true });
      const r = await p;
      expect(r.ok).toBe(true);
    });
  });

  describe("mpris content-script entry point", () => {
    it("mpris.report_update forwards as mpris.publish (fire-and-forget)", async () => {
      const r = await env.sendMessage({
        kind: "mpris.report_update",
        title: "Song", artist: "Artist", state: "playing",
        url: "https://music.example/",
      });
      expect(r).toEqual({ ok: true });
      const req = await waitForSent("mpris.publish");
      expect(req).toMatchObject({
        title: "Song", artist: "Artist", playback_status: "playing",
      });
    });

    it("includes tab_id when sender carries a tab", async () => {
      env.sendMessage(
        { kind: "mpris.report_update", title: "X", state: "playing" },
        { id: env.scope.chrome.runtime.id, tab: { id: 42 } },
      );
      const req = await waitForSent("mpris.publish");
      expect(req.tab_id).toBe(42);
    });
  });

  describe("screenlock content-script entry points", () => {
    it("screenlock.report_inhibit forwards as screenlock.inhibit", async () => {
      const r = await env.sendMessage({
        kind: "screenlock.report_inhibit",
        reason: "fullscreen_video",
      });
      expect(r).toEqual({ ok: true });
      const req = await waitForSent("screenlock.inhibit");
      expect(req).toMatchObject({ reason: "fullscreen_video" });
    });

    it("screenlock.report_release forwards as screenlock.release", async () => {
      await env.sendMessage({
        kind: "screenlock.report_release", reason: "fullscreen_exit",
      });
      const req = await waitForSent("screenlock.release");
      expect(req).toMatchObject({ reason: "fullscreen_exit" });
    });

    it("tabs.onRemoved fires release for tabs with active inhibit", async () => {
      await env.sendMessage(
        { kind: "screenlock.report_inhibit", reason: "fullscreen_video" },
        { id: env.scope.chrome.runtime.id, tab: { id: 7 } },
      );
      await waitForSent("screenlock.inhibit");
      const beforeReleases = env.port.sent.filter((m) => m.op === "screenlock.release").length;
      const tabRemovedListeners = env.scope.chrome.tabs.onRemoved._listeners;
      for (const cb of tabRemovedListeners) cb(7, { isWindowClosing: false });
      // Wait for the release to land.
      await new Promise((resolve, reject) => {
        const start = Date.now();
        const tick = () => {
          const after = env.port.sent.filter((m) => m.op === "screenlock.release").length;
          if (after > beforeReleases) return resolve();
          if (Date.now() - start > 1000) return reject(new Error("no release"));
          setTimeout(tick, 5);
        };
        tick();
      });
      const release = env.port.sent.filter((m) => m.op === "screenlock.release").at(-1);
      expect(release.reason).toBe("tab_removed");
    });

    it("does NOT fire release for a tab without an active inhibit", async () => {
      const tabRemovedListeners = env.scope.chrome.tabs.onRemoved._listeners;
      for (const cb of tabRemovedListeners) cb(999, { isWindowClosing: false });
      await new Promise((r) => setTimeout(r, 30));
      const releases = env.port.sent.filter((m) => m.op === "screenlock.release");
      expect(releases).toHaveLength(0);
    });
  });
});
