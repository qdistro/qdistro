const assert = require("assert");
const fs = require("fs");
const path = require("path");
const Lease = require("../Services/Qdwin/RemoteDisplayLease.js");

const now = Math.floor(Date.now() / 1000);
function request(changes) {
  return Object.assign({
    schema: "qdistro-mm-shell-layout-v1",
    request_id: "1".repeat(32),
    generation: 90,
    session_id: "display-session",
    slot_name: "rdp-0",
    enabled: true,
    logical_x: 1280,
    logical_y: 0,
    width: 1280,
    height: 800,
    scale: 1,
    expires_at: now + 30,
  }, changes || {});
}

const live = [
  {name: "headless", enabled: true, x: 0, y: 0,
   width: 1280, height: 800, refresh: 60000, scale: 1, transform: 0},
  {name: "rdp-0", enabled: false, x: 0, y: 0,
   width: 1280, height: 800, refresh: 30000, scale: 1, transform: 0},
];

// ensures: only the exact generation-bound slot is changed; local output stays intact.
const enabled = Lease.buildSlotLayout(live, request());
assert.deepStrictEqual(enabled[0], live[0]);
assert.deepStrictEqual(enabled[1], Object.assign({}, live[1], {
  enabled: true, x: 1280, y: 0, width: 1280, height: 800,
  scale: 1, transform: 0,
}));

// ensures: disabling the leased slot retains the surviving local desktop.
const disabled = Lease.buildSlotLayout(enabled, request({enabled: false}));
assert.strictEqual(disabled[0].enabled, true);
assert.strictEqual(disabled[1].enabled, false);

// ensures: missing/duplicate slots and disabling the last output fail closed.
assert.strictEqual(Lease.buildSlotLayout([live[0]], request()), null);
assert.strictEqual(Lease.buildSlotLayout(
  [live[0], live[1], Object.assign({}, live[1])], request()), null);
assert.strictEqual(Lease.buildSlotLayout(
  [Object.assign({}, live[1], {enabled: true})], request({enabled: false})), null);

// ensures: schema drift, stale actions, and injected general-layout fields fail closed.
assert.strictEqual(Lease.validateRequest(request({schema: "v2"}), now), false);
assert.strictEqual(Lease.validateRequest(request({expires_at: now}), now), false);
assert.strictEqual(Lease.validateRequest(request({outputs: []}), now), false);
assert.strictEqual(Lease.validateRequest(request({slot_name: "headless"}), now), false);

// ensures: busctl's typed string is decoded exactly; malformed output is not authority.
const encoded = JSON.stringify(JSON.stringify(request()));
assert.deepStrictEqual(Lease.parseBusctlString("s " + encoded), request());
assert.strictEqual(Lease.parseBusctlString('s "not json"'), null);
assert.strictEqual(Lease.parseBusctlString("b true"), null);

const qml = fs.readFileSync(path.join(
  __dirname, "../Services/Qdwin/RemoteDisplayLease.qml"), "utf8");
const binding = fs.readFileSync(path.join(
  __dirname, "../qml-plugin/qdwin-binding.cpp"), "utf8");
// ensures: no same-uid Quickshell IPC can mint display authority; the service
// authenticates a direct qdshell busctl child and every result is tag-correlated.
assert.ok(!qml.includes("IpcHandler"));
assert.ok(qml.includes('"ClaimLayout"'));
assert.ok(qml.includes('"AcknowledgeLayout"'));
assert.ok(qml.includes('["gdbus", "wait", "--session", root.bus]'));
assert.ok(qml.includes("running: root._serviceSeen"));
assert.ok(qml.includes("applyOutputLayoutTagged"));
assert.ok(binding.includes("emit layoutTaggedResult(tag, ok, cancelled)"));
assert.ok(binding.includes("if (!tag.isEmpty())"));

console.log("remote display lease: exact slot delta + fail-closed parsing PASS");
