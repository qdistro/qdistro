// Behavioural tests for the tier3s additions to ClipboardGate.qml and
// Tier3FocusIPC.qml (paravirt ΔB6; sol B-ii finding: the MIME decision
// paths and tier3s handle membership had no executable coverage).
//
// The functions are extracted from the production .qml sources and
// executed under Node via tests/lib/qmlextract.js — this exercises the
// real gate bodies, not re-typed copies. A regression that removes the
// strict-MIME branch or tier3s focus membership fails here even though
// the QML never runs on the host.

"use strict";

const assert = require("assert");
const QE = require("./lib/qmlextract.js");

var cgSrc = QE.read("Services/Qdshell/ClipboardGate.qml");
var ipcSrc = QE.read("Services/Qdistro/Tier3FocusIPC.qml");

// ─── fake window model + imported-singleton shims ─────────────────────────
// Tier3FocusIPC bodies reference the Tier3Apps/Tier4Apps/Tier3sApps
// singletons' <t>Windows ListModels ({count, get(i) -> row}).
function model(rows) {
    return { count: rows.length, get: function(i) { return rows[i]; } };
}
function shells(t3, t3s, t4) {
    return {
        Tier3Apps: { tier3Windows: t3 === null ? null : model(t3) },
        Tier3sApps: { tier3sWindows: t3s === null ? null : model(t3s) },
        Tier4Apps: { tier4Windows: t4 === null ? null : model(t4) },
    };
}

// ─── ClipboardGate: strict-MIME source classification ─────────────────────

var isStrict = QE.compileQmlFunctionWith(cgSrc, "_isStrictMimeSource", {});
var mimeTier = QE.compileQmlFunctionWith(cgSrc, "_strictMimeTier", {});

(function testStrictMimeSource() {
    // ensures: a tier3s offer is MIME-gated exactly like tier-4
    assert.strictEqual(isStrict("qdistro.tier3s.dev"), true,
        "qdistro.tier3s.<silo> must be a strict-MIME source");
    assert.strictEqual(isStrict("qdistro.tier4.vm1"), true,
        "qdistro.tier4.<silo> must remain a strict-MIME source");
    // ensures: the tier3/tier3s prefix collision cannot misclassify —
    // 'qdistro.tier3.' must NOT match a tier3s app id and vice versa
    assert.strictEqual(isStrict("qdistro.tier3.dev"), false,
        "a plain tier-3 app id must NOT take the strict path");
    assert.strictEqual(isStrict("qdistro.tier3s"), false,
        "the bare engine name without '.<silo>' is not a source id");
    assert.strictEqual(isStrict("qdistro.tier3sx.foo"), false,
        "prefix boundary: 'qdistro.tier3sx.' is not 'qdistro.tier3s.'");
    assert.strictEqual(isStrict(""), false);
    assert.strictEqual(isStrict("qdistro.tier2.dev"), false);
})();

(function testStrictMimeTier() {
    // ensures: the strip is logged under the right tier so drivers can grep it
    assert.strictEqual(mimeTier("qdistro.tier3s.dev"), "tier3s");
    assert.strictEqual(mimeTier("qdistro.tier4.vm1"), "tier4");
})();

// ─── ClipboardGate: the strip itself, on the production allow-list ────────

(function testStripTier4Mimes() {
    // Feed the compiled body the allow-list extracted from the SAME source —
    // the test breaks if production narrows the list or the extractor misses.
    var allowed = QE.extractStringArray(cgSrc, "_tier4AllowedMimeBases");
    assert.deepStrictEqual(allowed, ["text/plain", "text/uri-list"],
        "ClipboardGate._tier4AllowedMimeBases drifted — recheck the gate");
    var strip = QE.compileQmlFunctionWith(
        cgSrc, "_stripTier4Mimes", { root: { _tier4AllowedMimeBases: allowed } });
    // ensures: image/* and text/html from a tier3s source never reach the
    // broker/policy consult; parameters survive, junk is dropped
    var out = strip(["text/plain", "image/png", "text/html",
                     "text/uri-list", "text/plain;charset=utf-8",
                     "", 7, " text/plain "]);
    assert.deepStrictEqual(out.sort(), [" text/plain ", "text/plain",
                                        "text/plain;charset=utf-8", "text/uri-list"].sort());
    // dedup on the FULL string: identical repeats collapse, distinct
    // spellings survive (the offerer sees what it offered)
    assert.deepStrictEqual(strip(["text/plain", "text/plain"]),
        ["text/plain"]);
    // a source offering ONLY forbidden types strips to empty — the caller
    // then denies on 'tier3s-no-allowed-mimes'
    assert.deepStrictEqual(strip(["image/png", "application/octet-stream"]), []);
})();

// ─── Tier3FocusIPC: tier3s handle membership ──────────────────────────────

(function testIsTier3Handle() {
    var env = shells([{ silo: "a", handle: 11 }],
                     [{ silo: "s3s", handle: 77 }],
                     [{ silo: "v", handle: 55 }]);
    var isH = QE.compileQmlFunctionWith(ipcSrc, "_isTier3Handle", env);
    // ensures: a tier3s toplevel accepts focus injection for s127
    assert.strictEqual(isH(77), true, "tier3s window handle must be accepted");
    // ensures: the pre-existing tier-3/tier-4 boundary is unchanged
    assert.strictEqual(isH(11), true);
    assert.strictEqual(isH(55), true);
    // ensures: an arbitrary handle still cannot be focus-stolen
    assert.strictEqual(isH(9999), false);
    assert.strictEqual(isH(0), false);
})();

(function testIsTier3HandleNullModels() {
    // a shell where the tier3s service has no windows yet (null model)
    // must still answer correctly for the tiers that do
    var env = shells(null, null, null);
    var isH = QE.compileQmlFunctionWith(ipcSrc, "_isTier3Handle", env);
    assert.strictEqual(isH(77), false);
})();

(function testFindSiloHandle() {
    var env = shells([{ silo: "shared", handle: 11 }],
                     [{ silo: "only3s", handle: 77 },
                      { silo: "shared", handle: 78 }],
                     []);
    var find = QE.compileQmlFunctionWith(ipcSrc, "_findSiloHandle", env);
    // ensures: a tier3s-only silo resolves to its tier3s handle (ΔB6)
    assert.strictEqual(find("only3s"), 77);
    // documented first-match ordering: tier3 wins over tier3s on a tie
    assert.strictEqual(find("shared"), 11);
    // ensures: garbage input cannot fabricate a handle
    assert.strictEqual(find("nosuch"), -1);
    assert.strictEqual(find(""), -1);
    assert.strictEqual(find(null), -1);
    assert.strictEqual(find("x".repeat(65)), -1);
})();

console.log("test_tier3s_gate_behaviour: all checks passed");
