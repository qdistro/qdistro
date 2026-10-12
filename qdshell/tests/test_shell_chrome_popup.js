// Source invariants for the shell-chrome + context-menu popup client.
//
// Pins the wire-ordering and serial contracts the qdwin_shell_v1
// chrome/popup protocol depends on:
//  - surfaces must carry a committed buffer BEFORE attach_decoration /
//    show_popup (the compositor refuses an uncommitted side and an
//    uncommitted popup surface has nothing to composite),
//  - the live input serial is threaded from chrome_button /
//    popup_button into show_popup / begin_interactive_move — a
//    fabricated (literal-0) serial trips `invalid_grab` and kills the
//    shell's wayland connection,
//  - popup teardown destroys the qdwin_popup_v1 object BEFORE the
//    wl_surface/wl_buffer it views, so the compositor never reads a
//    surface whose buffer the client just freed,
//  - dead parents purge their chrome + popup proxies on
//    toplevel_removed, and the disconnect path drops the bookkeeping
//    without marshalling destructors.
//
// Also pins the QML contract (Services/Qdwin/Qdwin.qml): the north
// strip's left press raises + interactive-moves on the live serial,
// right press opens the menu through showPopup with the same serial,
// and menu actions resolve against per-parent action maps that die
// with the popup and with the parent.
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const binding = fs.readFileSync(
    path.join(__dirname, "..", "qml-plugin", "qdwin-binding.cpp"), "utf8");
const qml = fs.readFileSync(
    path.join(__dirname, "..", "Services", "Qdwin", "Qdwin.qml"), "utf8");

function body(name) {
    const m = binding.match(
        new RegExp(name + "[^{]*\\{([\\s\\S]*?)\\n\\}"));
    assert.ok(m, name + " not found in qdwin-binding.cpp");
    return m[1];
}

// --- serial threading: live input serial in, no fabricated serials ---
// ensures: a stale/fabricated serial can never reach show_popup or
//          begin_interactive_move (both raise invalid_grab and kill the
//          binding connection — this is a fail-loud wire contract)
{
    const moveCall = binding.match(
        /qdwin_shell_v1_begin_interactive_move\(shell_, handle, serial\)/);
    assert.ok(moveCall,
        "begin_interactive_move must pass the forwarded input serial");
    const popupCall = binding.match(
        /qdwin_shell_v1_show_popup\(\s*shell_, parent, surf, serial, x, y\)/);
    assert.ok(popupCall,
        "show_popup must pass the forwarded input serial");
    // No request may hardcode a serial argument.
    assert.ok(
        !/qdwin_shell_v1_(show_popup|begin_interactive_move|begin_interactive_resize)\([^)]*,\s*0\s*[,)]/
            .test(binding),
        "no zero/literal serial may reach the interactive/move/popup requests");
}

// --- committed-before-request ordering -------------------------------
// ensures: the compositor never sees a chrome/popup surface with no
//          content (attach_decoration refuses it, show_popup would
//          composite an empty view)
{
    const attach = body("bool QdwinBinding::attachChrome");
    assert.ok(attach.indexOf("wl_surface_commit") >= 0
              && attach.indexOf("wl_surface_commit")
                     < attach.indexOf("reattachDecoration"),
        "chrome surface must commit its buffer before attach_decoration");

    const show = body("bool QdwinBinding::showPopup");
    assert.ok(show.indexOf("wl_surface_commit") >= 0
              && show.indexOf("wl_surface_commit")
                     < show.indexOf("qdwin_shell_v1_show_popup"),
        "popup surface must commit its buffer before show_popup");
    // The popup listener must be armed on the live object.
    assert.ok(/qdwin_popup_v1_add_listener\(pop, &kPopupListener, this\)/
              .test(show),
        "qdwin_popup_v1 needs the dismissed listener");
}

// --- teardown ordering: popup object before surface before buffer ----
// ensures: the compositor's popup view is torn down before the client
//          frees the surface/buffer it reads (UAF prevention)
{
    const tear = body("void QdwinBinding::popupTeardown");
    const iPop = tear.indexOf("qdwin_popup_v1_destroy");
    const iSurf = tear.indexOf("wl_surface_destroy");
    const iBuf = tear.indexOf("wl_buffer_destroy");
    assert.ok(iPop >= 0 && iSurf >= 0 && iBuf >= 0
              && iPop < iSurf && iSurf < iBuf,
        "popupTeardown must destroy popup → surface → buffer, in order");
    assert.ok(/popups_\.erase/.test(tear),
        "popupTeardown must drop the bookkeeping entry");
}

// --- hit-test contract ------------------------------------------------
// ensures: only a left-press inside a rendered row activates a menu
//          item, and the popup is gone before the action runs
{
    const hit = body("void QdwinBinding::popupHit");
    assert.ok(/state != 1/.test(hit) && /button != 0x110/.test(hit),
        "popupHit must gate on press-state + BTN_LEFT");
    assert.ok(/index >= e\.itemCount/.test(hit),
        "popupHit must reject rows past the item count");
    assert.ok(hit.indexOf("popupTeardown(parent)")
                  < hit.indexOf("popupItemTriggered"),
        "the popup must be torn down before the item action emits");
}

// --- dead-parent + disconnect cleanup ---------------------------------
// ensures: client-side proxies are reaped exactly once on every
//          lifecycle path; no dangling wl_surface/wl_buffer survives a
//          toplevel removal or a binding teardown
{
    const removed = binding.match(
        /static void toplevel_removed[\s\S]*?\n    \}/)[0];
    assert.ok(removed.indexOf("chromePurge(handle)") >= 0
              && removed.indexOf("popupTeardown(handle)") >= 0
              && removed.indexOf("chromePurge(handle)")
                     < removed.indexOf("toplevelRemoved"),
        "toplevel_removed must purge chrome+popup before the QML signal");

    const teardown = body("void QdwinBinding::teardown");
    assert.ok(/chromePopupTeardownState\(\)/.test(teardown),
        "teardown must drop chrome/popup bookkeeping");

    // chrome_button/popup_button are consumed, not stubs: each must
    // emit its signal and popup_button must drive the hit-test.
    const chrome = binding.match(
        /static void chrome_button[\s\S]*?\n    \}/)[0];
    assert.ok(/emit b->chromeButton\(/.test(chrome),
        "chrome_button must emit chromeButton with the wire tuple");
    const pop = binding.match(
        /static void popup_button[\s\S]*?\n    \}/)[0];
    assert.ok(/emit b->popupButton\(/.test(pop)
              && /popupHit\(/.test(pop),
        "popup_button must emit popupButton then hit-test");

    // The factories are bound on the main registry (same-client rule).
    assert.ok(/wl_registry_bind\(reg, name, &wl_compositor_interface/.test(binding)
              && /wl_registry_bind\(reg, name, &wl_shm_interface/.test(binding),
        "wl_compositor and wl_shm must bind on the binding's registry");
}

// --- QML wiring ---------------------------------------------------------
{
    // The north strip: left press raises + drags on the live serial;
    // right press opens the menu on the same serial.
    const chrome = qml.match(/onChromeButton:[\s\S]*?\n        \}/)[0];
    assert.ok(/side !== 0 \|\| state !== 1/.test(chrome),
        "only N-chrome presses act");
    assert.ok(/button === 0x110/.test(chrome)
              && /requestRaise\(handle\)/.test(chrome)
              && /beginInteractiveMove\(handle, serial\)/.test(chrome),
        "BTN_LEFT press must raise + beginInteractiveMove on the serial");
    assert.ok(/button === 0x111/.test(chrome)
              && /_openWindowMenu\(handle, serial, x, y\)/.test(chrome),
        "BTN_RIGHT press must open the window menu on the serial");

    const open = qml.match(/function _openWindowMenu[\s\S]*?\n    \}/)[0];
    assert.ok(/showPopup\(handle, serial, x, y, items\)/.test(open),
        "the menu must open via showPopup with the live serial");
    assert.ok(/_menuActions\[handle\]/.test(open),
        "menu actions must be recorded per parent for the trigger");
    assert.ok(/qdistro\.mm/.test(open),
        "mm-remote windows must not get a dead Close item");

    const run = qml.match(/function _runMenuAction[\s\S]*?\n    \}/)[0];
    for (const verb of ["requestMinimize", "requestMaximize",
                        "closeWindow"]) {
        assert.ok(run.includes(verb + "(parent"),
            "_runMenuAction must route " + verb + " to the parent handle");
    }
    assert.ok(/delete root\._menuActions\[parent\]/.test(run),
        "the action map must be dropped when the menu resolves");

    // Dismissal paths drop the action map on both the compositor
    // `dismissed` event and parent removal.
    assert.ok(/onPopupDismissed[\s\S]*?delete root\._menuActions\[parent\]/
              .test(qml),
        "compositor dismissal must drop the action map");
    assert.ok(/delete root\._menuActions\[handle\]/.test(qml),
        "toplevel removal must drop the action map");

    // Chrome follows geometry and un-follows fullscreen.
    assert.ok(/onToplevelGeometry[\s\S]*?_syncChromeForRow/.test(qml),
        "geometry changes must re-sync the chrome strip");
    assert.ok(/state & 2[\s\S]*?detachChrome/.test(qml),
        "fullscreen windows must not keep the chrome strip");
    // Rebind replay: rows persist across a compositor reconnect, but a
    // row whose toplevel closed while unbound must NOT be re-decorated
    // (attach_decoration on its handle killed every rebind — tiered-
    // isolation s49, 2026-10-11). The bound transition defers the
    // re-attach and only toplevels the compositor replayed qualify.
    assert.ok(/root\._replayedHandles = \(\{\}\);[\s\S]*?Qt\.callLater\(root\._resyncChromeAfterReplay\)/
              .test(qml),
        "rebind must defer chrome re-attach until the replay is in");
    assert.ok(/onToplevelAdded[\s\S]*?_replayedHandles\[handle\] = true/
              .test(qml),
        "toplevel replay must mark its handle live");
    {
        const vm = require("vm");
        const synced = [];
        const rows = [{handle: 24}, {handle: 25}, {handle: 27}];
        const ctx = vm.createContext({
            windows: {count: rows.length, get: i => rows[i]},
            _syncChromeForRow: i => synced.push(rows[i].handle),
            _replayedHandles: {25: true, 27: true},
        });
        ctx.root = ctx;
        vm.runInContext(qml.match(
            /^ {4}function _resyncChromeAfterReplay\([^]*?^ {4}}/m)[0], ctx);
        ctx._resyncChromeAfterReplay();
        assert.deepStrictEqual(synced, [25, 27],
            "only replayed (live) handles get chrome after a rebind");
        assert.strictEqual(ctx._replayedHandles, null,
            "the replay window closes after the resync");
        ctx._resyncChromeAfterReplay();
        assert.deepStrictEqual(synced, [25, 27],
            "a second resync outside the replay window is a no-op");
    }

    // Silo identity: setBorderColor also repaints the strip.
    const sbc = qml.match(/function setBorderColor[\s\S]*?\n    \}/)[0];
    assert.ok(/chromeArgb/.test(sbc),
        "setBorderColor must also record the strip's silo colour");
}

console.log("test_shell_chrome_popup: all invariants held");
