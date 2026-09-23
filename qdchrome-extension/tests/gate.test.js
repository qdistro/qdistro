// gate.js — enforces the options-page module toggles + origin
// allowlist. These tests pin the contract layer (dispatcher in/out
// gating and the background onMessage gating), per the "deterministic
// signal is the vitest contract suite" convention.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension, loadWithBackground, makeFakeChrome } from "./helpers.js";

// Build a fake chrome whose storage.local returns a fixed config and
// whose storage.onChanged is fireable.
function fakeChromeWithConfig(config) {
  const fc = makeFakeChrome({
    storage: {
      local: {
        get: (keys, cb) => {
          const out = {};
          for (const k of keys) if (k in config) out[k] = config[k];
          cb(out);
        },
        set: (v, cb) => cb && cb(),
      },
      onChanged: {
        _listeners: [],
        addListener(cb) { this._listeners.push(cb); },
        fire(changes, area) {
          for (const cb of this._listeners) cb(changes, area);
        },
      },
    },
  });
  return fc;
}

describe("qdistroGate — mapping", () => {
  let g;
  beforeEach(() => { g = loadExtension().scope.qdistroGate; });

  it("maps every wire op to its owning module", () => {
    expect(g.opModule("tabs.list")).toBe("tabs");
    expect(g.opModule("tabs.open")).toBe("tabs");
    expect(g.opModule("pwd.fill")).toBe("pwd");
    expect(g.opModule("pwd.fill_confirm")).toBe("pwd");
    expect(g.opModule("page.extract")).toBe("pageExtract");
    expect(g.opModule("page.extract.request")).toBe("pageExtract");
    expect(g.opModule("cookies.export")).toBe("cookies");
    expect(g.opModule("mpris.publish")).toBe("mpris");
    expect(g.opModule("mpris.control")).toBe("mpris");
    expect(g.opModule("downloads.notify")).toBe("downloads");
    expect(g.opModule("notifications.show")).toBe("notifications");
    expect(g.opModule("screenlock.inhibit")).toBe("screenlock");
  });

  it("never gates the screenlock UNDO path (codex #3)", () => {
    // release must always be allowed to undo a prior inhibit, even when
    // the screenlock module is disabled — otherwise a lock-inhibit gets
    // stranded on. Inhibit (acquire) IS gated; release is not.
    expect(g.opModule("screenlock.release")).toBe(null);
    expect(g.opEnabled("screenlock.release")).toBe(true);
    expect(g.kindModule("screenlock.report_release")).toBe(null);
    expect(g.kindEnabled("screenlock.report_release")).toBe(true);
  });

  it("still gates the screenlock ACQUIRE path", () => {
    expect(g.opModule("screenlock.inhibit")).toBe("screenlock");
    expect(g.kindModule("screenlock.report_inhibit")).toBe("screenlock");
  });

  it("treats infrastructure ops (qdistro.*) as ungated", () => {
    expect(g.opModule("qdistro.handshake")).toBe(null);
    expect(g.opModule("qdistro.ping")).toBe(null);
    expect(g.opEnabled("qdistro.handshake")).toBe(true);
  });

  it("maps req.kind entries to modules", () => {
    expect(g.kindModule("cookies.export")).toBe("cookies");
    expect(g.kindModule("pwd.request_fill")).toBe("pwd");
    expect(g.kindModule("mpris.report_update")).toBe("mpris");
    expect(g.kindModule("screenlock.report_inhibit")).toBe("screenlock");
    expect(g.kindModule("status")).toBe(null);
    expect(g.kindModule("ping")).toBe(null);
  });
});

describe("qdistroGate — module enablement defaults", () => {
  it("enables every module when storage has no config (manifest defaults)", () => {
    const g = loadExtension().scope.qdistroGate;
    expect(g.isModuleEnabled("tabs")).toBe(true);
    expect(g.isModuleEnabled("cookies")).toBe(true);
    expect(g.opEnabled("cookies.export")).toBe(true);
  });

  it("respects an explicit false from storage", () => {
    const chrome = fakeChromeWithConfig({ modules: { cookies: false } });
    const g = loadExtension({ chrome }).scope.qdistroGate;
    expect(g.isModuleEnabled("cookies")).toBe(false);
    expect(g.isModuleEnabled("tabs")).toBe(true); // unlisted → default on
    expect(g.opEnabled("cookies.export")).toBe(false);
    expect(g.kindEnabled("cookies.export")).toBe(false);
  });

  it("re-reads config on storage.onChanged", () => {
    const chrome = fakeChromeWithConfig({ modules: { pwd: true } });
    const env = loadExtension({ chrome });
    const g = env.scope.qdistroGate;
    expect(g.isModuleEnabled("pwd")).toBe(true);
    chrome.storage.onChanged.fire(
      { modules: { newValue: { pwd: false } } }, "local");
    expect(g.isModuleEnabled("pwd")).toBe(false);
  });

  it("ignores storage.onChanged for non-local areas", () => {
    const chrome = fakeChromeWithConfig({});
    const env = loadExtension({ chrome });
    const g = env.scope.qdistroGate;
    chrome.storage.onChanged.fire(
      { modules: { newValue: { tabs: false } } }, "sync");
    expect(g.isModuleEnabled("tabs")).toBe(true);
  });
});

describe("qdistroGate — origin allowlist", () => {
  it("denies every origin when the allowlist is empty (closed by default, J11)", () => {
    const g = loadExtension().scope.qdistroGate;
    // Empty/unset allowlist is now closed — a fresh install ships the
    // page-initiated surface off, not open to every site.
    expect(g.isOriginAllowed("https://anything.example")).toBe(false);
    expect(g.isOriginAllowed("https://example.com/login")).toBe(false);
  });

  it("allows all origins ONLY when `*` is explicitly listed (J11 opt-in)", () => {
    const chrome = fakeChromeWithConfig({ origin_allowlist: ["*"] });
    const g = loadExtension({ chrome }).scope.qdistroGate;
    expect(g.isOriginAllowed("https://anything.example/")).toBe(true);
    expect(g.isOriginAllowed("http://plain.test/")).toBe(true);
    // `*` restores the pre-J11 semantics, including opaque/unparsable
    // URLs, so it is a faithful "all origins" replacement.
    expect(g.isOriginAllowed("chrome://settings")).toBe(true);
    expect(g.isOriginAllowed("about:blank")).toBe(true);
  });

  it("treats `*` as all-origins even alongside other entries", () => {
    const chrome = fakeChromeWithConfig({
      origin_allowlist: ["https://example.com", "*"],
    });
    const g = loadExtension({ chrome }).scope.qdistroGate;
    expect(g.isOriginAllowed("https://unlisted.test/")).toBe(true);
  });

  it("does NOT treat a bare-host `*` lookalike as all-origins", () => {
    // A literal host entry that merely contains a star (e.g. a typo)
    // must not open the gate — only an entry that is exactly `*`.
    const chrome = fakeChromeWithConfig({ origin_allowlist: ["*.example.com"] });
    const g = loadExtension({ chrome }).scope.qdistroGate;
    expect(g.isOriginAllowed("https://app.example.com/")).toBe(true);
    expect(g.isOriginAllowed("https://unlisted.test/")).toBe(false);
  });

  it("restricts to exact hosts when set", () => {
    const chrome = fakeChromeWithConfig({
      origin_allowlist: ["https://example.com"],
    });
    const g = loadExtension({ chrome }).scope.qdistroGate;
    expect(g.isOriginAllowed("https://example.com/login")).toBe(true);
    expect(g.isOriginAllowed("https://evil.test/")).toBe(false);
    // exact-host: a subdomain is NOT matched by a bare host entry.
    expect(g.isOriginAllowed("https://sub.example.com/")).toBe(false);
  });

  it("supports leading-wildcard subdomain patterns", () => {
    const chrome = fakeChromeWithConfig({
      origin_allowlist: ["https://*.internal.corp"],
    });
    const g = loadExtension({ chrome }).scope.qdistroGate;
    expect(g.isOriginAllowed("https://app.internal.corp/")).toBe(true);
    expect(g.isOriginAllowed("https://internal.corp/")).toBe(true);
    expect(g.isOriginAllowed("https://internal.corp.evil.test/")).toBe(false);
  });

  it("accepts bare-host entries (no scheme), matching either scheme", () => {
    const chrome = fakeChromeWithConfig({ origin_allowlist: ["example.com"] });
    const g = loadExtension({ chrome }).scope.qdistroGate;
    expect(g.isOriginAllowed("https://example.com/")).toBe(true);
    expect(g.isOriginAllowed("http://example.com/")).toBe(true);
  });

  it("enforces the scheme when the entry carries one (codex #2)", () => {
    const chrome = fakeChromeWithConfig({
      origin_allowlist: ["https://bank.example"],
    });
    const g = loadExtension({ chrome }).scope.qdistroGate;
    expect(g.isOriginAllowed("https://bank.example/")).toBe(true);
    // http:// must NOT be allowed by an https:// entry.
    expect(g.isOriginAllowed("http://bank.example/")).toBe(false);
  });

  it("ignores a port in the entry (host-based match)", () => {
    const chrome = fakeChromeWithConfig({
      origin_allowlist: ["https://example.com:8443"],
    });
    const g = loadExtension({ chrome }).scope.qdistroGate;
    expect(g.isOriginAllowed("https://example.com/")).toBe(true);
  });

  it("rejects non-http(s) / unparsable URLs when an allowlist is set", () => {
    const chrome = fakeChromeWithConfig({ origin_allowlist: ["example.com"] });
    const g = loadExtension({ chrome }).scope.qdistroGate;
    expect(g.isOriginAllowed("chrome://settings")).toBe(false);
    expect(g.isOriginAllowed("")).toBe(false);
    expect(g.isOriginAllowed("about:blank")).toBe(false);
  });
});

describe("dispatcher gating via gate", () => {
  it("rejects an INBOUND op whose module is disabled with module_disabled", async () => {
    const chrome = fakeChromeWithConfig({ modules: { tabs: false } });
    const env = loadExtension({ chrome });
    env.scope.qdistroPort.connect();
    await env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.list", request_id: "r1-x",
    });
    const reply = env.port.sent.find(
      (m) => m.op === "tabs.list.reply" && m.request_id === "r1-x");
    expect(reply).toBeTruthy();
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("module_disabled");
  });

  it("still routes an inbound op whose module is enabled", async () => {
    const chrome = fakeChromeWithConfig({ modules: { tabs: true } });
    const env = loadExtension({ chrome });
    env.scope.qdistroPort.connect();
    await env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.list", request_id: "r2-x",
    });
    const reply = env.port.sent.find(
      (m) => m.op === "tabs.list.reply" && m.request_id === "r2-x");
    expect(reply.ok).toBe(true);
  });

  it("rejects an OUTBOUND request whose module is disabled, never touching the port", async () => {
    const chrome = fakeChromeWithConfig({ modules: { mpris: false } });
    const env = loadExtension({ chrome });
    env.scope.qdistroPort.connect();
    const before = env.port.sent.length;
    await expect(
      env.scope.qdistroDispatcher.request("mpris.publish", { title: "x" }),
    ).rejects.toThrow(/module_disabled/);
    expect(env.port.sent.length).toBe(before); // nothing sent on the wire
  });

  it("never gates infrastructure ops even with a sparse modules config", async () => {
    const chrome = fakeChromeWithConfig({ modules: { tabs: false } });
    const env = loadExtension({ chrome });
    env.scope.qdistroPort.connect();
    // qdistro.ping must still go out.
    const p = env.scope.qdistroDispatcher.request("qdistro.ping", { echo: "1" });
    const sent = env.port.sent.find((m) => m.op === "qdistro.ping");
    expect(sent).toBeTruthy();
    env.port.deliver({ op: "qdistro.ping.reply", request_id: sent.request_id, ok: true });
    await expect(p).resolves.toMatchObject({ ok: true });
  });
});

describe("background onMessage gating via gate", () => {
  const popupSender = {
    id: "test-ext-id",
    url: "chrome-extension://test-ext-id/popup.html",
  };
  const tabSender = (url) => ({
    id: "test-ext-id",
    tab: { id: 5, url },
  });

  it("refuses a disabled module's req.kind with module_disabled", async () => {
    const chrome = fakeChromeWithConfig({ modules: { cookies: false } });
    const env = loadWithBackground({ chrome });
    const r = await env.sendMessage({ kind: "cookies.export" }, popupSender);
    expect(r).toEqual({ ok: false, error: "module_disabled" });
  });

  it("refuses a content-script op from an off-allowlist origin", async () => {
    const chrome = fakeChromeWithConfig({
      origin_allowlist: ["https://allowed.example"],
    });
    const env = loadWithBackground({ chrome });
    const r = await env.sendMessage(
      { kind: "pwd.request_fill", url: "https://evil.test/" },
      tabSender("https://evil.test/"),
    );
    expect(r).toEqual({ ok: false, error: "origin_not_allowed" });
  });

  it("refuses a content-script op when NO allowlist is configured (closed by default, J11)", async () => {
    // No origin_allowlist saved → the default is now closed, so even a
    // benign-looking site cannot drive the bridge until the user opts in.
    const env = loadWithBackground({ chrome: fakeChromeWithConfig({}) });
    const r = await env.sendMessage(
      { kind: "pwd.request_fill", url: "https://anything.example/" },
      tabSender("https://anything.example/"),
    );
    expect(r).toEqual({ ok: false, error: "origin_not_allowed" });
  });

  it("FAILS CLOSED when the gate module is absent entirely", async () => {
    // The gate used to be consulted as `self.qdistroGate && !allowed`,
    // so an extension whose gate.js never loaded (or threw before
    // exporting) ran every page-initiated op ungated — the same end
    // state J11 was about. Deleting the export must deny, not allow.
    const env = loadWithBackground({
      chrome: fakeChromeWithConfig({
        origin_allowlist: ["https://allowed.example"],
      }),
    });
    delete env.scope.qdistroGate;
    const r = await env.sendMessage(
      { kind: "pwd.request_fill", url: "https://allowed.example/" },
      tabSender("https://allowed.example/"),
    );
    expect(r).toEqual({ ok: false, error: "origin_not_allowed" });
  });

  it("allows a content-script op from an allowlisted origin (reaches the bridge)", async () => {
    const chrome = fakeChromeWithConfig({
      origin_allowlist: ["https://allowed.example"],
    });
    const env = loadWithBackground({ chrome });
    env.scope.qdistroPort.connect();
    // The fake bridge never replies, so the response Promise won't
    // settle — we don't await it. We assert the gate did NOT short-
    // circuit: the op reached qdistroPwd.fill and went out on the wire.
    env.sendMessage(
      { kind: "pwd.request_fill", url: "https://allowed.example/login" },
      tabSender("https://allowed.example/login"),
    );
    // mint() (crypto.subtle HMAC) + fill() are async; poll until the
    // frame is on the wire rather than guessing a fixed delay.
    let sent;
    for (let i = 0; i < 50 && !sent; i++) {
      await new Promise((r) => setTimeout(r, 2));
      sent = env.port.sent.find((m) => m.op === "pwd.fill");
    }
    expect(sent).toBeTruthy();
  });

  it("status/ping bypass the module gate even when modules are off", async () => {
    const chrome = fakeChromeWithConfig({ modules: { cookies: false } });
    const env = loadWithBackground({ chrome });
    const r = await env.sendMessage({ kind: "status" }, popupSender);
    expect(r.ok).toBe(true);
    expect(typeof r.connected).toBe("boolean");
  });

  it("lets a screenlock RELEASE through even when screenlock is disabled (codex #3)", async () => {
    const chrome = fakeChromeWithConfig({ modules: { screenlock: false } });
    const env = loadWithBackground({ chrome });
    env.scope.qdistroPort.connect();
    // Inhibit (acquire) is refused...
    const inhibit = await env.sendMessage(
      { kind: "screenlock.report_inhibit" },
      { id: "test-ext-id", url: "https://x.test/", tab: { id: 7, url: "https://x.test/" } },
    );
    expect(inhibit).toEqual({ ok: false, error: "module_disabled" });
    // ...but release (undo) is allowed so a prior inhibit can't strand.
    const release = await env.sendMessage(
      { kind: "screenlock.report_release" },
      { id: "test-ext-id", url: "https://x.test/", tab: { id: 7, url: "https://x.test/" } },
    );
    expect(release).toEqual({ ok: true });
  });

  it("gates a content-script op on the SENDING FRAME url, not the top tab (codex #3)", async () => {
    const chrome = fakeChromeWithConfig({
      origin_allowlist: ["https://allowed.example"],
    });
    const env = loadWithBackground({ chrome });
    // Top-level tab is allowlisted, but the message comes from an
    // off-list iframe (sender.url). It must be refused.
    const r = await env.sendMessage(
      { kind: "pwd.request_fill" },
      {
        id: "test-ext-id",
        url: "https://evil-iframe.test/",
        tab: { id: 5, url: "https://allowed.example/" },
      },
    );
    expect(r).toEqual({ ok: false, error: "origin_not_allowed" });
  });
});

describe("page.extract fails closed when the gate module is absent", () => {
  it("bridge-initiated page.extract.request refuses with no gate", async () => {
    const env = loadExtension({
      chrome: fakeChromeWithConfig({ origin_allowlist: ["*"] }),
    });
    env.scope.qdistroPort.connect();
    delete env.scope.qdistroGate;
    await env.scope.qdistroDispatcher.handleInbound({
      op: "page.extract.request", request_id: "rp-nogate", tab_id: 5,
      mode: "visible_text",
    });
    const reply = env.port.sent.find(
      (m) => m.op === "page.extract.request.reply");
    expect(reply).toMatchObject({ ok: false, error: "origin_not_allowed" });
  });
});

describe("bridge-initiated page.extract.request honours the origin allowlist (codex #4)", () => {
  it("refuses extraction from an off-allowlist tab", async () => {
    const chrome = makeFakeChrome({
      storage: {
        local: {
          get: (keys, cb) => {
            const cfg = { origin_allowlist: ["https://allowed.example"] };
            const out = {};
            for (const k of keys) if (k in cfg) out[k] = cfg[k];
            cb(out);
          },
          set: (v, cb) => cb && cb(),
        },
        onChanged: { addListener() {} },
      },
      tabs: {
        query: (q, cb) => cb([]),
        create: (p, cb) => cb({ id: 99, ...p }),
        remove: (ids, cb) => cb(),
        get: (id, cb) => cb({ id, url: "https://off-list.test/" }),
        executeScript: (tabId, opts, cb) => cb && cb([{ result: {} }]),
        onRemoved: { addListener() {} },
      },
    });
    const env = loadExtension({ chrome });
    env.scope.qdistroPort.connect();
    await env.scope.qdistroDispatcher.handleInbound({
      op: "page.extract.request", request_id: "rp-1", tab_id: 5,
      mode: "visible_text",
    });
    const reply = env.port.sent.find(
      (m) => m.op === "page.extract.request.reply" && m.request_id === "rp-1");
    expect(reply).toBeTruthy();
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("origin_not_allowed");
  });
});

describe("cold-start readiness (codex #1)", () => {
  it("does not fail open: a disabled module is honoured even when the first storage read is async", async () => {
    // storage.local.get resolves on a later tick (async), simulating
    // the MV3 service-worker cold-start window.
    let deliver;
    const chrome = makeFakeChrome({
      storage: {
        local: {
          get: (keys, cb) => {
            deliver = () => cb({ modules: { tabs: false } });
          },
          set: (v, cb) => cb && cb(),
        },
        onChanged: { addListener() {} },
      },
    });
    const env = loadExtension({ chrome });
    env.scope.qdistroPort.connect();
    const gate = env.scope.qdistroGate;
    expect(gate.isLoaded()).toBe(false);
    // Fire an inbound tabs.list BEFORE the storage read lands.
    const p = env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.list", request_id: "cold-1",
    });
    // Now deliver the config (tabs disabled), then let handleInbound
    // observe it via the readiness await.
    deliver();
    await p;
    const reply = env.port.sent.find(
      (m) => m.op === "tabs.list.reply" && m.request_id === "cold-1");
    expect(reply).toBeTruthy();
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("module_disabled");
  });
});
