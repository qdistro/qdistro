const assert = require("assert");
const fs = require("fs");
const path = require("path");

const repo = path.resolve(__dirname, "..");
const layoutTab = fs.readFileSync(
    path.join(repo, "Modules/Panels/Settings/Tabs/Display/LayoutSubTab.qml"),
    "utf8");
const settingsQml = fs.readFileSync(
    path.join(repo, "Commons/Settings.qml"), "utf8");
const defaults = JSON.parse(fs.readFileSync(
    path.join(repo, "Assets/settings-default.json"), "utf8"));

assert.ok(settingsQml.includes("property JsonObject display"));
assert.ok(settingsQml.includes('property string primaryOutput: ""'));
assert.deepStrictEqual(defaults.display, { primaryOutput: "" });

assert.ok(
    layoutTab.includes("Settings.data.display.primaryOutput || \"\""),
    "reload should seed primary from persisted display.primaryOutput"
);
assert.ok(
    layoutTab.includes("Settings.data.display.primaryOutput = OutputLayout.choosePrimary(working);"),
    "choosing Primary should persist the shell-side primary output"
);

console.log("display-primary: persistence invariant passed");
