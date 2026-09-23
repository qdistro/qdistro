const assert = require("assert");
const Cfg = require("../Services/Hardware/PointerInputConfig.js");

// PointerInputConfig maps the persisted Settings.data.pointer policy to the
// numeric arg vector of qdwin_shell_v1.set_pointer_config (v28). qdshell only
// runs on qdwin; the compositor clamps server-side too, but these tests pin
// the canonical-value normalisation/clamping done on the shell side so the
// wire never carries garbage and the persist-only fallback round-trips.

// ─── pointerSpeed → accel milli-units (0..1 UI, 0.5 = neutral) ──────
assert.strictEqual(Cfg.speedToMilliUnits(0.5), 0, "0.5 UI → 0 (libinput neutral)");
assert.strictEqual(Cfg.speedToMilliUnits(0.0), -1000, "0.0 UI → -1000 (-1.0)");
assert.strictEqual(Cfg.speedToMilliUnits(1.0), 1000, "1.0 UI → 1000 (+1.0)");
assert.strictEqual(Cfg.speedToMilliUnits(0.75), 500, "0.75 UI → 500");
assert.strictEqual(Cfg.speedToMilliUnits(0.25), -500, "0.25 UI → -500");
// Out-of-range / garbage clamps into [-1000, 1000], non-numeric → neutral.
assert.strictEqual(Cfg.speedToMilliUnits(5), 1000, "above 1 clamps to +1000");
assert.strictEqual(Cfg.speedToMilliUnits(-3), -1000, "below 0 clamps to -1000");
assert.strictEqual(Cfg.speedToMilliUnits("abc"), 0, "non-numeric → neutral 0");
assert.strictEqual(Cfg.speedToMilliUnits(undefined), 0, "undefined → neutral 0");
assert.strictEqual(Cfg.speedToMilliUnits(NaN), 0, "NaN → neutral 0");
assert.ok(Cfg.speedToMilliUnits(0.3) >= -1000 && Cfg.speedToMilliUnits(0.3) <= 1000);

// ─── accel profile enum ─────────────────────────────────────────────
assert.strictEqual(Cfg.accelProfileEnum("adaptive"), 0);
assert.strictEqual(Cfg.accelProfileEnum("flat"), 1);
assert.strictEqual(Cfg.accelProfileEnum("FLAT"), 1, "case-insensitive");
assert.strictEqual(Cfg.accelProfileEnum("  adaptive "), 0, "trimmed");
assert.strictEqual(Cfg.accelProfileEnum("garbage"), 0, "unknown → adaptive");
assert.strictEqual(Cfg.accelProfileEnum(undefined), 0, "undefined → adaptive");
assert.strictEqual(Cfg.accelProfileEnum(null), 0, "null → adaptive");

// ─── scroll method enum ─────────────────────────────────────────────
assert.strictEqual(Cfg.scrollMethodEnum("none"), 0);
assert.strictEqual(Cfg.scrollMethodEnum("two_finger"), 1);
assert.strictEqual(Cfg.scrollMethodEnum("edge"), 2);
assert.strictEqual(Cfg.scrollMethodEnum("on_button_down"), 3);
assert.strictEqual(Cfg.scrollMethodEnum("TWO_FINGER"), 1, "case-insensitive");
assert.strictEqual(Cfg.scrollMethodEnum("garbage"), 1, "unknown → two_finger");
assert.strictEqual(Cfg.scrollMethodEnum(""), 1, "empty → two_finger");
assert.strictEqual(Cfg.scrollMethodEnum(undefined), 1, "undefined → two_finger");

// ─── full snapshot mapping + bool canonicalisation ──────────────────
var args = Cfg.toBindingArgs({
  pointerSpeed: 0.75,
  accelProfile: "flat",
  naturalScroll: true,
  scrollMethod: "edge",
  tapToClick: false,
  disableWhileTyping: true,
  leftHanded: true,
  // settings field name differs from the wire field (middle_emulation)
  middleClickEmulation: true
});
assert.strictEqual(args.accelSpeed, 500);
assert.strictEqual(args.accelProfile, 1);
assert.strictEqual(args.naturalScroll, 1, "bool → 1");
assert.strictEqual(args.tapToClick, 0, "false → 0");
assert.strictEqual(args.leftHanded, 1);
assert.strictEqual(args.middleEmulation, 1, "middleClickEmulation → middleEmulation");
assert.strictEqual(args.disableWhileTyping, 1);
assert.strictEqual(args.scrollMethod, 2);

// Empty/undefined settings object → safe neutral defaults (never throws).
var d = Cfg.toBindingArgs(undefined);
assert.strictEqual(d.accelSpeed, 0);
assert.strictEqual(d.accelProfile, 0);
assert.strictEqual(d.naturalScroll, 0);
assert.strictEqual(d.tapToClick, 0);
assert.strictEqual(d.scrollMethod, 1, "missing scrollMethod → two_finger default");

console.log("pointer-config: all assertions passed");
