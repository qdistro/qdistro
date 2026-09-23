const assert = require("assert");
const K = require("../Services/Keyboard/KeyboardXkb.js");

// ─── evdev.lst parsing ──────────────────────────────────────────────
// Representative snippet covering all four sections.
const lst = [
    '! model',
    '  pc105          Generic 105-key PC',
    '  thinkpad       ThinkPad',
    '',
    '! layout',
    '  us             English (US)',
    '  de             German',
    '  fr             French',
    '',
    '! variant',
    '  intl           us: English (US, intl., with dead keys)',
    '  dvorak         us: English (Dvorak)',
    '  nodeadkeys     de: German (no dead keys)',
    '',
    '! option',
    '  grp            Switching to another layout',
    '  grp:alt_shift_toggle  Alt+Shift',
    '  grp:caps_toggle       Caps Lock',
    '  compose        Position of Compose key',
    '  compose:ralt          Right Alt',
    ''
].join("\n");

const parsed = K.parseXkbList(lst);

// Models.
assert.strictEqual(parsed.models.length, 2);
assert.deepStrictEqual(parsed.models[0], { key: "pc105", name: "Generic 105-key PC" });
assert.deepStrictEqual(parsed.models[1], { key: "thinkpad", name: "ThinkPad" });

// Layouts.
assert.strictEqual(parsed.layouts.length, 3);
const layoutKeys = parsed.layouts.map(l => l.key);
assert.deepStrictEqual(layoutKeys, ["us", "de", "fr"]);
assert.strictEqual(parsed.layouts[0].name, "English (US)");

// Variants grouped by owning layout (token before the colon).
assert.ok(parsed.variants.us, "us variants present");
assert.strictEqual(parsed.variants.us.length, 2);
assert.deepStrictEqual(parsed.variants.us.map(v => v.key), ["intl", "dvorak"]);
assert.ok(parsed.variants.de, "de variants present");
assert.deepStrictEqual(parsed.variants.de.map(v => v.key), ["nodeadkeys"]);
assert.ok(!parsed.variants.fr, "fr has no variants");

// Options grouped, group header name applied, members collected.
const grp = parsed.options.find(o => o.group === "grp");
assert.ok(grp, "grp option group present");
assert.strictEqual(grp.name, "Switching to another layout");
assert.deepStrictEqual(grp.options.map(o => o.key), ["grp:alt_shift_toggle", "grp:caps_toggle"]);
const compose = parsed.options.find(o => o.group === "compose");
assert.ok(compose, "compose option group present");
assert.strictEqual(compose.name, "Position of Compose key");
assert.deepStrictEqual(compose.options.map(o => o.key), ["compose:ralt"]);

// ─── setxkbmap argv: single layout, no variant, no options ──────────
let argv = K.setxkbmapArgv({ layouts: ["us"] });
assert.deepStrictEqual(argv, ["setxkbmap", "-layout", "us", "-option", ""]);

// Default layout "us" when none given.
assert.deepStrictEqual(K.setxkbmapArgv({}), ["setxkbmap", "-layout", "us", "-option", ""]);

// ─── setxkbmap argv: multi layout + variants + options + model ──────
argv = K.setxkbmapArgv({
    model: "pc105",
    layouts: ["us", "de", "fr"],
    variants: { us: "intl", de: "nodeadkeys" }, // fr has no variant
    xkbOptions: ["caps:escape"],
    switchShortcut: "grp:alt_shift_toggle",
    composeKey: "compose:ralt"
});
assert.deepStrictEqual(argv, [
    "setxkbmap",
    "-model", "pc105",
    "-layout", "us,de,fr",
    "-variant", "intl,nodeadkeys,",   // positional, trailing empty for fr
    "-option", "",                     // initial clear
    "-option", "caps:escape",
    "-option", "grp:alt_shift_toggle",
    "-option", "compose:ralt"
]);

// No -variant flag when all variants empty.
argv = K.setxkbmapArgv({ layouts: ["us", "de"], variants: {} });
assert.ok(argv.indexOf("-variant") === -1, "no -variant when all empty");

// ─── malicious input stays a single argv token ─────────────────────
// Layout / option strings carrying shell metacharacters must remain individual
// argv elements; they must NEVER be folded into a shell string. (The QML side
// passes argv to setxkbmap directly / _q()-quotes each token, so these are
// inert data.)
const evilLayout = "us; rm -rf ~";
const evilOption = "$(touch /tmp/pwned)";
const evilModel = "`reboot`";
const evilSwitch = "grp:toggle && curl evil";
argv = K.setxkbmapArgv({
    model: evilModel,
    layouts: [evilLayout],
    xkbOptions: [evilOption],
    switchShortcut: evilSwitch
});
// Each malicious string is exactly one argv element (not split, not merged).
assert.ok(argv.includes(evilLayout), "evil layout is a single token");
assert.ok(argv.includes(evilModel), "evil model is a single token");
assert.ok(argv.includes(evilOption), "evil option is a single token");
assert.ok(argv.includes(evilSwitch), "evil switch is a single token");
// And no token silently concatenated metacharacters with a flag.
argv.forEach(tok => {
    assert.strictEqual(typeof tok, "string");
});
// The dangerous payload occupies its own slot right after its flag.
assert.strictEqual(argv[argv.indexOf("-model") + 1], evilModel);
assert.strictEqual(argv[argv.indexOf("-layout") + 1], evilLayout);
// No single token is a runnable compound shell command of flag+payload.
assert.ok(!argv.some(t => t === ("-layout " + evilLayout)), "flag and payload never merged");

// ─── xset r rate argv ───────────────────────────────────────────────
assert.deepStrictEqual(K.xsetRepeatArgv(300, 25), ["xset", "r", "rate", "300", "25"]);
// Rounding + clamp to >= 1.
assert.deepStrictEqual(K.xsetRepeatArgv(299.6, 24.4), ["xset", "r", "rate", "300", "24"]);
assert.deepStrictEqual(K.xsetRepeatArgv(0, 0), ["xset", "r", "rate", "1", "1"]);
assert.deepStrictEqual(K.xsetRepeatArgv(-50, -3), ["xset", "r", "rate", "1", "1"]);
// xset argv is fully tokenised, never a shell string.
const xs = K.xsetRepeatArgv(300, 25);
assert.ok(Array.isArray(xs));
assert.notStrictEqual(xs[0], "sh");

// ─── shell-string builders: quote-by-construction at the sh -c boundary ─
// The QML apply path chains tools via `sh -c`, so it serializes to a shell
// string. The pure setxkbmapShellCmd/xsetRepeatShellCmd builders must quote
// EVERY user value unconditionally — including values that begin with "-" or
// carry shell metacharacters — so nothing can break out of its single-quoted
// word. (Regression guard for a heuristic that left "-"-prefixed tokens raw.)

// Single quoting helper round-trips a benign value.
assert.strictEqual(K.shellQuote("us"), "'us'");
// Embedded single quote is escaped, not terminated early.
assert.strictEqual(K.shellQuote("a'b"), "'a'\\''b'");

// Benign multi-layout command mirrors setxkbmap with each value quoted.
let cmd = K.setxkbmapShellCmd({
    model: "pc105",
    layouts: ["us", "de"],
    variants: { us: "intl" },
    xkbOptions: ["caps:escape"]
}, true);
assert.strictEqual(
    cmd,
    "setxkbmap -model 'pc105' -layout 'us,de' -variant 'intl,' -option '' -option 'caps:escape'"
);

// Unavailable tool -> empty string (skipped in the chain).
assert.strictEqual(K.setxkbmapShellCmd({ layouts: ["us"] }, false), "");

// MALICIOUS values, including "-"-prefixed ones, must be fully single-quoted.
const danger = {
    model: "-foo; touch /tmp/pwned",          // starts with '-'
    layouts: ["us; rm -rf ~"],
    xkbOptions: ["$(touch /tmp/pwned)", "-x && curl evil"],
    switchShortcut: "`reboot`",
    composeKey: "a'b"                          // embedded single quote
};
cmd = K.setxkbmapShellCmd(danger, true);
// Every payload appears ONLY inside a single-quoted word (preceded by ' ).
assert.ok(cmd.includes("'-foo; touch /tmp/pwned'"), "leading-dash model quoted");
assert.ok(cmd.includes("'us; rm -rf ~'"), "layout with metachars quoted");
assert.ok(cmd.includes("'$(touch /tmp/pwned)'"), "command-substitution option quoted");
assert.ok(cmd.includes("'-x && curl evil'"), "leading-dash option quoted");
assert.ok(cmd.includes("'`reboot`'"), "backtick switch quoted");
assert.ok(cmd.includes("'a'\\''b'"), "single-quote in compose escaped");
// No metacharacter appears OUTSIDE single quotes: the command must contain no
// unquoted ';', '&', '$', '`' or '(' from the payloads. Strip all quoted
// segments and assert what remains has no shell metacharacters.
const withoutQuoted = cmd.replace(/'(?:[^']|'\\'')*'/g, "");
assert.ok(!/[;&$`()]/.test(withoutQuoted),
    "no unquoted shell metacharacter survives serialization");
// Flags are still present as bare literals.
assert.ok(/(^|\s)-model(\s|$)/.test(withoutQuoted), "-model literal present");
assert.ok(/(^|\s)-layout(\s|$)/.test(withoutQuoted), "-layout literal present");
assert.ok(/(^|\s)-option(\s|$)/.test(withoutQuoted), "-option literal present");

// xset shell builder quotes the two numeric values.
assert.strictEqual(K.xsetRepeatShellCmd(300, 25, true), "xset r rate '300' '25'");
assert.strictEqual(K.xsetRepeatShellCmd(0, 0, true), "xset r rate '1' '1'");
assert.strictEqual(K.xsetRepeatShellCmd(300, 25, false), "");

// ─── qdwin set_key_repeat (v28) arg clamping ────────────────────────
// rate in [0, 255] (0 = off), delay in [1, 10000] ms. The compositor clamps
// again server-side; the shell sends canonical values so the wire is clean.
assert.deepStrictEqual(K.repeatToQdwinArgs(25, 500), { rate: 25, delay: 500 },
    "in-range values pass through");
assert.deepStrictEqual(K.repeatToQdwinArgs(0, 1), { rate: 0, delay: 1 },
    "min edges (rate 0 = off, delay 1)");
assert.deepStrictEqual(K.repeatToQdwinArgs(255, 10000), { rate: 255, delay: 10000 },
    "max edges");
assert.deepStrictEqual(K.repeatToQdwinArgs(99999, 99999), { rate: 255, delay: 10000 },
    "above max clamps");
assert.deepStrictEqual(K.repeatToQdwinArgs(-5, 0), { rate: 0, delay: 1 },
    "below min clamps (rate→0, delay→1)");
assert.deepStrictEqual(K.repeatToQdwinArgs(25.7, 500.4), { rate: 26, delay: 500 },
    "rounded");
assert.deepStrictEqual(K.repeatToQdwinArgs("30", "600"), { rate: 30, delay: 600 },
    "numeric strings parsed");
assert.deepStrictEqual(K.repeatToQdwinArgs("x", undefined), { rate: 25, delay: 500 },
    "non-finite → settings defaults");
assert.deepStrictEqual(K.repeatToQdwinArgs(Infinity, NaN), { rate: 25, delay: 500 },
    "Infinity/NaN → settings defaults");

console.log("keyboard-xkb: all assertions passed");
