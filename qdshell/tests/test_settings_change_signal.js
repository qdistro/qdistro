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
const setter = settings.slice(settings.indexOf("function defineObservableSettingProperty"));
assert.match(setter, /set: function \(newValue\) \{[\s\S]*?root\.settingChanged\(target, key, stored\)/,
    "the observable setter must emit settingChanged(target, key, stored)");
assert.match(setter, /var changed = newValue !== stored/,
    "an unchanged scalar must not emit");
assert.match(setter, /!root\.loadingSettingsData/,
    "loading must not emit");

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

console.log("ok - settings change signal guard");
