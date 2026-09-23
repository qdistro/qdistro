// Tests for the advanced pointer helpers added for the Mouse-tab parity work:
// tablet/Wacom detection, per-device override resolution, tablet-mapping
// normalization/clamping, disabled-device filtering, and the qdwin-only
// persist-only / no-command-builder invariants. Pure Node test — mirrors the
// dual QML/Node PointerInputParse.js module.

const assert = require("assert");
const P = require("../Services/Hardware/PointerInputParse.js");

// ─── Tablet / Wacom detection (libinput) ─────────────────────────────
const liTabletFixture = [
    'Device:           Wacom Intuos Pro M Pen',
    'Kernel:           /dev/input/event20',
    'Capabilities:     tablet',
    '',
    'Device:           Logitech USB Receiver',
    'Kernel:           /dev/input/event4',
    'Capabilities:     pointer',
    'Tap-to-click:     n/a',
    'Scroll methods:   *button',
    ''
].join("\n");

const liT = {};
P.parseLibinput(liTabletFixture.split("\n")).forEach(d => { liT[d.name] = d; });
assert.ok(liT["Wacom Intuos Pro M Pen"], "wacom tablet detected via libinput");
assert.strictEqual(liT["Wacom Intuos Pro M Pen"].type, "tablet");
assert.strictEqual(liT["Wacom Intuos Pro M Pen"].hasTap, false, "tablet has no tap control");
assert.strictEqual(liT["Logitech USB Receiver"].type, "mouse");

// ─── Tablet detection (/proc) ────────────────────────────────────────
const procTabletFixture = [
    'I: Bus=0003 Vendor=056a Product=0357 Version=0111',
    'N: Name="Wacom Intuos Pro M Pen"',
    'H: Handlers=event20 mouse3',
    'B: EV=b',          // EV_ABS, no EV_REL
    'B: ABS=1000003',
    '',
    'I: Bus=0003 Vendor=222a Product=0001 Version=0100',
    'N: Name="Weida Touchscreen"',
    'H: Handlers=event8',
    'B: EV=b',          // EV_ABS but no tablet name / pen / prop -> excluded
    'B: ABS=2608000 3',
    ''
].join("\n");

const procT = {};
P.parseProc(procTabletFixture.split("\n")).forEach(d => { procT[d.name] = d; });
assert.ok(procT["Wacom Intuos Pro M Pen"], "wacom tablet detected via /proc");
assert.strictEqual(procT["Wacom Intuos Pro M Pen"].type, "tablet");
assert.ok(!procT["Weida Touchscreen"], "plain touchscreen still excluded");

// ─── Per-device override resolution ──────────────────────────────────
const global = {
    accelProfile: "adaptive",
    pointerSpeed: 0.5,
    naturalScroll: false,
    scrollMethod: "two_finger",
    tapToClick: true,
    disableWhileTyping: true,
    leftHanded: false,
    horizontalScroll: true
};

// Fallback to global when no override exists for the id.
const noOverride = P.resolveDeviceSettings(global, {}, "Some Mouse");
assert.strictEqual(noOverride.pointerSpeed, 0.5);
assert.strictEqual(noOverride.accelProfile, "adaptive");
assert.strictEqual(noOverride.leftHanded, false);

// Happy path: device-specific override shadows only the keys it sets.
const overrides = {
    "Logitech USB Receiver": { pointerSpeed: 0.9, leftHanded: true },
    "SynPS/2 Synaptics TouchPad": { naturalScroll: true, scrollMethod: "edge" }
};
const mouseEff = P.resolveDeviceSettings(global, overrides, "Logitech USB Receiver");
assert.strictEqual(mouseEff.pointerSpeed, 0.9, "override pointerSpeed applied");
assert.strictEqual(mouseEff.leftHanded, true, "override leftHanded applied");
assert.strictEqual(mouseEff.accelProfile, "adaptive", "unset key falls back to global");
assert.strictEqual(mouseEff.horizontalScroll, true, "unset key falls back to global");

const tpEff = P.resolveDeviceSettings(global, overrides, "SynPS/2 Synaptics TouchPad");
assert.strictEqual(tpEff.naturalScroll, true);
assert.strictEqual(tpEff.scrollMethod, "edge");
assert.strictEqual(tpEff.pointerSpeed, 0.5, "touchpad falls back to global speed");

// Unknown device id -> all globals.
const unknown = P.resolveDeviceSettings(global, overrides, "No Such Device");
assert.deepStrictEqual(
    [unknown.pointerSpeed, unknown.scrollMethod, unknown.naturalScroll],
    [0.5, "two_finger", false],
    "unknown id resolves to global values");

// Null/garbage inputs are tolerated.
assert.strictEqual(P.resolveDeviceSettings(global, null, "x").pointerSpeed, 0.5);
assert.strictEqual(P.resolveDeviceSettings(global, undefined, undefined).accelProfile, "adaptive");
assert.strictEqual(P.resolveDeviceSettings(null, {}, "x").pointerSpeed, undefined);

// A hostile override carrying extra keys cannot inject unknown fields.
const injected = P.resolveDeviceSettings(global, { "d": { __proto__: { evil: 1 }, pointerSpeed: 0.2, notAKey: "boom" } }, "d");
assert.strictEqual(injected.pointerSpeed, 0.2);
assert.ok(!("notAKey" in injected), "non-overridable keys are dropped");
assert.ok(!("evil" in injected), "prototype-injected keys are dropped");

// Prototype-named device ids must not resolve to inherited object members.
// On a plain {} map, "toString"/"constructor"/"__proto__" exist on the
// prototype; the helpers must treat them as absent (fall back to global) and
// must never report a phantom override.
["toString", "constructor", "__proto__", "hasOwnProperty"].forEach(pid => {
    const r = P.resolveDeviceSettings(global, {}, pid);
    assert.strictEqual(r.pointerSpeed, 0.5, "prototype-named id falls back to global: " + pid);
    assert.strictEqual(P.hasDeviceOverride({}, pid), false, "no phantom override for: " + pid);
});
// A real own-key override under a prototype-collision name still works.
const protoOv = {}; protoOv["toString"] = { pointerSpeed: 0.33 };
assert.strictEqual(P.resolveDeviceSettings(global, protoOv, "toString").pointerSpeed, 0.33);
assert.strictEqual(P.hasDeviceOverride(protoOv, "toString"), true);

// Disabled-filter must not treat a device named "toString" as disabled.
const protoDevs = [{ id: "toString", type: "mouse" }, { id: "constructor", type: "mouse" }];
assert.strictEqual(P.filterEnabledDevices(protoDevs, []).length, 2,
    "prototype-named devices are not phantom-disabled");
assert.strictEqual(P.filterEnabledDevices(protoDevs, ["toString"]).length, 1,
    "prototype-named device can be explicitly disabled");

// hasDeviceOverride predicate.
assert.strictEqual(P.hasDeviceOverride(overrides, "Logitech USB Receiver"), true);
assert.strictEqual(P.hasDeviceOverride(overrides, "No Such Device"), false);
assert.strictEqual(P.hasDeviceOverride({ "d": {} }, "d"), false, "empty override object is not an override");
assert.strictEqual(P.hasDeviceOverride(null, "d"), false);

// ─── Tablet mapping normalization / clamping ─────────────────────────
const fullDefault = P.normalizeTabletMapping(undefined);
assert.deepStrictEqual(fullDefault, {
    output: "", aspect: "keep", area: { x: 0, y: 0, w: 1, h: 1 }
}, "default mapping is full surface, all outputs, keep aspect");

// Out-of-range values are clamped into [0,1] and kept inside the surface.
const clamped = P.normalizeTabletMapping({
    output: "HDMI-1", aspect: "stretch",
    area: { x: 0.8, y: -0.5, w: 0.9, h: 2.0 }
});
assert.strictEqual(clamped.output, "HDMI-1");
assert.strictEqual(clamped.aspect, "stretch");
assert.strictEqual(clamped.area.x, 0.8);
assert.strictEqual(clamped.area.y, 0, "negative y clamped to 0");
assert.ok(Math.abs(clamped.area.w - 0.2) < 1e-9, "w shrunk so x+w <= 1");
assert.strictEqual(clamped.area.h, 1, "h clamped to 1 and fits since y=0");

// Degenerate sizes collapse to a small epsilon, never zero/negative.
const degenerate = P.normalizeTabletMapping({ area: { x: 0.2, y: 0.2, w: 0, h: -3 } });
assert.ok(degenerate.area.w > 0, "zero width becomes positive epsilon");
assert.ok(degenerate.area.h > 0, "negative height becomes positive epsilon");

// Origin at the far edge (x=1) must still yield an in-bounds, non-degenerate
// region: x is pulled back so x+w <= 1 and w stays >= epsilon.
const edge = P.normalizeTabletMapping({ area: { x: 1, y: 1, w: 1, h: 1 } });
assert.ok(edge.area.x + edge.area.w <= 1 + 1e-9, "x+w stays within surface even when x=1");
assert.ok(edge.area.y + edge.area.h <= 1 + 1e-9, "y+h stays within surface even when y=1");
assert.ok(edge.area.w > 0 && edge.area.h > 0, "region remains non-degenerate when origin at edge");

// Property invariant: for any clamped mapping, origin>=0, size>=epsilon,
// origin+size<=1. Spot-check a grid of hostile inputs.
[0, 0.5, 0.99, 1, 5, -2].forEach(ox => [0, 0.7, 1, 9].forEach(ow => {
    const m = P.normalizeTabletMapping({ area: { x: ox, y: 0, w: ow, h: 1 } });
    assert.ok(m.area.x >= 0 && m.area.w > 0 && m.area.x + m.area.w <= 1 + 1e-9,
        `area invariant holds for x=${ox} w=${ow}`);
}));

// NaN / non-numeric values fall back to defaults.
const nan = P.normalizeTabletMapping({ area: { x: "abc", y: NaN, w: undefined, h: null } });
assert.strictEqual(nan.area.x, 0);
assert.strictEqual(nan.area.y, 0);
assert.strictEqual(nan.area.w, 1, "missing w defaults to full");

// Unknown aspect coerces to "keep"; non-string output coerces to "".
const coerced = P.normalizeTabletMapping({ output: 123, aspect: "weird" });
assert.strictEqual(coerced.aspect, "keep");
assert.strictEqual(coerced.output, "");

// ─── Disabled-device filtering ───────────────────────────────────────
const devs = [
    { id: "Logitech USB Receiver", type: "mouse" },
    { id: "SynPS/2 Synaptics TouchPad", type: "touchpad" },
    { id: "Wacom Intuos Pro M Pen", type: "tablet" }
];
const enabled = P.filterEnabledDevices(devs, ["SynPS/2 Synaptics TouchPad"]);
assert.strictEqual(enabled.length, 2, "disabled device removed");
assert.ok(!enabled.some(d => d.id === "SynPS/2 Synaptics TouchPad"), "touchpad filtered out");
assert.ok(enabled.some(d => d.id === "Logitech USB Receiver"), "mouse kept");

// Empty / null disabled list keeps everything; input not mutated.
assert.strictEqual(P.filterEnabledDevices(devs, []).length, 3);
assert.strictEqual(P.filterEnabledDevices(devs, null).length, 3);
assert.strictEqual(devs.length, 3, "input array not mutated");
assert.strictEqual(P.filterEnabledDevices(null, []).length, 0, "non-array devices -> empty");

// isDeviceDisabled predicate.
assert.strictEqual(P.isDeviceDisabled(["a", "b"], "b"), true);
assert.strictEqual(P.isDeviceDisabled(["a", "b"], "c"), false);
assert.strictEqual(P.isDeviceDisabled(null, "a"), false);

// ─── Injection safety: per-device/tablet persist-only, no commands ───
// qdshell is qdwin-only. The v28 pointer request carries a global policy only,
// so per-device overrides, disabled devices and tablet mapping are persist-only.
// There must be no helper that turns a device id into a command/argv. A device
// id containing shell metacharacters flows through every advanced helper as
// opaque data only.
const evilId = "wacom; rm -rf / #$(touch pwned)`whoami`";
const evilDevices = [{ id: evilId, name: evilId, type: "tablet" }];
const evilOverrides = {}; evilOverrides[evilId] = { pointerSpeed: 0.7 };

// resolution treats it as a plain map key.
assert.strictEqual(P.resolveDeviceSettings(global, evilOverrides, evilId).pointerSpeed, 0.7);
// filtering treats it as a plain id; it never becomes an argument.
assert.strictEqual(P.filterEnabledDevices(evilDevices, [evilId]).length, 0);
assert.strictEqual(P.isDeviceDisabled([evilId], evilId), true);
// mapping output (also potentially device-derived) stays an opaque string.
const evilMap = P.normalizeTabletMapping({ output: evilId });
assert.strictEqual(evilMap.output, evilId, "output kept verbatim as a label, never executed");

// No exported helper builds a command, references sway, or any shell dispatch.
const exported = Object.keys(P);
exported.forEach(name => {
    assert.ok(!/sway|hyprctl|wlr|wlopm|argv|command|exec|spawn|dispatch/i.test(name),
        "no exported helper looks like a command builder/dispatcher: " + name);
});
assert.strictEqual(typeof P.buildSwayInputCommands, "undefined", "sway command builder must be gone");

console.log("pointer-overrides: all assertions passed");
