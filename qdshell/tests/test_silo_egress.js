const assert = require("assert");
const E = require("../Services/Qdistro/SiloEgress.js");

function busctl(rows) {
  return JSON.stringify({
    type: "s",
    data: [JSON.stringify(rows)],
  });
}

assert.deepStrictEqual(E.parseBusctlListSilos(""), []);
assert.deepStrictEqual(E.parseBusctlListSilos("not json"), []);
assert.deepStrictEqual(E.parseBusctlListSilos(busctl([{ name: "work" }])), [{ name: "work" }]);

assert.strictEqual(E.normaliseEgress(null), "legacy");
assert.strictEqual(E.normaliseEgress(""), "legacy");
assert.strictEqual(E.normaliseEgress("none"), "none");
assert.strictEqual(E.normaliseEgress("direct"), "direct");
assert.strictEqual(E.normaliseEgress("wg:corp"), "wg:corp");
assert.strictEqual(E.normaliseEgress("surprise"), "unknown");

const rows = [
  { name: "dark", uid: 2001, state: "Active", egress: "none" },
  { name: "stopped", uid: 2002, state: "Stopped", egress: "direct" },
  { name: "mail", uid: 2003, state: "Active", egress: "wg:corp" },
  { name: "web", uid: 2004, state: "Active", egress: "direct" },
  { name: "legacy", uid: 2005, state: "Active", egress: null },
];

const active = E.activeEgressRows(rows);
assert.deepStrictEqual(active.map(r => r.name), ["legacy", "mail", "web"]);
assert.deepStrictEqual(active.map(r => r.label), ["host", "corp", "direct"]);

const s = E.summary(rows, 2);
assert.strictEqual(s.active, true);
assert.strictEqual(s.count, 3);
assert.strictEqual(s.label, "legacy:host, mail:corp +1");
assert.strictEqual(s.detail, "legacy:host, mail:corp, web:direct");

const noEgress = E.summary([{ name: "dark", state: "Active", egress: "none" }]);
assert.strictEqual(noEgress.active, false);
assert.strictEqual(noEgress.count, 0);

console.log("silo-egress: all assertions passed");
process.exit(0);
