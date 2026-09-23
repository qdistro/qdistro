// Regression guard for the qdwin-only compositor cleanup
// (issues/qdshell/qdwin-only-compositor-cleanup.md).
//
// qdshell supports EXACTLY one compositor: qdwin. It is a fork of Noctalia,
// which supported many compositors, so foreign-compositor scaffolding kept
// creeping back in during refactors. This test fails loudly if any of it
// returns:
//
//   * live dispatch to a foreign-WM IPC binary (swaymsg / hyprctl / wlopm /
//     wlr-randr / swayidle) — qdwin exposes everything via qdwin_shell_v1, so
//     there is never a code path that shells out to these.
//   * dead compositor-IDENTITY flags (Qdwin.isHyprland / isNiri / isSway /
//     isMango / isLabwc / isScroll) — only `isQdwin` is a real identity.
//   * orphaned helper scripts / foreign-WM config templates that the cleanup
//     removed (labwc-workspace-helper.py; ~/.config/{sway,niri} writers).
//
// It is intentionally source-text based (no runtime needed): it walks the QML
// + JS + shell + python tree, strips comments, and asserts the forbidden
// tokens never appear in CODE. Comments that merely document the removal (e.g.
// "the previous swaymsg builder was removed") are allowed — that is the point
// of stripping comments first. Pure Node test, no deps.

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..");

// Directories we never scan: tests assert about the forbidden tokens on
// purpose; vendored wayland is third-party; .git is history.
const SKIP_DIRS = new Set([".git", "tests", "Tests", "vendor", "node_modules"]);

function walk(dir) {
    let out = [];
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        if (e.isDirectory()) {
            if (SKIP_DIRS.has(e.name)) continue;
            out = out.concat(walk(path.join(dir, e.name)));
        } else {
            out.push(path.join(dir, e.name));
        }
    }
    return out;
}

// Strip line comments so prose that documents the removal does not trip the
// guard. We keep it deliberately simple: `//` for qml/js, `#` for sh/py. This
// over-strips inside string literals containing those tokens, which is fine —
// no real foreign-WM dispatch lives only inside such a string.
function stripComments(text, ext) {
    return text
        .split("\n")
        .map((line) => {
            if (ext === ".qml" || ext === ".js") {
                const i = line.indexOf("//");
                return i >= 0 ? line.slice(0, i) : line;
            }
            // .sh / .py
            const h = line.indexOf("#");
            return h >= 0 ? line.slice(0, h) : line;
        })
        .join("\n")
        // Block comments (/* ... */) used by qml/js.
        .replace(/\/\*[\s\S]*?\*\//g, "");
}

const SCAN_EXT = new Set([".qml", ".js", ".sh", ".py"]);
const files = walk(REPO).filter((f) => SCAN_EXT.has(path.extname(f)));

assert.ok(files.length > 50, "guard should scan a meaningful slice of the tree");

// ─── 1. No live foreign-WM IPC dispatch ──────────────────────────────────
// Word-boundary match so we don't flag e.g. "swayidle" inside an unrelated
// identifier; these are real binary names invoked via argv.
const DISPATCH = [/\bswaymsg\b/, /\bhyprctl\b/, /\bwlopm\b/, /\bwlr-randr\b/, /\bswayidle\b/];

// ─── 2. No dead compositor-identity flags ────────────────────────────────
// `isQdwin` is the only legit identity and is NOT in this list.
const DEAD_FLAGS = [
    /\bisHyprland\b/,
    /\bisNiri\b/,
    /\bisSway\b/,
    /\bisMango\b/,
    /\bisLabwc\b/,
    /\bisScroll\b/,
];

// ─── 3. No foreign-WM config writers ─────────────────────────────────────
// Only COMPOSITOR config dirs are forbidden. Note `~/.config/hypr/` is
// deliberately NOT here: TemplateRegistry's `hyprtoolkit` entry writes a color
// theme for the Hyprtoolkit *widget library* (app theming, same bucket as
// GTK/Qt/btop/yazi), not Hyprland-compositor dispatch — see Bucket 3 of
// issues/qdshell/qdwin-only-compositor-cleanup.md ("merely contain a WM
// substring … leave alone").
const CONFIG_WRITERS = [/\.config\/sway\b/, /\.config\/niri\b/, /qdshell\.kdl\b/];

const violations = [];
for (const f of files) {
    const ext = path.extname(f);
    const code = stripComments(fs.readFileSync(f, "utf8"), ext);
    const rel = path.relative(REPO, f);
    for (const re of [...DISPATCH, ...DEAD_FLAGS, ...CONFIG_WRITERS]) {
        const m = code.match(re);
        if (m) violations.push(`${rel}: forbidden token ${m[0]} (pattern ${re})`);
    }
}

assert.deepStrictEqual(
    violations,
    [],
    "qdwin-only invariant violated — foreign-compositor scaffolding reintroduced:\n  " +
        violations.join("\n  ")
);

// ─── 4. Orphaned helper / template files must stay deleted ────────────────
const MUST_NOT_EXIST = [
    "Scripts/python/src/compositor/labwc-workspace-helper.py",
];
for (const rel of MUST_NOT_EXIST) {
    assert.ok(
        !fs.existsSync(path.join(REPO, rel)),
        `${rel} must remain deleted (orphaned foreign-WM helper)`
    );
}

console.log(
    "qdwin-only-guard: all assertions passed (" +
        files.length +
        " source files scanned, no foreign-compositor dispatch/flags/writers)"
);
