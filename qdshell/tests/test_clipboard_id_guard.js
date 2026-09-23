// Source-text regression guard for the cliphist numeric-id contract
// (fable/qdshell-cliphist-id-guard.md).
//
// ClipboardService.qml interpolates a cliphist entry id into several `sh -c`
// command strings (copy / paste / decode-to-data-url / delete). Those ids must
// ALWAYS pass the `ClipboardActions.validId` gate (`/^\d+$/`) first, so a
// non-numeric id can never inject shell syntax. decodeAuthoritative + _purgeId
// already gated; this run extended the gate to the remaining four sites and
// routed all of them through the single shared helper.
//
// This test is intentionally source-text based (no QML runtime needed): it
// reads ClipboardService.qml and asserts that
//   (1) the single id-validation helper exists in ClipboardActions.js and is
//       exported, and
//   (2) every line that interpolates a `${...}` id-bearing expression into a
//       cliphist `sh -c` command uses the guarded `safeId` / `idStr` binding
//       (the value returned by validId) — never the raw `${id}` / `${job.id}`.
// It fails loudly if a future refactor reintroduces an unguarded interpolation.

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const SVC = path.resolve(
    __dirname, "..", "Services", "Keyboard", "ClipboardService.qml");
const ACTIONS = path.resolve(
    __dirname, "..", "Services", "Keyboard", "ClipboardActions.js");

const svc = fs.readFileSync(SVC, "utf8");
const actions = fs.readFileSync(ACTIONS, "utf8");

// --- (1) the shared helper exists and is exported -------------------------
assert.ok(/function\s+validId\s*\(/.test(actions),
    "ClipboardActions.js must define validId()");
assert.ok(/\/\^\\d\+\$\/\.test\(/.test(actions),
    "validId must gate on the /^\\d+$/ numeric pattern");
assert.ok(/validId\s*:\s*validId/.test(actions),
    "validId must be exported from ClipboardActions.js");

// Behavioural sanity: load it and confirm the gate actually rejects an
// injection-shaped id and accepts a bare integer.
const CA = require("../Services/Keyboard/ClipboardActions.js");
assert.strictEqual(CA.validId("1; rm -rf ~"), null);
assert.strictEqual(CA.validId("123"), "123");

// --- (2) no unguarded id interpolation into a cliphist `sh -c` line -------
// Find every line that (a) is an `sh -c` command containing `cliphist` and
// (b) interpolates a `${...}` expression. The interpolated id token MUST be a
// validId-derived binding, i.e. `safeId` or `idStr`. Raw `${id}` / `${job.id}`
// (or any other un-validated binding) is forbidden.
const lines = svc.split("\n");
const ID_TOKENS = /\$\{\s*([A-Za-z_$][\w$.]*)\s*\}/g;
// Bindings that are ONLY ever assigned the result of ClipboardActions.validId.
const SAFE_BINDINGS = new Set(["safeId", "idStr"]);
// Interpolated tokens that are not ids and are safe by construction (the mime
// type / paste-key fragments are fixed constants — see ClipboardService.qml
// list parser allowlist and the wtype literals).
const NON_ID_TOKENS = new Set(["typeArg", "pasteKeys"]);

const offenders = [];
lines.forEach((line, i) => {
    if (!/["'`]sh["'`]\s*,\s*["'`]-c["'`]/.test(line) &&
        !/`[^`]*cliphist[^`]*\$\{/.test(line)) {
        // Only inspect lines that build an sh -c cliphist command string.
        if (!/cliphist/.test(line) || !/\$\{/.test(line)) return;
    }
    if (!/cliphist/.test(line) && !/cmd\s*=/.test(line)) return;
    if (!/\$\{/.test(line)) return;

    let m;
    ID_TOKENS.lastIndex = 0;
    while ((m = ID_TOKENS.exec(line)) !== null) {
        const tok = m[1];
        if (NON_ID_TOKENS.has(tok)) continue;
        if (SAFE_BINDINGS.has(tok)) continue;
        offenders.push(`L${i + 1}: unguarded id token \${${tok}} -> ${line.trim()}`);
    }
});

assert.deepStrictEqual(
    offenders, [],
    "cliphist id interpolated into `sh -c` without the validId guard:\n  " +
        offenders.join("\n  "));

// Belt-and-braces: the literal raw tokens we replaced must be gone from the
// command-string interpolations.
assert.ok(!/cliphist decode \$\{id\}/.test(svc),
    "`cliphist decode ${id}` must be replaced by the guarded ${safeId}");
assert.ok(!/cliphist decode \$\{job\.id\}/.test(svc),
    "`cliphist decode ${job.id}` must be replaced by the guarded ${safeId}");

console.log("clipboard-id-guard: all assertions passed");
