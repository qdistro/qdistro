// Test helpers — load the extension's source files into a synthetic
// `self` global so the IIFE modules attach their exports there.
//
// Vanilla JS modules use the `self` global of an event page. In Node
// we synthesize the same shape and eval each source into a fresh
// scope per call — cheap, no jsdom dependency, no transpiler.
//
// Difference from qdchrome-extension/tests/helpers.js: the fake
// `browser` returns Promises from API methods (the Firefox-native
// shape), because src/api.js binds to `browser` and modules `await`
// the results directly.
//
// @ts-check
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { webcrypto } from "node:crypto";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(__dirname, "..", "src");

export function makeFakePort() {
  const listeners = { msg: [], dc: [] };
  const sent = [];
  const port = {
    error: null,
    postMessage: (m) => { sent.push(m); },
    disconnect: () => {
      for (const cb of listeners.dc) cb();
    },
    onMessage: { addListener: (cb) => listeners.msg.push(cb) },
    onDisconnect: { addListener: (cb) => listeners.dc.push(cb) },
  };
  return {
    port, sent,
    deliver: (msg) => { for (const cb of listeners.msg) cb(msg); },
    triggerDisconnect: () => { for (const cb of listeners.dc) cb(); },
  };
}

function makeEvent() {
  const listeners = [];
  return {
    listeners,
    addListener: (cb) => { listeners.push(cb); },
    fire: (...args) => { for (const cb of listeners) cb(...args); },
  };
}

export function makeFakeBrowser(overrides = {}) {
  // Capture runtime.onMessage listeners so tests can fire synthetic
  // sendMessage calls and inspect the listener's Promise reply.
  const onMessageListeners = [];
  const onTabRemovedListeners = [];
  const fakes = {
    runtime: {
      id: "test-ext-id",
      lastError: null,
      connectNative: () => { throw new Error("override connectNative"); },
      getURL: (p) => `moz-extension://test-ext-id/${p}`,
      onStartup: { addListener: () => {} },
      onInstalled: { addListener: () => {} },
      onMessage: {
        addListener: (cb) => onMessageListeners.push(cb),
        _listeners: onMessageListeners,
      },
    },
    tabs: {
      query: (_q) => Promise.resolve([]),
      create: (p) => Promise.resolve({ id: 99, ...p }),
      remove: (_ids) => Promise.resolve(),
      get: (id) => Promise.resolve({ id, url: "https://example.com/" }),
      sendMessage: (_tabId, message) =>
        Promise.resolve({ ok: true, action: message && message.action }),
      onRemoved: {
        addListener: (cb) => onTabRemovedListeners.push(cb),
        _listeners: onTabRemovedListeners,
      },
    },
    cookies: {
      getAll: (_q) => Promise.resolve([]),
    },
    downloads: {
      onChanged: makeEvent(),
      search: (_q) => Promise.resolve([]),
    },
    notifications: {
      onClicked: makeEvent(),
      onClosed: makeEvent(),
      create: (_id, _opts) => Promise.resolve("notif-1"),
    },
    contextMenus: {
      create: (_def) => {},
      onClicked: makeEvent(),
    },
    contextualIdentities: {
      query: (_q) => Promise.resolve([]),
      create: (props) => Promise.resolve({
        cookieStoreId: "firefox-container-99",
        name: props.name, color: props.color,
        colorCode: "#37adff", icon: props.icon, iconUrl: "",
      }),
      remove: (id) => Promise.resolve({
        cookieStoreId: id, name: "removed", color: "",
        colorCode: "", icon: "", iconUrl: "",
      }),
    },
    storage: {
      local: {
        get: (_k) => Promise.resolve({}),
        set: (_v) => Promise.resolve(),
      },
      onChanged: makeEvent(),
    },
    scripting: {
      executeScript: () => Promise.resolve([{ result: {} }]),
    },
  };
  return Object.assign(fakes, overrides);
}

// A fake browser whose stored config opts in to all origins (a single
// `*` allowlist entry). Since J11 the origin allowlist is CLOSED BY
// DEFAULT, so tests that exercise op-forwarding mechanics (not the
// origin gate itself) must explicitly allow origins or every
// page-initiated op is refused. Origin-gate behaviour is covered
// directly in gate.test.js. Firefox-shaped: storage.local.get returns
// a Promise.
export function makeFakeBrowserAllOrigins(overrides = {}) {
  const browser = makeFakeBrowser(overrides);
  const local = browser.storage.local;
  browser.storage = {
    ...browser.storage,
    local: { ...local, get: (_keys) => Promise.resolve({ origin_allowlist: ["*"] }) },
  };
  return browser;
}

export { makeEvent };

/**
 * Load the extension source into a fresh global scope and return it.
 * Each call yields an independent `self` so tests don't bleed.
 */
export function loadExtension(opts = {}) {
  const fakeBrowser = opts.browser || makeFakeBrowser();
  const fakePortHandle = opts.portHandle || makeFakePort();
  fakeBrowser.runtime.connectNative = () => fakePortHandle.port;

  const scope = {};
  scope.self = scope;
  scope.console = console;
  scope.browser = fakeBrowser;
  scope.crypto = globalThis.crypto || webcrypto;
  scope.setTimeout = setTimeout;
  scope.clearTimeout = clearTimeout;
  scope.setInterval = setInterval;
  scope.clearInterval = clearInterval;
  scope.Date = Date;
  scope.Math = Math;
  scope.JSON = JSON;
  scope.Promise = Promise;
  scope.Error = Error;
  scope.Array = Array;
  scope.Object = Object;
  scope.String = String;
  scope.Number = Number;
  scope.Boolean = Boolean;
  scope.Map = Map;
  scope.Set = Set;
  scope.Symbol = Symbol;

  function evalFile(rel) {
    const filename = path.join(SRC, rel);
    const code = fs.readFileSync(filename, "utf8");
    // Each source is an IIFE bound to `self`. We pass `browser` as an extra
    // param so the api.js `typeof browser !== "undefined"` check sees the
    // synthetic object, and run with `this` === scope. Compiling via
    // vm.compileFunction with the real on-disk `filename` is what lets the V8
    // coverage provider attribute the executed lines back to src/<rel> (a bare
    // `new Function` produces an anonymous script with no URL, so coverage
    // stays 0%).
    const fn = vm.compileFunction(
      code,
      ["self", "browser", "console"],
      { filename },
    );
    fn.call(scope, scope, scope.browser, scope.console);
  }

  evalFile("api.js");
  evalFile("port.js");
  evalFile("dispatcher.js");
  evalFile("intent.js");
  evalFile("gate.js");
  // Seed a default session secret so tests that call mint() don't
  // need to drive a full qdistro.handshake first. Tests can call
  // setSessionSecretHex(null) to exercise the pre-handshake path.
  if (!opts.skipSessionSecret) {
    scope.qdistroIntent.setSessionSecretHex(
      "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    );
  }
  evalFile("modules/tabs.js");
  evalFile("modules/pwd.js");
  evalFile("modules/pageExtract.js");
  evalFile("modules/cookies.js");
  evalFile("modules/containers.js");
  evalFile("modules/mpris.js");
  evalFile("modules/downloads.js");
  evalFile("modules/notifications.js");
  evalFile("modules/screenlock.js");

  if (opts.loadBackground) {
    evalFile("background.js");
  }

  return { scope, port: fakePortHandle };
}

/**
 * Convenience wrapper that also loads `src/background.js`. Returns
 * a `sendMessage(req, senderOverride?)` helper that fires the
 * captured runtime.onMessage listener and returns its Promise reply.
 */
export function loadWithBackground(opts = {}) {
  const env = loadExtension({ ...opts, loadBackground: true });
  const listeners = env.scope.browser.runtime.onMessage._listeners;
  const sendMessage = (req, senderOverride) => {
    const sender = senderOverride || { id: env.scope.browser.runtime.id };
    for (const cb of listeners) {
      const ret = cb(req, sender);
      if (ret && typeof ret.then === "function") return ret;
    }
    return Promise.resolve(undefined);
  };
  return { ...env, sendMessage };
}
