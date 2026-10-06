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

// paravirt ΔB6: tier3s (gVisor + waypipe bridge) derives the BARE silo,
// the same shape tier-3 uses — the canonical key for clipboard rules,
// launch records and broker lineage is <silo>, never "tier3s/<silo>".
// ensures: a tier3s toplevel's clipboard identity is its bare silo name
sameStableSilo(
    {
        sandboxEngine: "qdistro.tier3s",
        appId: "qdistro.tier3s.smoke",
        instanceId: "launch-token-a",
    },
    {
        sandboxEngine: "qdistro.tier3s",
        appId: "qdistro.tier3s.smoke",
        instanceId: "launch-token-b",
    },
    "smoke"
);

// ensures: the tier3s launch token (instance_id) never enters the silo key
assert.strictEqual(
    ClipboardSilo.fromSecctx("qdistro.tier3s", "qdistro.tier3s.smoke", "0123456789abcdef"),
    ClipboardSilo.fromSecctx("qdistro.tier3s", "qdistro.tier3s.smoke", "fedcba9876543210")
);

// ensures: "qdistro.tier3s" engine is NOT claimed by the tier3 prefix branch
// (its result would be identical here, so prove it via the inverse: a tier3s
// engine carrying a NON-tier3s-shaped app_id falls through to the generic
// engine:app_id pair, never to a bare-silo shape)
assert.strictEqual(
    ClipboardSilo.fromSecctx("qdistro.tier3s", "not-a-tier3s-appid", "instance-a"),
    "qdistro.tier3s:not-a-tier3s-appid"
);
// ensures: a tier3 engine carrying a tier3s-SHAPED app_id is NOT a tier3s
// silo — the app_id "qdistro.tier3s.evil" does not start with
// "qdistro.tier3." (the 's' breaks the prefix) and the engine is not
// "qdistro.tier3s", so the generic namespaced engine:app_id pair applies.
assert.strictEqual(
    ClipboardSilo.fromSecctx("qdistro.tier3", "qdistro.tier3s.evil", "instance-a"),
    "qdistro.tier3:qdistro.tier3s.evil"
);
// ensures: a tier3s engine with a MISSING app_id fails safe to "" — no
// engine-only bucket, and the tier3s MIME strip is never dodged by an
// empty app_id collapsing into a shared identity.
assert.strictEqual(
    ClipboardSilo.fromSecctx("qdistro.tier3s", "", "instance-a"),
    ""
);
// ensures: a tier3s silo and a same-named tier-3 silo stay distinct is NOT
// guaranteed — they share the bare-silo namespace BY DESIGN (canonical key
// is <silo> on both tiers), so prove at least that a tier3s silo never
// collapses into the tier2/tier5 shapes.
assert.notStrictEqual(
    ClipboardSilo.fromSecctx("qdistro.tier3s", "qdistro.tier3s.smoke", "tok"),
    ClipboardSilo.fromSecctx("qdistro.tier5", "qdistro.tier5.smoke", "tok")
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
