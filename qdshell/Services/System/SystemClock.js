// SystemClock — pure, side-effect-free helpers for system clock control via
// systemd-timedated (`timedatectl`). NO Process / FileView / Settings /
// Quickshell access: only string/array transforms and validation. Usable from
// both QML (import "SystemClock.js" as SystemClock) and Node
// (require("./SystemClock.js")) so the timedatectl parsing, validation and
// command building can be unit-tested headless.
//
// Four responsibilities:
//   1. parse `timedatectl show` machine-readable KEY=VALUE output into a
//      structured state object ({ timezone, ntp, ntpSynchronized, ... }).
//   2. parse `timedatectl list-timezones` into a clean array of zone names.
//   3. validate / normalize a timezone (MUST be a member of the enumerated set)
//      and a manual datetime string (strict regex + field range checks).
//   4. build the exact fully-tokenised argv arrays for set-timezone / set-ntp /
//      set-time (no shell string, so a crafted timezone or datetime can never
//      inject — every value stays a single argv token).

// ─── timedatectl show parsing ───────────────────────────────────────
// `timedatectl show` prints stable, machine-readable lines:
//   Timezone=Europe/Berlin
//   NTP=yes
//   NTPSynchronized=yes
//   LocalRTC=no
//   CanNTP=yes
//   TimeUSec=Thu 2026-05-29 12:34:56 CEST
//   ...
// We parse them into a flat map plus a few normalized convenience fields.
// Booleans in `show` output are the literal strings "yes"/"no" (older systemd
// used "true"/"false" in some fields, so accept both).
function parseShow(text) {
    var raw = {};
    var lines = String(text || "").split("\n");
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        if (line === "")
            continue;
        var eq = line.indexOf("=");
        if (eq <= 0)
            continue;
        var key = line.substring(0, eq).trim();
        var val = line.substring(eq + 1); // value may legitimately contain '='
        raw[key] = val;
    }

    return {
        "raw": raw,
        "timezone": raw.Timezone || "",
        "ntp": _parseBool(raw.NTP),
        "ntpSynchronized": _parseBool(raw.NTPSynchronized),
        "localRTC": _parseBool(raw.LocalRTC),
        // CanNTP is absent on some systemd versions; default to true so the
        // toggle is not needlessly disabled when the field is simply missing.
        "canNTP": raw.CanNTP === undefined ? true : _parseBool(raw.CanNTP),
        "timeUSec": raw.TimeUSec || ""
    };
}

function _parseBool(v) {
    if (v === undefined || v === null)
        return false;
    var s = String(v).trim().toLowerCase();
    return s === "yes" || s === "true" || s === "1" || s === "on";
}

// ─── list-timezones parsing ─────────────────────────────────────────
// `timedatectl list-timezones` prints one zone per line. We trim, drop blanks,
// and keep only well-formed zone names (defensive against any stray banner /
// pager artefact). The result is the authoritative allow-list for validation.
function parseTimezones(text) {
    var out = [];
    var lines = String(text || "").split("\n");
    for (var i = 0; i < lines.length; i++) {
        var z = lines[i].trim();
        if (z === "")
            continue;
        if (!isWellFormedTimezone(z))
            continue;
        out.push(z);
    }
    return out;
}

// A syntactically well-formed IANA timezone name: one or more "/"-separated
// components, each STARTING with an alphanumeric and otherwise made of
// [A-Za-z0-9._+-], and the whole thing not absolute and containing no "..".
// Requiring a leading alphanumeric on every component blocks option-shaped
// names like "-foo" (which timedatectl could mis-parse as a flag) — real IANA
// zone components always begin with a letter or digit, never a dash/dot/plus.
// This is a *shape* check only; membership in the enumerated list is the real
// gate (see normalizeTimezone).
function isWellFormedTimezone(z) {
    if (typeof z !== "string" || z === "")
        return false;
    if (z.length > 128)
        return false;
    if (z.indexOf("..") !== -1)
        return false;
    return /^[A-Za-z0-9][A-Za-z0-9._+-]*(\/[A-Za-z0-9][A-Za-z0-9._+-]*)*$/.test(z);
}

// ─── timezone validation / normalization ────────────────────────────
// Returns the timezone string IFF it is an exact member of `zones` (the
// enumerated list-timezones set). Otherwise returns null. This is the
// injection gate: anything not in the OS-provided list — including a crafted
// value like "America/New_York; rm -rf /" — is rejected outright. Membership
// is exact-match (no normalization beyond a trim), so no metacharacter survives.
function normalizeTimezone(tz, zones) {
    if (typeof tz !== "string")
        return null;
    var t = tz.trim();
    if (t === "")
        return null;
    if (!Array.isArray(zones))
        return null;
    // Exact membership check against the OS-enumerated allow-list.
    if (zones.indexOf(t) === -1)
        return null;
    return t;
}

// ─── datetime validation ─────────────────────────────────────────────
// Manual clock set requires "YYYY-MM-DD HH:MM:SS". We validate against a
// strict regex AND range-check each field (including day-of-month with leap
// years) so a syntactically-valid but impossible date is rejected before it
// ever reaches timedatectl. Returns the normalized string or null.
var _DATETIME_RE = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/;

function validateDateTime(s) {
    if (typeof s !== "string")
        return null;
    var m = String(s).match(_DATETIME_RE);
    if (!m)
        return null;
    var year = parseInt(m[1], 10);
    var month = parseInt(m[2], 10);
    var day = parseInt(m[3], 10);
    var hour = parseInt(m[4], 10);
    var minute = parseInt(m[5], 10);
    var second = parseInt(m[6], 10);

    // systemd accepts years roughly in this band; clamp to a sane range so a
    // typo cannot set the RTC to year 0 / 9999 and confuse downstream clocks.
    if (year < 1970 || year > 2100)
        return null;
    if (month < 1 || month > 12)
        return null;
    if (hour > 23 || minute > 59 || second > 59)
        return null;
    var dim = _daysInMonth(year, month);
    if (day < 1 || day > dim)
        return null;

    // Re-serialize from the parsed integers so the output is canonical and can
    // contain nothing but digits, dashes, spaces and colons.
    return _pad(year, 4) + "-" + _pad(month, 2) + "-" + _pad(day, 2) + " " + _pad(hour, 2) + ":" + _pad(minute, 2) + ":" + _pad(second, 2);
}

function _isLeapYear(y) {
    return (y % 4 === 0 && y % 100 !== 0) || (y % 400 === 0);
}

function _daysInMonth(y, m) {
    var d = [31, _isLeapYear(y) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    return d[m - 1];
}

function _pad(n, width) {
    var s = String(n);
    while (s.length < width)
        s = "0" + s;
    return s;
}

// ─── argv builders ────────────────────────────────────────────────────
// Each builder returns a fully-tokenised argv array (or null on rejected
// input). Every user value is its OWN array element, so shell metacharacters
// stay inert data — they are never folded into a shell command line. There is
// deliberately NO `sh -c` variant for these: the QML side runs them via
// Quickshell.execDetached(argv) / Process { command: argv }, so the argv form
// is the only interface.

// set-timezone: validated against the enumerated zone list.
function buildSetTimezoneArgv(tz, zones) {
    var t = normalizeTimezone(tz, zones);
    if (t === null)
        return null;
    return ["timedatectl", "set-timezone", t];
}

// set-ntp <bool>: the boolean is mapped to the literal "true"/"false" tokens
// timedatectl expects — never a passthrough of arbitrary text.
function buildSetNtpArgv(enabled) {
    return ["timedatectl", "set-ntp", enabled ? "true" : "false"];
}

// set-time "<datetime>": validated/normalized. Note timedatectl takes the
// datetime as a SINGLE argument (with a space inside it), which is exactly one
// argv element here — the embedded space does not split it because there is no
// shell.
function buildSetTimeArgv(s) {
    var v = validateDateTime(s);
    if (v === null)
        return null;
    return ["timedatectl", "set-time", v];
}

if (typeof module !== "undefined") {
    module.exports = {
        parseShow: parseShow,
        parseTimezones: parseTimezones,
        isWellFormedTimezone: isWellFormedTimezone,
        normalizeTimezone: normalizeTimezone,
        validateDateTime: validateDateTime,
        buildSetTimezoneArgv: buildSetTimezoneArgv,
        buildSetNtpArgv: buildSetNtpArgv,
        buildSetTimeArgv: buildSetTimeArgv
    };
}
