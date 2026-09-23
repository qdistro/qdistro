/** @vitest-environment jsdom */
// screenlock-content.js — fullscreenchange listener that reports
// inhibit/release to the background event page.
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import vm from "node:vm";

const SRC_PATH = resolve(__dirname, "..", "src", "content", "screenlock-content.js");
const SRC = readFileSync(
  SRC_PATH,
  "utf8",
);

function makeBrowser() {
  const sent = [];
  return {
    sent,
    browser: {
      runtime: {
        sendMessage(msg) {
          sent.push(msg);
          return Promise.resolve({ ok: true });
        },
      },
    },
  };
}

function load(env) {
  globalThis.browser = env.browser;
  // Compile with the real on-disk `filename` (vs `new Function`'s anonymous,
  // URL-less script) so V8 coverage attributes lines to the on-disk source.
  // No parsingContext => current (jsdom) context, so document stays available.
  vm.compileFunction(SRC, [], { filename: SRC_PATH })();
}

function setFullscreen(el) {
  // jsdom doesn't implement fullscreen; fake the property + fire the
  // event. The content script reads document.fullscreenElement on
  // every fullscreenchange.
  Object.defineProperty(document, "fullscreenElement", {
    configurable: true, value: el,
  });
  document.dispatchEvent(new Event("fullscreenchange"));
}

describe("screenlock-content.js", () => {
  let env;

  beforeEach(() => {
    env = makeBrowser();
    load(env);
  });

  afterEach(() => {
    delete globalThis.browser;
    // Reset jsdom DOM and globals for the next case.
    document.body.innerHTML = "";
    try {
      Object.defineProperty(document, "fullscreenElement", {
        configurable: true, value: null,
      });
    } catch (_) {}
  });

  it("fullscreen entry on a non-video element reports presentation", () => {
    const div = document.createElement("div");
    document.body.appendChild(div);
    setFullscreen(div);
    const frame = env.sent.find((m) => m.kind === "screenlock.report_inhibit");
    expect(frame).toBeTruthy();
    expect(frame.reason).toBe("fullscreen_presentation");
    expect(frame.tab_url).toBe(location.href);
  });

  it("fullscreen entry on a playing <video> reports video", () => {
    const v = document.createElement("video");
    Object.defineProperty(v, "paused", { value: false });
    document.body.appendChild(v);
    setFullscreen(v);
    const frame = env.sent.find((m) => m.kind === "screenlock.report_inhibit");
    expect(frame.reason).toBe("fullscreen_video");
  });

  it("fullscreen entry on a paused <video> reports presentation, not video", () => {
    const v = document.createElement("video");
    Object.defineProperty(v, "paused", { value: true });
    document.body.appendChild(v);
    setFullscreen(v);
    const frame = env.sent.find((m) => m.kind === "screenlock.report_inhibit");
    expect(frame.reason).toBe("fullscreen_presentation");
  });

  it("fullscreen entry on a container with an inner playing video reports video", () => {
    const wrap = document.createElement("div");
    const v = document.createElement("video");
    Object.defineProperty(v, "paused", { value: false });
    wrap.appendChild(v);
    document.body.appendChild(wrap);
    setFullscreen(wrap);
    const frame = env.sent.find((m) => m.kind === "screenlock.report_inhibit");
    expect(frame.reason).toBe("fullscreen_video");
  });

  it("fullscreen exit after an active inhibit reports release", () => {
    const div = document.createElement("div");
    document.body.appendChild(div);
    setFullscreen(div);
    setFullscreen(null);
    const release = env.sent.find((m) => m.kind === "screenlock.report_release");
    expect(release).toBeTruthy();
    expect(release.reason).toBe("fullscreen_exit");
  });

  it("fullscreen exit without a prior inhibit does NOT report", () => {
    setFullscreen(null);
    expect(env.sent.find((m) => m.kind === "screenlock.report_release"))
      .toBeUndefined();
  });

  it("pagehide while inhibit is active reports release with reason:'tab_unload'", () => {
    const div = document.createElement("div");
    document.body.appendChild(div);
    setFullscreen(div);
    window.dispatchEvent(new Event("pagehide"));
    const release = env.sent.find(
      (m) => m.kind === "screenlock.report_release" && m.reason === "tab_unload",
    );
    expect(release).toBeTruthy();
  });

  it("pagehide without an active inhibit is a no-op", () => {
    window.dispatchEvent(new Event("pagehide"));
    expect(env.sent).toHaveLength(0);
  });

  it("webkitfullscreenchange also drives the listener", () => {
    const div = document.createElement("div");
    document.body.appendChild(div);
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true, value: div,
    });
    document.dispatchEvent(new Event("webkitfullscreenchange"));
    const frame = env.sent.find((m) => m.kind === "screenlock.report_inhibit");
    expect(frame).toBeTruthy();
  });
});
