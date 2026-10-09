// Settings change signal guard.
//
// Settings.data is a recovered PLAIN JS object (Commons/Settings.qml), so
// `Connections { target: Settings.data.<section> }` never binds: QML logs
// "Unable to assign QJSValue to QObject*" and the handlers never run. That
// left dark/light switching dead: the shell wrote colorSchemes.darkMode but
// nothing regenerated the palette, so neither the shell nor the published
// presentation snapshot followed.
//
// Settings now emits settingChanged(owner, key, value) from every observable
// setter. This guard locks:
//   * the signal and its emission from the setter (with the unchanged-scalar
//     and loading suppressions);
//   * the colorSchemes consumers (theming, hooks, dark-mode scheduling) use it
//     and none of them goes back to a dead Connections target.

"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

const settings = read("Commons/Settings.qml");
assert.match(settings, /signal settingChanged\(var owner, string key, var value\)/,
    "Settings must declare settingChanged(owner, key, value)");
// The observable machinery is a Proxy wrapper, not per-key
// Object.defineProperty accessors: QV4 SIGSEGVs in
// internalDefineOwnProperty when defineProperty runs while a component is
// being finalized (the settings Loader incubation path) — the proxy set
// trap never mutates the target's shape, so the write path cannot hit it.
const observable = settings.slice(settings.indexOf("function makeObservableSettings"));
assert.ok(observable.length > 0 && observable.includes("new Proxy"),
    "settings must be observable through a Proxy wrapper");
assert.match(observable, /set: function \(target, key, newValue\) \{[\s\S]*?root\.settingChanged\(proxy, key, stored\)/,
    "the observable set trap must emit settingChanged(proxy, key, stored)");
assert.match(observable, /var changed = newValue !== old/,
    "an unchanged scalar must not emit");
assert.match(observable, /!root\.loadingSettingsData/,
    "loading must not emit");
// ensures: Object.defineProperty never returns to the settings write
// path — it is what SEGV'd the shell under QV4 finalize/incubation.
// Scan ALL executable code in the file (comment lines stripped): a helper
// called from the proxy path would hit the same engine bug, so scoping the
// ban to the observable block alone would leave the hole open.
const settingsCode = settings.split("\n")
    .filter((l) => !/^\s*\/\//.test(l)).join("\n");
assert.ok(!/Object\.defineProperty/.test(settingsCode),
    "Settings.qml must not use Object.defineProperty anywhere (QV4 SEGV)");

// --- Executable coverage of the real observable machinery ------------------
// The functions are plain JS embedded in the QML file; extract them by
// brace matching and run them in Node against a stub root, so the contract
// below is exercised on the SAME source QV4 executes — not on a re-written
// copy that could drift.
function extractFunction(src, name) {
    const start = src.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `${name} must exist in Settings.qml`);
    const braceStart = src.indexOf("{", start);
    let depth = 0;
    for (let i = braceStart; i < src.length; i++) {
        if (src[i] === "{") depth++;
        else if (src[i] === "}") {
            depth--;
            if (depth === 0) return src.slice(start, i + 1);
        }
    }
    assert.fail(`${name}: unbalanced braces in Settings.qml`);
}
const stubRoot = {
    emitted: [],
    saved: 0,
    settingChanged(owner, key, value) { this.emitted.push({ owner, key, value }); },
    queueSettingsSave() { this.saved++; },
    loadingSettingsData: false,
    _settingsProxies: null,
};
const makeObservableSettings = new Function(
    "root",
    `${extractFunction(settings, "isPlainObject")}\n` +
    `${extractFunction(settings, "makeObservableSettings")}\n` +
    `return makeObservableSettings;`
)(stubRoot);
{
    const section = { darkMode: false, nested: { depth: 1 }, list: [1, 2] };
    const data = makeObservableSettings({ colorSchemes: section });

    // Write path: a changed scalar persists, queues a save, and emits.
    stubRoot.emitted.length = 0; stubRoot.saved = 0;
    data.colorSchemes.darkMode = true;
    assert.strictEqual(section.darkMode, true, "write must reach the raw object");
    assert.strictEqual(stubRoot.saved, 1, "write must queue a save");
    assert.strictEqual(stubRoot.emitted.length, 1, "changed scalar must emit");
    assert.strictEqual(stubRoot.emitted[0].key, "darkMode");
    assert.strictEqual(stubRoot.emitted[0].value, true);
    // Identity stays stable: the emitted owner IS Settings.data.colorSchemes.
    assert.strictEqual(stubRoot.emitted[0].owner, data.colorSchemes,
        "emitted owner must be the cached section proxy");

    // Unchanged scalar and load-time writes are suppressed.
    stubRoot.emitted.length = 0;
    data.colorSchemes.darkMode = true;
    assert.strictEqual(stubRoot.emitted.length, 0, "unchanged scalar must not emit");
    stubRoot.loadingSettingsData = true;
    data.colorSchemes.darkMode = false;
    assert.strictEqual(stubRoot.emitted.length, 0, "loading must not emit");
    stubRoot.loadingSettingsData = false;

    // Children wrap lazily and re-wrapping is a no-op (WeakMap identity).
    const nested = data.colorSchemes.nested;
    assert.strictEqual(nested.__qdshellObservable, true,
        "plain-object children must be observable proxies");
    assert.strictEqual(data.colorSchemes.nested, nested,
        "repeated reads must return the same proxy");
    assert.strictEqual(makeObservableSettings(data.colorSchemes), data.colorSchemes,
        "re-wrapping an observable section must be a no-op");
    stubRoot.emitted.length = 0;
    nested.depth = 2;
    assert.strictEqual(stubRoot.emitted.length, 1, "nested writes must emit");
    assert.strictEqual(stubRoot.emitted[0].owner, nested);

    // The proxy must serialize exactly like the raw tree it wraps.
    assert.deepStrictEqual(JSON.parse(JSON.stringify(data)),
        { colorSchemes: { darkMode: false, nested: { depth: 2 }, list: [1, 2] } },
        "proxy serialization must match the raw settings tree");
}

const consumers = {
    "Services/Theming/AppThemeService.qml": ["darkMode", "monitorForColors", "generationMethod"],
    "Services/Theming/ColorSchemeService.qml": ["darkMode"],
    "Services/Control/HooksService.qml": ["darkMode"],
    "Services/Location/DarkModeService.qml": ["schedulingMode", "manualSunrise", "manualSunset"],
};
for (const [rel, keys] of Object.entries(consumers)) {
    const src = read(rel);
    assert.ok(!/target:\s*Settings\.data\.colorSchemes\b/.test(src),
        `${rel}: Connections on Settings.data.colorSchemes never binds`);
    assert.match(src, /target:\s*Settings\s*\n\s*function onSettingChanged\(owner, key, value\)/,
        `${rel}: must react through Settings.settingChanged`);
    assert.match(src, /Settings\.data\.colorSchemes/, `${rel}: must filter on the colorSchemes owner`);
    for (const key of keys) {
        assert.ok(src.includes(`"${key}"`), `${rel}: must handle key ${key}`);
    }
}

// colors.json writers (the switch now fires, so their races became visible):
// ColorSchemeService writes the whole document once, and Color.qml writes its
// adapter once per commit, never once per assigned field.
const scheme = read("Services/Theming/ColorSchemeService.qml");
const writer = scheme.slice(scheme.indexOf("function writeColorsToDisk"));
assert.match(writer, /colorsWriter\.setText\(JSON\.stringify\(doc/,
    "writeColorsToDisk must write the whole document in one setText");
assert.ok(!/colorsWriter\.path = ""/.test(scheme),
    "the path bounce re-reads the old file into the writer");
assert.ok(!/JsonAdapter\s*\{\s*id:\s*out\b/.test(scheme),
    "no per-field JsonAdapter writer for colors.json");
const color = read("Commons/Color.qml");
assert.match(color, /onAdapterUpdated:\s*\{\s*\/\/[\s\S]*?if \(root\.committingTarget\)\s*return;/,
    "Color.qml must not write colors.json per field during a commit");
const commit = color.slice(color.indexOf("function commitTargetPalette"));
assert.match(commit, /customColorsData\.mOnHover = pal\.mOnHover;\s*customColorsFile\.writeAdapter\(\);\s*root\.committingTarget = false;/,
    "commitTargetPalette must write once after all sixteen fields");

// No QML file may target a Settings.data section with Connections: it never
// binds (plain object). Code lines only; comments may mention the pattern.
function walk(dir, out) {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, e.name);
        if (e.isDirectory()) {
            if (e.name !== "tests" && e.name !== "node_modules" && !e.name.startsWith("."))
                walk(p, out);
        } else if (e.name.endsWith(".qml")) {
            out.push(p);
        }
    }
    return out;
}
const dead = [];
for (const file of walk(ROOT, [])) {
    fs.readFileSync(file, "utf8").split("\n").forEach((line, i) => {
        if (/^\s*\/\//.test(line))
            return;
        if (/\btarget:\s*Settings\.data\b/.test(line))
            dead.push(`${path.relative(ROOT, file)}:${i + 1}`);
    });
}
assert.deepStrictEqual(dead, [], `dead Connections targets on Settings.data: ${dead.join(", ")}`);

console.log("ok - settings change signal guard");
