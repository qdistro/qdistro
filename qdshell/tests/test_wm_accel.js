const assert = require("assert");
const A = require("../Services/Qdwin/WmAccel.js");

// WmAccel translates a human accelerator string into the (modifier
// bitmask, linux keycode) pair qdwin_shell_v1.register_hotkey expects.
// Modifier bits: ctrl=1, alt=2, super=4, shift=8. These tests pin the
// mapping, the fallback-to-null behaviour for unmappable input, and the
// injection-safety property that nothing outside the allowlist parses.

// ─── The five default WM shortcuts ──────────────────────────────────
assert.deepStrictEqual(A.parse("Alt+F4"), { modifiers: 2, key: 62 },
                       "Alt+F4 -> alt + KEY_F4");
assert.deepStrictEqual(A.parse("Super+Up"), { modifiers: 4, key: 103 },
                       "Super+Up -> super + KEY_UP");
assert.deepStrictEqual(A.parse("Super+F"), { modifiers: 4, key: 33 },
                       "Super+F -> super + KEY_F");
assert.deepStrictEqual(A.parse("Super+Left"), { modifiers: 4, key: 105 },
                       "Super+Left -> super + KEY_LEFT");
assert.deepStrictEqual(A.parse("Super+Right"), { modifiers: 4, key: 106 },
                       "Super+Right -> super + KEY_RIGHT");

// ─── Modifier combining + aliases ───────────────────────────────────
assert.deepStrictEqual(A.parse("Ctrl+Shift+Q"), { modifiers: 1 | 8, key: 16 },
                       "ctrl|shift + KEY_Q");
assert.deepStrictEqual(A.parse("Control+Alt+Delete"),
                       { modifiers: 1 | 2, key: 111 }, "control/alt aliases");
assert.strictEqual(A.parse("Meta+Tab").modifiers, 2, "meta -> alt bit");
assert.strictEqual(A.parse("Win+D").modifiers, 4, "win -> super bit");
assert.strictEqual(A.parse("Cmd+Space").modifiers, 4, "cmd -> super bit");

// ─── Case / whitespace tolerance ────────────────────────────────────
assert.deepStrictEqual(A.parse("super+left"), { modifiers: 4, key: 105 },
                       "lowercase");
assert.deepStrictEqual(A.parse("  Super+Right  "), { modifiers: 4, key: 106 },
                       "outer whitespace trimmed");

// ─── No-key / modifier-only / empty → null ──────────────────────────
assert.strictEqual(A.parse("Super"), null, "modifier-only -> null");
assert.strictEqual(A.parse("Ctrl+Alt"), null, "all-modifiers -> null");
assert.strictEqual(A.parse(""), null, "empty -> null");
assert.strictEqual(A.parse("   "), null, "whitespace -> null");
assert.strictEqual(A.parse(undefined), null, "undefined -> null");
assert.strictEqual(A.parse(null), null, "null -> null");

// ─── Unmappable key token → null (no partial combo) ─────────────────
assert.strictEqual(A.parse("Super+NoSuchKey"), null, "unknown key -> null");
assert.strictEqual(A.parse("Ctrl+F13"), null, "F13 not in table -> null");

// ─── Injection safety: anything outside [A-Za-z0-9_+-] is rejected ──
assert.strictEqual(A.parse("Alt+F4 kill; touch /tmp/pwned"), null,
                   "spaces/semicolons rejected");
assert.strictEqual(A.parse("Super+`reboot`"), null, "backticks rejected");
assert.strictEqual(A.parse("Super+$(x)"), null, "command-subst rejected");
assert.strictEqual(A.parse("Super+L\nSuper+R"), null, "newline rejected");

// ─── F-keys, digits, letters all map to plausible keycodes ──────────
assert.strictEqual(A.parse("F1").key, 59);
assert.strictEqual(A.parse("F12").key, 88);
assert.strictEqual(A.parse("1").key, 2);
assert.strictEqual(A.parse("0").key, 11);
assert.strictEqual(A.parse("a").key, 30);
assert.strictEqual(A.parse("z").key, 44);

console.log("test_wm_accel: all assertions passed");
