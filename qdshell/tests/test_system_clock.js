const assert = require("assert");
const SC = require("../Services/System/SystemClock.js");

// ─── timedatectl show parsing ───────────────────────────────────────
// Representative machine-readable output.
const show = [
    "Timezone=Europe/Berlin",
    "LocalRTC=no",
    "CanNTP=yes",
    "NTP=yes",
    "NTPSynchronized=yes",
    "TimeUSec=Thu 2026-05-29 12:34:56 CEST",
    "RTCTimeUSec=Thu 2026-05-29 10:34:56 UTC",
    ""
].join("\n");

let st = SC.parseShow(show);
assert.strictEqual(st.timezone, "Europe/Berlin");
assert.strictEqual(st.ntp, true);
assert.strictEqual(st.ntpSynchronized, true);
assert.strictEqual(st.localRTC, false);
assert.strictEqual(st.canNTP, true);
assert.strictEqual(st.timeUSec, "Thu 2026-05-29 12:34:56 CEST");
// Raw map preserves every key, including values that contain spaces.
assert.strictEqual(st.raw.RTCTimeUSec, "Thu 2026-05-29 10:34:56 UTC");

// NTP off + older true/false spelling for booleans.
st = SC.parseShow("Timezone=UTC\nNTP=no\nNTPSynchronized=false\n");
assert.strictEqual(st.timezone, "UTC");
assert.strictEqual(st.ntp, false);
assert.strictEqual(st.ntpSynchronized, false);
// CanNTP absent -> defaults to true (do not needlessly disable the toggle).
assert.strictEqual(st.canNTP, true);

// Garbage / empty input parses to a safe empty-ish state, never throws.
st = SC.parseShow("");
assert.strictEqual(st.timezone, "");
assert.strictEqual(st.ntp, false);
st = SC.parseShow(null);
assert.strictEqual(st.timezone, "");

// ─── list-timezones parsing ─────────────────────────────────────────
const tzText = [
    "UTC",
    "Africa/Abidjan",
    "America/New_York",
    "Europe/Berlin",
    "Pacific/Auckland",
    "",                          // blank dropped
    "  Asia/Tokyo  ",            // trimmed
    "Not A Zone With Spaces",    // malformed -> dropped
    "America/New_York; rm -rf /" // injection attempt -> dropped (has space/;)
].join("\n");

const zones = SC.parseTimezones(tzText);
assert.deepStrictEqual(zones, [
    "UTC",
    "Africa/Abidjan",
    "America/New_York",
    "Europe/Berlin",
    "Pacific/Auckland",
    "Asia/Tokyo"
]);
// The crafted entries never make it into the allow-list.
assert.ok(zones.indexOf("America/New_York; rm -rf /") === -1);
assert.ok(zones.indexOf("Not A Zone With Spaces") === -1);

// ─── isWellFormedTimezone shape check ───────────────────────────────
assert.ok(SC.isWellFormedTimezone("Europe/Berlin"));
assert.ok(SC.isWellFormedTimezone("UTC"));
assert.ok(SC.isWellFormedTimezone("America/Argentina/Buenos_Aires"));
assert.ok(!SC.isWellFormedTimezone(""));
assert.ok(!SC.isWellFormedTimezone("/etc/passwd"));        // leading slash
assert.ok(!SC.isWellFormedTimezone("../../etc/passwd"));   // traversal
assert.ok(!SC.isWellFormedTimezone("Europe/Berlin; reboot")); // space + ;
assert.ok(!SC.isWellFormedTimezone("$(reboot)"));

// ─── normalizeTimezone: the injection gate ──────────────────────────
// Accepts an exact member of the enumerated set.
assert.strictEqual(SC.normalizeTimezone("Europe/Berlin", zones), "Europe/Berlin");
assert.strictEqual(SC.normalizeTimezone("  UTC  ", zones), "UTC"); // trimmed
// Rejects anything not in the list — including a crafted injection string.
assert.strictEqual(SC.normalizeTimezone("America/New_York; rm -rf /", zones), null);
assert.strictEqual(SC.normalizeTimezone("Europe/London", zones), null); // not enumerated
assert.strictEqual(SC.normalizeTimezone("", zones), null);
assert.strictEqual(SC.normalizeTimezone("$(reboot)", zones), null);
assert.strictEqual(SC.normalizeTimezone("Europe/Berlin", null), null); // no list -> reject

// ─── validateDateTime ───────────────────────────────────────────────
// Valid input round-trips canonically.
assert.strictEqual(SC.validateDateTime("2026-05-29 12:34:56"), "2026-05-29 12:34:56");
assert.strictEqual(SC.validateDateTime("2024-02-29 00:00:00"), "2024-02-29 00:00:00"); // leap day
// Malformed / out-of-range / injection -> null.
assert.strictEqual(SC.validateDateTime("2026-05-29T12:34:56"), null);   // 'T' separator
assert.strictEqual(SC.validateDateTime("2026-5-9 1:2:3"), null);        // unpadded
assert.strictEqual(SC.validateDateTime("2026-13-01 00:00:00"), null);   // month 13
assert.strictEqual(SC.validateDateTime("2026-02-30 00:00:00"), null);   // Feb 30
assert.strictEqual(SC.validateDateTime("2023-02-29 00:00:00"), null);   // non-leap Feb 29
assert.strictEqual(SC.validateDateTime("2026-05-29 24:00:00"), null);   // hour 24
assert.strictEqual(SC.validateDateTime("2026-05-29 12:60:00"), null);   // minute 60
assert.strictEqual(SC.validateDateTime("1969-12-31 23:59:59"), null);   // before 1970
assert.strictEqual(SC.validateDateTime("2026-05-29 12:34:56; reboot"), null); // trailing payload
assert.strictEqual(SC.validateDateTime("$(date)"), null);
assert.strictEqual(SC.validateDateTime(""), null);
assert.strictEqual(SC.validateDateTime(null), null);

// ─── argv builders are argv arrays, never `sh -c` ───────────────────
let argv = SC.buildSetTimezoneArgv("Europe/Berlin", zones);
assert.deepStrictEqual(argv, ["timedatectl", "set-timezone", "Europe/Berlin"]);
assert.ok(Array.isArray(argv));
assert.notStrictEqual(argv[0], "sh");
assert.ok(argv.indexOf("-c") === -1, "no -c flag => not a shell string");

// A crafted timezone is REJECTED before any argv is built (returns null).
assert.strictEqual(SC.buildSetTimezoneArgv("America/New_York; rm -rf /", zones), null);
assert.strictEqual(SC.buildSetTimezoneArgv("`reboot`", zones), null);
assert.strictEqual(SC.buildSetTimezoneArgv("Europe/Berlin", []), null); // empty allow-list

// set-ntp maps the bool to literal true/false tokens (never raw text).
assert.deepStrictEqual(SC.buildSetNtpArgv(true), ["timedatectl", "set-ntp", "true"]);
assert.deepStrictEqual(SC.buildSetNtpArgv(false), ["timedatectl", "set-ntp", "false"]);

// set-time: valid datetime stays a SINGLE argv token (the embedded space does
// not split it — there is no shell).
argv = SC.buildSetTimeArgv("2026-05-29 12:34:56");
assert.deepStrictEqual(argv, ["timedatectl", "set-time", "2026-05-29 12:34:56"]);
assert.strictEqual(argv.length, 3, "datetime is exactly one argv element");
assert.notStrictEqual(argv[0], "sh");
// Malformed datetime -> null (no command built).
assert.strictEqual(SC.buildSetTimeArgv("2026-05-29 12:34:56; rm -rf /"), null);
assert.strictEqual(SC.buildSetTimeArgv("garbage"), null);

// Every built argv element is a plain string (no nested arrays / shell wrap).
[SC.buildSetTimezoneArgv("UTC", zones),
 SC.buildSetNtpArgv(true),
 SC.buildSetTimeArgv("2026-05-29 12:34:56")].forEach(function (a) {
    assert.ok(Array.isArray(a));
    a.forEach(function (tok) { assert.strictEqual(typeof tok, "string"); });
});

// ════════════════════════════════════════════════════════════════════
// EXPANDED COVERAGE
// ════════════════════════════════════════════════════════════════════

// ─── parseShow robustness: CRLF, blanks, extra/missing keys, '=' in value ─
// Note: parseShow splits on "\n"; a CRLF file leaves a trailing "\r" on each
// value. The KEY (before '=') is .trim()'d, so keys are clean; values are not
// trimmed, so a "\r" rides along — assert the key still resolves and the value
// is the raw remainder (this documents the exact contract).
{
  // NOTE: parseShow splits on "\n" and does NOT trim values, so a CRLF file
  // leaves a trailing "\r" on the structured timezone too. This documents the
  // exact (imperfect) contract — a value-with-CR is NOT laundered. Booleans ARE
  // trimmed by _parseBool, so NTP=yes\r still parses true.
  const crlf = "Timezone=Europe/Berlin\r\nNTP=yes\r\n\r\n";
  const p = SC.parseShow(crlf);
  assert.strictEqual(p.raw.Timezone, "Europe/Berlin\r", "value keeps trailing CR (not trimmed)");
  assert.strictEqual(p.timezone, "Europe/Berlin\r", "structured timezone inherits the raw CR");
  assert.strictEqual(p.ntp, true, "boolean parse trims trailing CR");
  // Crucially, even a CR-tainted timezone is laundered by normalizeTimezone,
  // which .trim()s before the exact-membership check — so the trailing CR is
  // stripped and it matches the clean enumerated name. The argv that results
  // therefore carries the CLEAN name, never the CR-tainted one.
  assert.strictEqual(SC.normalizeTimezone(p.timezone, ["Europe/Berlin"]), "Europe/Berlin",
    "CR is trimmed before membership check -> clean name used");
  assert.deepStrictEqual(SC.buildSetTimezoneArgv(p.timezone, ["Europe/Berlin"]),
    ["timedatectl", "set-timezone", "Europe/Berlin"],
    "argv carries the trimmed clean name, no CR leaks into the command token");
}
{
  // Blank lines, a key with no '=' , a leading '=' (eq<=0 dropped), extra
  // unknown keys, and a value that itself contains '='.
  const messy = [
    "",
    "Timezone=Asia/Tokyo",
    "garbage-no-equals",
    "=leadingEquals",            // eq===0 -> dropped
    "SomeFutureKey=whatever",    // unknown key preserved in raw only
    "TimeUSec=Thu 2026-05-29 12:00:00 JST=DST",  // '=' inside value
    "",
  ].join("\n");
  const p = SC.parseShow(messy);
  assert.strictEqual(p.timezone, "Asia/Tokyo");
  assert.strictEqual(p.raw["garbage-no-equals"], undefined, "line without '=' dropped");
  assert.strictEqual(p.raw[""], undefined, "leading '=' line dropped");
  assert.strictEqual(p.raw.SomeFutureKey, "whatever", "unknown key kept in raw map");
  assert.strictEqual(p.timeUSec, "Thu 2026-05-29 12:00:00 JST=DST",
    "value with embedded '=' preserved whole");
}
{
  // Entirely missing keys -> safe defaults; NTPSynchronized absent -> false,
  // CanNTP absent -> true, timezone absent -> "".
  const p = SC.parseShow("LocalRTC=yes\n");
  assert.strictEqual(p.timezone, "");
  assert.strictEqual(p.ntp, false);
  assert.strictEqual(p.ntpSynchronized, false);
  assert.strictEqual(p.localRTC, true);
  assert.strictEqual(p.canNTP, true, "CanNTP missing -> default true");
  assert.strictEqual(p.timeUSec, "");
}
// CanNTP explicitly "no" disables the toggle.
assert.strictEqual(SC.parseShow("CanNTP=no\n").canNTP, false);
// _parseBool accepts the documented spellings via the parsed booleans.
assert.strictEqual(SC.parseShow("NTP=on\n").ntp, true, "'on' is truthy");
assert.strictEqual(SC.parseShow("NTP=1\n").ntp, true, "'1' is truthy");
assert.strictEqual(SC.parseShow("NTP=YES\n").ntp, true, "case-insensitive");
assert.strictEqual(SC.parseShow("NTP=off\n").ntp, false, "'off' is falsy");
assert.strictEqual(SC.parseShow("NTP=\n").ntp, false, "empty value -> false");
// parseShow on non-string never throws.
assert.strictEqual(SC.parseShow(undefined).timezone, "");
assert.strictEqual(SC.parseShow(12345).timezone, "");

// ─── parseTimezones extra robustness (CRLF + duplicates kept) ────────
{
  const z = SC.parseTimezones("UTC\r\nEurope/Berlin\r\nUTC\r\n");
  // CRLF: each line is trimmed in parseTimezones, so CR is stripped.
  assert.deepStrictEqual(z, ["UTC", "Europe/Berlin", "UTC"],
    "CRLF trimmed; parseTimezones does NOT dedupe (mirrors OS output)");
  assert.deepStrictEqual(SC.parseTimezones(null), []);
  assert.deepStrictEqual(SC.parseTimezones(""), []);
}

// ─── isWellFormedTimezone additional shape cases ────────────────────
assert.ok(SC.isWellFormedTimezone("Etc/GMT+12"), "'+' allowed in component");
assert.ok(SC.isWellFormedTimezone("America/Argentina/La_Rioja"));
assert.ok(!SC.isWellFormedTimezone("Europe//Berlin"), "empty component rejected");
assert.ok(!SC.isWellFormedTimezone("Europe/Berlin/"), "trailing slash rejected");
assert.ok(!SC.isWellFormedTimezone("/UTC"), "leading slash rejected");
assert.ok(!SC.isWellFormedTimezone("Europe/Ber..lin"), "embedded '..' rejected");
assert.ok(!SC.isWellFormedTimezone("a/" + "b".repeat(200)), "over-128 length rejected");
assert.ok(!SC.isWellFormedTimezone(42), "non-string rejected");
assert.ok(!SC.isWellFormedTimezone(null));
// Option-shaped components are rejected: every component must START with an
// alphanumeric, so a leading dash/dot/plus (which timedatectl could mis-read as
// a flag) is refused at the shape gate — and therefore dropped by parseTimezones.
assert.ok(!SC.isWellFormedTimezone("-foo"), "leading-dash component rejected (option-injection guard)");
assert.ok(!SC.isWellFormedTimezone("Europe/-Berlin"), "leading-dash subcomponent rejected");
assert.ok(!SC.isWellFormedTimezone(".hidden"), "leading-dot component rejected");
assert.ok(!SC.isWellFormedTimezone("+zone"), "leading-plus component rejected");
assert.deepStrictEqual(SC.parseTimezones("-foo\nEurope/Berlin\n"), ["Europe/Berlin"],
    "parseTimezones drops an option-shaped zone, keeps the clean one");

// ─── normalizeTimezone: leading-dash + traversal-shaped strings ─────
// Even values that LOOK plausible are rejected unless they are exact members.
{
  // normalizeTimezone gates purely on EXACT membership (the shape check lives in
  // isWellFormedTimezone / parseTimezones, which already drop option-shaped
  // names). So a leading-dash or traversal-shaped value is rejected here simply
  // by not being in the allow-list:
  assert.strictEqual(SC.normalizeTimezone("-foo", ["Europe/Berlin"]), null,
    "leading-dash zone NOT in list -> rejected");
  assert.strictEqual(SC.normalizeTimezone("../../etc", ["Europe/Berlin"]), null,
    "traversal-shaped zone NOT in list -> rejected");
  // Membership is exact even for odd strings — but buildSetTimezoneArgv below
  // shows the real interface only ever ships enumerated, OS-blessed names.
  assert.strictEqual(SC.normalizeTimezone("America/New_York; rm -rf /",
    ["Europe/Berlin", "UTC"]), null);
  assert.strictEqual(SC.normalizeTimezone("  Europe/Berlin\t", ["Europe/Berlin"]),
    "Europe/Berlin", "surrounding whitespace trimmed before membership check");
  assert.strictEqual(SC.normalizeTimezone(42, ["UTC"]), null, "non-string rejected");
  assert.strictEqual(SC.normalizeTimezone("UTC", "UTC"), null, "non-array allow-list rejected");
}

// ─── validateDateTime additional boundary cases ─────────────────────
assert.strictEqual(SC.validateDateTime("1970-01-01 00:00:00"), "1970-01-01 00:00:00", "lower year bound");
assert.strictEqual(SC.validateDateTime("2100-12-31 23:59:59"), "2100-12-31 23:59:59", "upper year bound");
assert.strictEqual(SC.validateDateTime("2101-01-01 00:00:00"), null, "year just over bound");
assert.strictEqual(SC.validateDateTime("2026-00-01 00:00:00"), null, "month 0");
assert.strictEqual(SC.validateDateTime("2026-01-00 00:00:00"), null, "day 0");
assert.strictEqual(SC.validateDateTime("2026-01-32 00:00:00"), null, "Jan has 31 days");
assert.strictEqual(SC.validateDateTime("2026-04-31 00:00:00"), null, "April has 30 days");
assert.strictEqual(SC.validateDateTime("2000-02-29 00:00:00"), "2000-02-29 00:00:00", "2000 is leap (div 400)");
assert.strictEqual(SC.validateDateTime("1900-02-29 00:00:00"), null, "1900 not leap (div 100, in band? no -> year<1970)");
assert.strictEqual(SC.validateDateTime("2026-12-31 23:59:59"), "2026-12-31 23:59:59", "max valid fields");
assert.strictEqual(SC.validateDateTime(" 2026-05-29 12:34:56"), null, "leading space breaks anchored regex");
assert.strictEqual(SC.validateDateTime("2026-05-29 12:34:56 "), null, "trailing space breaks anchored regex");
assert.strictEqual(SC.validateDateTime("2026-05-29  12:34:56"), null, "double space rejected");
assert.strictEqual(SC.validateDateTime(42), null, "non-string rejected");

// ─── argv builders: explicit no-`sh -c`, all-string, no metachar tokens ─
{
  const zonesOk = ["Europe/Berlin", "UTC", "America/New_York"];
  // Build every kind of argv and assert the safety invariants on each.
  const builds = [
    SC.buildSetTimezoneArgv("Europe/Berlin", zonesOk),
    SC.buildSetNtpArgv(true),
    SC.buildSetNtpArgv(false),
    SC.buildSetTimeArgv("2026-05-29 12:34:56"),
  ];
  builds.forEach(function (argv, i) {
    assert.ok(Array.isArray(argv), "build #" + i + " is an array");
    // First element is the program; it is NEVER a shell.
    assert.notStrictEqual(argv[0], "sh", "build #" + i + " is not sh");
    assert.notStrictEqual(argv[0], "bash", "build #" + i + " is not bash");
    assert.strictEqual(argv.indexOf("-c"), -1, "build #" + i + " has no -c (not a shell string)");
    assert.strictEqual(argv[0], "timedatectl", "build #" + i + " invokes timedatectl directly");
    argv.forEach(function (tok) {
      assert.strictEqual(typeof tok, "string", "every token is a string");
    });
  });
  // The set-time argv carries the datetime as ONE element WITH a space inside —
  // proving the space does not split it (no shell word-splitting).
  const tArgv = SC.buildSetTimeArgv("2026-05-29 12:34:56");
  assert.strictEqual(tArgv.length, 3);
  assert.ok(tArgv[2].indexOf(" ") !== -1, "datetime element legitimately contains a space");

  // buildSetNtpArgv NEVER passes through arbitrary text — only the two literals.
  // (Truthy/falsy inputs both collapse to the canonical "true"/"false".)
  assert.deepStrictEqual(SC.buildSetNtpArgv("anything truthy"), ["timedatectl", "set-ntp", "true"]);
  assert.deepStrictEqual(SC.buildSetNtpArgv(0), ["timedatectl", "set-ntp", "false"]);
  assert.deepStrictEqual(SC.buildSetNtpArgv(""), ["timedatectl", "set-ntp", "false"]);
  // No NTP argv token ever contains the user's raw value.
  ["true", "false"].forEach(function (lit) {
    const a = SC.buildSetNtpArgv(lit === "true");
    assert.strictEqual(a[2], lit);
  });

  // CRITICAL: a crafted timezone / datetime returns null (no argv at all), so
  // there is nothing for a shell to ever see.
  assert.strictEqual(SC.buildSetTimezoneArgv("Europe/Berlin; rm -rf /", zonesOk), null);
  assert.strictEqual(SC.buildSetTimezoneArgv("$(reboot)", zonesOk), null);
  assert.strictEqual(SC.buildSetTimezoneArgv("../../etc", zonesOk), null);
  assert.strictEqual(SC.buildSetTimezoneArgv("-foo", zonesOk), null);
  assert.strictEqual(SC.buildSetTimezoneArgv("Europe/Berlin", null), null, "no allow-list -> null");
  assert.strictEqual(SC.buildSetTimeArgv("2026-05-29 12:34:56 && reboot"), null);
  assert.strictEqual(SC.buildSetTimeArgv("`date`"), null);
  assert.strictEqual(SC.buildSetTimeArgv(null), null);

  // For ANY successfully-built argv, no element contains a shell metacharacter
  // EXCEPT the single legitimate space inside the datetime token. Verify that
  // the only space lives in the set-time payload and nowhere in set-timezone.
  const meta = /[;&|`$()<>\\"'\t\n*?]/;
  [SC.buildSetTimezoneArgv("Europe/Berlin", zonesOk),
   SC.buildSetNtpArgv(true)].forEach(function (argv) {
    argv.forEach(function (tok) {
      assert.ok(!meta.test(tok) && tok.indexOf(" ") === -1,
        "no metachar / no space in token: " + tok);
    });
  });
  // set-time tokens: no shell metachar; the only space is the datetime separator.
  SC.buildSetTimeArgv("2026-05-29 12:34:56").forEach(function (tok) {
    assert.ok(!meta.test(tok), "no shell metachar in set-time token: " + tok);
  });
}

console.log("system-clock: all assertions passed");
