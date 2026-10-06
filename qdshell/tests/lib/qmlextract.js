// Shared QML source-execution helpers for the Node gate tests.
//
// QML function bodies are plain ECMAScript, so the extracted text is
// directly compilable under Node. Extracting and EXECUTING the body from
// the .qml source means the test exercises the actual production logic —
// not a re-typed copy that can drift silently.
//
// Lexically robust for the targeted functions: the scanner runs over a
// comment/string-MASKED copy so a commented-out or quoted `function name(`
// cannot be matched, and braces inside strings/comments are never counted.
// Caveat: the masker is a pragmatic scanner, not a full JS lexer — it does
// NOT model regex literals or `${...}` template interpolation. If a future
// target uses those constructs, extend maskCommentsAndStrings first.

"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

var ROOT = path.resolve(__dirname, "..", "..");

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), "utf8");
}

// Replaces the CONTENT of //-lines, block comments, and '...' / "..." /
// `...` literals with spaces, keeping every character offset (and
// newlines) identical to the original.
function maskCommentsAndStrings(src) {
    var out = src.split("");
    var i = 0, n = src.length;
    var inLine = false, inBlock = false, inStr = false, q = "";
    while (i < n) {
        var c = src[i], c2 = i + 1 < n ? src[i + 1] : "";
        if (inLine) {
            if (c === "\n") inLine = false; else out[i] = " ";
            i++; continue;
        }
        if (inBlock) {
            if (c === "*" && c2 === "/") { out[i] = " "; out[i + 1] = " "; i += 2; inBlock = false; continue; }
            if (c !== "\n") out[i] = " ";
            i++; continue;
        }
        if (inStr) {
            if (c === "\\") { out[i] = " "; if (i + 1 < n && src[i + 1] !== "\n") out[i + 1] = " "; i += 2; continue; }
            if (c === q) { inStr = false; out[i] = " "; i++; continue; }
            if (c !== "\n") out[i] = " ";
            i++; continue;
        }
        if (c === "/" && c2 === "/") { inLine = true; out[i] = " "; i++; continue; }
        if (c === "/" && c2 === "*") { inBlock = true; out[i] = " "; out[i + 1] = " "; i += 2; continue; }
        if (c === '"' || c === "'" || c === "`") { inStr = true; q = c; out[i] = " "; i++; continue; }
        i++;
    }
    return out.join("");
}

// Extracts a whole `function name(...) { ... }` (brace-balanced) and
// asserts there is EXACTLY ONE real declaration. Returns the source
// slice, or null if not found.
function extractFunction(source, name) {
    var masked = maskCommentsAndStrings(source);
    var re = new RegExp("function\\s+" + name + "\\s*\\(", "g");
    var starts = [], m;
    while ((m = re.exec(masked)) !== null) starts.push(m.index);
    assert.strictEqual(starts.length, 1,
        "expected exactly one real declaration of function " + name +
        " in source; found " + starts.length +
        " (a stale/duplicate copy would let the guard execute the wrong body)");
    var start = starts[0];
    var paren = masked.indexOf("(", start);
    var depth = 0, i, close = -1;
    for (i = paren; i < masked.length; i++) {
        if (masked[i] === "(") depth++;
        else if (masked[i] === ")") { depth--; if (depth === 0) { close = i; break; } }
    }
    if (close === -1) return null;
    var brace = masked.indexOf("{", close);
    if (brace === -1) return null;
    depth = 0;
    for (i = brace; i < masked.length; i++) {
        if (masked[i] === "{") depth++;
        else if (masked[i] === "}") { depth--; if (depth === 0) return source.slice(start, i + 1); }
    }
    return null;
}

// Compiles a QML function into a callable, injecting the entries of `env`
// as free variables to satisfy the body's member/global references (e.g.
// `{ root: {...} }` for `root.<prop>`, `{ Tier3Apps: {...} }` for an
// imported singleton). This executes the ACTUAL QML logic, not a re-typed
// copy — so a drift in the algorithm, not just the constants, fails.
function compileQmlFunctionWith(source, name, env) {
    var text = extractFunction(source, name);
    assert.ok(text, "QML function " + name + " not found in source");
    var keys = Object.keys(env);
    var vals = keys.map(function(k) { return env[k]; });
    return Function.apply(null, keys.concat(["return (" + text + ");"]))
        .apply(null, vals);
}

// Back-compat shape for callers that only need `root` injected.
function compileQmlFunction(source, name, root) {
    return compileQmlFunctionWith(source, name, { root: root });
}

// Extracts `propertyName: "value"` / `property string foo: "value"` /
// `var propertyName = "value"`; returns the value or null.
function extractStringProp(source, propertyName) {
    var patterns = [
        new RegExp('property\\s+string\\s+' + propertyName + '\\s*:\\s*"([^"]*)"'),
        new RegExp('var\\s+' + propertyName + '\\s*=\\s*"([^"]*)"'),
    ];
    for (var i = 0; i < patterns.length; i++) {
        var m = source.match(patterns[i]);
        if (m) return m[1];
    }
    return null;
}

// Extracts the comma-separated "#rrggbb" strings from a QML/JS array
// literal whose property is named `propertyName` (e.g. siloPalette /
// SILO_PALETTE). Returns the array, or null.
function extractPalette(source, propertyName) {
    var patterns = [
        new RegExp(propertyName + '\\s*(?::\\s*|=\\s*)\\[([^\\]]+)\\]', 's'),
    ];
    for (var i = 0; i < patterns.length; i++) {
        var m = source.match(patterns[i]);
        if (m) {
            var block = m[1];
            var colours = [];
            var re = /"(#[0-9a-fA-F]{6})"/g;
            var cm;
            while ((cm = re.exec(block)) !== null) {
                colours.push(cm[1]);
            }
            return colours;
        }
    }
    return null;
}

// Extracts a `propertyName: ["a", "b", ...]` / `var propertyName = [..]`
// array of plain double-quoted strings. Returns the strings, or null.
function extractStringArray(source, propertyName) {
    var m = source.match(new RegExp(propertyName + '\\s*(?::\\s*|=\\s*)\\[([^\\]]+)\\]', 's'));
    if (!m) return null;
    var out = [];
    var re = /"([^"]*)"/g, cm;
    while ((cm = re.exec(m[1])) !== null) out.push(cm[1]);
    return out;
}

module.exports = {
    read: read,
    maskCommentsAndStrings: maskCommentsAndStrings,
    extractFunction: extractFunction,
    compileQmlFunction: compileQmlFunction,
    compileQmlFunctionWith: compileQmlFunctionWith,
    extractStringProp: extractStringProp,
    extractPalette: extractPalette,
    extractStringArray: extractStringArray,
};
