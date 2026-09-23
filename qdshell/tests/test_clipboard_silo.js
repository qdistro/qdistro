const assert = require("assert");
const ClipboardSilo = require("../Services/Qdshell/ClipboardSilo.js");

function sameStableSilo(left, right, expected) {
    const leftSilo = ClipboardSilo.fromSecctx(
        left.sandboxEngine,
        left.appId,
        left.instanceId
    );
    const rightSilo = ClipboardSilo.fromSecctx(
        right.sandboxEngine,
        right.appId,
        right.instanceId
    );

    assert.strictEqual(leftSilo, expected);
    assert.strictEqual(rightSilo, expected);
    assert.strictEqual(leftSilo, rightSilo);
}

sameStableSilo(
    {
        sandboxEngine: "qdistro.tier2",
        appId: "work/firefox",
        instanceId: "launch-token-a",
    },
    {
        sandboxEngine: "qdistro.tier2",
        appId: "work/firefox",
        instanceId: "launch-token-b",
    },
    "tier2/work"
);

sameStableSilo(
    {
        sandboxEngine: "qdistro.tier3",
        appId: "qdistro.tier3.user1",
        instanceId: "launch-token-a",
    },
    {
        sandboxEngine: "qdistro.tier3",
        appId: "qdistro.tier3.user1",
        instanceId: "launch-token-b",
    },
    "user1"
);

sameStableSilo(
    {
        sandboxEngine: "qdistro.tier5",
        appId: "qdistro.tier5.firefox",
        instanceId: "launch-token-a",
    },
    {
        sandboxEngine: "qdistro.tier5",
        appId: "qdistro.tier5.firefox",
        instanceId: "launch-token-b",
    },
    "vm-firefox"
);

// Non-qdistro engine (third-party, e.g. flatpak): the silo is the
// stable engine:app_id pair and is INDEPENDENT of the per-launch
// instance_id. Two launches of the same app must land in one silo.
sameStableSilo(
    {
        sandboxEngine: "flatpak",
        appId: "org.example.App",
        instanceId: "launch-token-a",
    },
    {
        sandboxEngine: "flatpak",
        appId: "org.example.App",
        instanceId: "launch-token-b",
    },
    "flatpak:org.example.App"
);

// instance_id must never leak into the silo: different launch tokens,
// same (engine, app_id) -> same silo (regression guard for the bug
// where the non-qdistro path returned instance_id verbatim).
assert.strictEqual(
    ClipboardSilo.fromSecctx("flatpak", "org.example.App", "launch-token-a"),
    ClipboardSilo.fromSecctx("flatpak", "org.example.App", "launch-token-b")
);

// app_id missing/empty -> NO stable identity. Must NOT derive from
// instance_id, and must NOT collapse into an engine-only bucket (that
// would group every unrelated client of the engine, and would let a
// tier client with a missing app_id dodge its tier MIME stripping).
// Fail safe to "" so the caller treats it as unknown / not same-silo.
assert.strictEqual(
    ClipboardSilo.fromSecctx("flatpak", "", "instance-a"),
    ""
);
// A tier engine with a missing app_id must NOT become a shared bucket.
assert.strictEqual(
    ClipboardSilo.fromSecctx("qdistro.tier4", "", "instance-a"),
    ""
);

// Both engine and app_id empty -> no stable identity. Must fail safe
// to "" (caller treats as unknown / not same-silo), NEVER return the
// instance_id as a silo.
assert.strictEqual(
    ClipboardSilo.fromSecctx("", "", "instance-a"),
    ""
);

// Engine wins over a misleading app_id prefix: a non-tier3 engine
// carrying a tier3-looking app_id is keyed by engine:app_id, never by
// instance_id.
assert.strictEqual(
    ClipboardSilo.fromSecctx("flatpak", "qdistro.tier3.user1", "instance-a"),
    "flatpak:qdistro.tier3.user1"
);

assert.strictEqual(
    ClipboardSilo.fromSecctx("qdistro.tier5", "qdistro.tier3.user1", "launch-token-a"),
    "qdistro.tier5:qdistro.tier3.user1"
);

// Distinct silos must stay distinct (no false same-silo collapse).
assert.notStrictEqual(
    ClipboardSilo.fromSecctx("qdistro.tier3", "qdistro.tier3.user1", "same-token"),
    ClipboardSilo.fromSecctx("qdistro.tier3", "qdistro.tier3.user2", "same-token")
);

console.log("clipboard-silo: all assertions passed");
