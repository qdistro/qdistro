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

// Evaluate the two width bindings exactly as written, against stubbed
// Style/Overlay values: always positive, never wider than a usable overlay.
const availExpr = (dlg.match(/readonly property real overlayAvailable: (.+)\n/) || [])[1];
const widthExpr = (dlg.match(/\n\s*width: (Math\.min\(.+\))\n/) || [])[1];
assert.ok(availExpr && widthExpr, "overlayAvailable and width bindings present");
function dialogWidth(overlayWidth, uiScaleRatio) {
    const Style = { uiScaleRatio, marginL: Math.round(13 * uiScaleRatio) };
    const Overlay = { overlay: overlayWidth === null ? null : { width: overlayWidth } };
    const overlayAvailable = new Function("Style", "Overlay", `return ${availExpr};`)(Style, Overlay);
    return new Function("Style", "Overlay", "overlayAvailable", `return ${widthExpr};`)(Style, Overlay, overlayAvailable);
}
for (const [ow, scale] of [[null, 1], [0, 1], [10, 1], [26, 1], [27, 1], [640, 1], [640, 2], [1280, 1], [2000, 1.5]]) {
    const w = dialogWidth(ow, scale);
    assert.ok(w > 0, `width must be positive (overlay=${ow}, scale=${scale}) got ${w}`);
    if (ow !== null && ow > 2 * Math.round(13 * scale)) {
        assert.ok(w <= ow - 2 * Math.round(13 * scale),
            `width ${w} must fit overlay ${ow} minus margins (scale ${scale})`);
    }
}
assert.strictEqual(dialogWidth(640, 1), 480, "640-logical overlay keeps the preferred width");
assert.strictEqual(dialogWidth(400, 1), 374, "a narrow overlay bounds the width");
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

// The confirm/revert cycle must work more than once (gui scenario 02, run
// gui-20261005T071039Z-3023221: after one timed-out revert, Apply did nothing).
assert.match(src, /^import qs\.Services\.UI$/m,
    "ToastService is used, so qs.Services.UI must be imported (ReferenceError otherwise)");
// Code only: comments may quote the forbidden assignment.
const timer = src.slice(src.indexOf("id: confirmTimer"), src.indexOf("onPhaseChanged"))
    .split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
assert.match(timer, /running: root\.phase === "confirming"/, "timer runs only while confirming");
assert.ok(!/running\s*=\s*false/.test(timer),
    "assigning `running` breaks its binding; later confirms never time out");
const apply = src.slice(src.indexOf("function applyNow"), src.indexOf("function step"));
assert.match(apply, /baseSerial = Qdwin\.outputSerial;\s*if \(!Qdwin\.applyOutputLayout\(list, baseSerial\)\)/,
    "apply must use the live output serial");
const result = src.slice(src.indexOf("function onOutputLayoutResult"));
assert.match(result, /const wasReverting = root\.phase === "reverting";[\s\S]*?if \(wasReverting && root\.phase === "idle"\)\s*root\.reload\(\);/,
    "a finished revert must reload the working copy");

console.log("ok - display confirm dialog guard");
