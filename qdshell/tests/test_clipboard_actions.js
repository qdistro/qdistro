const assert = require("assert");
const ClipboardActions = require("../Services/Keyboard/ClipboardActions.js");

// Helper to build a minimal entry.
function entry(id, preview, isImage) {
    return { id: String(id), preview: preview || "", isImage: !!isImage };
}

// ---------------------------------------------------------------------------
// Ordering: recent (input order preserved) vs most-used (usage desc, stable).
// ---------------------------------------------------------------------------
{
    // cliphist lists most-recent-first; "recent" must preserve that order.
    const entries = [entry(3, "c"), entry(2, "b"), entry(1, "a")];
    const recent = ClipboardActions.orderEntries(entries, "recent", {});
    assert.deepStrictEqual(recent.map(e => e.id), ["3", "2", "1"]);

    // Unknown ordering behaves like recent.
    const dflt = ClipboardActions.orderEntries(entries, "something-else", {});
    assert.deepStrictEqual(dflt.map(e => e.id), ["3", "2", "1"]);

    // most-used: usage count descending; ties preserve recency order.
    const usage = { "1": 5, "2": 5, "3": 1 };
    const mostUsed = ClipboardActions.orderEntries(entries, "most-used", usage);
    // id 1 and id 2 both have count 5; recency order has 2 before 1, so the
    // stable tie-break keeps 2 ahead of 1. id 3 (count 1) trails.
    assert.deepStrictEqual(mostUsed.map(e => e.id), ["2", "1", "3"]);

    // ordering must not mutate the input array.
    assert.deepStrictEqual(entries.map(e => e.id), ["3", "2", "1"]);
}

// ---------------------------------------------------------------------------
// Trim: cap to N entries (after ordering), reporting overflow ids as purged.
// ---------------------------------------------------------------------------
{
    const entries = [entry(5), entry(4), entry(3), entry(2), entry(1)];
    const res = ClipboardActions.trimToMax(entries, 3);
    assert.deepStrictEqual(res.kept.map(e => e.id), ["5", "4", "3"]);
    assert.deepStrictEqual(res.purged, ["2", "1"]);

    // No cap when maxEntries <= 0.
    const none = ClipboardActions.trimToMax(entries, 0);
    assert.deepStrictEqual(none.kept.map(e => e.id), ["5", "4", "3", "2", "1"]);
    assert.deepStrictEqual(none.purged, []);

    // List shorter than cap: nothing purged.
    const under = ClipboardActions.trimToMax([entry(1), entry(2)], 5);
    assert.deepStrictEqual(under.kept.map(e => e.id), ["1", "2"]);
    assert.deepStrictEqual(under.purged, []);
}

// ---------------------------------------------------------------------------
// Age expiry: drop entries older than max-age; keep undated + newer; boundary.
// ---------------------------------------------------------------------------
{
    const now = 1000000; // arbitrary "now" in unix seconds
    const maxAgeDays = 1; // cutoff = now - 86400
    const cutoff = now - 86400;
    const firstSeen = {
        "old": cutoff - 1, // strictly older than cutoff -> expired
        "boundary": cutoff, // exactly at cutoff -> KEPT (seen < cutoff is false)
        "new": cutoff + 1 // newer than cutoff -> kept
        // "undated" intentionally absent -> kept (can't date it)
    };
    const entries = [entry("new"), entry("boundary"), entry("undated"), entry("old")];
    const res = ClipboardActions.applyAgeExpiry(entries, firstSeen, maxAgeDays, now);
    assert.deepStrictEqual(res.kept.map(e => e.id), ["new", "boundary", "undated"]);
    assert.deepStrictEqual(res.purged, ["old"]);

    // maxAgeDays <= 0 disables expiry: everything kept, even ancient entries.
    const disabled = ClipboardActions.applyAgeExpiry(
        entries, { "old": 0 }, 0, now);
    assert.deepStrictEqual(disabled.kept.map(e => e.id),
        ["new", "boundary", "undated", "old"]);
    assert.deepStrictEqual(disabled.purged, []);
}

// ---------------------------------------------------------------------------
// Ignore-pattern matching: matches, non-matches, invalid-regex safe fallback.
// ---------------------------------------------------------------------------
{
    // Direct predicate.
    assert.strictEqual(ClipboardActions.ignoreMatches("secret", "my secret token"), true);
    assert.strictEqual(ClipboardActions.ignoreMatches("secret", "nothing here"), false);
    // Empty pattern -> never ignore.
    assert.strictEqual(ClipboardActions.ignoreMatches("", "anything"), false);
    // Invalid regex -> safe fallback to false (do not ignore / do not throw).
    assert.strictEqual(ClipboardActions.ignoreMatches("(", "anything"), false);

    // Partition filter: matching ids purged, others kept in order.
    const entries = [
        entry("1", "password=hunter2"),
        entry("2", "hello world"),
        entry("3", "api password again")
    ];
    const filtered = ClipboardActions.applyIgnoreFilter(entries, "password");
    assert.deepStrictEqual(filtered.kept.map(e => e.id), ["2"]);
    assert.deepStrictEqual(filtered.purged, ["1", "3"]);

    // Empty pattern -> keep all, purge none.
    const noPat = ClipboardActions.applyIgnoreFilter(entries, "");
    assert.deepStrictEqual(noPat.kept.map(e => e.id), ["1", "2", "3"]);
    assert.deepStrictEqual(noPat.purged, []);

    // Invalid pattern -> keep all (safe fallback), nothing purged.
    const bad = ClipboardActions.applyIgnoreFilter(entries, "[");
    assert.deepStrictEqual(bad.kept.map(e => e.id), ["1", "2", "3"]);
    assert.deepStrictEqual(bad.purged, []);
}

// ---------------------------------------------------------------------------
// Regex-action matching: correct actions + capture groups; non-matching text.
// ---------------------------------------------------------------------------
{
    const actions = [
        { name: "url", regexPattern: "https?://(\\S+)", command: "open-url" },
        { name: "always", regexPattern: "", command: "always-cmd" },
        { name: "phone", regexPattern: "(\\d{3}-\\d{4})", command: "dial" },
        { name: "no-command", regexPattern: ".*", command: "" }, // skipped: no command
        { name: "bad-regex", regexPattern: "(", command: "boom" } // skipped: invalid
    ];

    // Matching text: a URL. "url" matches (group = host/path), "always" matches
    // (empty pattern), "phone" does not. no-command + bad-regex skipped.
    const urlMatches = ClipboardActions.matchingActions(actions, "visit https://example.com/x");
    assert.deepStrictEqual(urlMatches.map(m => m.rule.name), ["url", "always"]);
    assert.strictEqual(urlMatches[0].group1, "example.com/x");
    assert.strictEqual(urlMatches[1].group1, ""); // empty pattern -> no group

    // Phone text: "always" + "phone" match; phone captures the number.
    const phoneMatches = ClipboardActions.matchingActions(actions, "call 555-1234 now");
    assert.deepStrictEqual(phoneMatches.map(m => m.rule.name), ["always", "phone"]);
    const phoneRule = phoneMatches.find(m => m.rule.name === "phone");
    assert.strictEqual(phoneRule.group1, "555-1234");

    // Non-matching text (no url, no phone): only the always-rule fires.
    const plain = ClipboardActions.matchingActions(actions, "just some plain text");
    assert.deepStrictEqual(plain.map(m => m.rule.name), ["always"]);

    // revalidateActionGroup mirrors the same gate.
    assert.strictEqual(
        ClipboardActions.revalidateActionGroup(actions[0], "https://host/p"), "host/p");
    assert.strictEqual(
        ClipboardActions.revalidateActionGroup(actions[0], "no url here"), null);
    assert.strictEqual(
        ClipboardActions.revalidateActionGroup(actions[1], "anything"), ""); // empty pat
    assert.strictEqual(
        ClipboardActions.revalidateActionGroup(actions[4], "x"), null); // invalid regex
    assert.strictEqual(
        ClipboardActions.revalidateActionGroup({ command: "" }, "x"), null); // no command
}

// ---------------------------------------------------------------------------
// INJECTION SAFETY (the most important assertion):
// hostile clipboard text must be carried ONLY as a literal env value / argv
// element and must NEVER leak any shell metacharacter into a command-string
// field.
// ---------------------------------------------------------------------------
{
    const HOSTILE = "$(touch /tmp/pwn); `id`; rm -rf ~";
    const rule = { name: "evil", regexPattern: "(.*)", command: "cat - > /tmp/out" };

    // Re-validate to obtain the capture group exactly as the service does, then
    // build the execution payload from the (uncapped) hostile text.
    const group1 = ClipboardActions.revalidateActionGroup(rule, HOSTILE);
    assert.notStrictEqual(group1, null); // rule matches
    const exec = ClipboardActions.buildActionExecution(rule, HOSTILE, group1);

    // 1. The full hostile text is present VERBATIM as the QD_CLIP env value.
    assert.strictEqual(exec.env.QD_CLIP, HOSTILE);
    // The capture group also carries the hostile text verbatim (pattern is .*).
    assert.strictEqual(exec.env.QD_CLIP_1, HOSTILE);
    // Environment array form likewise carries it as a literal "KEY=VALUE".
    assert.ok(exec.environment.indexOf("QD_CLIP=" + HOSTILE) !== -1);
    assert.ok(exec.environment.indexOf("QD_CLIP_1=" + HOSTILE) !== -1);

    // 2. The command string (the only thing the shell parses) is built ONLY
    //    from the user-authored rule.command + the fixed printf wrapper. The
    //    hostile clipboard text must NOT appear anywhere in it.
    const commandString = exec.argv[2]; // ["sh", "-c", <commandString>]
    assert.strictEqual(exec.argv[0], "sh");
    assert.strictEqual(exec.argv[1], "-c");
    // The fixed wrapper references the env var, never the literal text.
    assert.strictEqual(
        commandString,
        "printf '%s' \"$QD_CLIP\" | { cat - > /tmp/out ; }");
    // Hostile text (and each of its dangerous fragments) is absent from the
    // command string: the only way it reaches the command is via $QD_CLIP.
    assert.ok(commandString.indexOf(HOSTILE) === -1,
        "hostile text must not be concatenated into the command string");
    ["$(touch", "/tmp/pwn", "`id`", "rm -rf ~", "rm -rf"].forEach(function (frag) {
        assert.ok(commandString.indexOf(frag) === -1,
            "shell-dangerous fragment leaked into command string: " + frag);
    });

    // 3. No argv element other than the rule's own command template carries the
    //    hostile text. (argv[2] is asserted above; argv[0]/[1] are constants.)
    exec.argv.forEach(function (a) {
        assert.ok(a.indexOf(HOSTILE) === -1,
            "hostile text leaked into an argv element: " + a);
    });

    // 4. The stdin source is the env var, proving the text is fed via env+stdin
    //    rather than interpolation.
    assert.strictEqual(exec.stdinSource, "$QD_CLIP");

    // 5. Even with a command template that itself contains metacharacters, the
    //    hostile clipboard text still never enters the command string.
    const rule2 = { name: "pipe", regexPattern: "", command: "grep foo | wc -l" };
    const exec2 = ClipboardActions.buildActionExecution(rule2, HOSTILE, "");
    assert.strictEqual(exec2.env.QD_CLIP, HOSTILE);
    assert.ok(exec2.argv[2].indexOf(HOSTILE) === -1);
    assert.ok(exec2.argv[2].indexOf("/tmp/pwn") === -1);
}

// ---------------------------------------------------------------------------
// Long-input bound: untrusted text used as a RegExp subject is capped to the
// service's cap (16 KiB) before matching. The execution PAYLOAD still carries
// the FULL text (env+stdin handle arbitrary length safely).
// ---------------------------------------------------------------------------
{
    assert.strictEqual(ClipboardActions.REGEX_SUBJECT_CAP, 16384);

    const longText = "A".repeat(20000); // > 16 KiB
    const subject = ClipboardActions.regexSubject(longText);
    assert.strictEqual(subject.length, 16384);

    // A pattern that can only match beyond the cap must NOT match the capped
    // subject (proving the cap is actually applied during matching).
    const beyondCapRule = { name: "tail", regexPattern: "A{20000}", command: "c" };
    const m = ClipboardActions.matchingActions([beyondCapRule], longText);
    assert.deepStrictEqual(m, []); // 20000 A's can't be found in 16384 chars

    // A pattern within the cap still matches.
    const withinCapRule = { name: "head", regexPattern: "A{16000}", command: "c" };
    const m2 = ClipboardActions.matchingActions([withinCapRule], longText);
    assert.deepStrictEqual(m2.map(x => x.rule.name), ["head"]);

    // The execution payload carries the FULL untrusted text (not the capped
    // subject) via the env var, so the command sees complete content.
    const exec = ClipboardActions.buildActionExecution(
        { command: "cat" }, longText, "");
    assert.strictEqual(exec.env.QD_CLIP.length, 20000);
    // ...and still never in the command string.
    assert.ok(exec.argv[2].indexOf("A".repeat(100)) === -1);
}

// ---------------------------------------------------------------------------
// Numeric-id guard: validId gates every id that is interpolated into an
// `sh -c` cliphist command (copy/paste/decode/delete). Only a bare
// non-negative integer is accepted (surrounding whitespace is trimmed off);
// anything carrying shell metacharacters, interior whitespace, signs, or
// non-digits is rejected (-> null).
// ---------------------------------------------------------------------------
{
    // Valid: bare integers (and surrounding whitespace is trimmed off).
    assert.strictEqual(ClipboardActions.validId("0"), "0");
    assert.strictEqual(ClipboardActions.validId("42"), "42");
    assert.strictEqual(ClipboardActions.validId(42), "42"); // number coerces
    assert.strictEqual(ClipboardActions.validId("  7 "), "7"); // trimmed
    assert.strictEqual(ClipboardActions.validId("007"), "007"); // leading zeros ok

    // Invalid: every form that could carry shell syntax or is non-numeric.
    const bad = [
        "", "   ", "1a", "a1", "-1", "+1", "1.0", "1 2", "1;rm -rf ~",
        "1 | wl-copy", "$(id)", "`id`", "1\n2", "1\t2", "0x10", "1e3",
        "  ", "abc", null, undefined, {}, [], "12 34",
        "1; touch /tmp/pwn", "1 && reboot"
    ];
    bad.forEach(function (v) {
        assert.strictEqual(
            ClipboardActions.validId(v), null,
            "validId must reject non-numeric/unsafe id: " + JSON.stringify(v));
    });

    // The accepted value contains ONLY digits, so nothing it returns can ever
    // introduce a shell metacharacter when interpolated into `cliphist decode
    // <id>` / `echo <id> | cliphist delete`.
    ["0", "1", "999999999"].forEach(function (v) {
        const out = ClipboardActions.validId(v);
        assert.ok(out !== null && /^\d+$/.test(out),
            "accepted id must be pure digits: " + v);
    });
}

console.log("clipboard-actions: all assertions passed");
