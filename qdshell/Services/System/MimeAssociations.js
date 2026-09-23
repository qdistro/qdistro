// Pure logic for the Default Apps tab's MIME-type-level association editor.
// Dependency-free so it runs both inside QML
// (`import "MimeAssociations.js" as MimeAssociations`) and under plain Node for
// tests (`require(".../MimeAssociations.js")`).
//
// SECURITY: MIME type strings, .desktop ids and friendly descriptions all
// originate from on-disk files, package metadata and user input — treat them as
// UNTRUSTED. Any value that may reach a command line (the `xdg-mime default
// <app.desktop> <mime/type>` invocation) is first validated by
// isValidMimeType()/isValidDesktopId(). A value like "text/plain; rm -rf ~" or
// "evil.desktop;reboot" must never survive to an argv element. We additionally
// only ever build a fully-tokenized argv (no shell), so even a value that
// somehow slipped past validation would not be word-split or interpreted by a
// shell.

// ---------------------------------------------------------------------------
// Validation (the injection guard)
// ---------------------------------------------------------------------------

// A valid XDG MIME type: "type/subtype". Each part is a restricted charset of
// letters, digits and the small set of punctuation that legitimately appears in
// real registered media types (dot, plus, hyphen, underscore). Exactly one
// slash. Deliberately NARROWER than the RFC token grammar: we exclude the RFC
// token characters that are also shell metacharacters (& ! # ^ $ etc.) so that
// the documented "NO shell metacharacters" invariant holds even as defense in
// depth — no whitespace, no path separators beyond the single type/subtype
// slash, no "..".
function isValidMimeType(mime) {
    if (typeof mime !== "string")
        return false;
    if (mime.length === 0 || mime.length > 255)
        return false;
    if (mime.indexOf("..") !== -1)
        return false;
    // Exactly one slash, non-empty type and subtype, restricted charset.
    return (/^[A-Za-z0-9][A-Za-z0-9._+-]*\/[A-Za-z0-9][A-Za-z0-9._+-]*$/).test(mime);
}

// A valid XDG desktop id: a basename ending in ".desktop". Vendor-prefixed ids
// use "-" as the directory separator (e.g. "org.kde.foo.desktop") so we allow
// letters, digits, dot, underscore and hyphen. NO slash, NO whitespace, NO
// shell metacharacters, NO "..".
function isValidDesktopId(id) {
    if (typeof id !== "string")
        return false;
    if (id.length === 0 || id.length > 255)
        return false;
    if (id.indexOf("..") !== -1)
        return false;
    if (!/\.desktop$/.test(id))
        return false;
    return (/^[A-Za-z0-9][A-Za-z0-9._-]*\.desktop$/).test(id);
}

// ---------------------------------------------------------------------------
// .desktop MimeType= parsing
// ---------------------------------------------------------------------------

// Parse a `MimeType=` field value (semicolon-separated, possibly trailing ';')
// into a de-duplicated, order-preserving list of MIME type strings. Invalid /
// malformed entries are dropped so they can never become selectable values.
function parseMimeTypeField(value) {
    if (typeof value !== "string")
        return [];
    var parts = value.split(";");
    var out = [];
    var seen = {};
    for (var i = 0; i < parts.length; i++) {
        var m = parts[i].trim();
        if (m === "")
            continue;
        if (!isValidMimeType(m))
            continue;
        if (seen[m])
            continue;
        seen[m] = true;
        out.push(m);
    }
    return out;
}

// ---------------------------------------------------------------------------
// mimeapps.list parse / merge
// ---------------------------------------------------------------------------

// Parse a mimeapps.list body into a structured object:
//   { sections: { name: { keys: {mime:value}, order: [mime,...] }, ... },
//     order: [sectionName,...] }
// Values are kept verbatim (semicolon-separated id lists). Only the section
// header and key=value lines are modeled; comments/blank lines are not
// preserved (the writer re-emits a canonical body).
function parseMimeappsList(text) {
    var lines = (text || "").split("\n");
    var sections = {};
    var order = [];
    var current = null;
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i];
        var trimmed = line.trim();
        if (trimmed === "" || trimmed.charAt(0) === "#")
            continue;
        var sm = trimmed.match(/^\[(.+)\]$/);
        if (sm) {
            current = sm[1];
            if (!(current in sections)) {
                sections[current] = { keys: {}, order: [] };
                order.push(current);
            }
            continue;
        }
        if (current === null)
            continue;
        var eq = line.indexOf("=");
        if (eq === -1)
            continue;
        var key = line.slice(0, eq).trim();
        var val = line.slice(eq + 1).trim();
        if (key === "")
            continue;
        if (!(key in sections[current].keys))
            sections[current].order.push(key);
        sections[current].keys[key] = val;
    }
    return { sections: sections, order: order };
}

// Return the first valid desktop id from a (possibly semicolon-separated)
// mimeapps.list value, or "" if none is valid.
function firstValidDesktopId(value) {
    if (typeof value !== "string")
        return "";
    var parts = value.split(";");
    for (var i = 0; i < parts.length; i++) {
        var id = parts[i].trim();
        if (id !== "" && isValidDesktopId(id))
            return id;
    }
    return "";
}

// Build a map of mime -> desktopId from a parsed mimeapps.list's
// [Default Applications] section (taking the first valid id of each value).
function defaultApplications(parsed) {
    var out = {};
    if (!parsed || !parsed.sections)
        return out;
    var sec = parsed.sections["Default Applications"];
    if (!sec)
        return out;
    for (var i = 0; i < sec.order.length; i++) {
        var mime = sec.order[i];
        if (!isValidMimeType(mime))
            continue; // drop malformed/untrusted MIME keys
        var id = firstValidDesktopId(sec.keys[mime]);
        if (id !== "")
            out[mime] = id;
    }
    return out;
}

// Merge user defaults over system defaults to compute the effective resolved
// default for each MIME type. Both arguments are mime->desktopId maps. The user
// map wins. Returns a fresh merged map.
function mergeDefaults(systemDefaults, userDefaults) {
    var out = {};
    var k;
    systemDefaults = systemDefaults || {};
    userDefaults = userDefaults || {};
    for (k in systemDefaults)
        if (Object.prototype.hasOwnProperty.call(systemDefaults, k))
            out[k] = systemDefaults[k];
    for (k in userDefaults)
        if (Object.prototype.hasOwnProperty.call(userDefaults, k))
            out[k] = userDefaults[k];
    return out;
}

// Apply a single set/clear of a MIME default to a parsed mimeapps.list object,
// scoping the change to the [Default Applications] section ONLY (other sections
// such as [Added Associations] / [Removed Associations] are left untouched).
// `desktopId` of "" / null clears the entry. Mutates and returns `parsed`.
// Throws on invalid input so a bad value never reaches disk.
function applyDefault(parsed, mime, desktopId) {
    if (!isValidMimeType(mime))
        throw new Error("invalid MIME type: " + mime);
    if (desktopId && !isValidDesktopId(desktopId))
        throw new Error("invalid desktop id: " + desktopId);
    parsed = parsed || { sections: {}, order: [] };
    var SEC = "Default Applications";
    if (!(SEC in parsed.sections)) {
        parsed.sections[SEC] = { keys: {}, order: [] };
        parsed.order.push(SEC);
    }
    var sec = parsed.sections[SEC];
    if (desktopId) {
        if (!(mime in sec.keys))
            sec.order.push(mime);
        sec.keys[mime] = desktopId;
    } else {
        if (mime in sec.keys) {
            delete sec.keys[mime];
            var idx = sec.order.indexOf(mime);
            if (idx !== -1)
                sec.order.splice(idx, 1);
        }
    }
    return parsed;
}

// Serialize a parsed mimeapps.list object back to a canonical body
// (newline-terminated). Section order and key order are preserved.
function serializeMimeappsList(parsed) {
    if (!parsed || !parsed.sections)
        return "";
    var out = [];
    for (var i = 0; i < parsed.order.length; i++) {
        var name = parsed.order[i];
        var sec = parsed.sections[name];
        if (!sec)
            continue;
        out.push("[" + name + "]");
        for (var j = 0; j < sec.order.length; j++) {
            var key = sec.order[j];
            out.push(key + "=" + sec.keys[key]);
        }
        out.push("");
    }
    return out.join("\n");
}

// ---------------------------------------------------------------------------
// MIME type catalog: build + search
// ---------------------------------------------------------------------------

// Build a sorted, de-duplicated catalog of MIME types from:
//   - `desktopEntries`: map of desktopId -> { name, mimeTypes:[...] } (installed
//     apps' declared support).
//   - `extraTypes`: optional flat list of additional MIME strings (e.g. from
//     /usr/share/mime), each validated.
//   - `descriptions`: optional map of mime -> friendly description string.
// Returns an array of { mime, description, handlers: [desktopId,...] } sorted by
// mime. Only valid MIME types are included. `handlers` is the list of installed
// apps that declare support for the type (sorted, de-duplicated desktop ids).
function buildMimeCatalog(desktopEntries, extraTypes, descriptions) {
    desktopEntries = desktopEntries || {};
    descriptions = descriptions || {};
    var handlersByMime = {};
    var allMimes = {};

    var ids = Object.keys(desktopEntries);
    for (var i = 0; i < ids.length; i++) {
        var id = ids[i];
        if (!isValidDesktopId(id))
            continue;
        var entry = desktopEntries[id] || {};
        var mimes = entry.mimeTypes || [];
        for (var j = 0; j < mimes.length; j++) {
            var mime = mimes[j];
            if (!isValidMimeType(mime))
                continue;
            allMimes[mime] = true;
            if (!handlersByMime[mime])
                handlersByMime[mime] = {};
            handlersByMime[mime][id] = true;
        }
    }

    if (extraTypes) {
        for (var k = 0; k < extraTypes.length; k++) {
            var t = extraTypes[k];
            if (isValidMimeType(t))
                allMimes[t] = true;
        }
    }

    var mimeList = Object.keys(allMimes);
    mimeList.sort(function(a, b) {
        return a < b ? -1 : (a > b ? 1 : 0);
    });

    var catalog = [];
    for (var m = 0; m < mimeList.length; m++) {
        var mt = mimeList[m];
        var handlers = handlersByMime[mt] ? Object.keys(handlersByMime[mt]) : [];
        handlers.sort(function(a, b) {
            return a < b ? -1 : (a > b ? 1 : 0);
        });
        catalog.push({
            mime: mt,
            description: descriptions[mt] || "",
            handlers: handlers
        });
    }
    return catalog;
}

// Filter a catalog (from buildMimeCatalog) by a free-text query, matching the
// MIME string or its friendly description (case-insensitive substring). An
// empty/whitespace query returns the whole catalog. Order is preserved.
function searchMimeCatalog(catalog, query) {
    catalog = catalog || [];
    var q = (typeof query === "string" ? query : "").trim().toLowerCase();
    if (q === "")
        return catalog.slice();
    var out = [];
    for (var i = 0; i < catalog.length; i++) {
        var item = catalog[i];
        var mime = (item.mime || "").toLowerCase();
        var desc = (item.description || "").toLowerCase();
        if (mime.indexOf(q) !== -1 || desc.indexOf(q) !== -1)
            out.push(item);
    }
    return out;
}

// ---------------------------------------------------------------------------
// SAFE argv builder for `xdg-mime default <app.desktop> <mime/type>`
// ---------------------------------------------------------------------------

// Build the fully-tokenized argv to set a MIME default. Returns null (NOT a
// shell string) if either value is invalid — the caller must treat null as
// "refuse to run". Because this is an argv array passed straight to
// execDetached (no shell), the values are never word-split or interpreted; the
// validation above is a second, defense-in-depth layer.
function buildXdgMimeDefaultArgv(desktopId, mime) {
    if (!isValidDesktopId(desktopId) || !isValidMimeType(mime))
        return null;
    return ["xdg-mime", "default", desktopId, mime];
}

if (typeof module !== "undefined") {
    module.exports = {
        isValidMimeType: isValidMimeType,
        isValidDesktopId: isValidDesktopId,
        parseMimeTypeField: parseMimeTypeField,
        parseMimeappsList: parseMimeappsList,
        firstValidDesktopId: firstValidDesktopId,
        defaultApplications: defaultApplications,
        mergeDefaults: mergeDefaults,
        applyDefault: applyDefault,
        serializeMimeappsList: serializeMimeappsList,
        buildMimeCatalog: buildMimeCatalog,
        searchMimeCatalog: searchMimeCatalog,
        buildXdgMimeDefaultArgv: buildXdgMimeDefaultArgv
    };
}
