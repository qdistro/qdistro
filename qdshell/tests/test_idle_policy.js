const assert = require("assert");
const P = require("../Services/Power/IdlePolicy.js");

// IdlePolicy.resolveArming decides the two ext-idle-notify timeouts (ms) from
// the power policy: inactivity action (armed only with a real action) and
// display-off, with presentation mode suppressing both. Minutes -> ms.

// ─── Normal arming ──────────────────────────────────────────────────
assert.deepStrictEqual(P.resolveArming("suspend", 30, 15, false),
                       { inactivityMs: 30 * 60000, displayOffMs: 15 * 60000 },
                       "suspend@30 + displayoff@15");
assert.deepStrictEqual(P.resolveArming("hibernate", 10, 5, false),
                       { inactivityMs: 10 * 60000, displayOffMs: 5 * 60000 });

// ─── action "nothing" disarms only the inactivity slot ──────────────
assert.deepStrictEqual(P.resolveArming("nothing", 30, 15, false),
                       { inactivityMs: 0, displayOffMs: 15 * 60000 },
                       "nothing -> no inactivity arm, display-off still armed");
assert.deepStrictEqual(P.resolveArming("", 30, 15, false).inactivityMs, 0,
                       "empty action -> not armed");

// ─── presentation mode suppresses BOTH ──────────────────────────────
assert.deepStrictEqual(P.resolveArming("suspend", 30, 15, true),
                       { inactivityMs: 0, displayOffMs: 0 },
                       "presentation mode -> both off");

// ─── zero / negative / garbage timeout == never ─────────────────────
assert.strictEqual(P.resolveArming("suspend", 0, 15, false).inactivityMs, 0,
                   "0 min inactivity -> never");
assert.strictEqual(P.resolveArming("suspend", 30, 0, false).displayOffMs, 0,
                   "0 min display-off -> never");
assert.strictEqual(P.resolveArming("suspend", -5, 15, false).inactivityMs, 0,
                   "negative -> never");
assert.strictEqual(P.resolveArming("suspend", "x", 15, false).inactivityMs, 0,
                   "garbage -> never");

// ─── case-insensitive action token ──────────────────────────────────
assert.strictEqual(P.resolveArming("NOTHING", 30, 15, false).inactivityMs, 0,
                   "NOTHING (uppercase) -> not armed");
assert.ok(P.resolveArming("Suspend", 30, 15, false).inactivityMs > 0);

console.log("test_idle_policy: all assertions passed");
