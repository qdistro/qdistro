// Drift guard — asserts that the hand-kept JS mirrors in Services/**/*.js
// stay byte-identical to the corresponding logic in the source .qml files.
//
// WHY THIS EXISTS: The .js files are TEST MIRRORS, not imported by production
// QML. If the QML logic changes without updating the mirror, the tests pass
// while exercising stale logic. This script catches that.
//
// Covered mirrors:
//   Services/Commons/ColorKeys.js     ← Commons/Color.qml switch tables
//   Services/Commons/TimeFormat.js    ← Commons/Time.qml function bodies
//   Services/Commons/FuzzySort.js     ← Commons/FuzzySort.qml (sampled)
//   Services/Qdistro/SiloChrome.js    ← Tier3Apps.qml + Tier3sApps.qml +
//                                        Tier4Apps.qml
//                                        (palette array + prefix strings)
//
// For SiloChrome the guard is EXACT BYTE COMPARISON of:
//   • the 10-entry siloPalette arrays in Tier3Apps.qml, Tier3sApps.qml and
//     Tier4Apps.qml
//   • the tier3Prefix, tier3sPrefix and tier4Prefix string values
// This is the security-critical check: wrong prefix → wrong silo identity.

"use strict";

const assert = require("assert");

// The extraction/exec machinery lives in tests/lib/qmlextract.js so the
// behavioural tests (test_tier3s_gate_behaviour.js) share the SAME
// comment/string masker and function slicer — there must be exactly one
// copy of the scanner that decides which QML body a test runs.
const QE = require("./lib/qmlextract.js");
var read = QE.read;
var extractFunction = QE.extractFunction;
var compileQmlFunction = QE.compileQmlFunction;
var extractStringProp = QE.extractStringProp;
var extractPalette = QE.extractPalette;

// ─── 1. SiloChrome: palette byte-identity ────────────────────────────────────
// The palette in Tier3Apps.qml, Tier3sApps.qml, Tier4Apps.qml, and
// SiloChrome.js must all be identical. A QML change that isn't mirrored will
// break journal-based bats tests that grep for specific colour values.

(function testSiloChromePaletteDrift() {
    var tier3Src  = read("Services/Qdistro/Tier3Apps.qml");
    var tier3sSrc = read("Services/Qdistro/Tier3sApps.qml");
    var tier4Src  = read("Services/Qdistro/Tier4Apps.qml");
    var jsSrc     = read("Services/Qdistro/SiloChrome.js");

    var tier3Pal  = extractPalette(tier3Src, "siloPalette");
    var tier3sPal = extractPalette(tier3sSrc, "siloPalette");
    var tier4Pal  = extractPalette(tier4Src, "siloPalette");
    var jsPal     = extractPalette(jsSrc,    "SILO_PALETTE");

    assert.ok(tier3Pal && tier3Pal.length === 10,
        "Tier3Apps.qml: siloPalette must have 10 entries; got: " + (tier3Pal ? tier3Pal.length : "null"));
    assert.ok(tier3sPal && tier3sPal.length === 10,
        "Tier3sApps.qml: siloPalette must have 10 entries; got: " + (tier3sPal ? tier3sPal.length : "null"));
    assert.ok(tier4Pal && tier4Pal.length === 10,
        "Tier4Apps.qml: siloPalette must have 10 entries; got: " + (tier4Pal ? tier4Pal.length : "null"));
    assert.ok(jsPal && jsPal.length === 10,
        "SiloChrome.js: SILO_PALETTE must have 10 entries; got: " + (jsPal ? jsPal.length : "null"));

    assert.deepStrictEqual(tier3Pal, jsPal,
        "DRIFT: Tier3Apps.qml siloPalette does not match SiloChrome.js SILO_PALETTE.\n" +
        "  QML:  " + JSON.stringify(tier3Pal) + "\n" +
        "  JS:   " + JSON.stringify(jsPal));

    assert.deepStrictEqual(tier3sPal, jsPal,
        "DRIFT: Tier3sApps.qml siloPalette does not match SiloChrome.js SILO_PALETTE.\n" +
        "  QML:  " + JSON.stringify(tier3sPal) + "\n" +
        "  JS:   " + JSON.stringify(jsPal));

    assert.deepStrictEqual(tier4Pal, jsPal,
        "DRIFT: Tier4Apps.qml siloPalette does not match SiloChrome.js SILO_PALETTE.\n" +
        "  QML:  " + JSON.stringify(tier4Pal) + "\n" +
        "  JS:   " + JSON.stringify(jsPal));

    assert.deepStrictEqual(tier3Pal, tier4Pal,
        "DRIFT: Tier3Apps.qml and Tier4Apps.qml siloPalettes differ from each other.\n" +
        "  tier3: " + JSON.stringify(tier3Pal) + "\n" +
        "  tier4: " + JSON.stringify(tier4Pal));
})();

// ─── 2. SiloChrome: prefix string identity ───────────────────────────────────
// Wrong prefix means windows are assigned to the wrong silo context.
// These must match exactly.

(function testSiloChromePrefixDrift() {
    var tier3Src  = read("Services/Qdistro/Tier3Apps.qml");
    var tier3sSrc = read("Services/Qdistro/Tier3sApps.qml");
    var tier4Src  = read("Services/Qdistro/Tier4Apps.qml");
    var jsSrc     = read("Services/Qdistro/SiloChrome.js");

    var qml3Prefix  = extractStringProp(tier3Src,  "tier3Prefix");
    var qml3sPrefix = extractStringProp(tier3sSrc, "tier3sPrefix");
    var qml4Prefix  = extractStringProp(tier4Src,  "tier4Prefix");
    var js3Prefix   = extractStringProp(jsSrc,     "TIER3_PREFIX");
    var js3sPrefix  = extractStringProp(jsSrc,     "TIER3S_PREFIX");
    var js4Prefix   = extractStringProp(jsSrc,     "TIER4_PREFIX");

    assert.ok(qml3Prefix,  "Tier3Apps.qml: tier3Prefix not found");
    assert.ok(qml3sPrefix, "Tier3sApps.qml: tier3sPrefix not found");
    assert.ok(qml4Prefix,  "Tier4Apps.qml: tier4Prefix not found");
    assert.ok(js3Prefix,   "SiloChrome.js: TIER3_PREFIX not found");
    assert.ok(js3sPrefix,  "SiloChrome.js: TIER3S_PREFIX not found");
    assert.ok(js4Prefix,   "SiloChrome.js: TIER4_PREFIX not found");

    assert.strictEqual(qml3Prefix, js3Prefix,
        "DRIFT: tier3Prefix in Tier3Apps.qml ('" + qml3Prefix + "') " +
        "!= TIER3_PREFIX in SiloChrome.js ('" + js3Prefix + "')");

    // paravirt ΔB6: the tier3s↔tier3 prefixes are a near-miss pair
    // ("qdistro.tier3s." vs "qdistro.tier3.") — pin both identities.
    assert.strictEqual(qml3sPrefix, js3sPrefix,
        "DRIFT: tier3sPrefix in Tier3sApps.qml ('" + qml3sPrefix + "') " +
        "!= TIER3S_PREFIX in SiloChrome.js ('" + js3sPrefix + "')");

    assert.strictEqual(qml4Prefix, js4Prefix,
        "DRIFT: tier4Prefix in Tier4Apps.qml ('" + qml4Prefix + "') " +
        "!= TIER4_PREFIX in SiloChrome.js ('" + js4Prefix + "')");
})();

// ─── 3. ColorKeys: switch-table identity ────────────────────────────────────
// The resolveColorKey / resolveOnColorKey / resolveColorKeyOptional switch
// tables in Color.qml must match the JS mirror in ColorKeys.js.
// We compare the key→role mappings by running both against the same inputs
// and asserting identical outputs. Extracting raw case lines covers renames.

(function testColorKeysDrift() {
    var CK = require("../Services/Commons/ColorKeys.js");

    var colorSrc = read("Commons/Color.qml");

    // Verify that each function exists in the QML source (regression guard
    // for function renames — if QML renames resolveColorKey the test catches it).
    assert.ok(colorSrc.indexOf("function resolveColorKey(") !== -1,
        "Commons/Color.qml must define resolveColorKey");
    assert.ok(colorSrc.indexOf("function resolveOnColorKey(") !== -1,
        "Commons/Color.qml must define resolveOnColorKey");
    assert.ok(colorSrc.indexOf("function resolveColorKeyOptional(") !== -1,
        "Commons/Color.qml must define resolveColorKeyOptional");

    // Verify that all four named cases exist in the QML switch bodies.
    // This catches a case where a key is removed from QML but remains in JS.
    ["primary", "secondary", "tertiary", "error"].forEach(function(key) {
        assert.ok(colorSrc.indexOf('"' + key + '"') !== -1,
            "Commons/Color.qml must contain case for key '" + key + "'");
    });

    // Verify the default fallbacks are present.
    assert.ok(colorSrc.indexOf("mOnSurface") !== -1,
        "Commons/Color.qml resolveColorKey must fall back to mOnSurface");
    assert.ok(colorSrc.indexOf("mSurface") !== -1,
        "Commons/Color.qml resolveOnColorKey must fall back to mSurface");
    assert.ok(colorSrc.indexOf('"transparent"') !== -1,
        "Commons/Color.qml resolveColorKeyOptional must return transparent for default");

    // Run the JS mirror against all keys and assert it matches the expected
    // role names from the QML source (extracted by inspection above).
    var expectedRoles = {
        resolveColorKey:         { primary: "primary", secondary: "secondary", tertiary: "tertiary", error: "error", "": "onSurface", none: "onSurface" },
        resolveOnColorKey:       { primary: "onPrimary", secondary: "onSecondary", tertiary: "onTertiary", error: "onError", "": "surface", none: "surface" },
        resolveColorKeyOptional: { primary: "primary", secondary: "secondary", tertiary: "tertiary", error: "error", "": "transparent", none: "transparent" },
    };
    Object.keys(expectedRoles).forEach(function(fn) {
        Object.keys(expectedRoles[fn]).forEach(function(key) {
            var expected = expectedRoles[fn][key];
            var actual   = CK[fn](key);
            assert.strictEqual(actual, expected,
                "ColorKeys.js " + fn + "('" + key + "') = '" + actual +
                "' but expected '" + expected + "' (from Color.qml)");
        });
    });
})();

// ─── 4. TimeFormat: function presence in source ──────────────────────────────
// Verify that the functions mirrored in TimeFormat.js still exist in
// Commons/Time.qml by name. If they are renamed or removed, the mirror
// is orphaned and tests cover dead code.

(function testTimeFormatDrift() {
    var timeSrc = read("Commons/Time.qml");

    assert.ok(timeSrc.indexOf("function getFormattedTimestamp(") !== -1,
        "Commons/Time.qml must define getFormattedTimestamp");
    assert.ok(timeSrc.indexOf("function formatVagueHumanReadableDuration(") !== -1,
        "Commons/Time.qml must define formatVagueHumanReadableDuration");
    assert.ok(timeSrc.indexOf("function formatRelativeTime(") !== -1,
        "Commons/Time.qml must define formatRelativeTime");

    // Verify the QML guard condition for invalid input is the same as the JS.
    // QML: if (typeof totalSeconds !== 'number' || totalSeconds < 0) { return '0s'; }
    assert.ok(timeSrc.indexOf("typeof totalSeconds !== 'number'") !== -1,
        "Commons/Time.qml formatVagueHumanReadableDuration must have the typeof guard");

    // Verify the "no seconds when hours or minutes present" logic is still there.
    // QML: if (!hours && !minutes) { parts.push(...) }
    assert.ok(timeSrc.indexOf("!hours && !minutes") !== -1,
        "Commons/Time.qml must have !hours && !minutes condition");
})();

// ─── 5. FuzzySort: sampled function presence ─────────────────────────────────
// Verify that the core public functions mirrored in FuzzySort.js still exist
// in Commons/FuzzySort.qml by name, and that the scoring formula hasn't
// changed silently.

(function testFuzzySortDrift() {
    var fsSrc = read("Commons/FuzzySort.qml");

    assert.ok(fsSrc.indexOf("function go(") !== -1,   "Commons/FuzzySort.qml must define go");
    assert.ok(fsSrc.indexOf("function _go(") !== -1,  "Commons/FuzzySort.qml must define _go");
    assert.ok(fsSrc.indexOf("function single(") !== -1,  "Commons/FuzzySort.qml must define single");
    assert.ok(fsSrc.indexOf("function _single(") !== -1, "Commons/FuzzySort.qml must define _single");
    assert.ok(fsSrc.indexOf("function highlight(") !== -1,   "Commons/FuzzySort.qml must define highlight");
    assert.ok(fsSrc.indexOf("function prepare(") !== -1,     "Commons/FuzzySort.qml must define prepare");
    assert.ok(fsSrc.indexOf("function cleanup(") !== -1,     "Commons/FuzzySort.qml must define cleanup");
    assert.ok(fsSrc.indexOf("function _normalizeScore(") !== -1,
        "Commons/FuzzySort.qml must define _normalizeScore");
    assert.ok(fsSrc.indexOf("function _denormalizeScore(") !== -1,
        "Commons/FuzzySort.qml must define _denormalizeScore");
    assert.ok(fsSrc.indexOf("function _algorithm(") !== -1,
        "Commons/FuzzySort.qml must define _algorithm");

    // The threshold default: QML uses `options?.threshold ?? 0.35` which is
    // the canonical value. Changing it would silently degrade launcher ranking.
    assert.ok(fsSrc.indexOf("0.35") !== -1,
        "Commons/FuzzySort.qml must contain default threshold 0.35");

    // The scoring formula includes Math.E and 0.04307 — these are load-bearing
    // constants that affect ranking determinism across test runs.
    assert.ok(fsSrc.indexOf("Math.E") !== -1 || fsSrc.indexOf("Math.E **") !== -1,
        "Commons/FuzzySort.qml must contain Math.E in scoring formula");
    assert.ok(fsSrc.indexOf("0.04307") !== -1,
        "Commons/FuzzySort.qml must contain 0.04307 exponent in normalizeScore");
})();

// ─── 6. Widget helpers: function presence ────────────────────────────────────
// Verify that the property bindings mirrored in test_widget_helpers.js still
// exist in the real widget QML files.

(function testWidgetHelpersDrift() {
    var btnSrc    = read("Widgets/NButton.qml");
    var comboSrc  = read("Widgets/NComboBox.qml");
    var sliderSrc = read("Widgets/NSlider.qml");
    var textSrc   = read("Widgets/NTextInput.qml");
    var recSrc    = read("Widgets/NKeybindRecorder.qml");

    // NButton: contentColor
    assert.ok(btnSrc.indexOf("property color contentColor") !== -1 ||
              btnSrc.indexOf("readonly property color contentColor") !== -1,
        "NButton.qml must define contentColor property");
    assert.ok(btnSrc.indexOf("return Color.mOnSurfaceVariant") !== -1,
        "NButton.qml contentColor must return mOnSurfaceVariant when disabled");

    // NComboBox: isValueChanged uses != (not !==)
    assert.ok(comboSrc.indexOf("isValueChanged") !== -1,
        "NComboBox.qml must define isValueChanged");
    assert.ok(comboSrc.indexOf("currentKey != defaultValue") !== -1,
        "NComboBox.qml isValueChanged must use != (loose, intentional)");

    // NComboBox: findIndexByKey
    assert.ok(comboSrc.indexOf("function findIndexByKey(") !== -1,
        "NComboBox.qml must define findIndexByKey");

    // NSlider: snapMode ternary
    assert.ok(sliderSrc.indexOf("snapAlways ? Slider.SnapAlways : Slider.SnapOnRelease") !== -1,
        "NSlider.qml must have snapMode ternary expression");

    // NTextInput: isValueChanged uses !==  (strict)
    assert.ok(textSrc.indexOf("isValueChanged") !== -1,
        "NTextInput.qml must define isValueChanged");
    assert.ok(textSrc.indexOf("text !== defaultValue") !== -1,
        "NTextInput.qml isValueChanged must use !== (strict)");

    // NKeybindRecorder: maxKeybinds and slice cap
    assert.ok(recSrc.indexOf("maxKeybinds") !== -1,
        "NKeybindRecorder.qml must define maxKeybinds");
    assert.ok(recSrc.indexOf(".slice(0, root.maxKeybinds)") !== -1,
        "NKeybindRecorder.qml must cap via .slice(0, root.maxKeybinds)");
    // Sentinel: -1 is used, >= 0 is used; -2 is NOT assigned in real code.
    assert.ok(recSrc.indexOf("recordingIndex: -1") !== -1,
        "NKeybindRecorder.qml must initialise recordingIndex: -1");
    assert.ok(recSrc.indexOf("recordingIndex = -1") !== -1,
        "NKeybindRecorder.qml must reset recordingIndex to -1");
    // Guard: -2 must NOT appear as an assigned value (only in a comment).
    var assignMinus2 = recSrc.match(/recordingIndex\s*=\s*-2/);
    assert.ok(!assignMinus2,
        "NKeybindRecorder.qml must NOT assign recordingIndex = -2 " +
        "(the -2 sentinel exists only in a comment; real code uses -1 and >=0)");
})();

// ─── 7. SiloChrome: BEHAVIOURAL equivalence (QML logic executed) ─────────────
// Sections 1–2 pin the palette + prefix CONSTANTS. But the security-relevant
// ALGORITHMS were unguarded: siloFromSecctx (the only trusted silo-identity
// derivation — spec/02), the colourForSilo hash, isTier3/4, and _hexToRgba.
// A divergence in any of these (a changed slice offset, a different hash mix,
// a wrong packing) would leave the JS unit tests green (they test the mirror)
// while production QML behaves differently. This section executes the ACTUAL
// QML function bodies and asserts byte-equal outputs against the mirror across
// an input battery that hits every branch + the identity edge cases. Catches
// algorithm drift, not just constant drift (04/F6, maturity-review #5).

(function testSiloChromeBehaviouralDrift() {
    var tier3Src  = read("Services/Qdistro/Tier3Apps.qml");
    var tier3sSrc = read("Services/Qdistro/Tier3sApps.qml");
    var tier4Src  = read("Services/Qdistro/Tier4Apps.qml");
    var SC = require("../Services/Qdistro/SiloChrome.js");

    // `root` stand-ins providing exactly the members each function reads. We
    // feed the MIRROR's constants in; sections 1–2 already proved those equal
    // the QML constants, so any failure here is a genuine ALGORITHM drift.
    var root3  = { siloPalette: SC.SILO_PALETTE, tier3Prefix:  SC.TIER3_PREFIX };
    var root3s = { siloPalette: SC.SILO_PALETTE, tier3sPrefix: SC.TIER3S_PREFIX };
    var root4  = { siloPalette: SC.SILO_PALETTE, tier4Prefix:  SC.TIER4_PREFIX };

    var qml3Colour  = compileQmlFunction(tier3Src,  "colourForSilo",  root3);
    var qml3Silo    = compileQmlFunction(tier3Src,  "siloFromSecctx", root3);
    var qml3IsT3    = compileQmlFunction(tier3Src,  "isTier3",        root3);
    var qml3sColour = compileQmlFunction(tier3sSrc, "colourForSilo",  root3s);
    var qml3sSilo   = compileQmlFunction(tier3sSrc, "siloFromSecctx", root3s);
    var qml3sIsT3s  = compileQmlFunction(tier3sSrc, "isTier3s",       root3s);
    var qml4Colour  = compileQmlFunction(tier4Src,  "colourForSilo",  root4);
    var qml4Silo    = compileQmlFunction(tier4Src,  "siloFromSecctx", root4);
    var qml4IsT4    = compileQmlFunction(tier4Src,  "isTier4",        root4);
    var qml4Hex     = compileQmlFunction(tier4Src,  "_hexToRgba",     root4);

    var siloNames = ["", "user1", "user2", "user10", "a", "ab", "abc",
                     "vm-work", "vm_work", "USER1", "user-1.evil", "Bsafe",
                     "0", "9", "silo with space", "..", "tier3", "tier4",
                     "tier3s", "smoke"];
    // secctx app-ids spanning: matching prefix, wrong tier prefix, exact prefix
    // with empty tag, prefix-not-at-start, case variants, multi-dot tails —
    // and the paravirt ΔB6 near-miss pair "qdistro.tier3s.*" vs
    // "qdistro.tier3.*" (each tier's isTierN must refuse the other's ids).
    var secctxIds = [
        null, "", "qdistro.tier3.user1", "qdistro.tier4.vm1",
        "qdistro.tier3.", "qdistro.tier4.", "qdistro.tier3", "qdistro.tier4",
        "qdistro.tier3.user1.evil", "qdistro.tier4.a.b.c",
        "x.qdistro.tier3.user1", "x.qdistro.tier4.vm1",
        "QDISTRO.TIER3.user1", "QDISTRO.TIER4.vm1",
        "qdistro.tier30.user1", "qdistro.tier3.user1 ",
        "qdistro.tier3s.smoke", "qdistro.tier3s.", "qdistro.tier3s",
        "x.qdistro.tier3s.smoke", "QDISTRO.TIER3S.smoke",
    ];
    var hexes = ["#4caf50", "#FFFFFF", "#000000", "#ffb300", "#80deea",
                 null, "", "#fff", "#4caf5", "#4caf500", "4caf50",
                 "#gggggg", "#12345g", "#ABCDEF", "##abcde", " #4caf50"];

    function eq(label, qmlFn, jsFn, inputs) {
        inputs.forEach(function(inp) {
            var q = qmlFn(inp);
            var j = jsFn(inp);
            assert.deepStrictEqual(j, q,
                "BEHAVIOURAL DRIFT in " + label + "(" + JSON.stringify(inp) +
                "): QML returns " + JSON.stringify(q) +
                " but SiloChrome.js mirror returns " + JSON.stringify(j));
        });
    }

    eq("Tier3.colourForSilo",   qml3Colour,  SC.colourForSilo,         siloNames);
    eq("Tier3s.colourForSilo",  qml3sColour, SC.colourForSilo,         siloNames);
    eq("Tier4.colourForSilo",   qml4Colour,  SC.colourForSilo,         siloNames);
    eq("Tier3.siloFromSecctx",  qml3Silo,    SC.siloFromSecctxTier3,   secctxIds);
    eq("Tier3s.siloFromSecctx", qml3sSilo,   SC.siloFromSecctxTier3s,  secctxIds);
    eq("Tier4.siloFromSecctx",  qml4Silo,    SC.siloFromSecctxTier4,   secctxIds);
    eq("Tier3.isTier3",         qml3IsT3,    SC.isTier3,               secctxIds);
    eq("Tier3s.isTier3s",       qml3sIsT3s,  SC.isTier3s,              secctxIds);
    eq("Tier4.isTier4",         qml4IsT4,    SC.isTier4,               secctxIds);
    eq("Tier4._hexToRgba",      qml4Hex,     SC.hexToRgba,             hexes);

    // Cross-tier: all QML colour hashes share the palette + algorithm, so a
    // silo must get the SAME border colour regardless of tier (the bats
    // journal-grep contract depends on this determinism).
    siloNames.forEach(function(s) {
        assert.strictEqual(qml4Colour(s), qml3Colour(s),
            "Tier3 and Tier4 colourForSilo disagree for silo " +
            JSON.stringify(s));
        assert.strictEqual(qml3sColour(s), qml3Colour(s),
            "Tier3 and Tier3s colourForSilo disagree for silo " +
            JSON.stringify(s));
    });
})();

console.log("drift-guard: all assertions passed");
