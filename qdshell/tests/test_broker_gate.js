const assert = require("assert");
const BrokerGate = require("../Services/Qdshell/BrokerGate.js");

assert.deepStrictEqual(
    BrokerGate.parseStringVerdict(0, 's "allow"\n', "broker-unavailable"),
    { verdict: "allow", reason: "broker:allow" }
);

assert.deepStrictEqual(
    BrokerGate.parseStringVerdict(0, 's "deny"\n', "broker-unavailable"),
    { verdict: "deny", reason: "broker:deny" }
);

assert.deepStrictEqual(
    BrokerGate.parseStringVerdict(1, "", "broker-unavailable"),
    { verdict: "deny", reason: "broker-unavailable" }
);

assert.deepStrictEqual(
    BrokerGate.parseStringVerdict(0, "not busctl", "broker-unavailable"),
    { verdict: "deny", reason: "broker-malformed" }
);

assert.deepStrictEqual(
    BrokerGate.parseStringVerdict(0, 's "unknown"\n', "broker-unavailable"),
    { verdict: "deny", reason: "broker:unknown" }
);

assert.strictEqual(BrokerGate.qdwinDecision("allow"), 0);
assert.strictEqual(BrokerGate.qdwinDecision("deny"), 1);
assert.strictEqual(
    BrokerGate.nestedProxyAction("org.freedesktop.weston.wayland-terminal"),
    "qdistro.nested.advertise:org.freedesktop.weston.wayland-terminal"
);
assert.deepStrictEqual(
    BrokerGate.nestedProxyDetails("org.example.App", 1000),
    { app_id: "org.example.App", origin_uid: 1000 }
);
assert.strictEqual(BrokerGate.knownSilo("user1"), true);
assert.strictEqual(BrokerGate.knownSilo("unknown"), false);
assert.strictEqual(BrokerGate.knownSilo(""), false);
