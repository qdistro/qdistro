// Display layout confirm-or-revert dialog guard.
//
// At 200% on a 1280x800 output (640x400 logical) the dialog ran off-screen:
// its word-wrapped body reported the unwrapped text width and the Dialog had
// no width, so "Keep changes" was cut off and every scale change reverted
// after 15 s. Its stock Controls background was also light under the
// shell's light text (body invisible in dark mode). This guard locks the
// overlay-bounded width and the shell-coloured background/text.

"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.resolve(__dirname,
    "../Modules/Panels/Settings/Tabs/Display/LayoutSubTab.qml"), "utf8");
const dlg = src.slice(src.indexOf("id: confirmDialog"));

assert.match(dlg, /width: Math\.min\([\s\S]*?Overlay\.overlay\.width[\s\S]*?\)/,
    "confirm dialog width must be bounded by the overlay width");
assert.match(dlg, /background: Rectangle \{[\s\S]*?color: Color\.mSurface/,
    "confirm dialog must draw a shell-coloured background");
const texts = dlg.match(/NText \{[\s\S]*?\n {6}\}/g) || [];
assert.ok(texts.length >= 2, "title and body are NText");
for (const t of texts) {
    assert.match(t, /color: Color\.mOnSurface/, "dialog text must use mOnSurface");
    assert.match(t, /wrapMode: Text\.WordWrap/, "dialog text must wrap");
}
assert.ok(!/^\s*title:/m.test(dlg.split("ColumnLayout")[0]),
    "no stock Dialog title (unthemed header)");
assert.match(dlg, /display\.layout\.keep/, "Keep changes button present");

console.log("ok - display confirm dialog guard");
