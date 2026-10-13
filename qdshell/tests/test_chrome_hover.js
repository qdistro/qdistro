// Frame hover + frame cursors (qdwin_shell_v1 v37).
//
// The shell never sees wl_pointer enter/motion on its own chrome
// surfaces, so qdwin reports chrome_motion / chrome_leave and the shell
// answers with set_chrome_cursor. Pins:
//  - the binding binds v37, dispatches both events as signals and gates
//    setChromeCursor on a v37 binding (a lower compositor would reject
//    the opcode and drop the shell's connection),
//  - the title-bar raster takes the hovered button index,
//  - Qdwin.qml's hover policy: resize cursors exactly where
//    _frameResizeEdges grips (none when maximised), the default arrow
//    elsewhere, a button highlight only over a button, and requests /
//    repaints only on change (motion is per pointer event),
//  - hover state is dropped on leave and on toplevel removal.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const binding = fs.readFileSync(
    path.join(__dirname, "..", "qml-plugin", "qdwin-binding.cpp"), "utf8");
const header = fs.readFileSync(
    path.join(__dirname, "..", "qml-plugin", "qdwin-binding.h"), "utf8");
const qml = fs.readFileSync(
    path.join(__dirname, "..", "Services", "Qdwin", "Qdwin.qml"), "utf8");

// --- binding plumbing ---------------------------------------------------
{
    assert.ok(/constexpr uint32_t kBindVersion = 37;/.test(binding),
        "the binding must bind qdwin_shell_v1 v37");
    assert.ok(/\.chrome_motion\s*= QdwinBindingDispatch::chrome_motion/.test(binding)
              && /\.chrome_leave\s*= QdwinBindingDispatch::chrome_leave/.test(binding),
        "both v37 events need listener entries");
    assert.ok(/emit b->chromeMotion\(handle, side,/.test(binding),
        "chrome_motion must reach QML with the wire tuple");
    assert.ok(/->chromeLeave\(handle\)/.test(binding),
        "chrome_leave must reach QML");
    const set = binding.match(
        /void QdwinBinding::setChromeCursor[^{]*\{([\s\S]*?)\n\}/);
    assert.ok(set, "setChromeCursor not found");
    assert.ok(/shellVersion_ < 37\) return;/.test(set[1])
              && set[1].indexOf("shellVersion_ < 37")
                     < set[1].indexOf("qdwin_shell_v1_set_chrome_cursor"),
        "setChromeCursor must be gated on a v37 binding before marshalling");
    assert.ok(/void chromeMotion\(quint32 handle, quint32 side, double x, double y\);/
              .test(header) && /void chromeLeave\(quint32 handle\);/.test(header),
        "the signals must be declared");
    const render = binding.match(
        /QImage QdwinBinding::renderTitleBar[^{]*\{([\s\S]*?)\n\}/);
    assert.ok(render && /i == hoverIndex/.test(render[1]),
        "the title bar must paint the hovered button");
    assert.ok(/renderTitleBar\(outerWidth, titleHeight, rgba,\s*title, active, buttons,\s*hoverIndex\)/
              .test(binding),
        "attachFrame must pass the hover index to the raster");
}

// --- QML policy ---------------------------------------------------------
assert.ok(/onChromeMotion: \(handle, side, x, y\) => \{\s*root\._onChromeMotion\(handle, side, x, y\);/
          .test(qml), "chromeMotion must drive _onChromeMotion");
assert.ok(/onChromeLeave: \(handle\) => \{\s*root\._onChromeLeave\(handle\);/
          .test(qml), "chromeLeave must drive _onChromeLeave");
assert.ok(/onToplevelRemoved[\s\S]*?_chromeHover\.handle === handle\)\s*root\._chromeHover = \{handle: 0/
          .test(qml), "a removed toplevel must not keep the hover");
assert.ok(/root\._chromeHover\.handle === row\.handle\s*\? root\._chromeHover\.index : -1\)/
          .test(qml), "the frame sync must paint the current hover only on its own row");

function load(fns, extra) {
    const ctx = vm.createContext(Object.assign({
        chromeHeight: 24, chromeBorder: 4, resizeGrip: 4, cornerGrip: 16,
        _chromeHover: {handle: 0, index: -1, shape: 0},
    }, extra));
    ctx.root = ctx;
    for (const fn of fns)
        vm.runInContext(qml.match(new RegExp(
            "^ {4}function " + fn + "\\([^]*?^ {4}}", "m"))[0], ctx);
    return ctx;
}

{
    const ctx = load(["_resizeCursorShape"]);
    const want = {1: 19, 2: 22, 4: 25, 8: 18, 5: 21, 9: 20, 6: 24, 10: 23, 0: 1};
    for (const [edges, shape] of Object.entries(want))
        assert.strictEqual(ctx._resizeCursorShape(Number(edges)), shape,
            "edges " + edges + " → cursor shape " + shape);
}

{
    const cursor = [], repaint = [];
    const rows = {7: {handle: 7, state: 0, sandboxEngine: "",
                      width: 400, height: 300},
                  8: {handle: 8, state: 1, sandboxEngine: "",
                      width: 400, height: 300}};
    const ctx = load(["_titleButtons", "_frameBorder", "_frameResizeEdges",
                      "_resizeCursorShape", "_titleButtonAt",
                      "_onChromeMotion", "_onChromeLeave"], {
        _windowByHandle: h => rows[h] || null,
        _repaintChromeFor: h => repaint.push(h),
        qdwinBinding: {setChromeCursor: (h, s) => cursor.push([h, s])},
    });
    // Row 7: outer width 408, buttons start at 408 - 3*24 = 336.
    ctx._onChromeMotion(7, 0, 200, 12);              // title bar body
    assert.deepStrictEqual(cursor, [[7, 1]], "title bar → default arrow");
    assert.deepStrictEqual(repaint, [], "no button, no repaint");
    ctx._onChromeMotion(7, 0, 210, 12);
    assert.deepStrictEqual(cursor, [[7, 1]], "unchanged shape → no request");
    ctx._onChromeMotion(7, 0, 340, 12);              // minimize button
    assert.deepStrictEqual(repaint, [7], "entering a button repaints");
    assert.strictEqual(ctx._chromeHover.index, 0);
    ctx._onChromeMotion(7, 0, 345, 12);
    assert.deepStrictEqual(repaint, [7], "same button → no repaint");
    ctx._onChromeMotion(7, 0, 400, 2);               // close, in the top grip
    assert.strictEqual(ctx._chromeHover.index, 2,
        "the top grip yields to the buttons for hover as for presses");
    assert.deepStrictEqual(cursor, [[7, 1]], "buttons keep the arrow");
    ctx._onChromeMotion(7, 0, 200, 2);               // top grip
    assert.deepStrictEqual(cursor[cursor.length - 1], [7, 19], "n-resize");
    assert.deepStrictEqual(repaint, [7, 7, 7], "leaving the button repaints");
    ctx._onChromeMotion(7, 2, 400, 1);               // bottom-right corner
    assert.deepStrictEqual(cursor[cursor.length - 1], [7, 23], "se-resize");
    ctx._onChromeMotion(7, 3, 1, 100);               // W edge
    assert.deepStrictEqual(cursor[cursor.length - 1], [7, 25], "w-resize");
    ctx._onChromeMotion(7, 1, 1, 290);               // E edge, low → corner
    assert.deepStrictEqual(cursor[cursor.length - 1], [7, 23], "E corner");

    ctx._onChromeMotion(7, 0, 380, 12);              // maximize button
    const before = repaint.length;
    ctx._onChromeLeave(8);                           // someone else's leave
    assert.strictEqual(ctx._chromeHover.handle, 7, "foreign leave ignored");
    ctx._onChromeLeave(7);
    assert.strictEqual(JSON.stringify(ctx._chromeHover),
        JSON.stringify({handle: 0, index: -1, shape: 0}), "leave resets");
    assert.strictEqual(repaint.length, before + 1,
        "leaving with a highlighted button repaints it away");

    // A maximised window: no edges → never a resize cursor.
    cursor.length = 0;
    ctx._onChromeMotion(8, 0, 100, 1);
    assert.deepStrictEqual(cursor, [[8, 1]],
        "a maximised frame has no resize grips");

    // Chrome → chrome with a highlight: the old frame is repainted clean,
    // and the new one starts fresh (cursor re-requested).
    ctx._onChromeMotion(8, 0, 400 - 10, 12);         // row 8: 400 wide, close
    assert.ok(ctx._chromeHover.index >= 0);
    repaint.length = 0; cursor.length = 0;
    ctx._onChromeMotion(7, 0, 200, 12);
    assert.deepStrictEqual(repaint, [8], "the old frame loses its highlight");
    assert.deepStrictEqual(cursor, [[7, 1]],
        "a new frame always gets its cursor (qdwin reset it on leave)");

    // Unknown handle (row already gone): no state change, no requests.
    cursor.length = 0;
    ctx._onChromeMotion(99, 0, 10, 10);
    assert.deepStrictEqual(cursor, []);
    assert.strictEqual(ctx._chromeHover.handle, 7);
}

console.log("test_chrome_hover: all invariants held");
