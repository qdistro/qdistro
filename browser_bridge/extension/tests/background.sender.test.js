// Finding #14 — bundled browser_bridge extension sender hardening + RNG.
//
// The bundled extension has no vitest harness (unlike qdchrome/qdfirefox),
// so this is a self-contained assertion-style test runnable with plain
// node:  `node background.sender.test.js`. It loads background.js inside a
// vm sandbox with stubbed WebExtension APIs, captures the registered
// runtime.onMessage listener, and asserts:
//   1. messages whose sender.id !== runtime.id are rejected,
//   2. same-id messages reach the dispatcher,
//   3. nextRequestId() uses crypto.getRandomValues (not Math.random).
//
// Exit code 0 = pass, non-zero = fail.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const SRC = path.join(__dirname, "..", "background.js");
let source = fs.readFileSync(SRC, "utf8");

// --- build a sandbox with stubbed WebExtension + crypto APIs ----------

let capturedListener = null;
const fakePort = {
  onMessage: { addListener() {} },
  onDisconnect: { addListener() {} },
  postMessage() {},
  disconnect() {},
};

const RUNTIME_ID = "qdistro-bundle@test";
let getRandomValuesCalls = 0;
let mathRandomCalls = 0;

const chrome = {
  runtime: {
    id: RUNTIME_ID,
    lastError: null,
    connectNative() { return fakePort; },
    onMessage: {
      addListener(fn) { capturedListener = fn; },
    },
    getURL(p) { return "chrome-extension://" + RUNTIME_ID + "/" + p; },
  },
  action: { setBadgeText() {}, setBadgeBackgroundColor() {} },
  downloads: { onChanged: { addListener() {} } },
  notifications: { onClicked: { addListener() {} } },
};

const realRandom = Math.random;
const sandbox = {
  chrome,
  browser: undefined,
  console: { log() {}, warn() {}, error() {} },
  setTimeout() {},
  clearTimeout() {},
  self: { addEventListener() {} },
  TextEncoder,
  crypto: {
    getRandomValues(buf) {
      getRandomValuesCalls++;
      for (let i = 0; i < buf.length; i++) buf[i] = (i * 37 + 11) & 0xff;
      return buf;
    },
    subtle: {},
  },
  Math: new Proxy(Math, {
    get(t, p) {
      if (p === "random") {
        return function () { mathRandomCalls++; return realRandom(); };
      }
      return t[p];
    },
  }),
  Uint8Array,
  Array,
  Date,
};
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "background.js" });

// --- assertions -------------------------------------------------------

assert.ok(capturedListener, "runtime.onMessage listener was registered");

// 1. Foreign sender is rejected, response is {ok:false, error:sender_rejected}
{
  let resp = null;
  const ret = capturedListener(
    { action: "cookies.export" },
    { id: "evil-other-extension" },
    (r) => { resp = r; });
  assert.strictEqual(ret, false, "foreign sender: listener returns false");
  assert.ok(resp && resp.ok === false, "foreign sender: not ok");
  assert.strictEqual(resp.error, "sender_rejected",
    "foreign sender: error=sender_rejected");
}

// 2. Missing sender is rejected too.
{
  let resp = null;
  capturedListener({ action: "ping" }, undefined, (r) => { resp = r; });
  assert.ok(resp && resp.ok === false && resp.error === "sender_rejected",
    "missing sender rejected");
}

// 3. Same-id sender is accepted (a known action returns true for async).
{
  let resp = null;
  const ret = capturedListener(
    { action: "get_status" },
    { id: RUNTIME_ID },
    (r) => { resp = r; });
  // get_status responds synchronously and returns false; the key point is
  // it was NOT rejected with sender_rejected.
  assert.ok(!(resp && resp.error === "sender_rejected"),
    "same-id sender must not be rejected");
}

// 4. nextRequestId uses crypto.getRandomValues, not Math.random.
{
  const before = getRandomValuesCalls;
  const beforeMath = mathRandomCalls;
  const id = sandbox.nextRequestId
    ? sandbox.nextRequestId()
    : null;
  // nextRequestId is a top-level function declaration -> on the sandbox.
  assert.ok(typeof sandbox.nextRequestId === "function",
    "nextRequestId is defined");
  assert.ok(getRandomValuesCalls > before,
    "nextRequestId calls crypto.getRandomValues");
  assert.strictEqual(mathRandomCalls, beforeMath,
    "nextRequestId must NOT call Math.random");
  assert.ok(/^ext-\d+-[0-9a-f]+$/.test(id),
    "request id shape is ext-<seq>-<hex>: " + id);
}

// 5. Source-level guard: the rejected branch exists.
assert.ok(/sender\.id\s*!==\s*api\.runtime\.id/.test(source),
  "source contains sender.id !== api.runtime.id guard");

console.log("OK background.sender.test.js: all assertions passed");
