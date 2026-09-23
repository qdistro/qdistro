// Pure-JS keyboard shortcut helpers shared between QML and Node tests.
//
// Dual-module: usable from QML via
//   import "ShortcutConflicts.js" as Conflicts
// and from Node via require("../Services/Keyboard/ShortcutConflicts.js").
//
// Responsibilities (all pure, no Qt / no shell):
//   - normalizeCombo:   canonicalise a key-combo string so that combos that
//                       differ only in modifier order or case compare equal.
//   - findConflicts:    given a list of {id, combo} entries, return groups of
//                       ids that share the same canonical combo.
//   - buildExecArgv:    turn an UNTRUSTED user command string into an argv
//                       array that is run WITHOUT a shell (never interpolated
//                       into `sh -c`), so shell metacharacters are inert.
//   - buildCustomShortcut: build the canonical persisted record for a custom
//                       application-command shortcut.
//
// SECURITY: custom shortcut commands are UNTRUSTED user input. They MUST NOT
// be string-interpolated into a shell command line. buildExecArgv returns an
// argv vector executed directly (execvp-style), so a command like
// "firefox; rm -rf ~" becomes a single program name with literal arguments and
// the "; rm -rf ~" can never reach a shell for evaluation.

// Recognised modifier tokens, mapped to a canonical spelling. The KEYS of this
// map (after lowercasing the input token) decide what counts as a modifier;
// the VALUE is the canonical display/compare spelling.
var MODIFIER_CANON = {
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "mod1": "Alt",
    "shift": "Shift",
    "super": "Super",
    "meta": "Super",
    "win": "Super",
    "cmd": "Super",
    "hyper": "Hyper"
};

// Stable ordering for modifiers in the canonical form.
var MODIFIER_ORDER = ["Ctrl", "Alt", "Shift", "Super", "Hyper"];

function _modRank(canonMod) {
    var idx = MODIFIER_ORDER.indexOf(canonMod);
    return idx === -1 ? MODIFIER_ORDER.length : idx;
}

// Normalise a single key-combo string into a canonical form:
//   - split on "+"
//   - trim each token
//   - canonicalise + de-duplicate modifiers, sort them into MODIFIER_ORDER
//   - lowercase the non-modifier key (so "Q" == "q")
//   - rejoin "Mod+Mod+key"
// Returns "" for empty / whitespace-only input.
function normalizeCombo(combo) {
    if (combo === undefined || combo === null)
        return "";
    var raw = String(combo).trim();
    if (raw === "")
        return "";

    var parts = raw.split("+");
    var mods = [];
    var seenMods = {};
    var keys = [];

    for (var i = 0; i < parts.length; i++) {
        var token = parts[i].trim();
        if (token === "")
            continue;
        var lower = token.toLowerCase();
        if (Object.prototype.hasOwnProperty.call(MODIFIER_CANON, lower)) {
            var canon = MODIFIER_CANON[lower];
            if (!seenMods[canon]) {
                seenMods[canon] = true;
                mods.push(canon);
            }
        } else {
            // Non-modifier key. Compare case-insensitively.
            keys.push(lower);
        }
    }

    mods.sort(function (a, b) {
        return _modRank(a) - _modRank(b);
    });

    return mods.concat(keys).join("+");
}

// Detect conflicts across a list of {id, combo} entries.
// Returns an array of groups, where each group is an array of ids that all
// resolve to the same canonical combo (length >= 2). Entries whose combo
// normalises to "" are ignored (an unset combo never conflicts). The returned
// groups preserve the input order of ids; groups are ordered by first
// appearance.
function findConflicts(entries) {
    var buckets = {};
    var order = [];
    if (!entries || entries.length === undefined)
        return [];

    for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        if (!e)
            continue;
        var canon = normalizeCombo(e.combo);
        if (canon === "")
            continue;
        if (!Object.prototype.hasOwnProperty.call(buckets, canon)) {
            buckets[canon] = [];
            order.push(canon);
        }
        buckets[canon].push(e.id);
    }

    var groups = [];
    for (var j = 0; j < order.length; j++) {
        var ids = buckets[order[j]];
        if (ids.length >= 2)
            groups.push(ids);
    }
    return groups;
}

// Convenience: does `combo` collide with any combo in `entries` (excluding the
// entry whose id === selfId)? Returns the conflicting id, or null.
function firstConflictWith(combo, entries, selfId) {
    var canon = normalizeCombo(combo);
    if (canon === "" || !entries || entries.length === undefined)
        return null;
    for (var i = 0; i < entries.length; i++) {
        var e = entries[i];
        if (!e || e.id === selfId)
            continue;
        if (normalizeCombo(e.combo) === canon)
            return e.id;
    }
    return null;
}

// Programs that are themselves shells: if a user makes one of these argv[0]
// and passes -c, the rest of the command line WOULD be evaluated by a shell,
// defeating the no-shell guarantee. We refuse to build such an argv.
var SHELL_PROGRAMS = {
    "sh": true,
    "bash": true,
    "dash": true,
    "zsh": true,
    "ksh": true,
    "fish": true,
    "ash": true,
    "csh": true,
    "tcsh": true,
    "busybox": true
};

// basename of a (possibly path-qualified) program token.
function _basename(prog) {
    var p = String(prog || "");
    var slash = p.lastIndexOf("/");
    return slash === -1 ? p : p.slice(slash + 1);
}

// Tokenise a command string into an argv vector with a minimal POSIX-ish
// parser that honours single and double quotes. Shell metacharacters
// (; | & $ ` > < etc.) are NOT interpreted — they stay literal inside whatever
// token they appear in. This is the raw tokeniser; callers should prefer
// buildExecArgv, which additionally refuses shell-invocation argv vectors.
function tokenizeCommand(command) {
    if (command === undefined || command === null)
        return [];
    var s = String(command);
    var argv = [];
    var cur = "";
    var hasToken = false;
    var i = 0;
    var n = s.length;
    var inSingle = false;
    var inDouble = false;

    while (i < n) {
        var ch = s.charAt(i);
        if (inSingle) {
            if (ch === "'") {
                inSingle = false;
            } else {
                cur += ch;
            }
        } else if (inDouble) {
            if (ch === "\"") {
                inDouble = false;
            } else if (ch === "\\" && i + 1 < n) {
                // In double quotes, backslash only escapes " and \ (POSIX-ish).
                var next = s.charAt(i + 1);
                if (next === "\"" || next === "\\") {
                    cur += next;
                    i++;
                } else {
                    cur += ch;
                }
            } else {
                cur += ch;
            }
        } else if (ch === "'") {
            inSingle = true;
            hasToken = true;
        } else if (ch === "\"") {
            inDouble = true;
            hasToken = true;
        } else if (ch === "\\" && i + 1 < n) {
            cur += s.charAt(i + 1);
            hasToken = true;
            i++;
        } else if (ch === " " || ch === "\t" || ch === "\n" || ch === "\r") {
            if (hasToken) {
                argv.push(cur);
                cur = "";
                hasToken = false;
            }
        } else {
            cur += ch;
            hasToken = true;
        }
        i++;
    }
    if (hasToken)
        argv.push(cur);
    return argv;
}

// Does this argv re-enter a shell that would evaluate a command string? True
// when argv[0]'s basename is a known shell AND a "-c"-style flag is present
// (the flag that makes a shell evaluate its argument as a command line).
function isShellInvocation(argv) {
    if (!argv || argv.length === undefined || argv.length === 0)
        return false;

    var start = 0;
    var prog = _basename(argv[0]).toLowerCase();

    // `env [opts] [VAR=VAL]... PROGRAM [ARGS]` runs PROGRAM directly. env itself
    // is harmless, but `env sh -c ...` would re-enter a shell — so peel the env
    // prefix and re-check the program env actually launches.
    //
    // env's option grammar is fiddly (some options take arguments; -S /
    // --split-string RE-PARSES its argument into a whole command line). Rather
    // than model it fully, we FAIL SAFE: only a small whitelist of harmless
    // no-arg flags and `-u/--unset NAME` (one arg) are tolerated; any other
    // option means we cannot be sure what env will launch, so we treat the
    // command as an unsafe shell invocation.
    if (prog === "env") {
        var ENV_NOARG = {
            "-i": true,
            "--ignore-environment": true,
            "-0": true,
            "--null": true,
            "-v": true,
            "--debug": true
        };
        var k = 1;
        while (k < argv.length) {
            var t = String(argv[k]);
            if (t.length > 0 && t.charAt(0) === "-") {
                if (t === "-u" || t === "--unset" || t.indexOf("--unset=") === 0) {
                    // Consumes a following NAME unless given as --unset=NAME.
                    if (t === "-u" || t === "--unset")
                        k++;
                    k++;
                    continue;
                }
                if (ENV_NOARG[t]) {
                    k++;
                    continue;
                }
                // Unknown / argument-bearing / command-synthesising option
                // (e.g. -S, --split-string, --chdir, -C): cannot reason about
                // it safely — refuse.
                return true;
            }
            if (t.indexOf("=") !== -1) {
                // NAME=VALUE assignment.
                k++;
                continue;
            }
            break; // first non-option, non-assignment token is the program.
        }
        if (k >= argv.length)
            return false; // env with no program to launch.
        start = k;
        prog = _basename(argv[start]).toLowerCase();
    }

    if (!SHELL_PROGRAMS[prog])
        return false;
    for (var i = start + 1; i < argv.length; i++) {
        var a = String(argv[i]);
        // "-c", or a bundled short-flag set containing c (e.g. "-lc", "-xc").
        if (a === "-c")
            return true;
        if (a.length >= 2 && a.charAt(0) === "-" && a.charAt(1) !== "-" && a.indexOf("c") !== -1)
            return true;
    }
    return false;
}

// Build an argv vector from an UNTRUSTED command string WITHOUT invoking a
// shell. The command is tokenised (quotes honoured) and the resulting argv is
// meant to be spawned directly (e.g. Quickshell Process with `command: argv`),
// never `sh -c`. Shell metacharacters in the input stay literal.
//
// SECURITY: if the resulting argv would itself re-enter a shell (e.g. the user
// typed `sh -c 'rm -rf ~'`), we REFUSE and return [] — building it would hand
// an untrusted command line to a real shell, defeating the whole guarantee.
//
// Returns [] for an empty / whitespace-only command or a shell invocation.
function buildExecArgv(command) {
    var argv = tokenizeCommand(command);
    if (isShellInvocation(argv))
        return [];
    return argv;
}

// Build the canonical persisted record for a custom application-command
// shortcut. `combo` and `command` are UNTRUSTED. We store the raw command (so
// the user can edit it back) plus the normalised combo for fast conflict
// comparison; consumers MUST run the command via buildExecArgv, never a shell
// string. Returns null if either combo or command is empty after trimming.
function buildCustomShortcut(combo, command, name) {
    var normCombo = normalizeCombo(combo);
    var cmd = command === undefined || command === null ? "" : String(command).trim();
    if (normCombo === "" || cmd === "")
        return null;
    // Refuse commands that would re-enter a shell (e.g. `sh -c '...'`): such a
    // record could never be run safely via buildExecArgv anyway.
    if (isShellInvocation(tokenizeCommand(cmd)))
        return null;
    var displayName = name === undefined || name === null ? "" : String(name).trim();
    if (displayName === "")
        displayName = cmd;
    return {
        "combo": normCombo,
        "command": cmd,
        "name": displayName
    };
}

// Dual export: CommonsJS (Node tests) when `module` exists; otherwise the
// functions are simply visible to QML's `import ... as Conflicts`.
if (typeof module !== "undefined" && module.exports) {
    module.exports = {
        normalizeCombo: normalizeCombo,
        findConflicts: findConflicts,
        firstConflictWith: firstConflictWith,
        tokenizeCommand: tokenizeCommand,
        isShellInvocation: isShellInvocation,
        buildExecArgv: buildExecArgv,
        buildCustomShortcut: buildCustomShortcut,
        MODIFIER_ORDER: MODIFIER_ORDER
    };
}
