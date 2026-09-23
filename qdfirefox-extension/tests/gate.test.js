// gate.js — enforces the options-page module toggles + origin
// allowlist. Pins the contract layer (dispatcher in/out gating and
// the background onMessage gating), per the vitest-contract-suite
// convention. Firefox variant: browser.* is Promise-based.
import { describe, it, expect, beforeEach } from "vitest";
import { loadExtension, loadWithBackground, makeFakeBrowser } from "./helpers.js";

// Build a fake browser whose storage.local.get resolves a fixed config
// and whose storage.onChanged is fireable.
function fakeBrowserWithConfig(config) {
  return makeFakeBrowser({
    storage: {
      local: {
        get: (keys) => {
          const out = {};
          const ks = Array.isArray(keys) ? keys : [keys];
          for (const k of ks) if (k in config) out[k] = config[k];
          return Promise.resolve(out);
        },
        set: (_v) => Promise.resolve(),
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
}

// The gate loads config asynchronously (browser.storage.local.get
// returns a Promise). Drain microtasks before asserting.
async function loadGate(browser) {
  const env = loadExtension(browser ? { browser } : {});
  await new Promise((r) => setTimeout(r, 0));
  return env;
}

describe("qdistroGate — mapping", () => {
  let g;
  beforeEach(async () => { g = (await loadGate()).scope.qdistroGate; });

  it("maps every wire op to its owning module (incl. containers)", () => {
    expect(g.opModule("tabs.list")).toBe("tabs");
    expect(g.opModule("pwd.fill")).toBe("pwd");
    expect(g.opModule("page.extract")).toBe("pageExtract");
    expect(g.opModule("cookies.export")).toBe("cookies");
    expect(g.opModule("mpris.publish")).toBe("mpris");
    expect(g.opModule("downloads.notify")).toBe("downloads");
    expect(g.opModule("notifications.show")).toBe("notifications");
    expect(g.opModule("screenlock.inhibit")).toBe("screenlock");
    expect(g.opModule("containers.list")).toBe("containers");
    expect(g.opModule("containers.create")).toBe("containers");
    expect(g.opModule("containers.remove")).toBe("containers");
  });

  it("never gates the screenlock UNDO path (codex #3)", () => {
    expect(g.opModule("screenlock.release")).toBe(null);
    expect(g.opEnabled("screenlock.release")).toBe(true);
    expect(g.kindModule("screenlock.report_release")).toBe(null);
    expect(g.kindEnabled("screenlock.report_release")).toBe(true);
  });

  it("treats infrastructure ops (qdistro.*) as ungated", () => {
    expect(g.opModule("qdistro.handshake")).toBe(null);
    expect(g.opEnabled("qdistro.handshake")).toBe(true);
  });

  it("maps req.kind entries to modules", () => {
    expect(g.kindModule("cookies.export")).toBe("cookies");
    expect(g.kindModule("pwd.request_fill")).toBe("pwd");
    expect(g.kindModule("containers.list")).toBe("containers");
    expect(g.kindModule("status")).toBe(null);
  });
});

describe("qdistroGate — module enablement", () => {
  it("enables every module when storage has no config", async () => {
    const g = (await loadGate()).scope.qdistroGate;
    expect(g.isModuleEnabled("tabs")).toBe(true);
    expect(g.isModuleEnabled("containers")).toBe(true);
  });

  it("respects an explicit false from storage", async () => {
    const browser = fakeBrowserWithConfig({ modules: { containers: false } });
    const g = (await loadGate(browser)).scope.qdistroGate;
    expect(g.isModuleEnabled("containers")).toBe(false);
    expect(g.isModuleEnabled("tabs")).toBe(true);
    expect(g.opEnabled("containers.list")).toBe(false);
  });

  it("re-reads config on storage.onChanged", async () => {
    const browser = fakeBrowserWithConfig({ modules: { pwd: true } });
    const env = await loadGate(browser);
    const g = env.scope.qdistroGate;
    expect(g.isModuleEnabled("pwd")).toBe(true);
    browser.storage.onChanged.fire(
      { modules: { newValue: { pwd: false } } }, "local");
    expect(g.isModuleEnabled("pwd")).toBe(false);
  });
});

describe("qdistroGate — origin allowlist", () => {
  it("denies every origin when the allowlist is empty (closed by default, J11)", async () => {
    const g = (await loadGate()).scope.qdistroGate;
    // Empty/unset allowlist is closed — a fresh install ships the
    // page-initiated surface off, not open to every site (iso2 `06` F1).
    expect(g.isOriginAllowed("https://anything.example")).toBe(false);
    expect(g.isOriginAllowed("https://example.com/login")).toBe(false);
  });

  it("allows all origins ONLY when `*` is explicitly listed (J11 opt-in)", async () => {
    const browser = fakeBrowserWithConfig({ origin_allowlist: ["*"] });
    const g = (await loadGate(browser)).scope.qdistroGate;
    expect(g.isOriginAllowed("https://anything.example/")).toBe(true);
    expect(g.isOriginAllowed("http://plain.test/")).toBe(true);
    // `*` restores the pre-J11 semantics, including opaque/unparsable
    // URLs, so it is a faithful "all origins" replacement.
    expect(g.isOriginAllowed("moz-extension://x/options.html")).toBe(true);
    expect(g.isOriginAllowed("about:blank")).toBe(true);
  });

  it("treats `*` as all-origins even alongside other entries", async () => {
    const browser = fakeBrowserWithConfig({
      origin_allowlist: ["https://example.com", "*"],
    });
    const g = (await loadGate(browser)).scope.qdistroGate;
    expect(g.isOriginAllowed("https://evil.test/")).toBe(true);
    expect(g.isOriginAllowed("https://unlisted.test/")).toBe(true);
  });

  it("does NOT treat a bare-host `*` lookalike as all-origins", async () => {
    // A literal host entry that merely contains a star (e.g. a typo)
    // must not open the gate — only an entry that is exactly `*`.
    const browser = fakeBrowserWithConfig({ origin_allowlist: ["*.example.com"] });
    const g = (await loadGate(browser)).scope.qdistroGate;
    expect(g.isOriginAllowed("https://app.example.com/")).toBe(true);
    expect(g.isOriginAllowed("https://unlisted.test/")).toBe(false);
  });

  it("restricts to exact hosts and supports *. wildcards", async () => {
    const browser = fakeBrowserWithConfig({
      origin_allowlist: ["https://example.com", "https://*.internal.corp"],
    });
    const g = (await loadGate(browser)).scope.qdistroGate;
    expect(g.isOriginAllowed("https://example.com/x")).toBe(true);
    expect(g.isOriginAllowed("https://sub.example.com/")).toBe(false);
    expect(g.isOriginAllowed("https://app.internal.corp/")).toBe(true);
    expect(g.isOriginAllowed("https://internal.corp/")).toBe(true);
    expect(g.isOriginAllowed("https://evil.test/")).toBe(false);
  });

  it("rejects non-http(s) URLs when an allowlist is set", async () => {
    const browser = fakeBrowserWithConfig({ origin_allowlist: ["example.com"] });
    const g = (await loadGate(browser)).scope.qdistroGate;
    expect(g.isOriginAllowed("about:blank")).toBe(false);
    expect(g.isOriginAllowed("")).toBe(false);
  });

  it("enforces the scheme when the entry carries one (codex #2)", async () => {
    const browser = fakeBrowserWithConfig({
      origin_allowlist: ["https://bank.example"],
    });
    const g = (await loadGate(browser)).scope.qdistroGate;
    expect(g.isOriginAllowed("https://bank.example/")).toBe(true);
    expect(g.isOriginAllowed("http://bank.example/")).toBe(false);
  });
});

describe("dispatcher gating via gate", () => {
  it("rejects an INBOUND op whose module is disabled", async () => {
    const browser = fakeBrowserWithConfig({ modules: { tabs: false } });
    const env = await loadGate(browser);
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

  it("rejects an OUTBOUND request whose module is disabled, never touching the port", async () => {
    const browser = fakeBrowserWithConfig({ modules: { containers: false } });
    const env = await loadGate(browser);
    env.scope.qdistroPort.connect();
    const before = env.port.sent.length;
    await expect(
      env.scope.qdistroDispatcher.request("containers.list", {}),
    ).rejects.toThrow(/module_disabled/);
    expect(env.port.sent.length).toBe(before);
  });

  it("never gates infrastructure ops", async () => {
    const browser = fakeBrowserWithConfig({ modules: { tabs: false } });
    const env = await loadGate(browser);
    env.scope.qdistroPort.connect();
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
    url: "moz-extension://test-ext-id/src/popup.html",
  };
  const tabSender = (url) => ({ id: "test-ext-id", tab: { id: 5, url } });

  async function loadBg(browser) {
    const env = loadWithBackground(browser ? { browser } : {});
    await new Promise((r) => setTimeout(r, 0));
    return env;
  }

  it("refuses a disabled module's req.kind", async () => {
    const browser = fakeBrowserWithConfig({ modules: { cookies: false } });
    const env = await loadBg(browser);
    const r = await env.sendMessage({ kind: "cookies.export" }, popupSender);
    expect(r).toEqual({ ok: false, error: "module_disabled" });
  });

  it("refuses a content-script op from an off-allowlist origin", async () => {
    const browser = fakeBrowserWithConfig({
      origin_allowlist: ["https://allowed.example"],
    });
    const env = await loadBg(browser);
    const r = await env.sendMessage(
      { kind: "pwd.request_fill", url: "https://evil.test/" },
      tabSender("https://evil.test/"),
    );
    expect(r).toEqual({ ok: false, error: "origin_not_allowed" });
  });

  it("refuses a content-script op when NO allowlist is configured (closed by default, J11)", async () => {
    // No origin_allowlist saved → the default is closed, so even a
    // benign-looking site cannot drive the bridge until the user opts in.
    const env = await loadBg(fakeBrowserWithConfig({}));
    const r = await env.sendMessage(
      { kind: "pwd.request_fill", url: "https://anything.example/" },
      tabSender("https://anything.example/"),
    );
    expect(r).toEqual({ ok: false, error: "origin_not_allowed" });
  });

  it("FAILS CLOSED when the gate module is absent entirely", async () => {
    // The gate used to be consulted as `self.qdistroGate && !allowed`,
    // so an extension whose gate.js never loaded (or threw before
    // exporting) ran every page-initiated op ungated — the same
    // end state J11 was about. Deleting the export must deny, not allow.
    const env = await loadBg(fakeBrowserWithConfig({
      origin_allowlist: ["https://allowed.example"],
    }));
    delete env.scope.qdistroGate;
    const r = await env.sendMessage(
      { kind: "pwd.request_fill", url: "https://allowed.example/" },
      tabSender("https://allowed.example/"),
    );
    expect(r).toEqual({ ok: false, error: "origin_not_allowed" });
  });

  it("status bypasses the module gate", async () => {
    const browser = fakeBrowserWithConfig({ modules: { cookies: false } });
    const env = await loadBg(browser);
    const r = await env.sendMessage({ kind: "status" }, popupSender);
    expect(r.ok).toBe(true);
  });

  it("lets a screenlock RELEASE through even when screenlock is disabled (codex #3)", async () => {
    const browser = fakeBrowserWithConfig({ modules: { screenlock: false } });
    const env = await loadBg(browser);
    env.scope.qdistroPort.connect();
    const inhibit = await env.sendMessage(
      { kind: "screenlock.report_inhibit" },
      { id: "test-ext-id", url: "https://x.test/", tab: { id: 7, url: "https://x.test/" } },
    );
    expect(inhibit).toEqual({ ok: false, error: "module_disabled" });
    const release = await env.sendMessage(
      { kind: "screenlock.report_release" },
      { id: "test-ext-id", url: "https://x.test/", tab: { id: 7, url: "https://x.test/" } },
    );
    expect(release).toEqual({ ok: true });
  });

  it("gates a content-script op on the SENDING FRAME url, not the top tab (codex #3)", async () => {
    const browser = fakeBrowserWithConfig({
      origin_allowlist: ["https://allowed.example"],
    });
    const env = await loadBg(browser);
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
    const browser = fakeBrowserWithConfig({ origin_allowlist: ["*"] });
    const env = loadExtension({ browser });
    await new Promise((r) => setTimeout(r, 0));
    env.scope.qdistroPort.connect();
    delete env.scope.qdistroGate;
    await env.scope.qdistroDispatcher.handleInbound({
      op: "page.extract.request", request_id: 9, tab_id: 5,
      mode: "visible_text",
    });
    const reply = env.port.sent.find(
      (m) => m.op === "page.extract.request.reply");
    expect(reply).toMatchObject({ ok: false, error: "origin_not_allowed" });
  });
});

describe("bridge-initiated page.extract.request honours the origin allowlist (codex #4)", () => {
  it("refuses extraction from an off-allowlist tab", async () => {
    const browser = makeFakeBrowser({
      storage: {
        local: {
          get: (keys) => {
            const cfg = { origin_allowlist: ["https://allowed.example"] };
            const out = {};
            const ks = Array.isArray(keys) ? keys : [keys];
            for (const k of ks) if (k in cfg) out[k] = cfg[k];
            return Promise.resolve(out);
          },
          set: (_v) => Promise.resolve(),
        },
        onChanged: { addListener() {} },
      },
    });
    browser.tabs.get = (id) => Promise.resolve({ id, url: "https://off-list.test/" });
    const env = loadExtension({ browser });
    await new Promise((r) => setTimeout(r, 0));
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
  it("does not fail open: a disabled module is honoured when the first storage read resolves late", async () => {
    let resolveGet;
    const browser = makeFakeBrowser({
      storage: {
        local: {
          get: (_keys) => new Promise((res) => { resolveGet = res; }),
          set: (_v) => Promise.resolve(),
        },
        onChanged: { addListener() {} },
      },
    });
    const env = loadExtension({ browser });
    env.scope.qdistroPort.connect();
    const gate = env.scope.qdistroGate;
    expect(gate.isLoaded()).toBe(false);
    // Fire an inbound tabs.list BEFORE the storage read resolves.
    const p = env.scope.qdistroDispatcher.handleInbound({
      op: "tabs.list", request_id: "cold-1",
    });
    // Resolve the config (tabs disabled) on a later tick.
    resolveGet({ modules: { tabs: false } });
    await p;
    const reply = env.port.sent.find(
      (m) => m.op === "tabs.list.reply" && m.request_id === "cold-1");
    expect(reply).toBeTruthy();
    expect(reply.ok).toBe(false);
    expect(reply.error).toBe("module_disabled");
  });
});
