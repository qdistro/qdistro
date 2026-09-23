const assert = require("assert");
const ClipboardBroker = require("../Services/Qdshell/ClipboardBroker.js");

assert.deepStrictEqual(
    ClipboardBroker.parseCheckClipboardTransferResult(0, 's "allow"\n'),
    { verdict: "allow", reason: "broker:allow" }
);

assert.deepStrictEqual(
    ClipboardBroker.parseCheckClipboardTransferResult(0, 's "deny"\n'),
    { verdict: "deny", reason: "broker:deny" }
);

assert.deepStrictEqual(
    ClipboardBroker.parseCheckClipboardTransferResult(1, ""),
    { verdict: "deny", reason: "broker-unavailable" }
);

assert.deepStrictEqual(
    ClipboardBroker.parseCheckClipboardTransferResult(0, 's "prompt"\n'),
    { verdict: "deny", reason: "broker-unknown-verdict" }
);

assert.deepStrictEqual(
    ClipboardBroker.parseCheckClipboardTransferResult(0, "not busctl"),
    { verdict: "deny", reason: "broker-malformed" }
);

assert.strictEqual(ClipboardBroker.hasKnownIdentity("user1", "admin"), true);
assert.strictEqual(ClipboardBroker.hasKnownIdentity("unknown", "admin"), false);
assert.strictEqual(ClipboardBroker.hasKnownIdentity("user1", "unknown"), false);
assert.strictEqual(ClipboardBroker.hasKnownIdentity("", "admin"), false);
