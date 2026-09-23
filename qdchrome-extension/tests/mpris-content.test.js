/** @vitest-environment jsdom */
// mpris-content.js — observes navigator.mediaSession + <audio>/<video>
// elements, reports changes to the background as mpris.report_update,
// and responds to inbound mpris.do_action by driving the media
// element directly.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

const SRC_PATH = resolve(__dirname, "..", "src", "content", "mpris-content.js");
const SRC = readFileSync(SRC_PATH, "utf8");

function makeBrowser() {
  const sent = [];
  const inboundListeners = [];
  return {
    sent,
    inboundListeners,
    fireInbound(req, sender = {}) {
      const results = [];
      for (const cb of inboundListeners) {
        const r = cb(req, sender);
        if (r && typeof r.then === "function") results.push(r);
      }
      return Promise.all(results);
    },
    chrome: {
      runtime: {
        sendMessage(msg) {
          sent.push(msg);
          return Promise.resolve({ ok: true });
        },
        onMessage: {
          addListener(cb) { inboundListeners.push(cb); },
        },
      },
    },
  };
}

function load(env) {
  globalThis.chrome = env.chrome;
  // Compile with the real on-disk `filename` (instead of `new Function`, whose
  // anonymous script carries no URL) so the V8 coverage provider attributes the
  // executed lines back to src/content/mpris-content.js. No parsingContext =>
  // runs in the current (jsdom) context, so document/navigator stay available.
  vm.compileFunction(SRC, [], { filename: SRC_PATH })();
}

function setMediaSession({ metadata, playbackState } = {}) {
  Object.defineProperty(navigator, "mediaSession", {
    configurable: true,
    value: {
      metadata: metadata || null,
      playbackState: playbackState || "none",
      setActionHandler: () => {},
    },
  });
}

describe("mpris-content.js", () => {
  let env;

  beforeEach(() => {
    vi.useFakeTimers();
    env = makeBrowser();
    setMediaSession({ metadata: null, playbackState: "none" });
  });

  afterEach(() => {
    vi.useRealTimers();
    delete globalThis.chrome;
    document.body.innerHTML = "";
    document.title = "";
    try { delete navigator.mediaSession; } catch (_) {}
  });

  it("does not report when there's no <audio>/<video> on the page", async () => {
    load(env);
    vi.advanceTimersByTime(1500);
    await Promise.resolve();
    expect(env.sent.find((m) => m.kind === "mpris.report_update"))
      .toBeUndefined();
  });

  it("reports mpris.report_update when a media element is present and snapshot changes", async () => {
    const audio = document.createElement("audio");
    Object.defineProperty(audio, "currentTime", { value: 12.7 });
    Object.defineProperty(audio, "duration", { value: 200 });
    document.body.appendChild(audio);
    document.title = "Now Playing";
    load(env);
    // MutationObserver fires async — give it a tick.
    await Promise.resolve();
    // The poll's reportIfChanged fires on the first tick.
    vi.advanceTimersByTime(1000);
    await Promise.resolve();
    const frame = env.sent.find((m) => m.kind === "mpris.report_update");
    expect(frame).toBeTruthy();
    expect(frame.title).toBe("Now Playing");
    expect(frame.state).toBe("none");
    expect(frame.position).toBe(12);
    expect(frame.duration).toBe(200);
    expect(frame.url).toBe(location.href);
  });

  it("uses mediaSession.metadata fields when present", async () => {
    setMediaSession({
      metadata: {
        title: "Track One", artist: "Artist", album: "Album",
        artwork: [{ src: "https://x/art.jpg" }],
      },
      playbackState: "playing",
    });
    const audio = document.createElement("audio");
    document.body.appendChild(audio);
    load(env);
    await Promise.resolve();
    vi.advanceTimersByTime(1000);
    await Promise.resolve();
    const frame = env.sent.find((m) => m.kind === "mpris.report_update");
    expect(frame.title).toBe("Track One");
    expect(frame.artist).toBe("Artist");
    expect(frame.album).toBe("Album");
    expect(frame.art_url).toBe("https://x/art.jpg");
    expect(frame.state).toBe("playing");
  });

  it("event-driven play/pause forces an immediate report", async () => {
    const audio = document.createElement("audio");
    document.body.appendChild(audio);
    load(env);
    await Promise.resolve();
    // First report happens synchronously when the play event fires
    // (the source clears lastSnapshot, then calls reportIfChanged).
    audio.dispatchEvent(new Event("play"));
    // sendMessage returns a Promise; resolve it.
    await Promise.resolve();
    const frame = env.sent.find((m) => m.kind === "mpris.report_update");
    expect(frame).toBeTruthy();
  });

  it("suppresses duplicate snapshots in the polling tick", async () => {
    const audio = document.createElement("audio");
    document.body.appendChild(audio);
    load(env);
    await Promise.resolve();
    vi.advanceTimersByTime(1000);
    await Promise.resolve();
    const after1 = env.sent.filter((m) => m.kind === "mpris.report_update").length;
    vi.advanceTimersByTime(1000);
    await Promise.resolve();
    const after2 = env.sent.filter((m) => m.kind === "mpris.report_update").length;
    expect(after2).toBe(after1);
  });

  describe("inbound mpris.do_action", () => {
    it("'play' calls media.play()", async () => {
      const audio = document.createElement("audio");
      const calls = [];
      audio.play = () => { calls.push("play"); };
      audio.pause = () => { calls.push("pause"); };
      document.body.appendChild(audio);
      load(env);
      await Promise.resolve();
      const [reply] = await env.fireInbound({ kind: "mpris.do_action", action: "play" });
      expect(calls).toEqual(["play"]);
      expect(reply).toEqual({ ok: true, action: "play" });
    });

    it("'pause' calls media.pause()", async () => {
      const audio = document.createElement("audio");
      audio.play = () => {};
      const calls = [];
      audio.pause = () => { calls.push("pause"); };
      document.body.appendChild(audio);
      load(env);
      await Promise.resolve();
      const [reply] = await env.fireInbound({ kind: "mpris.do_action", action: "pause" });
      expect(calls).toEqual(["pause"]);
      expect(reply.ok).toBe(true);
    });

    it("'playpause' toggles play when paused", async () => {
      const audio = document.createElement("audio");
      Object.defineProperty(audio, "paused", { value: true, configurable: true });
      const calls = [];
      audio.play = () => { calls.push("play"); };
      audio.pause = () => { calls.push("pause"); };
      document.body.appendChild(audio);
      load(env);
      await Promise.resolve();
      const [reply] = await env.fireInbound({ kind: "mpris.do_action", action: "playpause" });
      expect(calls).toEqual(["play"]);
      expect(reply).toEqual({ ok: true, action: "playpause" });
    });

    it("'playpause' toggles pause when playing", async () => {
      const audio = document.createElement("audio");
      Object.defineProperty(audio, "paused", { value: false, configurable: true });
      const calls = [];
      audio.play = () => { calls.push("play"); };
      audio.pause = () => { calls.push("pause"); };
      document.body.appendChild(audio);
      load(env);
      await Promise.resolve();
      const [reply] = await env.fireInbound({ kind: "mpris.do_action", action: "playpause" });
      expect(calls).toEqual(["pause"]);
      expect(reply.ok).toBe(true);
    });

    it("'stop' pauses and rewinds to 0", async () => {
      const audio = document.createElement("audio");
      let ct = 5;
      Object.defineProperty(audio, "currentTime", {
        get() { return ct; }, set(v) { ct = v; },
      });
      const calls = [];
      audio.play = () => { calls.push("play"); };
      audio.pause = () => { calls.push("pause"); };
      document.body.appendChild(audio);
      load(env);
      await Promise.resolve();
      const [reply] = await env.fireInbound({ kind: "mpris.do_action", action: "stop" });
      expect(calls).toEqual(["pause"]);
      expect(ct).toBe(0);
      expect(reply).toEqual({ ok: true, action: "stop" });
    });

    it("'seek' sets media.currentTime to value", async () => {
      const audio = document.createElement("audio");
      let ct = 0;
      Object.defineProperty(audio, "currentTime", {
        get() { return ct; }, set(v) { ct = v; },
      });
      document.body.appendChild(audio);
      load(env);
      await Promise.resolve();
      const [reply] = await env.fireInbound({
        kind: "mpris.do_action", action: "seek", value: 42,
      });
      expect(ct).toBe(42);
      expect(reply.ok).toBe(true);
    });

    it("'next' / 'previous' reply ok:false action_unsupported_by_page", async () => {
      const audio = document.createElement("audio");
      document.body.appendChild(audio);
      load(env);
      await Promise.resolve();
      const [r1] = await env.fireInbound({ kind: "mpris.do_action", action: "next" });
      const [r2] = await env.fireInbound({ kind: "mpris.do_action", action: "previous" });
      expect(r1).toEqual({ ok: false, error: "action_unsupported_by_page" });
      expect(r2).toEqual({ ok: false, error: "action_unsupported_by_page" });
    });

    it("unknown action replies unknown_action", async () => {
      const audio = document.createElement("audio");
      document.body.appendChild(audio);
      load(env);
      await Promise.resolve();
      const [reply] = await env.fireInbound({
        kind: "mpris.do_action", action: "scrub",
      });
      expect(reply.error).toBe("unknown_action");
    });

    it("replies no_media_element when no <audio>/<video> exists", async () => {
      load(env);
      const [reply] = await env.fireInbound({
        kind: "mpris.do_action", action: "play",
      });
      expect(reply.error).toBe("no_media_element");
    });

    it("ignores messages whose kind is not mpris.do_action", async () => {
      const audio = document.createElement("audio");
      audio.play = () => {};
      document.body.appendChild(audio);
      load(env);
      const results = await env.fireInbound({ kind: "something.else" });
      // The listener returns undefined for non-mpris messages; the
      // dispatcher harness drops them.
      expect(results).toEqual([]);
    });
  });
});
