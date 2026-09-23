// Test helpers — load the extension's source files into a synthetic
// `self` global so the IIFE modules attach their exports there.
//
// Vanilla JS modules (the rest of the source) use the `self` global
// of a service worker. In Node we synthesize the same shape and use
// vm.runInThisContext via `eval` for each source — cheap, no
// jsdom dependency, no transpiler.
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

/**
 * Build a listener registry that captures `addListener` callbacks so
 * tests can fire them synthetically (used for chrome.downloads,
 * chrome.notifications, chrome.contextMenus).
 */
function makeEvent() {
  const listeners = [];
  return {
    listeners,
    addListener: (cb) => { listeners.push(cb); },
    fire: (...args) => { for (const cb of listeners) cb(...args); },
  };
}

export function makeFakeChrome(overrides = {}) {
  // Capture listeners so background tests can fire synthetic events.
  const onMessageListeners = [];
  const onTabRemovedListeners = [];
  const fakes = {
    runtime: {
      id: "test-ext-id",
      lastError: null,
      connectNative: () => { throw new Error("override connectNative"); },
      getURL: (p) => `chrome-extension://test-ext-id/${p}`,
      onStartup: { addListener: () => {} },
      onInstalled: { addListener: () => {} },
      onMessage: {
        addListener: (cb) => onMessageListeners.push(cb),
        _listeners: onMessageListeners,
      },
    },
    tabs: {
      query: (q, cb) => cb([]),
      create: (p, cb) => cb({ id: 99, ...p }),
      remove: (ids, cb) => cb(),
      get: (id, cb) => cb({ id, url: "https://example.com/" }),
      sendMessage: (tabId, message, cb) => cb && cb({ ok: true, action: message && message.action }),
      executeScript: (tabId, opts, cb) => cb && cb([{ result: {} }]),
      onRemoved: {
        addListener: (cb) => onTabRemovedListeners.push(cb),
        _listeners: onTabRemovedListeners,
      },
    },
    cookies: {
      getAll: (q, cb) => cb([]),
    },
    downloads: {
      onChanged: makeEvent(),
      search: (q, cb) => cb([]),
    },
    notifications: {
      onClicked: makeEvent(),
      onClosed: makeEvent(),
      create: (id, opts, cb) => cb && cb("notif-1"),
    },
    contextMenus: {
      create: (def, cb) => { cb && cb(); },
      onClicked: makeEvent(),
    },
    storage: {
      local: { get: (k, cb) => cb({}), set: (v, cb) => cb && cb() },
      onChanged: makeEvent(),
    },
    scripting: { executeScript: () => Promise.resolve([{ result: {} }]) },
  };
  return Object.assign(fakes, overrides);
}

// A fake chrome whose stored config opts in to all origins (a single
// `*` allowlist entry). Since J11 the origin allowlist is CLOSED BY
// DEFAULT, so tests that exercise op-forwarding mechanics (not the
// origin gate itself) must explicitly allow origins or every
// page-initiated op is refused. Origin-gate behaviour is covered
// directly in gate.test.js.
export function makeFakeChromeAllOrigins(overrides = {}) {
  const chrome = makeFakeChrome(overrides);
  const local = chrome.storage.local;
  chrome.storage = {
    ...chrome.storage,
    local: { ...local, get: (_keys, cb) => cb({ origin_allowlist: ["*"] }) },
  };
  return chrome;
}

export { makeEvent };

/**
 * Load the extension source into a fresh global scope and return it.
 * Each call yields an independent `self` so tests don't bleed.
 */
export function loadExtension(opts = {}) {
  const fakeChrome = opts.chrome || makeFakeChrome();
  const fakePortHandle = opts.portHandle || makeFakePort();
  fakeChrome.runtime.connectNative = () => fakePortHandle.port;

  const scope = {};
  scope.self = scope;
  scope.console = console;
  scope.chrome = fakeChrome;
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
    // Each source file is an IIFE with `(function(root){ ... })(typeof self !== "undefined" ? self : globalThis)`.
    // We compile a small wrapper that injects `self`/`chrome`/`console` and
    // runs the source with `this` === scope. Compiling via vm.compileFunction
    // with the real on-disk `filename` is what lets the V8 coverage provider
    // attribute the executed lines back to src/<rel> (a bare `new Function`
    // produces an anonymous script with no URL, so coverage stays 0%).
    const fn = vm.compileFunction(
      code,
      ["self", "chrome", "console"],
      { filename },
    );
    fn.call(scope, scope, scope.chrome, scope.console);
  }

  evalFile("api.js");
  evalFile("port.js");
  evalFile("dispatcher.js");
  evalFile("intent.js");
  evalFile("gate.js");
  // Seed a default session secret so tests that call mint() don't
  // need to drive a full qdistro.handshake first. Tests can call
  // setSessionSecretHex(null) (or pass skipSessionSecret) to
  // exercise the pre-handshake path.
  if (!opts.skipSessionSecret) {
    scope.qdistroIntent.setSessionSecretHex(
      "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    );
  }
  evalFile("modules/tabs.js");
  evalFile("modules/pwd.js");
  evalFile("modules/pageExtract.js");
  evalFile("modules/cookies.js");
  evalFile("modules/mpris.js");
  evalFile("modules/downloads.js");
  evalFile("modules/notifications.js");
  evalFile("modules/screenlock.js");

  if (opts.loadBackground) {
    // background.js wraps importScripts in try/catch and falls back
    // to the MV2 path (assume globals already populated) on
    // ReferenceError. We don't define importScripts in scope, so the
    // catch path runs — which is what we want since evalFile already
    // populated everything.
    evalFile("background.js");
  }

  return { scope, port: fakePortHandle };
}

/**
 * Convenience wrapper that also evals `src/background.js` and
 * surfaces a sendMessage(req, senderOverride?) helper. Chrome's
 * runtime.onMessage uses a sendResponse callback (vs Firefox's
 * Promise-return); the helper wraps the callback in a Promise.
 */
export function loadWithBackground(opts = {}) {
  const env = loadExtension({ ...opts, loadBackground: true });
  const listeners = env.scope.chrome.runtime.onMessage._listeners;
  const sendMessage = (req, senderOverride) => {
    const sender = senderOverride || { id: env.scope.chrome.runtime.id };
    return new Promise((resolve) => {
      let resolved = false;
      const sendResponse = (r) => {
        if (resolved) return;
        resolved = true;
        resolve(r);
      };
      for (const cb of listeners) {
        cb(req, sender, sendResponse);
      }
    });
  };
  return { ...env, sendMessage };
}
