const assert = require("assert");
const P = require("../Services/Hardware/PointerInputParse.js");

// ─── /proc/bus/input/devices classification ─────────────────────────
// Representative blocks: a real USB mouse (EV_REL), a Synaptics touchpad
// (EV_ABS + INPUT_PROP_BUTTONPAD, plus a touchpad name), a TrackPoint pointing
// stick, a keyboard (must be excluded), and a touchscreen (EV_ABS but no
// touchpad prop and not named touchpad — excluded as it has no EV_REL and no
// pointer prop).
//
// EV bits: EV_SYN(0x1) EV_KEY(0x2) EV_REL(0x4) EV_ABS(0x8) ...
// PROP bits: INPUT_PROP_POINTER(0x1) INPUT_PROP_BUTTONPAD(0x4)
const procFixture = [
    'I: Bus=0003 Vendor=046d Product=c52b Version=0111',
    'N: Name="Logitech USB Receiver Mouse"',
    'P: Phys=usb-0000:00:14.0-1/input2',
    'H: Handlers=mouse0 event4',
    'B: EV=17',          // 0x17 has EV_REL(0x4) set -> mouse
    'B: KEY=ff0000 0 0 0 0',
    'B: REL=1943',
    '',
    'I: Bus=0018 Vendor=06cb Product=7a13 Version=0100',
    'N: Name="SYNA8004:00 06CB:7A13 Touchpad"',
    'P: Phys=i2c-SYNA8004:00',
    'H: Handlers=mouse1 event12',
    'B: EV=b',           // 0xb has EV_ABS(0x8), no EV_REL
    'B: KEY=e520 10000 0 0 0 0',
    'B: ABS=2e0800000000003',
    'B: PROP=5',         // INPUT_PROP_POINTER|BUTTONPAD -> touchpad
    '',
    'I: Bus=0011 Vendor=0002 Product=000a Version=0000',
    'N: Name="TPPS/2 IBM TrackPoint"',
    'P: Phys=isa0060/serio1/input0',
    'H: Handlers=mouse2 event13',
    'B: EV=7',           // EV_REL set, but classified trackpoint by name
    'B: KEY=70000 0 0 0 0',
    'B: REL=3',
    '',
    'I: Bus=0011 Vendor=0001 Product=0001 Version=ab83',
    'N: Name="AT Translated Set 2 keyboard"',
    'P: Phys=isa0060/serio0/input0',
    'H: Handlers=sysrq kbd event0',
    'B: EV=120013',      // EV_KEY only, no EV_REL/EV_ABS pointer -> excluded
    'B: KEY=402000000 3803078f800d001 feffffdfffefffff fffffffffffffffe',
    '',
    'I: Bus=0003 Vendor=222a Product=0001 Version=0100',
    'N: Name="Weida Hi-Tech CoolTouch System Touchscreen"',
    'P: Phys=usb-0000:00:14.0-3/input0',
    'H: Handlers=event8',
    'B: EV=b',           // EV_ABS but PROP absent -> not a touchpad
    'B: KEY=400 0 0 0 0 0',
    'B: ABS=2608000 3',
    // no PROP line, name is "Touchscreen" not "touchpad" -> excluded
    ''
].join("\n");

const procDevices = P.parseProc(procFixture.split("\n"));
const byName = {};
procDevices.forEach(d => { byName[d.name] = d; });

// Mouse present and classified.
assert.ok(byName["Logitech USB Receiver Mouse"], "mouse should be detected");
assert.strictEqual(byName["Logitech USB Receiver Mouse"].type, "mouse");

// Touchpad present and classified, with touchpad-only has* flags.
const tp = byName["SYNA8004:00 06CB:7A13 Touchpad"];
assert.ok(tp, "touchpad should be detected");
assert.strictEqual(tp.type, "touchpad");
assert.strictEqual(tp.hasTap, true);
assert.strictEqual(tp.hasDisableWhileTyping, true);

// TrackPoint classified as trackpoint by name.
assert.ok(byName["TPPS/2 IBM TrackPoint"], "trackpoint should be detected");
assert.strictEqual(byName["TPPS/2 IBM TrackPoint"].type, "trackpoint");

// Keyboard excluded.
assert.ok(!byName["AT Translated Set 2 keyboard"], "keyboard must be excluded");

// Touchscreen excluded.
assert.ok(!byName["Weida Hi-Tech CoolTouch System Touchscreen"], "touchscreen must be excluded");

// Exactly the three pointer devices, nothing else.
assert.strictEqual(procDevices.length, 3, "exactly 3 pointer devices expected");

// De-duplication by name (a physical mouse with several event nodes).
const dupFixture = [
    'I: Bus=0003 Vendor=046d Product=c52b',
    'N: Name="Dup Mouse"',
    'B: EV=17',
    '',
    'I: Bus=0003 Vendor=046d Product=c52b',
    'N: Name="Dup Mouse"',
    'B: EV=17',
    ''
].join("\n");
assert.strictEqual(P.parseProc(dupFixture.split("\n")).length, 1, "duplicate names de-duplicated");

// ─── libinput list-devices classification ───────────────────────────
const libinputFixture = [
    'Device:           Logitech USB Receiver',
    'Kernel:           /dev/input/event4',
    'Capabilities:     pointer',
    'Tap-to-click:     n/a',
    'Natural scrolling: disabled',
    'Disable-w-typing: n/a',
    'Scroll methods:   *button',
    '',
    'Device:           SynPS/2 Synaptics TouchPad',
    'Kernel:           /dev/input/event12',
    'Capabilities:     pointer gesture',
    'Tap-to-click:     disabled',
    'Natural scrolling: disabled',
    'Disable-w-typing: enabled',
    'Scroll methods:   *two-finger edge',
    '',
    'Device:           AT Translated Set 2 keyboard',
    'Kernel:           /dev/input/event0',
    'Capabilities:     keyboard',
    '',
    'Device:           Weida Touchscreen',
    'Kernel:           /dev/input/event8',
    'Capabilities:     touch',
    ''
].join("\n");

const liDevices = P.parseLibinput(libinputFixture.split("\n"));
const liByName = {};
liDevices.forEach(d => { liByName[d.name] = d; });

assert.strictEqual(liByName["Logitech USB Receiver"].type, "mouse");
assert.strictEqual(liByName["SynPS/2 Synaptics TouchPad"].type, "touchpad");
assert.strictEqual(liByName["SynPS/2 Synaptics TouchPad"].hasTap, true);
assert.strictEqual(liByName["SynPS/2 Synaptics TouchPad"].hasDisableWhileTyping, true);
assert.strictEqual(liByName["SynPS/2 Synaptics TouchPad"].hasScrollMethod, true);
assert.ok(!liByName["AT Translated Set 2 keyboard"], "keyboard (no pointer cap) excluded");
assert.ok(!liByName["Weida Touchscreen"], "touchscreen (touch, not pointer) excluded");
assert.strictEqual(liDevices.length, 2, "exactly 2 pointer devices from libinput");

// ─── parseEnum dispatch on @@SRC marker ─────────────────────────────
const enumLibinput = P.parseEnum("@@SRC:libinput\n" + libinputFixture);
assert.strictEqual(enumLibinput.source, "libinput");
assert.strictEqual(enumLibinput.devices.length, 2);

const enumProc = P.parseEnum("@@SRC:proc\n" + procFixture);
assert.strictEqual(enumProc.source, "proc");
assert.strictEqual(enumProc.devices.length, 3);

// No devices -> source "none".
const enumNone = P.parseEnum("@@SRC:proc\nI: Bus=0011\nN: Name=\"AT Translated Set 2 keyboard\"\nB: EV=120013\n");
assert.strictEqual(enumNone.source, "none");
assert.strictEqual(enumNone.devices.length, 0);

// ─── qdwin-only: no foreign-compositor command builder ───────────────
// qdshell is qdwin-only. Global pointer live apply goes through
// qdwin_shell_v1.set_pointer_config when v28 is available; this parser module
// must still never build sway/hyprland/wlr commands. A device name reaches the
// module only as opaque parsed data; it is never turned into a command argument.
assert.strictEqual(typeof P.buildSwayInputCommands, "undefined", "sway command builder must be gone");
assert.strictEqual(typeof P.swayInputArgv, "undefined", "swayInputArgv must be gone");
const exportedFns = Object.keys(P);
exportedFns.forEach(name => {
    assert.ok(!/sway/i.test(name), "no exported helper references sway: " + name);
});

console.log("pointer-parse: all assertions passed");
