const assert = require("assert");
const P = require("../Services/Power/PresentationPolicy.js");

// ─── idempotent inhibitor add/remove ────────────────────────────────
let l = [];
l = P.addInhibitor(l, "presentation-mode");
assert.deepStrictEqual(l, ["presentation-mode"]);
// adding the same id again is a no-op (idempotent)
let l2 = P.addInhibitor(l, "presentation-mode");
assert.deepStrictEqual(l2, ["presentation-mode"]);
// add returns a NEW array (QML reassignment requirement) — not the same ref
assert.notStrictEqual(P.addInhibitor(l, "x"), l);
// removing an absent id is a no-op
assert.deepStrictEqual(P.removeInhibitor(["a"], "b"), ["a"]);
// remove the only id
assert.deepStrictEqual(P.removeInhibitor(["presentation-mode"], "presentation-mode"), []);
// non-array input tolerated
assert.deepStrictEqual(P.addInhibitor(null, "z"), ["z"]);
assert.deepStrictEqual(P.removeInhibitor(undefined, "z"), []);
assert.strictEqual(P.hasInhibitor(["a", "b"], "b"), true);
assert.strictEqual(P.hasInhibitor([], "b"), false);

// ─── presentation-mode toggle ───────────────────────────────────────
assert.deepStrictEqual(P.applyPresentationMode([], true), ["presentation-mode"]);
assert.deepStrictEqual(P.applyPresentationMode(["manual"], true), ["manual", "presentation-mode"]);
// enabling twice idempotent
assert.deepStrictEqual(P.applyPresentationMode(["presentation-mode"], true), ["presentation-mode"]);
// disabling removes only the presentation id, leaves others intact
assert.deepStrictEqual(P.applyPresentationMode(["manual", "presentation-mode"], false), ["manual"]);
// disabling when not present is a no-op
assert.deepStrictEqual(P.applyPresentationMode(["manual"], false), ["manual"]);

// ─── auto-disable minutes clamping ──────────────────────────────────
assert.strictEqual(P.clampAutoDisableMinutes(0), 0);
assert.strictEqual(P.clampAutoDisableMinutes(30), 30);
assert.strictEqual(P.clampAutoDisableMinutes(-5), 0); // negative -> 0
assert.strictEqual(P.clampAutoDisableMinutes(99999), 1440); // capped at 24h
assert.strictEqual(P.clampAutoDisableMinutes("45"), 45); // numeric string
assert.strictEqual(P.clampAutoDisableMinutes(NaN), 0);
assert.strictEqual(P.clampAutoDisableMinutes(12.6), 13); // rounded

// ─── state restore ──────────────────────────────────────────────────
assert.strictEqual(P.shouldRestorePresentationMode(true), true);
assert.strictEqual(P.shouldRestorePresentationMode(false), false);
assert.strictEqual(P.shouldRestorePresentationMode(undefined), false);
assert.strictEqual(P.shouldRestorePresentationMode("true"), false); // strict bool only

// ─── disable-notifications-while-inhibited policy ───────────────────
assert.strictEqual(P.shouldSuppressNotifications(true, true), true);
assert.strictEqual(P.shouldSuppressNotifications(true, false), false);
assert.strictEqual(P.shouldSuppressNotifications(false, true), false);
assert.strictEqual(P.shouldSuppressNotifications(false, false), false);

// ─── viewer rows + INJECTION SAFETY ─────────────────────────────────
// Well-known ids get a "known" key; unknown ids fall back to raw verbatim.
const rows = P.inhibitorRows(["presentation-mode", "manual", "fullscreen", "app:firefox"]);
assert.strictEqual(rows.length, 4);
assert.strictEqual(rows[0].known, "presentation");
assert.strictEqual(rows[1].known, "manual");
assert.strictEqual(rows[2].known, "fullscreen");
assert.strictEqual(rows[3].known, null);
assert.strictEqual(rows[3].id, "app:firefox");

// An inhibitor id is OPAQUE UNTRUSTED text. A malicious app could register an
// id containing shell metacharacters. The policy must pass it through VERBATIM
// (for PlainText rendering) and must NOT transform/escape/eval it, and there
// must be no command-building helper that could interpolate it.
const evil = "$(rm -rf ~); `id`; ;|& <script>alert(1)</script>";
const evilRows = P.inhibitorRows([evil]);
assert.strictEqual(evilRows.length, 1);
assert.strictEqual(evilRows[0].id, evil, "untrusted id must be preserved verbatim, never transformed");
assert.strictEqual(evilRows[0].known, null);
// No exported helper may build a shell command / argv from an inhibitor id.
Object.keys(P).forEach(function (name) {
  assert.ok(!/cmd|command|argv|shell|exec|spawn/i.test(name), "no command-builder export: " + name);
});

// non-array tolerated
assert.deepStrictEqual(P.inhibitorRows(null), []);

// ════════════════════════════════════════════════════════════════════
// EXPANDED COVERAGE
// ════════════════════════════════════════════════════════════════════

// ─── exported well-known id constants are fixed, never user-derived ──
assert.strictEqual(P.PRESENTATION_INHIBITOR_ID, "presentation-mode");
assert.strictEqual(P.FULLSCREEN_INHIBITOR_ID, "fullscreen");

// ─── add/remove edge cases + non-mutation + round-trip ──────────────
// add does NOT mutate its input array (returns a fresh copy).
const baseList = ["a"];
const added = P.addInhibitor(baseList, "b");
assert.deepStrictEqual(baseList, ["a"], "addInhibitor must not mutate input");
assert.deepStrictEqual(added, ["a", "b"]);
// remove does NOT mutate its input array.
const remBase = ["a", "b"];
const removed = P.removeInhibitor(remBase, "a");
assert.deepStrictEqual(remBase, ["a", "b"], "removeInhibitor must not mutate input");
assert.deepStrictEqual(removed, ["b"]);
// remove returns a NEW array even when the id is absent (QML reassign safety).
assert.notStrictEqual(P.removeInhibitor(remBase, "absent"), remBase);
// add->remove round-trip returns to the original contents (order preserved).
let rt = ["manual", "fullscreen"];
rt = P.addInhibitor(rt, "presentation-mode");
rt = P.removeInhibitor(rt, "presentation-mode");
assert.deepStrictEqual(rt, ["manual", "fullscreen"], "add then remove restores list");
// hasInhibitor on non-array / absent id.
assert.strictEqual(P.hasInhibitor(null, "x"), false);
assert.strictEqual(P.hasInhibitor(undefined, "x"), false);
assert.strictEqual(P.hasInhibitor(["a"], "z"), false);

// ─── applyPresentationMode non-array + position of appended id ──────
assert.deepStrictEqual(P.applyPresentationMode(null, true), ["presentation-mode"]);
assert.deepStrictEqual(P.applyPresentationMode(undefined, false), []);
// Appends at the END, preserving existing order.
assert.deepStrictEqual(P.applyPresentationMode(["x", "y"], true), ["x", "y", "presentation-mode"]);

// ─── clampAutoDisableMinutes boundaries ─────────────────────────────
assert.strictEqual(P.clampAutoDisableMinutes(1440), 1440, "exactly 24h is kept");
assert.strictEqual(P.clampAutoDisableMinutes(1441), 1440, "just over 24h capped");
assert.strictEqual(P.clampAutoDisableMinutes(Infinity), 0, "Infinity -> 0 (not finite)");
assert.strictEqual(P.clampAutoDisableMinutes(-Infinity), 0);
assert.strictEqual(P.clampAutoDisableMinutes("not a number"), 0, "non-numeric string -> 0");
assert.strictEqual(P.clampAutoDisableMinutes(""), 0, "empty string Number('') is 0");
assert.strictEqual(P.clampAutoDisableMinutes(null), 0, "null Number(null) is 0");
assert.strictEqual(P.clampAutoDisableMinutes(undefined), 0, "undefined -> 0");
assert.strictEqual(P.clampAutoDisableMinutes(0.4), 0, "rounds down");
assert.strictEqual(P.clampAutoDisableMinutes(0.5), 1, "rounds up at .5");

// ─── shouldRestorePresentationMode strict-bool only ─────────────────
assert.strictEqual(P.shouldRestorePresentationMode(1), false, "1 is not strict true");
assert.strictEqual(P.shouldRestorePresentationMode(null), false);
assert.strictEqual(P.shouldRestorePresentationMode(0), false);

// ─── shouldSuppressNotifications strict-bool only (no truthy slip) ───
assert.strictEqual(P.shouldSuppressNotifications(1, 1), false, "truthy non-bool does not suppress");
assert.strictEqual(P.shouldSuppressNotifications("true", "true"), false);
assert.strictEqual(P.shouldSuppressNotifications(undefined, undefined), false);

// ─── inhibitorRows: verbatim non-string ids + duplicates ────────────
// Numeric / object ids are stringified for the id field but NEVER given a
// "known" label (they are not one of the well-known constants).
const mixedRows = P.inhibitorRows(["presentation-mode", 42, "manual", "manual"]);
assert.strictEqual(mixedRows.length, 4, "duplicates are NOT collapsed in the viewer");
assert.strictEqual(mixedRows[1].id, "42", "numeric id stringified for display");
assert.strictEqual(mixedRows[1].known, null);
assert.strictEqual(mixedRows[2].known, "manual");
assert.strictEqual(mixedRows[3].known, "manual", "second duplicate still labelled");

// ─── INJECTION SAFETY: reason-like + metachar ids preserved verbatim ─
// Several distinct untrusted ids (some shaped like "app:NAME — REASON")
// must each survive completely unmodified, char-for-char.
const untrusted = [
  "app:firefox — Playing video; rm -rf ~",
  "`reboot`",
  "$(touch /tmp/pwn)",
  "x|y&z>out <in",
  "id with spaces and 'quotes' and \"dquotes\""
];
const utRows = P.inhibitorRows(untrusted);
assert.strictEqual(utRows.length, untrusted.length);
utRows.forEach(function (row, i) {
  assert.strictEqual(row.id, untrusted[i],
    "untrusted id #" + i + " preserved byte-for-byte (no escaping/transform)");
  assert.strictEqual(row.known, null, "untrusted id is never mistaken for a well-known id");
});
// Defence-in-depth: the module exposes ZERO command/argv builders, so no
// untrusted id can ever be folded into a shell command anywhere.
const exportNames = Object.keys(P);
exportNames.forEach(function (name) {
  assert.ok(!/cmd|command|argv|shell|exec|spawn|sh\b/i.test(name),
    "no command-builder export present: " + name);
});

console.log("presentation-policy: all assertions passed");
