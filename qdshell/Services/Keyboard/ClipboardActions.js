// Pure, side-effect-free clipboard-parity algorithms shared by QML and Node.
//
// This module holds ONLY pure logic extracted from ClipboardService.qml: no
// Process/FileView/Settings/Logger access. The QML singleton imports it via
//   import "ClipboardActions.js" as ClipboardActions
// and calls these functions, so the history-ordering / retention / privacy /
// regex-action / injection-payload behaviour lives in one place that can be
// unit-tested headlessly under Node.
//
// SECURITY NOTE: clipboard text passed to these functions is UNTRUSTED. It is
// only ever used as a length-capped RegExp subject or as an environment-
// variable VALUE / stdin payload — NEVER concatenated into a shell command
// string. See buildActionExecution() for the injection-safe contract.

// Upper bound on how much UNTRUSTED clipboard text we feed into a user RegExp.
// A locally-configured catastrophic-backtracking pattern run against megabytes
// of hostile clipboard content could otherwise freeze the QML main thread;
// capping the subject bounds the worst case.
var REGEX_SUBJECT_CAP = 16384;

// A cliphist entry id is always a bare non-negative integer. Every code path
// that interpolates an id into an `sh -c` string (copy/paste/decode/delete)
// MUST gate on this first, so a non-numeric id can never inject shell syntax.
// Ids currently originate from cliphist's own `list` output (parsed with a
// `^(\d+)` regex), so this is defence-in-depth rather than a live hole — but
// the guard makes that property local to each call site instead of relying on
// a far-away parser invariant. Returns the canonical trimmed id string when
// valid, or null when the input is not a bare integer.
function validId(id) {
    var s = String(id === undefined || id === null ? "" : id).trim();
    return /^\d+$/.test(s) ? s : null;
}

// Cap untrusted text before it is used as a RegExp subject.
function regexSubject(text, cap) {
    var limit = (cap === undefined || cap === null) ? REGEX_SUBJECT_CAP : cap;
    // Mirrors the QML's String(text || "") coercion: falsy values (0, false,
    // "", null, undefined, NaN) all become the empty string.
    var s = String(text || "");
    return s.length > limit ? s.slice(0, limit) : s;
}

// Compile a user-authored pattern into a RegExp, or null if unset/invalid.
// The pattern is a trusted local setting; the subject it is tested against is
// untrusted but only ever a RegExp.test() input. Invalid patterns fail safe to
// null so the caller treats "no usable pattern" as "no match / ignore".
function compileRegex(pattern) {
    var pat = pattern || "";
    if (pat.length === 0)
        return null;
    try {
        return new RegExp(pat);
    } catch (e) {
        return null;
    }
}

// Does an entry's preview match the user's ignore pattern? Returns true only
// when a valid pattern is present AND it matches the (length-capped) subject.
// An empty or invalid pattern → false (do not ignore). Never throws.
function ignoreMatches(pattern, preview, cap) {
    var re = compileRegex(pattern);
    if (re === null)
        return false;
    try {
        return re.test(regexSubject(preview, cap));
    } catch (e) {
        return false;
    }
}

// Partition entries by the ignore pattern. Returns { kept, purged } where
// purged holds the ids whose preview matched the pattern (caller deletes them
// from the backing store) and kept holds the surviving entries in order. When
// the pattern is empty/invalid nothing is purged. Entries must be objects with
// at least { id, preview }; isImage entries are matched by their preview too,
// mirroring the QML which runs the ignore filter over all non-... entries.
function applyIgnoreFilter(entries, pattern, cap) {
    var list = entries || [];
    var re = compileRegex(pattern);
    if (re === null)
        return { kept: list.slice(), purged: [] };
    var kept = [];
    var purged = [];
    for (var i = 0; i < list.length; i++) {
        var it = list[i];
        var matched = false;
        try {
            matched = re.test(regexSubject(it.preview, cap));
        } catch (e) {
            matched = false;
        }
        if (matched)
            purged.push(it.id);
        else
            kept.push(it);
    }
    return { kept: kept, purged: purged };
}

// Drop entries older than maxAgeDays based on a first-seen timestamp map
// (id → unix seconds) and a "now" timestamp. Entries with no recorded
// first-seen time are KEPT (we cannot date them, so we never expire history we
// can't date). Boundary: an entry is expired only when seen < cutoff, i.e. an
// entry exactly maxAgeDays old (seen === cutoff) is KEPT.
// maxAgeDays <= 0 disables expiry. Returns { kept, purged }.
function applyAgeExpiry(entries, firstSeenById, maxAgeDays, now) {
    var list = entries || [];
    var days = Number(maxAgeDays) || 0;
    if (days <= 0)
        return { kept: list.slice(), purged: [] };
    var seenMap = firstSeenById || {};
    var cutoff = now - days * 86400;
    var kept = [];
    var purged = [];
    for (var i = 0; i < list.length; i++) {
        var it = list[i];
        var seen = seenMap[it.id];
        if (seen !== undefined && seen < cutoff)
            purged.push(it.id);
        else
            kept.push(it);
    }
    return { kept: kept, purged: purged };
}

// Order entries for display. cliphist lists most-recent-first natively, so the
// "recent" ordering preserves the input order. "most-used" sorts by this
// session's usage counts (descending), with a STABLE tie-break that preserves
// the incoming cliphist recency order. Any other ordering value behaves like
// "recent". Returns a NEW array; does not mutate the input.
function orderEntries(entries, ordering, usageCountById) {
    var list = entries || [];
    if (ordering !== "most-used")
        return list.slice();
    var usage = usageCountById || {};
    var idx = {};
    for (var i = 0; i < list.length; i++)
        idx[list[i].id] = i;
    return list.slice().sort(function (a, b) {
        var ua = usage[a.id] || 0;
        var ub = usage[b.id] || 0;
        if (ua !== ub)
            return ub - ua;
        // Stable tie-break: preserve cliphist recency order.
        return idx[a.id] - idx[b.id];
    });
}

// Cap the list to maxEntries (after ordering). Returns { kept, purged } where
// purged holds the ids of the trimmed-off overflow (caller deletes them so the
// backing store stays bounded). maxEntries <= 0 disables the cap.
function trimToMax(entries, maxEntries) {
    var list = entries || [];
    var max = Number(maxEntries) || 0;
    if (max <= 0 || list.length <= max)
        return { kept: list.slice(), purged: [] };
    var kept = list.slice(0, max);
    var overflow = list.slice(max);
    var purged = [];
    for (var i = 0; i < overflow.length; i++)
        purged.push(overflow[i].id);
    return { kept: kept, purged: purged };
}

// Return the action rules whose regex matches the given text, each annotated
// with its first capture group. Each input rule is { name, regexPattern,
// command }. The text is UNTRUSTED — only ever a length-capped RegExp subject.
// An empty pattern matches everything (XFCE clipman parity: blank regex =
// "always"); an invalid pattern skips the rule. Rules without a command are
// skipped. Returns an array of { rule, group1 } for the matching rules in
// input order.
function matchingActions(actions, text, cap) {
    var out = [];
    var rules = actions || [];
    var subject = regexSubject(text, cap);
    for (var i = 0; i < rules.length; i++) {
        var a = rules[i];
        if (!a || !a.command)
            continue;
        var pat = a.regexPattern || "";
        var re = null;
        if (pat.length > 0) {
            try {
                re = new RegExp(pat);
            } catch (e) {
                continue; // skip rules with invalid patterns
            }
        }
        // Empty pattern → always matches; otherwise require a match.
        var group1 = "";
        var matched = (re === null);
        if (re !== null) {
            var m = null;
            try {
                m = subject.match(re);
            } catch (e) {
                m = null;
            }
            matched = !!m;
            if (m && m.length > 1 && m[1] !== undefined)
                group1 = String(m[1]);
        }
        if (matched)
            out.push({ rule: a, group1: group1 });
    }
    return out;
}

// Re-validate a single rule against the (decoded, possibly full) text and
// return the first capture group, or null when the rule must NOT fire (no
// command, invalid pattern, or pattern does not match the text). An empty
// pattern always validates with group1 = "". This mirrors runActionRule's
// re-validation gate so a preview-vs-full-content mismatch can never cause an
// action to fire on text its pattern doesn't match.
function revalidateActionGroup(rule, text, cap) {
    if (!rule || !rule.command)
        return null;
    var subject = regexSubject(text, cap);
    var pat = rule.regexPattern || "";
    if (pat.length === 0)
        return "";
    var re = null;
    try {
        re = new RegExp(pat);
    } catch (e) {
        return null; // invalid pattern → do not run
    }
    var m = null;
    try {
        m = subject.match(re);
    } catch (e) {
        return null;
    }
    if (!m)
        return null; // decoded text must actually match
    if (m.length > 1 && m[1] !== undefined)
        return String(m[1]);
    return "";
}

// Build the INJECTION-SAFE execution payload for a regex action.
//
// SECURITY INVARIANT: clipboard content (full text) and the regex capture
// group are attacker-controlled. They are NEVER interpolated into the `sh -c`
// command line. The ONLY shell-parsed template is the user-authored
// `rule.command`. The untrusted text is carried solely as:
//   - environment VALUES: QD_CLIP (full text) and QD_CLIP_1 (first group), set
//     via Process.environment, never concatenated into the script; and
//   - the stdin payload, fed through `printf '%s' "$QD_CLIP"` so even stdin is
//     not interpolated.
// A payload like `; rm -rf ~` therefore lands as the VALUE of $QD_CLIP, not as
// a new command.
//
// Returns { command, argv, environment, env, stdinSource } where:
//   - command/argv: the argv passed to Process. The text DOES NOT appear here.
//   - environment: the QML Process.environment array form ["K=V", ...].
//   - env: the same as a plain object for easy assertion.
//   - stdinSource: documents that the full text reaches the command via the
//     $QD_CLIP env var read by printf (never via the command string).
function buildActionExecution(rule, text, group1) {
    // Mirrors the QML's String(text || "") env coercion exactly.
    var fullText = String(text || "");
    var g1 = String(group1 || "");
    // The user command is the ONLY shell template. printf reads $QD_CLIP so the
    // stdin payload is not interpolated either.
    var wrapper = "printf '%s' \"$QD_CLIP\" | { " + String(rule.command) + " ; }";
    var argv = ["sh", "-c", wrapper];
    return {
        command: argv,
        argv: argv,
        environment: ["QD_CLIP=" + fullText, "QD_CLIP_1=" + g1],
        env: { QD_CLIP: fullText, QD_CLIP_1: g1 },
        stdinSource: "$QD_CLIP"
    };
}

if (typeof module !== "undefined") {
    module.exports = {
        REGEX_SUBJECT_CAP: REGEX_SUBJECT_CAP,
        validId: validId,
        regexSubject: regexSubject,
        compileRegex: compileRegex,
        ignoreMatches: ignoreMatches,
        applyIgnoreFilter: applyIgnoreFilter,
        applyAgeExpiry: applyAgeExpiry,
        orderEntries: orderEntries,
        trimToMax: trimToMax,
        matchingActions: matchingActions,
        revalidateActionGroup: revalidateActionGroup,
        buildActionExecution: buildActionExecution
    };
}
