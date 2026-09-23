const assert = require("assert");
const fs = require("fs");
const path = require("path");
const Cfg = require("../Services/Hardware/PointerInputConfig.js");

const repo = path.resolve(__dirname, "..");
const mouseTab = fs.readFileSync(path.join(repo, "Modules/Panels/Settings/Tabs/Mouse/MouseTab.qml"), "utf8");
const service = fs.readFileSync(path.join(repo, "Services/Hardware/PointerInputService.qml"), "utf8");
const translations = JSON.parse(fs.readFileSync(path.join(repo, "Assets/Translations/en.json"), "utf8"));

// qdwin_shell_v1 v28 carries exactly one global pointer policy. These fields are
// live when CapabilityService.pointerConfig is true; everything else in the
// Mouse tab must be described as saved-only/persist-only.
const liveArgs = Cfg.toBindingArgs({
    pointerSpeed: 0.7,
    accelProfile: "flat",
    naturalScroll: true,
    scrollMethod: "edge",
    tapToClick: true,
    disableWhileTyping: true,
    leftHanded: true,
    middleClickEmulation: true,
    horizontalScroll: true,
    clickMethod: "clickfinger",
    doubleClickTime: 100,
    doubleClickDistance: 1,
    dragThreshold: 1,
    disabledDevices: ["mouse0"],
    perDeviceOverrides: { mouse0: { pointerSpeed: 0.1 } },
    tabletMapping: { output: "HDMI-A-1" }
});
assert.deepStrictEqual(Object.keys(liveArgs).sort(), [
    "accelProfile",
    "accelSpeed",
    "disableWhileTyping",
    "leftHanded",
    "middleEmulation",
    "naturalScroll",
    "scrollMethod",
    "tapToClick"
].sort(), "wire scope stays limited to the eight global v28 fields");

[
    "horizontalScroll",
    "clickMethod",
    "doubleClickTime",
    "doubleClickDistance",
    "dragThreshold",
    "disabledDevices",
    "perDeviceOverrides",
    "tabletMapping"
].forEach(field => {
    assert.ok(!Object.prototype.hasOwnProperty.call(liveArgs, field),
        field + " must not be implied live by the v28 binding args");
});

const mouseStrings = translations.panels.mouse;
assert.match(mouseStrings["live-fields-note"], /Controls marked below are saved only/);
assert.match(mouseStrings["click-method-note"], /saved only/);
assert.match(mouseStrings["horizontal-scroll-note"], /saved/);
assert.match(mouseStrings["per-device-description"], /saved only/);
assert.match(mouseStrings["tablet-description"], /saved only/);
assert.match(mouseStrings["doubleclick-note"], /does not apply them live/);

assert.ok(mouseTab.includes("readonly property bool globalPointerLive"),
    "MouseTab should use an explicit global-live capability instead of implying every control is live");
assert.ok(mouseTab.includes('text: I18n.tr("panels.mouse.live-fields-note")'),
    "live backend must show the exact live field scope");
assert.ok(mouseTab.includes('text: I18n.tr("panels.mouse.click-method-note")'),
    "click method needs its own saved-only note when global live apply is available");
assert.ok(mouseTab.includes('visible: root.globalPointerLive'),
    "saved-only notes for unsupported controls must still show with a live global backend");
assert.ok(service.includes("Per-device policy, click method, horizontal scroll and tablet mapping are"),
    "service comment must document the persist-only fields outside the global v28 request");

console.log("pointer-live-scope: all assertions passed");
