// Cross-repo drift guard — asserts qdshell's hand-mirrored qdistro identifiers
// (tier secctx prefixes, the AdminBroker1/SessionManager1 D-Bus names + object
// paths, the broker reply fields we parse, and the QDISTRO_SILO env name) still
// match qdistro's authoritative contract fixture.
//
// WHY THIS EXISTS: qdshell re-types these constants in QML/JS because it cannot
// import qdistro's Python/C. If qdistro renames a tier prefix or a bus name and
// qdshell isn't updated, nothing fails loudly — the shell just silently mislabels
// silos or talks to a dead bus name. The tier prefixes in particular are a
// SECURITY boundary (silo identity), so a mismatch must fail CI.
//
// Source of truth: ../qdistro/tests/contracts/qdistro_shell_contract.json, which
// qdistro's own tests/unit/test_shell_contract.py pins to its Python constants.
//
// This is the cross-repo complement to tests/test_drift_guard.js (which keeps the
// JS mirrors in sync with the QML *within* qdshell).
//
// If the sibling qdistro checkout is absent (qdshell unit lane run standalone),
// this guard SKIPS LOUDLY rather than failing — the integrated qci/bats path is
// where the sibling layout is guaranteed.

"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const QDISTRO_DIR = process.env.QDISTRO_DIR
    ? path.resolve(process.env.QDISTRO_DIR)
    : path.resolve(ROOT, "..", "qdistro");
const CONTRACT = path.join(QDISTRO_DIR, "tests", "contracts", "qdistro_shell_contract.json");

if (!fs.existsSync(CONTRACT)) {
    console.log("[SKIP] qdistro-contract-drift: sibling contract not found at " +
        CONTRACT + " (set QDISTRO_DIR or check out qdistro as a sibling). " +
        "The integrated qci/bats lane guarantees this layout.");
    process.exit(0);
}

// Strip human-facing _README/_doc keys, mirroring the Python guard.
function stripDoc(obj) {
    if (Array.isArray(obj)) return obj.map(stripDoc);
    if (obj && typeof obj === "object") {
        const out = {};
        for (const k of Object.keys(obj)) {
            if (k.startsWith("_")) continue;
            out[k] = stripDoc(obj[k]);
        }
        return out;
    }
    return obj;
}

const contract = stripDoc(JSON.parse(fs.readFileSync(CONTRACT, "utf8")));

// Remove //-line and /* */ block comments while PRESERVING string-literal
// contents, so neither literalFor nor the presence checks can be fooled by a
// stale value left in a comment (e.g. App1Apps.qml documents old bus names in
// prose). A proper scanner is needed because `//` / `/*` can appear inside a
// string literal.
function stripComments(src) {
    let out = "";
    let i = 0;
    const n = src.length;
    let inStr = false, quote = "";
    while (i < n) {
        const c = src[i], c2 = i + 1 < n ? src[i + 1] : "";
        if (inStr) {
            out += c;
            if (c === "\\") { if (i + 1 < n) out += c2; i += 2; continue; }
            if (c === quote) inStr = false;
            i++; continue;
        }
        if (c === '"' || c === "'" || c === "`") { inStr = true; quote = c; out += c; i++; continue; }
        if (c === "/" && c2 === "/") { while (i < n && src[i] !== "\n") i++; continue; }
        if (c === "/" && c2 === "*") {
            i += 2;
            while (i < n && !(src[i] === "*" && src[i + 1] === "/")) i++;
            i += 2; continue;
        }
        out += c; i++;
    }
    return out;
}

function read(rel) {
    // All checks run against comment-free (string-preserving) source.
    return stripComments(fs.readFileSync(path.join(ROOT, rel), "utf8"));
}

// Extract the value of a `key: "value"` (QML readonly property) or
// `key = "value"` (JS var/const) assignment, anchored so it cannot match a
// substring of a longer identifier. Returns the string or throws.
function literalFor(src, key) {
    const m = src.match(new RegExp('(?:^|[^\\w.])' + key + '\\s*[:=]\\s*"([^"]*)"'));
    assert.ok(m, `could not find a string assignment for ${key}`);
    return m[1];
}

let checks = 0;
function eq(actual, expected, what) {
    assert.strictEqual(actual, expected, `${what}: qdshell has ${JSON.stringify(actual)}, ` +
        `contract expects ${JSON.stringify(expected)}`);
    checks++;
}
function present(src, needle, what) {
    assert.ok(src.indexOf(needle) !== -1, `${what}: expected literal ${JSON.stringify(needle)} ` +
        `to appear in qdshell source but it does not`);
    checks++;
}

// ── 1. tier/secctx prefixes (security boundary) ──────────────────────────────
const px = contract.secctx_prefixes;
const tier3 = read("Services/Qdistro/Tier3Apps.qml");
const tier4 = read("Services/Qdistro/Tier4Apps.qml");
const tier5 = read("Services/Qdistro/VMApps.qml");
const silo = read("Services/Qdistro/SiloChrome.js");
const taskbar = read("Modules/Bar/Widgets/TaskbarLogic.js");

eq(literalFor(tier3, "tier3Prefix"), px.tier3, "Tier3Apps.qml tier3Prefix");
eq(literalFor(tier4, "tier4Prefix"), px.tier4, "Tier4Apps.qml tier4Prefix");
eq(literalFor(tier5, "tier5Prefix"), px.tier5, "VMApps.qml tier5Prefix");
eq(literalFor(silo, "TIER3_PREFIX"), px.tier3, "SiloChrome.js TIER3_PREFIX");
eq(literalFor(silo, "TIER4_PREFIX"), px.tier4, "SiloChrome.js TIER4_PREFIX");
// disp prefix is used inline in TaskbarLogic.js (id.indexOf("qdistro.disp."))
present(taskbar, '"' + px.disp + '"', "TaskbarLogic.js disp prefix");

// ── 2. D-Bus bus names + object paths ────────────────────────────────────────
const app1 = read("Services/Qdistro/App1Apps.qml");
eq(literalFor(app1, "_brokerName"), contract.dbus.admin_broker.bus_name, "App1Apps.qml _brokerName");
eq(literalFor(app1, "_sessionName"), contract.dbus.session_manager.bus_name, "App1Apps.qml _sessionName");
present(app1, contract.dbus.admin_broker.object_path, "App1Apps.qml AdminBroker1 object path");
present(app1, contract.dbus.session_manager.object_path, "App1Apps.qml SessionManager1 object path");

// ── 3. broker reply fields qdshell parses ────────────────────────────────────
// ListRules: PermissionsLogic.js reads each field as r.<field>.
const perms = read("Modules/Bar/Widgets/PermissionsLogic.js");
for (const field of contract.broker_reply_fields.ListRules) {
    present(perms, "r." + field, `PermissionsLogic.js ListRules field '${field}'`);
}
// ListReceivers: App1Apps.qml unpacks the (iss) tuple positionally — assert the
// parser still reads all three positions (drift in the tuple shape breaks this).
const recv = contract.broker_reply_fields.ListReceivers;
for (let i = 0; i < recv.tuple.length; i++) {
    present(app1, "r[" + i + "]", `App1Apps.qml ListReceivers tuple position ${i} (${recv.tuple[i]})`);
}
// ListSilos: App1Apps.qml reads row.<field> off each parsed silo object.
for (const field of contract.broker_reply_fields.ListSilos.fields) {
    present(app1, "row." + field, `App1Apps.qml ListSilos field '${field}'`);
}

// ── 4. env var name ──────────────────────────────────────────────────────────
present(app1, contract.env.silo, "App1Apps.qml QDISTRO_SILO env name");

console.log(`qdistro-contract-drift: OK (${checks} checks against ${path.relative(ROOT, CONTRACT)})`);
