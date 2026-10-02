// Fork-strip guard: upstream Noctalia's first-run setup wizard and its
// "Privacy Update" / upgrade consent modal (UpdateService + TelemetryService)
// stay stripped from qdshell.
//
// Replaces the agent scenario tests/integration/qdwin-noctalia/02-dismiss-
// privacy-modal.md. That scenario's regression target is static: the modal and
// the wizard could only come back through an upstream cherry-pick that re-adds
// their QML components or the Settings flag that opens the wizard. Its live
// half ("the bar and wallpaper are visible on a fresh launch with nothing in
// the way") is what qdwin-noctalia/01-bar-visible.md already screenshots, so
// the per-run GUI agent bought nothing over this host check.
//
// Asserts against the real tree (no mirrors):
//   1. no QML/JS file or directory named like the stripped components
//      (UpdateService, TelemetryService, TelemetryWizard, PrivacyModal,
//      SetupWizard*, Welcome* modules);
//   2. no live (non-comment) code references those components or their
//      user-visible strings ("Welcome to Noctalia", "Privacy Update",
//      "Setup Wizard");
//   3. Settings.qml keeps the wizard switch hard-wired off:
//      `readonly property bool shouldOpenSetupWizard: false`.

"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const SKIP_DIRS = new Set(["node_modules", "build", "build-qci", ".git", "tests", "Tests"]);

const NAME_RE = /^(UpdateService|TelemetryService|TelemetryWizard|PrivacyModal|PrivacyUpdate\w*|SetupWizard\w*|Welcome\w*)(\.qml|\.js)?$/i;
const CODE_RE = /\b(UpdateService|TelemetryService|TelemetryWizard|PrivacyModal|SetupWizard)\b|Welcome to Noctalia|Privacy Update|Setup Wizard/;

function walk(dir, out) {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
        if (SKIP_DIRS.has(ent.name)) continue;
        const p = path.join(dir, ent.name);
        out.push({ path: p, dir: ent.isDirectory() });
        if (ent.isDirectory()) walk(p, out);
    }
    return out;
}

// Remove // line comments and /* */ block comments, leaving string literals
// intact (so a user-visible string inside quotes is still seen). Quote state is
// tracked per LINE (QML/JS string literals here never span lines), so one odd
// regex literal containing a quote cannot desynchronise the rest of a file.
// Newlines are preserved so reported line numbers stay right.
function stripComments(src) {
    const out = [];
    let inBlock = false;
    for (const line of src.split("\n")) {
        let o = "", i = 0, q = null;
        while (i < line.length) {
            const c = line[i], n = line[i + 1];
            if (inBlock) {
                if (c === "*" && n === "/") { inBlock = false; i += 2; } else { i++; }
                continue;
            }
            if (q) {
                o += c;
                if (c === "\\") { o += n || ""; i += 2; continue; }
                if (c === q) q = null;
                i++;
                continue;
            }
            if (c === '"' || c === "'" || c === "`") { q = c; o += c; i++; continue; }
            if (c === "/" && n === "/") break;
            if (c === "/" && n === "*") { inBlock = true; i += 2; continue; }
            o += c;
            i++;
        }
        out.push(o);
    }
    return out.join("\n");
}

const entries = walk(ROOT, []);
const failures = [];

for (const e of entries) {
    const base = path.basename(e.path);
    if (NAME_RE.test(base) && (e.dir || /\.(qml|js)$/i.test(base))) {
        failures.push(`stripped upstream component is back: ${path.relative(ROOT, e.path)}`);
    }
}

let scanned = 0;
for (const e of entries) {
    if (e.dir || !/\.(qml|js)$/i.test(e.path)) continue;
    scanned++;
    const code = stripComments(fs.readFileSync(e.path, "utf8"));
    const lines = code.split("\n");
    for (let ln = 0; ln < lines.length; ln++) {
        // The inert wizard flag itself is asserted separately below.
        if (/readonly\s+property\s+bool\s+shouldOpenSetupWizard\s*:\s*false\b/.test(lines[ln])) continue;
        if (CODE_RE.test(lines[ln])) {
            failures.push(`live reference to a stripped component in ${path.relative(ROOT, e.path)}:${ln + 1}: ${lines[ln].trim()}`);
        }
    }
}
assert.ok(scanned > 100, `scanned only ${scanned} QML/JS files under ${ROOT}; walk is broken`);

const settings = fs.readFileSync(path.join(ROOT, "Commons", "Settings.qml"), "utf8");
const settingsCode = stripComments(settings);
const flagDecls = settingsCode.match(/property\s+\w+\s+shouldOpenSetupWizard\b[^\n]*/g) || [];
if (flagDecls.length !== 1 || !/^property\s+bool\s+shouldOpenSetupWizard\s*:\s*false\s*;?\s*$/.test(flagDecls[0].trim())
    || !/readonly\s+property\s+bool\s+shouldOpenSetupWizard\s*:\s*false\b/.test(settingsCode)) {
    failures.push(`Commons/Settings.qml must declare exactly 'readonly property bool shouldOpenSetupWizard: false' (found: ${JSON.stringify(flagDecls)})`);
}
if (/shouldOpenSetupWizard\s*=/.test(settingsCode.replace(/property\s+bool\s+shouldOpenSetupWizard\s*:/, ""))) {
    failures.push("Commons/Settings.qml assigns shouldOpenSetupWizard at runtime");
}

if (failures.length) {
    for (const f of failures) console.error("FAIL: " + f);
    process.exit(1);
}
console.log(`PASS: fork-strip guard (${scanned} QML/JS files; no wizard/privacy/update modal components)`);
