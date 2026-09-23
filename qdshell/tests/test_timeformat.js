const assert = require("assert");
const TF = require("../Services/Commons/TimeFormat.js");

// Tests for the pure time-formatting logic extracted from Commons/Time.qml.
// The live QML path requires a Qt timer and I18n singleton (VM-only);
// this pins the pure formatting rules on the host.

// ── getFormattedTimestamp: YYYYMMDD-HHMMSS shape ──
(function testGetFormattedTimestamp() {
  // Known date: 2026-06-11 at 09:05:03
  var d = new Date(2026, 5, 11, 9, 5, 3); // month is 0-based
  assert.strictEqual(TF.getFormattedTimestamp(d), "20260611-090503",
    "pads single-digit month/day/hour/min/sec with leading zero");

  // December 31, 2024 at 23:59:59
  var d2 = new Date(2024, 11, 31, 23, 59, 59);
  assert.strictEqual(TF.getFormattedTimestamp(d2), "20241231-235959");

  // January 1 at midnight
  var d3 = new Date(2000, 0, 1, 0, 0, 0);
  assert.strictEqual(TF.getFormattedTimestamp(d3), "20000101-000000",
    "midnight and January produce 00 fields");

  // null/undefined → uses current date, does not throw
  var ts = TF.getFormattedTimestamp(null);
  assert.ok(/^\d{8}-\d{6}$/.test(ts), "null arg → current date, correct shape: " + ts);

  var ts2 = TF.getFormattedTimestamp(undefined);
  assert.ok(/^\d{8}-\d{6}$/.test(ts2), "undefined arg → current date, correct shape");

  // Length is always 15: YYYYMMDD(8) + '-'(1) + HHMMSS(6)
  assert.strictEqual(TF.getFormattedTimestamp(d).length, 15, "output length is always 15");
})();

// ── formatVagueHumanReadableDuration: basic units ──
(function testFormatVague() {
  assert.strictEqual(TF.formatVagueHumanReadableDuration(0),   "0s",  "zero → 0s");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(1),   "1s",  "one second");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(59),  "59s", "59 seconds");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(60),  "1m",  "60s → 1m");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(61),  "1m",  "61s → 1m (no seconds when minutes present)");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(90),  "1m",  "90s → 1m");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(3599), "59m", "3599s → 59m");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(3600), "1h", "3600s → 1h");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(3660), "1h 1m", "3660s → 1h 1m");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(7200), "2h", "7200s → 2h");
  // NOTE: "1d 0s" is the faithful reproduction of the QML logic:
  // `if (!hours && !minutes)` is true even when days > 0, so seconds
  // always appear when both hours and minutes are zero — even with days.
  assert.strictEqual(TF.formatVagueHumanReadableDuration(86400), "1d 0s", "86400s → 1d 0s (QML faithful: seconds shown when no hours/minutes)");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(86400 + 3600), "1d 1h", "1d+1h → seconds suppressed by hours");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(86400 + 3660), "1d 1h 1m", "1d+1h+1m");
  // Seconds suppressed when hours or minutes are present
  assert.strictEqual(TF.formatVagueHumanReadableDuration(3601), "1h", "3601s → 1h (seconds suppressed)");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(125),  "2m", "125s → 2m (seconds suppressed)");
})();

// ── formatVagueHumanReadableDuration: edge cases ──
(function testFormatVagueEdge() {
  // Negative numbers → "0s" (guard for battery/uptime returning -1 temporarily)
  assert.strictEqual(TF.formatVagueHumanReadableDuration(-1), "0s", "negative → 0s");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(-3600), "0s");

  // Non-number types → "0s" (never throws)
  assert.strictEqual(TF.formatVagueHumanReadableDuration(null), "0s", "null → 0s");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(undefined), "0s", "undefined → 0s");
  assert.strictEqual(TF.formatVagueHumanReadableDuration("sixty"), "0s", "string → 0s");
  // NaN: typeof NaN === 'number' so the guard does NOT fire (faithful to QML).
  // Math.floor(NaN) === NaN; all comparisons with NaN are false so no parts
  // are pushed by if(days)/if(hours)/if(minutes); then !hours && !minutes is
  // !NaN && !NaN which is true, so parts.push(`${NaN}s`) → "NaNs".
  // This is a known QML-side quirk — document it, don't paper over it.
  // Pin the actual string so regressions (e.g. a fix to "0s") fail visibly.
  assert.strictEqual(TF.formatVagueHumanReadableDuration(NaN), "NaNs",
    "NaN input → 'NaNs' (faithful QML quirk; if QML fixes this update both)");

  // Floats are floored
  assert.strictEqual(TF.formatVagueHumanReadableDuration(59.9), "59s", "59.9 → 59s (floored)");
  assert.strictEqual(TF.formatVagueHumanReadableDuration(3600.5), "1h", "3600.5 → 1h");

  // Very large values
  var huge = TF.formatVagueHumanReadableDuration(10 * 86400 + 5 * 3600 + 30 * 60);
  assert.strictEqual(huge, "10d 5h 30m", "multi-day: 10d 5h 30m");
  // Pure days (no h/m) → shows seconds=0 per QML logic
  assert.strictEqual(TF.formatVagueHumanReadableDuration(2 * 86400), "2d 0s", "2 days exact → 2d 0s");
})();

// ── formatRelativeTime: threshold boundaries ──
(function testFormatRelative() {
  // We pass a fixed `now` so tests are deterministic and don't drift with time.
  var base = new Date("2026-06-11T12:00:00.000Z").getTime();

  function at(msAgo) {
    return new Date(base - msAgo);
  }

  // < 60s → "just now"
  assert.ok(TF.formatRelativeTime(at(0), undefined, base).indexOf("just now") !== -1, "0ms → just now");
  assert.ok(TF.formatRelativeTime(at(30000), undefined, base).indexOf("just now") !== -1, "30s → just now");
  assert.ok(TF.formatRelativeTime(at(59999), undefined, base).indexOf("just now") !== -1, "59.9s → just now");

  // 60s–119s → "1 minute ago"
  assert.ok(TF.formatRelativeTime(at(60000), undefined, base).indexOf("1 minute ago") !== -1, "60s → 1 minute ago");
  assert.ok(TF.formatRelativeTime(at(119999), undefined, base).indexOf("1 minute ago") !== -1, "119.9s → 1 minute ago");

  // 120s–3599s → "N minutes ago"
  var r120 = TF.formatRelativeTime(at(120000), undefined, base);
  assert.ok(r120.indexOf("2 minutes ago") !== -1, "120s → 2 minutes ago");
  var r300 = TF.formatRelativeTime(at(5 * 60 * 1000), undefined, base);
  assert.ok(r300.indexOf("5 minutes ago") !== -1, "5min → 5 minutes ago");

  // 3600s–7199s → "1 hour ago"
  assert.ok(TF.formatRelativeTime(at(3600000), undefined, base).indexOf("1 hour ago") !== -1, "1h → 1 hour ago");
  assert.ok(TF.formatRelativeTime(at(7199999), undefined, base).indexOf("1 hour ago") !== -1, "just under 2h → 1 hour ago");

  // 7200s–86399s → "N hours ago"
  var r7200 = TF.formatRelativeTime(at(7200000), undefined, base);
  assert.ok(r7200.indexOf("2 hours ago") !== -1, "2h → 2 hours ago");

  // 86400s–172799s → "1 day ago"
  assert.ok(TF.formatRelativeTime(at(86400000), undefined, base).indexOf("1 day ago") !== -1, "1d → 1 day ago");

  // >= 172800s → "N days ago"
  var r2d = TF.formatRelativeTime(at(172800000), undefined, base);
  assert.ok(r2d.indexOf("2 days ago") !== -1, "2d → 2 days ago");
  var r5d = TF.formatRelativeTime(at(5 * 86400000), undefined, base);
  assert.ok(r5d.indexOf("5 days ago") !== -1, "5d → 5 days ago");
})();

// ── formatRelativeTime: edge / null ──
(function testFormatRelativeEdge() {
  // null/undefined date → "" (never throws)
  assert.strictEqual(TF.formatRelativeTime(null), "", "null date → empty string");
  assert.strictEqual(TF.formatRelativeTime(undefined), "", "undefined date → empty string");
})();

// ── formatRelativeTime: custom tr function is called ──
(function testFormatRelativeCustomTr() {
  var base = new Date("2026-06-11T12:00:00.000Z").getTime();
  var called = [];
  function fakeTr(key, params) {
    called.push({ key: key, params: params });
    return "translated:" + key;
  }
  var result = TF.formatRelativeTime(new Date(base - 5 * 60 * 1000), fakeTr, base);
  assert.ok(called.length > 0, "tr function was called");
  assert.strictEqual(called[0].key, "notifications.time.diff-mm",
    "correct I18n key passed to tr for N-minutes-ago");
  assert.strictEqual(called[0].params.diff, 5, "diff param is 5");
  assert.ok(result.indexOf("translated:") !== -1, "custom tr return value used");
})();

console.log("timeformat: all assertions passed");
