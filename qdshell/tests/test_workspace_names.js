const assert = require("assert");
const fs = require("fs");
const path = require("path");

const repo = path.resolve(__dirname, "..");
const qml = fs.readFileSync(
    path.join(repo, "Services/Qdwin/Qdwin.qml"), "utf8");
const fn = qml.match(/function _pushWorkspaceNames\(\) \{[\s\S]*?\n    \}/);
assert.ok(fn, "_pushWorkspaceNames function should exist");
const body = fn[0];
const applyCount = qml.match(/function applyWorkspaceCount\(count\) \{[\s\S]*?\n    \}/);
assert.ok(applyCount, "explicit workspace-count apply function should exist");

assert.ok(
    body.includes("var desired = Math.max(1, Math.min(_settingsWorkspaceCount, 32));"),
    "_pushWorkspaceNames must account for the desired settings count"
);
assert.ok(
    body.includes("var count = Math.max(desired, live);"),
    "workspace-name push must cover newly requested workspaces before live count refreshes"
);
assert.ok(
    !body.includes("var count = bound ? qdwinBinding.workspaceCount"),
    "old live-count-only workspace-name loop must not return"
);

assert.ok(
    applyCount[0].includes("qdwinBinding.setWorkspaceCount(desired);"),
    "a settings UI count change must be pushed explicitly to the live compositor"
);
assert.ok(
    applyCount[0].includes("_settingsWorkspaceCount = desired;"),
    "explicit count apply must refresh the QML-side settings cache"
);

for (const relative of [
    "Modules/Panels/Settings/Tabs/Appearance/AppearanceTab.qml",
    "Modules/Panels/Settings/Bar/WidgetSettings/WorkspaceSettings.qml",
]) {
    const settingsQml = fs.readFileSync(path.join(repo, relative), "utf8");
    assert.ok(
        settingsQml.includes("Qdwin.applyWorkspaceCount(value);"),
        `${relative} must use the explicit live workspace-count boundary`
    );
}

console.log("workspace-names: growth push invariant passed");
