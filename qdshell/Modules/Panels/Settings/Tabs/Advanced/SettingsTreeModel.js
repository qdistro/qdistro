// SettingsTreeModel.js — pure logic for the Advanced raw settings editor.
//
// Dual QML/Node module: plain functions usable from both the QML tab
// (`import "SettingsTreeModel.js" as TreeModel`) and the Node test suite
// (`require(".../SettingsTreeModel.js")`). No QML/Quickshell imports here.
//
// Responsibilities:
//   - flatten a nested settings object into an ordered list of
//     {path, value, type} rows (one row per leaf, and one read-only row
//     per array/complex value);
//   - infer the editor type from a value (bool/number/string/array/object);
//   - filter rows by a case-insensitive substring on the dotted path;
//   - compute isChanged(value, defaultValue) mirroring the deep-compare
//     semantics of Settings.isValueChanged in Commons/Settings.qml.

// -----------------------------------------------------------------------
// inferType(value) -> "bool" | "number" | "string" | "array" | "object"
//
// Mirrors the QML editor-selection logic. null is treated as "string"
// (rendered as an editable empty/textual cell) so a null leaf is never
// silently dropped. undefined is also treated as "string".
function inferType(value) {
    if (typeof value === "boolean")
        return "bool";
    if (typeof value === "number")
        return "number";
    if (typeof value === "string")
        return "string";
    if (Array.isArray(value))
        return "array";
    if (value === null || value === undefined)
        return "string";
    if (typeof value === "object")
        return "object";
    // functions / symbols / bigint: treat as opaque string.
    return "string";
}

// -----------------------------------------------------------------------
// isLeaf(value) -> bool
//
// A leaf is anything that gets its own editable row: primitives, null,
// and arrays (arrays are shown as a single read-only JSON row, not
// recursed into). Plain objects are containers and are recursed into.
function isLeaf(value) {
    if (value === null || value === undefined)
        return true;
    if (Array.isArray(value))
        return true;
    return typeof value !== "object";
}

// -----------------------------------------------------------------------
// flatten(obj, [prefix]) -> [{path, value, type}, ...]
//
// Walks a nested plain object depth-first, emitting one row per leaf in a
// stable order (object key insertion order, which JSON preserves). Nested
// objects contribute their dotted path prefix (e.g. "power.lidCloseOnAC").
// Arrays and primitives are leaves; empty objects emit no row.
function flatten(obj, prefix) {
    var rows = [];
    if (obj === null || obj === undefined || typeof obj !== "object" || Array.isArray(obj))
        return rows;

    var base = prefix ? prefix + "." : "";
    var keys = Object.keys(obj);
    for (var i = 0; i < keys.length; i++) {
        var key = keys[i];
        var value = obj[key];
        var path = base + key;
        if (isLeaf(value)) {
            rows.push({ path: path, value: value, type: inferType(value) });
        } else {
            // Nested plain object: recurse. An empty object yields nothing.
            var child = flatten(value, path);
            for (var j = 0; j < child.length; j++)
                rows.push(child[j]);
        }
    }
    return rows;
}

// -----------------------------------------------------------------------
// filterRows(rows, query) -> filtered rows
//
// Case-insensitive substring match on the dotted path. An empty/blank
// query returns all rows unchanged.
function filterRows(rows, query) {
    if (!rows)
        return [];
    var q = (query === undefined || query === null) ? "" : String(query).trim().toLowerCase();
    if (q === "")
        return rows.slice();
    var out = [];
    for (var i = 0; i < rows.length; i++) {
        var p = rows[i] && rows[i].path ? String(rows[i].path).toLowerCase() : "";
        if (p.indexOf(q) !== -1)
            out.push(rows[i]);
    }
    return out;
}

// -----------------------------------------------------------------------
// isChanged(value, defaultValue) -> bool
//
// Mirrors Settings.isValueChanged's compare semantics:
//   - if defaultValue is undefined, there is nothing to compare against,
//     so the value is considered unchanged (false);
//   - objects/arrays are deep-compared via JSON.stringify;
//   - primitives are compared with strict !==.
function isChanged(value, defaultValue) {
    if (defaultValue === undefined)
        return false;
    if (typeof value === "object" && value !== null
        && typeof defaultValue === "object" && defaultValue !== null) {
        return JSON.stringify(value) !== JSON.stringify(defaultValue);
    }
    return value !== defaultValue;
}

// -----------------------------------------------------------------------
// formatValue(value, type) -> string
//
// Human-readable display string for a row value. Arrays/objects are shown
// as compact JSON (these rows are read-only in the editor).
function formatValue(value, type) {
    if (value === null || value === undefined)
        return "";
    if (type === "bool")
        return value ? "true" : "false";
    if (type === "array" || type === "object") {
        try {
            return JSON.stringify(value);
        } catch (e) {
            return String(value);
        }
    }
    return String(value);
}

// -----------------------------------------------------------------------
// parseNumber(text) -> finite number | null
//
// Strict numeric parse for the number editor: the ENTIRE trimmed string
// must be a valid finite number. Rejects partial input ("12abc"),
// thousands separators ("1,5"), and non-finite tokens ("Infinity",
// "NaN") that parseFloat()/Number() would otherwise let slip through.
function parseNumber(text) {
    if (text === null || text === undefined)
        return null;
    var s = String(text).trim();
    if (s === "")
        return null;
    // Number("") === 0 and Number(" ") === 0, already guarded above.
    var n = Number(s);
    if (typeof n !== "number" || !isFinite(n))
        return null;
    return n;
}

if (typeof module !== "undefined") {
    module.exports = {
        inferType: inferType,
        isLeaf: isLeaf,
        flatten: flatten,
        filterRows: filterRows,
        isChanged: isChanged,
        formatValue: formatValue,
        parseNumber: parseNumber,
    };
}
