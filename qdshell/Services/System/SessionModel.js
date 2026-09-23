// SessionModel — pure logic for the session save/restore feature.
//
// Dual QML/Node module (see ClipboardSilo.js for the pattern): every
// function here is side-effect free and reused both from
// SessionService.qml (live) and tests/test_session_model.js (node).
//
// SECURITY: appId / title / command strings sourced from the running
// window list (Qdwin.windows) are UNTRUSTED. Functions that build a
// launch path return an argv ARRAY (never an interpolated shell
// string). When a shell is unavoidable the caller must single-quote
// every token with quoteShellArg(); buildLaunchArgv() returns the safe
// argv directly so the common path never touches a shell.

// Maximum lengths to keep persisted snapshots bounded and the UI sane.
var MAX_NAME_LEN = 64;
var MAX_APPS_PER_SESSION = 256;

// Trim + collapse internal whitespace for a session name. Returns "".
function _cleanName(name) {
    if (name === undefined || name === null)
        return "";
    return String(name).replace(/\s+/g, " ").trim();
}

// Validate a proposed session name against an existing list of saved
// sessions. Returns { ok: bool, reason: string, name: string } where
// `name` is the cleaned form. `existing` is an array of session objects
// (each with a `.name`); when `selfName` is provided, a collision with
// that exact name is allowed (rename-to-self / overwrite-self).
function validateName(name, existing, selfName) {
    var clean = _cleanName(name);
    if (clean.length === 0)
        return { ok: false, reason: "empty", name: "" };
    if (clean.length > MAX_NAME_LEN)
        return { ok: false, reason: "too-long", name: clean };
    var list = existing || [];
    for (var i = 0; i < list.length; i++) {
        var e = list[i];
        var en = e ? _cleanName(e.name) : "";
        if (en.length === 0)
            continue;
        if (en.toLowerCase() === clean.toLowerCase()) {
            if (selfName !== undefined && selfName !== null
                    && _cleanName(selfName).toLowerCase() === clean.toLowerCase())
                continue; // overwriting self is fine
            return { ok: false, reason: "duplicate", name: clean };
        }
    }
    return { ok: true, reason: "", name: clean };
}

// Derive a launch command for a single window row. qdwin only exposes
// appId/title (no exec line), so the best stable launch key is the
// appId — typically a desktop-file id (e.g. "org.gnome.Calculator").
// Returns "" when nothing usable is present (the row is then skipped).
function commandForWindow(win) {
    if (!win || typeof win !== "object")
        return "";
    var appId = (win.appId === undefined || win.appId === null) ? "" : String(win.appId);
    appId = appId.trim();
    return appId;
}

// Build a single saved-app entry from a window row. Returns null when
// the row carries no usable launch key. The entry keeps both a launch
// `command` (the appId/desktop id) and a human `title` for display.
function appEntryFromWindow(win) {
    var cmd = commandForWindow(win);
    if (cmd.length === 0)
        return null;
    var title = (win && win.title !== undefined && win.title !== null) ? String(win.title) : "";
    return { command: cmd, appId: cmd, title: title };
}

// Build a snapshot's app list from an array of window rows. De-dupes by
// command (one launch entry per distinct app), preserving first-seen
// order, and caps the count. Returns an array of app entries.
function appsFromWindows(windows) {
    var out = [];
    var seen = Object.create(null); // null-proto: "__proto__" is a real key
    var list = windows || [];
    for (var i = 0; i < list.length && out.length < MAX_APPS_PER_SESSION; i++) {
        var entry = appEntryFromWindow(list[i]);
        if (!entry)
            continue;
        var key = entry.command;
        if (Object.prototype.hasOwnProperty.call(seen, key))
            continue;
        seen[key] = true;
        out.push(entry);
    }
    return out;
}

// Build a complete session snapshot object from a name + window list.
// `now` is an injectable timestamp (ms) for deterministic tests; when
// omitted Date.now() is used. Returns null when the name is invalid.
function buildSnapshot(name, windows, existing, now, selfName) {
    var v = validateName(name, existing, selfName);
    if (!v.ok)
        return null;
    var ts = (typeof now === "number") ? now : Date.now();
    return {
        name: v.name,
        created: ts,
        apps: appsFromWindows(windows)
    };
}

// Insert-or-replace a snapshot into a saved-session list (replace when a
// case-insensitive name match exists). Returns a NEW array (never
// mutates the input). Caller persists the result.
function upsertSession(sessions, snapshot) {
    var list = (sessions || []).slice();
    if (!snapshot || !snapshot.name)
        return list;
    var lname = String(snapshot.name).toLowerCase();
    for (var i = 0; i < list.length; i++) {
        var n = (list[i] && list[i].name) ? String(list[i].name).toLowerCase() : "";
        if (n === lname) {
            list[i] = snapshot;
            return list;
        }
    }
    list.push(snapshot);
    return list;
}

// Remove a saved session by (case-insensitive) name. Returns a NEW array.
function removeSession(sessions, name) {
    var clean = _cleanName(name).toLowerCase();
    var out = [];
    var list = sessions || [];
    for (var i = 0; i < list.length; i++) {
        var n = (list[i] && list[i].name) ? _cleanName(list[i].name).toLowerCase() : "";
        if (n === clean)
            continue;
        out.push(list[i]);
    }
    return out;
}

// Find a saved session by (case-insensitive) name. Returns the object or null.
function findSession(sessions, name) {
    var clean = _cleanName(name).toLowerCase();
    var list = sessions || [];
    for (var i = 0; i < list.length; i++) {
        var n = (list[i] && list[i].name) ? _cleanName(list[i].name).toLowerCase() : "";
        if (n === clean)
            return list[i];
    }
    return null;
}

// Serialize a saved-session list to a JSON string. Normalizes each entry
// so persisted data is well-formed regardless of input shape.
function serializeSessions(sessions) {
    return JSON.stringify(_normalizeSessions(sessions));
}

// Parse a JSON string back into a saved-session list. Tolerant of
// malformed input — returns [] rather than throwing.
function deserializeSessions(text) {
    if (text === undefined || text === null)
        return [];
    var parsed;
    try {
        parsed = (typeof text === "string") ? JSON.parse(text) : text;
    } catch (e) {
        return [];
    }
    return _normalizeSessions(parsed);
}

function _normalizeSessions(sessions) {
    var out = [];
    var seenNames = Object.create(null); // null-proto: "__proto__" safe
    if (!sessions || typeof sessions.length !== "number")
        return out;
    for (var i = 0; i < sessions.length; i++) {
        var s = sessions[i];
        if (!s || typeof s !== "object")
            continue;
        var name = _cleanName(s.name);
        if (name.length === 0 || name.length > MAX_NAME_LEN)
            continue;
        // Enforce unique session names (case-insensitive), matching
        // save-time behaviour. A duplicate from hand-edited JSON keeps
        // the FIRST occurrence and drops the rest so findSession /
        // upsertSession / delete all act on a single stable entry.
        var lname = name.toLowerCase();
        if (Object.prototype.hasOwnProperty.call(seenNames, lname))
            continue;
        seenNames[lname] = true;
        var apps = [];
        var seenCmds = Object.create(null); // null-proto: "__proto__" safe
        var rawApps = (s.apps && typeof s.apps.length === "number") ? s.apps : [];
        for (var j = 0; j < rawApps.length && apps.length < MAX_APPS_PER_SESSION; j++) {
            var a = rawApps[j];
            if (!a || typeof a !== "object")
                continue;
            var cmd = (a.command === undefined || a.command === null) ? "" : String(a.command).trim();
            if (cmd.length === 0)
                continue;
            // De-dupe app commands within a session (matches build-time).
            if (Object.prototype.hasOwnProperty.call(seenCmds, cmd))
                continue;
            seenCmds[cmd] = true;
            apps.push({
                command: cmd,
                appId: (a.appId === undefined || a.appId === null) ? cmd : String(a.appId),
                title: (a.title === undefined || a.title === null) ? "" : String(a.title)
            });
        }
        out.push({
            name: name,
            created: (typeof s.created === "number") ? s.created : 0,
            apps: apps
        });
    }
    return out;
}

// Shell-safe single-quote escaping (mirrors AutostartService.qml _q()).
// Only needed when a shell is unavoidable; the launch path below avoids
// it by returning an argv array.
function quoteShellArg(s) {
    return "'" + String(s).replace(/'/g, "'\\''") + "'";
}

// True iff `cmd` is a plausible freedesktop application id: a leading
// alphanumeric followed by id-safe characters only. No whitespace, no
// slash, no shell metacharacters, no path traversal — so it can never be
// an absolute/relative path nor a shell fragment.
function isDesktopId(cmd) {
    return typeof cmd === "string"
        && /^[A-Za-z0-9][A-Za-z0-9._+-]*$/.test(cmd);
}

// Build a SAFE launch argv for a saved app. The command originates from
// the UNTRUSTED `Qdwin.windows.appId`, so we launch EXCLUSIVELY through
// the freedesktop launcher `gtk-launch <app-id>` and ONLY when the
// command is a valid desktop-application id (isDesktopId). The id is a
// distinct argv element — never concatenated into a shell string — so
// embedded metacharacters cannot inject.
//
// We deliberately do NOT have a "treat the command as a raw executable
// path" fallback: a hostile client could set its appId to an absolute
// path (e.g. "/tmp/payload") which restore would then exec directly.
// Anything that is not a clean desktop id returns null and is skipped by
// the caller. (No shell is ever spawned on this path.)
// Returns a 2-element argv array, or null when the command is empty or
// not a valid desktop id.
function buildLaunchArgv(app) {
    var cmd = "";
    if (typeof app === "string")
        cmd = app;
    else if (app && typeof app === "object" && app.command !== undefined && app.command !== null)
        cmd = String(app.command);
    cmd = cmd.trim();
    if (cmd.length === 0)
        return null;
    if (!isDesktopId(cmd))
        return null;
    return ["gtk-launch", cmd];
}

// Returns true iff the given argv (as produced by buildLaunchArgv) is
// shell-safe: every element is a plain string and the array form means
// no shell is interpreting it. Provided for tests / defensive callers.
function isSafeArgv(argv) {
    if (!argv || typeof argv.length !== "number" || argv.length === 0)
        return false;
    for (var i = 0; i < argv.length; i++) {
        if (typeof argv[i] !== "string")
            return false;
    }
    return true;
}

if (typeof module !== "undefined") {
    module.exports = {
        MAX_NAME_LEN: MAX_NAME_LEN,
        MAX_APPS_PER_SESSION: MAX_APPS_PER_SESSION,
        validateName: validateName,
        commandForWindow: commandForWindow,
        appEntryFromWindow: appEntryFromWindow,
        appsFromWindows: appsFromWindows,
        buildSnapshot: buildSnapshot,
        upsertSession: upsertSession,
        removeSession: removeSession,
        findSession: findSession,
        serializeSessions: serializeSessions,
        deserializeSessions: deserializeSessions,
        quoteShellArg: quoteShellArg,
        isDesktopId: isDesktopId,
        buildLaunchArgv: buildLaunchArgv,
        isSafeArgv: isSafeArgv,
    };
}
