const assert = require("assert");
const Coalesce = require("../Services/Qdshell/ClipboardDenyCoalesce.js");

// Clipboard deny-clear COALESCER (clipboard.md §"deny-storm robustness"). The
// pure decision is ClipboardDenyCoalesce.shouldSendClear(); the side effects
// (the CLIPBOARD_GATE verdict line + binding.clearSelection() + the
// CLIPBOARD_CLEAR_COALESCED debug line) live in ClipboardGate.qml's
// _logDecisionAndMaybeClear. To exercise the WHOLE contract — not just the
// decision — this test reproduces that branch verbatim around the real module,
// with a fake binding recording clearSelection() calls and a journal sink,
// exactly as the QML does.
//
// ensures (overall): under a deny storm the gate issues at most one
// clear_selection wire call per denied-offer identity per window (so it can't
// overflow the 4 KB libwayland buffer and kill the shell↔compositor link),
// while NEVER suppressing the first clear for a genuinely new denied offer and
// NEVER dropping a CLIPBOARD_GATE verdict line — fail-closed is preserved.

const WINDOW = Coalesce.DEFAULT_WINDOW_MS; // 500

// Build a harness mirroring the QML singleton's _lastDenyClearByKey state and
// the deny branch of _logDecisionAndMaybeClear, including the explicit clock.
function makeGate(windowMs) {
    const gate = {
        lastDenyClearByKey: {},
        windowMs: (windowMs === undefined) ? WINDOW : windowMs,
        now: 0,
        clears: [],   // [{ seat, isPrimary }] in call order (real wire calls)
        verdicts: [], // every CLIPBOARD_GATE line (must be one per decision)
        coalesced: [] // every CLIPBOARD_CLEAR_COALESCED debug line
    };
    gate.tick = function (ms) { gate.now += ms; };

    // Mirror _logDecisionAndMaybeClear: ALWAYS log the verdict; on a deny, ask
    // the coalescer whether to issue the real clear or just log the suppressed
    // debug line.
    gate.decide = function (entry, verdict, reason) {
        gate.verdicts.push({
            seat: entry.seat || "default",
            srcSilo: entry.srcSilo, dstSilo: entry.dstSilo,
            mimeCsv: entry.mimeCsv, verdict: verdict, reason: reason
        });
        if (verdict !== "deny")
            return;
        if (Coalesce.shouldSendClear(gate.lastDenyClearByKey, entry,
                                     gate.now, gate.windowMs)) {
            gate.clears.push({ seat: entry.seat || "default",
                               isPrimary: entry.isPrimary });
        } else {
            gate.coalesced.push({ seat: entry.seat || "default",
                                  isPrimary: entry.isPrimary });
        }
    };
    return gate;
}

function denyEntry(over) {
    return Object.assign({
        seat: "default", isPrimary: false,
        srcSilo: "tier3/personal", dstSilo: "uid:1000",
        mimeCsv: "text/plain"
    }, over || {});
}

// ---------------------------------------------------------------------------
// 1. First deny clears; an identical deny inside the window is coalesced (no
//    wire call) but its verdict line is STILL logged.
// ensures: the fail-closed first clear always fires, and coalescing only drops
//          the redundant wire write, never the audit verdict.
// ---------------------------------------------------------------------------
{
    const g = makeGate();
    g.decide(denyEntry(), "deny", "unknown-identity");        // t=0 → clear
    g.tick(10);
    g.decide(denyEntry(), "deny", "unknown-identity");        // t=10 → coalesced
    g.tick(WINDOW - 50);
    g.decide(denyEntry(), "deny", "unknown-identity");        // t<window → coalesced

    assert.deepStrictEqual(g.clears, [{ seat: "default", isPrimary: false }]);
    assert.strictEqual(g.coalesced.length, 2);
    // EVERY decision logged a verdict line — the frozen contract is intact.
    assert.strictEqual(g.verdicts.length, 3);
    assert.ok(g.verdicts.every(v => v.verdict === "deny"));
}

// ---------------------------------------------------------------------------
// 2. After the window lapses, the next deny clears again (re-armed). The wire
//    rate is bounded to ~1 clear per window per key, not zero.
// ensures: coalescing is a rate limiter, not a permanent latch — a persistently
//          re-asserting source keeps getting re-cleared.
// ---------------------------------------------------------------------------
{
    const g = makeGate();
    g.decide(denyEntry(), "deny", "broker-deny");   // t=0 → clear
    g.tick(WINDOW);                                  // exactly window later
    g.decide(denyEntry(), "deny", "broker-deny");   // t=window → clear (>= w)
    g.tick(WINDOW + 1);
    g.decide(denyEntry(), "deny", "broker-deny");   // → clear
    assert.strictEqual(g.clears.length, 3);
    assert.strictEqual(g.coalesced.length, 0);
}

// ---------------------------------------------------------------------------
// 3. A DIFFERENT key always gets a fresh first clear, even inside the window of
//    another key. Distinct on seat, kind, src silo, dst silo, OR mime set.
// ensures: coalescing keys on the full denied-offer identity — it never
//          suppresses a clear for a genuinely different cross-silo offer.
// ---------------------------------------------------------------------------
{
    const g = makeGate();
    g.decide(denyEntry(), "deny", "r");                                   // base
    g.decide(denyEntry({ isPrimary: true }), "deny", "r");                // kind
    g.decide(denyEntry({ seat: "seat1" }), "deny", "r");                  // seat
    g.decide(denyEntry({ srcSilo: "tier2/work" }), "deny", "r");          // src
    g.decide(denyEntry({ dstSilo: "uid:1001" }), "deny", "r");            // dst
    g.decide(denyEntry({ mimeCsv: "text/plain,text/html" }), "deny", "r"); // mime
    // All six are distinct identities → six real clears, zero coalesced.
    assert.strictEqual(g.clears.length, 6);
    assert.strictEqual(g.coalesced.length, 0);
}

// ---------------------------------------------------------------------------
// 4. A backward clock step (NTP adjust) does NOT suppress a clear — fail-closed.
// ensures: time going backwards re-arms rather than wedging the coalescer into
//          dropping genuine clears.
// ---------------------------------------------------------------------------
{
    const g = makeGate();
    g.decide(denyEntry(), "deny", "r");   // t=0 → clear (records 0... but now=0)
    // Move forward then jump backward past the recorded stamp.
    g.now = 1000;
    g.decide(denyEntry(), "deny", "r");   // t=1000 → clear (re-armed, records 1000)
    g.now = 200;                          // clock stepped backward
    g.decide(denyEntry(), "deny", "r");   // now < last → clear (fail-closed)
    assert.strictEqual(g.clears.length, 3);
    assert.strictEqual(g.coalesced.length, 0);
}

// ---------------------------------------------------------------------------
// 5. The storm itself: 1000 identical denies fired across 10 s collapse to a
//    bounded handful of wire clears (~ duration/window + 1), not 1000.
// ensures: the producer-side storm is broken before it can overrun the buffer,
//          which is the whole point of the fix.
// ---------------------------------------------------------------------------
{
    const g = makeGate();
    const N = 1000;
    const totalMs = 10000;          // ~127/s equivalent rate, like the bug repro
    const step = totalMs / N;       // 10 ms between events
    for (let i = 0; i < N; i++) {
        g.decide(denyEntry(), "deny", "broker-deny");
        g.tick(step);
    }
    // Upper bound: one clear at t=0 plus one per fully-elapsed window.
    const maxClears = Math.floor(totalMs / WINDOW) + 1;
    assert.ok(g.clears.length <= maxClears,
              "clears " + g.clears.length + " must be <= " + maxClears);
    assert.ok(g.clears.length >= 1, "at least the first deny must clear");
    // Every single deny was still audited.
    assert.strictEqual(g.verdicts.length, N);
    assert.strictEqual(g.clears.length + g.coalesced.length, N);
}

// ---------------------------------------------------------------------------
// 6. Non-deny verdicts never clear and never touch the coalescer state.
// ensures: allow verdicts are inert here — only denies drive clears.
// ---------------------------------------------------------------------------
{
    const g = makeGate();
    g.decide(denyEntry(), "allow", "same-silo");
    g.decide(denyEntry(), "allow", "broker-allow");
    assert.deepStrictEqual(g.clears, []);
    assert.deepStrictEqual(g.coalesced, []);
    assert.strictEqual(g.verdicts.length, 2);
    assert.deepStrictEqual(g.lastDenyClearByKey, {});
}

// ---------------------------------------------------------------------------
// Direct unit checks of the pure module (no harness): key distinctness, the
// fail-safe collapse of missing fields, pruning bounds, and the window edge.
// ensures: the contract ClipboardGate depends on is stable.
// ---------------------------------------------------------------------------
{
    // coalesceKey distinctness: length-prefixing prevents field-boundary
    // aliasing — ("a","bc") must not collide with ("ab","c").
    const k1 = Coalesce.coalesceKey(denyEntry({ srcSilo: "a", dstSilo: "bc" }));
    const k2 = Coalesce.coalesceKey(denyEntry({ srcSilo: "ab", dstSilo: "c" }));
    assert.notStrictEqual(k1, k2);

    // SECURITY: a forged U+001F (the separator) inside an attacker-controlled
    // field must NOT alias two distinct offers. Inbound secctx/mimes are not
    // control-char-stripped before the gate, so this is reachable; length-
    // prefixing defeats it. Without the length prefix these two would collide.
    const SEP = Coalesce.FIELD_SEP;
    const a1 = Coalesce.coalesceKey(denyEntry({ srcSilo: "A", dstSilo: "B" + SEP + "C" }));
    const a2 = Coalesce.coalesceKey(denyEntry({ srcSilo: "A" + SEP + "B", dstSilo: "C" }));
    assert.notStrictEqual(a1, a2);
    // A literal "N:" inside a field is likewise unambiguous (injective).
    const b1 = Coalesce.coalesceKey(denyEntry({ srcSilo: "1:x", dstSilo: "y" }));
    const b2 = Coalesce.coalesceKey(denyEntry({ srcSilo: "1", dstSilo: "x:y" }));
    assert.notStrictEqual(b1, b2);

    // Missing fields collapse to stable fail-safe constants, not undefined.
    const kMissing = Coalesce.coalesceKey({ isPrimary: false });
    assert.strictEqual(kMissing.indexOf("undefined"), -1);
    // Length-prefixed form: "<len>:<field>" per field, joined by FIELD_SEP.
    assert.strictEqual(kMissing,
        ["default", "clipboard", "unknown", "unknown", ""]
            .map(f => f.length + ":" + f).join(SEP) + SEP);

    // isPrimary maps to a distinct kind token.
    assert.strictEqual(Coalesce.selectionKind(true), "primary");
    assert.strictEqual(Coalesce.selectionKind(false), "clipboard");

    // pruneExpired drops entries older than window*PRUNE_FACTOR and future-
    // stamped entries, keeps fresh ones.
    const st = {};
    st["fresh"] = 9000;
    st["old"] = 9000 - (WINDOW * Coalesce.PRUNE_FACTOR) - 1;
    st["future"] = 9000 + 1;
    Coalesce.pruneExpired(st, 9000, WINDOW);
    assert.deepStrictEqual(Object.keys(st).sort(), ["fresh"]);

    // shouldSendClear records the timestamp on a true result and bounds repeats.
    const s2 = {};
    assert.strictEqual(Coalesce.shouldSendClear(s2, denyEntry(), 0, WINDOW), true);
    assert.strictEqual(Coalesce.shouldSendClear(s2, denyEntry(), 1, WINDOW), false);
    // exactly at the window boundary it re-arms (>= window clears).
    assert.strictEqual(Coalesce.shouldSendClear(s2, denyEntry(), WINDOW, WINDOW), true);
}

console.log("clipboard-deny-coalesce: all assertions passed");
